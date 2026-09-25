"""同类题三级回落编排测试：own → 公共题库 → AI 变式生成；API 约束参数兼容。

服务层用 stub 向量库（不依赖真实 ChromaDB）；AI 走 MockProvider。
共享测试库下用成员断言，不做全量等值断言。
"""
from __future__ import annotations

import pytest

from backend.services.rag import RagHit, SimilarResult


class _StubStore:
    """记录调用并按预设返回的向量库替身。"""

    def __init__(self, own_hits: list[RagHit], bank_hits: list[RagHit]):
        self.own_hits = own_hits
        self.bank_hits = bank_hits
        self.bank_called = False
        self.own_kwargs: dict = {}

    def constrained_similar(self, query_text, **kwargs) -> SimilarResult:
        self.own_kwargs = kwargs
        top_k = kwargs.get("top_k") or 3
        return SimilarResult(hits=self.own_hits[:top_k], level=0)

    def similar_from_bank(self, query_text, **kwargs) -> SimilarResult:
        self.bank_called = True
        top_k = kwargs.get("top_k") or 3
        return SimilarResult(hits=self.bank_hits[:top_k], level=0)


def _own_hit(question_id: int) -> RagHit:
    return RagHit(question_id=question_id, distance=0.4, snippet=f"题{question_id}")


def _bank_hit(bank_id: str = "bank-math-9-001", **meta) -> RagHit:
    metadata = {"subject": "math", "grade": 9, "answer": "k≤1/2", "difficulty": "medium"}
    metadata.update(meta)
    return RagHit(
        question_id=-1,
        distance=0.4,
        snippet="公共题库：判别式求参数范围",
        source="bank",
        bank_id=bank_id,
        metadata=metadata,
    )


def _make_question(question_service, student_user, text="回落测试·源题：判别式"):
    return question_service.create_manual_question(
        student_user.id,
        content_markdown=text,
        answer="x",
        knowledge_points=["判别式"],
        subject="math",
        grade=9,
        region="北京",
    )


def test_own_recall_sufficient_no_fallback(question_service, student_user, monkeypatch):
    source = _make_question(question_service, student_user)
    other = _make_question(question_service, student_user, "回落测试·同类题")
    store = _StubStore(own_hits=[_own_hit(other.id)], bank_hits=[_bank_hit()])
    monkeypatch.setattr(question_service, "vector_store", store)

    outcome = question_service.similar_questions(source, user_id=student_user.id, top_k=1)
    assert len(outcome.items) == 1
    assert outcome.items[0].id == other.id
    assert outcome.items[0].source == "manual"  # 真实 SQL 题，来源不变
    assert store.bank_called is False
    # 约束参数透传给向量层
    assert store.own_kwargs["exclude_id"] == source.id


def test_bank_fallback_when_own_insufficient(question_service, student_user, monkeypatch):
    source = _make_question(question_service, student_user)
    store = _StubStore(own_hits=[], bank_hits=[_bank_hit()])
    monkeypatch.setattr(question_service, "vector_store", store)

    outcome = question_service.similar_questions(source, user_id=student_user.id, top_k=2)
    assert store.bank_called is True
    bank_items = [q for q in outcome.items if q.source == "bank"]
    assert len(bank_items) == 1
    item = bank_items[0]
    assert item.id < 0  # 负数占位 id，不与 SQL 题冲突
    assert item.answer == "k≤1/2"
    assert item.grade == 9
    assert "判别式" in item.content_markdown


def test_generated_fallback_when_both_empty(question_service, student_user, monkeypatch):
    source = _make_question(question_service, student_user)
    store = _StubStore(own_hits=[], bank_hits=[])
    monkeypatch.setattr(question_service, "vector_store", store)

    outcome = question_service.similar_questions(source, user_id=student_user.id, top_k=2)
    generated = [q for q in outcome.items if q.source == "generated"]
    assert len(generated) == 1  # MockProvider 固定 followup_question
    assert generated[0].content_markdown.strip()
    assert generated[0].knowledge_points == ["判别式"]


def test_strict_mode_disables_fallback(question_service, student_user, monkeypatch):
    source = _make_question(question_service, student_user)
    store = _StubStore(own_hits=[], bank_hits=[_bank_hit()])
    monkeypatch.setattr(question_service, "vector_store", store)

    outcome = question_service.similar_questions(
        source, user_id=student_user.id, strict=True, top_k=2
    )
    assert outcome.items == []
    assert store.bank_called is False
    assert store.own_kwargs["strict"] is True


def test_constraints_passthrough(question_service, student_user, monkeypatch):
    source = _make_question(question_service, student_user)
    store = _StubStore(own_hits=[], bank_hits=[])
    monkeypatch.setattr(question_service, "vector_store", store)

    question_service.similar_questions(
        source,
        user_id=student_user.id,
        subject="math",
        grade=9,
        region="北京",
        difficulty="medium",
        strict=True,
    )
    constraints = store.own_kwargs["constraints"]
    assert constraints.subject == "math"
    assert constraints.grade == 9
    assert constraints.region == "北京"
    assert constraints.difficulty == "medium"


# ---------- API 级：约束参数与旧调用兼容 ----------


@pytest.fixture(scope="module")
def client():
    from fastapi.testclient import TestClient

    from api.main import create_app

    return TestClient(create_app())


@pytest.fixture(scope="module")
def headers(client):
    resp = client.post(
        "/api/auth/login", json={"username": "demo", "password": "demo123"}
    )
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


def test_similar_endpoint_backward_compatible(client, headers, question_service, student_user):
    question = _make_question(question_service, student_user, "接口兼容·源题")

    resp = client.get(f"/api/questions/{question.id}/similar", headers=headers)
    assert resp.status_code == 200, resp.text
    assert isinstance(resp.json(), list)

    constrained = client.get(
        f"/api/questions/{question.id}/similar",
        headers=headers,
        params={"subject": "math", "grade": 9, "region": "北京", "difficulty": "medium"},
    )
    assert constrained.status_code == 200, constrained.text
    strict = client.get(
        f"/api/questions/{question.id}/similar",
        headers=headers,
        params={"strict": "true"},
    )
    assert strict.status_code == 200


def test_similar_endpoint_validates_params(client, headers, question_service, student_user):
    question = _make_question(question_service, student_user, "接口校验·源题")

    bad_grade = client.get(
        f"/api/questions/{question.id}/similar", headers=headers, params={"grade": 13}
    )
    assert bad_grade.status_code == 422
    bad_difficulty = client.get(
        f"/api/questions/{question.id}/similar",
        headers=headers,
        params={"difficulty": "superhard"},
    )
    assert bad_difficulty.status_code == 422
    missing = client.get("/api/questions/999999999/similar", headers=headers)
    assert missing.status_code == 404
