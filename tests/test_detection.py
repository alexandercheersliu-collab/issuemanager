"""同类题检测编排与 API 测试：发题（三来源）→ 判分 → 降级联动 → 历史。"""
from __future__ import annotations

import pytest

from backend.database import SessionLocal
from backend.models.orm import DetectionLog
from backend.services.detection import answers_match
from backend.services.detection import tested_ref_for as _tested_ref_for
from backend.services.rag import RagHit, SimilarResult


class _StubStore:
    def __init__(self, own_hits=None, bank_hits=None):
        self.own_hits = own_hits or []
        self.bank_hits = bank_hits or []

    def constrained_similar(self, query_text, **kwargs) -> SimilarResult:
        return SimilarResult(hits=self.own_hits[: kwargs.get("top_k") or 3], level=0)

    def similar_from_bank(self, query_text, **kwargs) -> SimilarResult:
        return SimilarResult(hits=self.bank_hits[: kwargs.get("top_k") or 3], level=0)


def _bank_hit() -> RagHit:
    return RagHit(
        question_id=-1,
        distance=0.4,
        snippet="公共题库：解方程 x² - 3x + 2 = 0",
        source="bank",
        bank_id="bank-math-9-001",
        metadata={"subject": "math", "grade": 9, "answer": "x=1 或 x=2", "difficulty": "medium"},
    )


def _make_question(question_service, student_user, text="检测编排·源题", answer="x=1"):
    return question_service.create_manual_question(
        student_user.id,
        content_markdown=text,
        answer=answer,
        knowledge_points=["一元二次方程"],
        subject="math",
        grade=9,
    )


def _log(log_id: int) -> DetectionLog:
    with SessionLocal() as session:
        log = session.get(DetectionLog, log_id)
        session.expunge(log)
        return log


# ---------- 纯函数 ----------


def test_answers_match():
    assert answers_match("x=1", "x=1")
    assert answers_match("x = 1", "x=1")  # 空白差异
    assert answers_match("Ｘ＝１", "x=1")  # 全角差异
    assert answers_match("x=1 或 x=2", "x=1 或 x=2")
    assert answers_match("答案是 x=1 或 x=2", "x=1 或 x=2")  # 包含
    assert answers_match("k≤1/2", "k≤1/2")
    assert not answers_match("x=1", "x=2")
    assert not answers_match("", "x=1")
    assert not answers_match("x=1", "1")  # 单字符包含不算对


def test_tested_ref_for():
    from backend.models.schemas import QuestionOut

    own = QuestionOut(id=42, user_id=1, image_path=None, content_markdown="题", answer="1")
    assert _tested_ref_for(own) == "42"
    bank = QuestionOut(
        id=-1, user_id=0, image_path=None, content_markdown="题库题", answer="1", source="bank"
    )
    assert _tested_ref_for(bank).startswith("bank:")
    gen = QuestionOut(
        id=-1, user_id=0, image_path=None,
        content_markdown="变式题", answer="", source="generated",
    )
    assert _tested_ref_for(gen).startswith("gen:")


# ---------- 服务层编排 ----------


def test_start_detection_own_hit(question_service, student_user, monkeypatch):
    source = _make_question(question_service, student_user)
    other = _make_question(question_service, student_user, "检测编排·同类题", answer="x=1")
    monkeypatch.setattr(
        question_service, "vector_store", _StubStore(own_hits=[RagHit(other.id, 0.4)])
    )

    challenge = question_service.start_detection(source.id, student_user.id)
    assert challenge["tested"]["source"] == "manual"  # 库内题来源不变
    assert "同类题" in challenge["tested"]["content"]
    assert "answer" not in challenge["tested"]  # 答案不下发

    log = _log(challenge["log_id"])
    assert log.result == "pending"
    assert log.tested_ref == str(other.id)
    assert log.tested_answer == "x=1"


def test_start_detection_generated_fallback_gets_answer(
    question_service, student_user, monkeypatch
):
    """generated 题无库存答案，发题时即时生成参考答案快照（MockProvider 固定答案）。"""
    source = _make_question(question_service, student_user)
    monkeypatch.setattr(question_service, "vector_store", _StubStore())

    challenge = question_service.start_detection(source.id, student_user.id)
    assert challenge["tested"]["source"] == "generated"
    log = _log(challenge["log_id"])
    assert log.tested_ref.startswith("gen:")
    assert log.tested_answer.strip()  # Mock 演示答案已快照


def test_submit_answer_and_demotion_e2e(question_service, student_user, monkeypatch):
    """端到端：连续 2 次答对 → 自动降级（间隔拉长 + mastered 口径一致）。

    两道不同 bank 题（同答案）：第二次发起走去重随机推新题，不再重推第一题。
    """
    source = _make_question(question_service, student_user)
    hits = [
        _bank_hit(),
        RagHit(
            question_id=-2,
            distance=0.5,
            snippet="公共题库：解方程 x² - 5x + 6 = 0",
            source="bank",
            bank_id="bank-math-9-002",
            metadata={"subject": "math", "grade": 9, "answer": "x=1 或 x=2", "difficulty": "medium"},
        ),
    ]
    monkeypatch.setattr(
        question_service, "vector_store", _StubStore(bank_hits=hits)
    )

    tested_refs: list[str] = []
    for expected_streak in (1, 2):
        challenge = question_service.start_detection(source.id, student_user.id)
        assert challenge["tested"]["source"] == "bank"
        outcome = question_service.submit_detection_answer(
            challenge["log_id"], student_user.id, "x=1 或 x=2"
        )
        assert outcome["result"] == "correct"
        assert outcome["streak"] == expected_streak
        tested_refs.append(_log(challenge["log_id"]).tested_ref)
    assert len(set(tested_refs)) == 2  # 去重随机：两次推题不同
    assert outcome["demoted"] is True
    assert outcome["state"] == "demoted"

    wrong = question_service.start_detection(source.id, student_user.id)
    outcome = question_service.submit_detection_answer(
        wrong["log_id"], student_user.id, "x=9"
    )
    assert outcome["result"] == "wrong"
    assert outcome["promoted"] is True
    assert outcome["state"] == "normal"


def test_submit_isolated_and_idempotent(question_service, student_user, monkeypatch):
    source = _make_question(question_service, student_user)
    monkeypatch.setattr(question_service, "vector_store", _StubStore(bank_hits=[_bank_hit()]))
    challenge = question_service.start_detection(source.id, student_user.id)

    with pytest.raises(LookupError):
        question_service.submit_detection_answer(
            challenge["log_id"], student_user.id + 999, "x=1 或 x=2"
        )
    question_service.submit_detection_answer(challenge["log_id"], student_user.id, "x=1 或 x=2")
    with pytest.raises(ValueError, match="不能重复作答"):
        question_service.submit_detection_answer(
            challenge["log_id"], student_user.id, "x=1 或 x=2"
        )
    with pytest.raises(LookupError):
        question_service.start_detection(source.id, student_user.id + 999)


def test_detection_history_via_service(question_service, student_user, monkeypatch):
    source = _make_question(question_service, student_user, "检测历史·源题")
    monkeypatch.setattr(question_service, "vector_store", _StubStore(bank_hits=[_bank_hit()]))
    challenge = question_service.start_detection(source.id, student_user.id)
    question_service.submit_detection_answer(challenge["log_id"], student_user.id, "x=1 或 x=2")

    history = question_service.detection_history(source.id, student_user.id)
    assert len(history) == 1
    assert history[0]["result"] == "correct"
    assert history[0]["streak"] == 1


# ---------- API 级 ----------


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


def test_detection_api_e2e(client, headers, question_service, student_user, monkeypatch):
    source = _make_question(question_service, student_user, "检测API·源题")
    # API 层自建 QuestionService（不走测试的 stub），依赖真实向量库/mock AI：
    # 测试库中同用户已有题目可召回或走兜底，不预设来源，只验证协议。
    start = client.post(f"/api/questions/{source.id}/detection/start", headers=headers)
    assert start.status_code == 201, start.text
    body = start.json()
    assert body["log_id"] > 0
    assert body["tested"]["content"].strip()
    assert body["tested"]["source"] in ("manual", "ai", "document", "bank", "generated")

    answer = client.post(
        f"/api/detection/{body['log_id']}/answer",
        headers=headers,
        json={"answer": "我的作答"},
    )
    assert answer.status_code == 200, answer.text
    assert answer.json()["result"] in ("correct", "wrong")

    history = client.get(
        f"/api/questions/{source.id}/detection/history", headers=headers
    )
    assert history.status_code == 200
    assert history.json()["count"] >= 1


def test_detection_api_404_and_409(client, headers, question_service, student_user):
    assert client.post("/api/questions/999999999/detection/start", headers=headers).status_code == 404
    assert (
        client.post(
            "/api/detection/999999999/answer", headers=headers, json={"answer": "x"}
        ).status_code
        == 404
    )
    assert (
        client.get("/api/questions/999999999/detection/history", headers=headers).status_code
        == 404
    )
    bad = client.post("/api/detection/1/answer", headers=headers, json={"answer": ""})
    assert bad.status_code == 422  # 空答案被请求模型拦截
