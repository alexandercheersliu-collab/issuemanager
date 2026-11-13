"""同类题三级回落编排测试：own → 公共题库 → AI 变式生成；API 约束参数兼容。

服务层用 stub 向量库（不依赖真实 ChromaDB）；AI 走 MockProvider。
共享测试库下用成员断言，不做全量等值断言。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from backend.models.schemas import FigureComparison, GeometryFigureDraft, GeometryVariant
from backend.services.geometry_figures import GeometryFigureService
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


def _make_question(
    question_service, student_user, text="回落测试·源题：判别式", figure_paths=None
):
    return question_service.create_manual_question(
        student_user.id,
        content_markdown=text,
        answer="x",
        knowledge_points=["判别式"],
        subject="math",
        grade=9,
        region="北京",
        textbook_version="人教版",
        figure_paths=figure_paths,
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


class _VariantRenderer:
    def __init__(self, tmp_path):
        self.tmp_path = tmp_path
        self.calls = []

    def render(self, source):
        self.calls.append(source)
        path = self.tmp_path / f"variant-{len(self.calls)}.png"
        path.write_bytes(b"new rendered figure")
        return SimpleNamespace(png_path=str(path))


def _geometry_source(question_service, student_user):
    source = _make_question(
        question_service,
        student_user,
        "几何源题：证明 AB ⟂ BC",
        figure_paths=["/source/figure.png"],
    )
    source.figure_specs = [
        {
            "kind": "geometry",
            "semantic_spec": {"points": ["A", "B", "C"], "relations": ["AB ⟂ BC"]},
            "tikz_source": "old tikz",
            "render_path": "/source/figure.png",
            "crop_path": None,
            "validation_status": "ai_passed",
            "validation_issues": [],
            "assumptions": ["原图不按比例"],
        }
    ]
    return source


def test_generated_fallback_without_figure_uses_followup_question(
    question_service, student_user, monkeypatch
):
    source = _make_question(question_service, student_user)
    monkeypatch.setattr(question_service, "vector_store", _StubStore([], []))
    captured = {}
    real_analyze = question_service.ai.analyze_text

    def _analyze_spy(text, hint="", context=None):
        captured["text"] = text
        return real_analyze(text, hint=hint, context=context)

    def _unexpected_geometry_service(*args, **kwargs):
        raise AssertionError("无图题不应构造几何服务")

    monkeypatch.setattr(question_service.ai, "analyze_text", _analyze_spy)
    monkeypatch.setattr(GeometryFigureService, "from_settings", _unexpected_geometry_service)

    outcome = question_service.similar_questions(source, user_id=student_user.id, top_k=2)

    generated = [q for q in outcome.items if q.source == "generated"]
    assert len(generated) == 1
    assert generated[0].content_markdown.strip()
    assert generated[0].figure_paths == []
    assert generated[0].figure_specs == []
    assert captured["text"] == source.content_markdown


def test_generated_geometry_fallback_uses_new_render_and_specs(
    question_service, student_user, monkeypatch, tmp_path
):
    source = _geometry_source(question_service, student_user)
    monkeypatch.setattr(question_service, "vector_store", _StubStore([], []))
    renderer = _VariantRenderer(tmp_path)
    service = GeometryFigureService(question_service.ai, renderer)
    draft = GeometryFigureDraft(
        kind="geometry",
        semantic_spec={"points": ["D", "E", "F"], "relations": ["DE ⟂ EF"]},
        tikz_source="new tikz",
        assumptions=["新图不按比例"],
    )
    generate_calls = []

    def _generate(content, **kwargs):
        generate_calls.append((content, kwargs))
        return GeometryVariant(question="几何新题：证明 DE ⟂ EF", figures=[draft])

    monkeypatch.setattr(question_service.ai, "generate_geometry_variant", _generate)
    monkeypatch.setattr(
        question_service.ai,
        "validate_geometry_variant",
        lambda png_bytes, **kwargs: FigureComparison(passed=True),
    )
    monkeypatch.setattr(
        GeometryFigureService, "from_settings", lambda ai, settings: service
    )

    outcome = question_service.similar_questions(source, user_id=student_user.id, top_k=2)

    generated = [q for q in outcome.items if q.source == "generated"]
    assert len(generated) == 1
    assert generated[0].content_markdown == "几何新题：证明 DE ⟂ EF"
    assert generated[0].figure_paths == [str(tmp_path / "variant-1.png")]
    assert generated[0].figure_paths != source.figure_paths
    assert generated[0].figure_specs[0].semantic_spec == draft.semantic_spec
    assert generated[0].figure_specs[0].assumptions == ["新图不按比例"]
    assert generated[0].figure_specs[0].render_path == generated[0].figure_paths[0]
    assert len(generate_calls) == 1
    assert generate_calls[0][0] == source.content_markdown


def test_invalid_geometry_variant_regenerates_once_then_returns_no_candidate(
    question_service, student_user, monkeypatch, tmp_path
):
    source = _geometry_source(question_service, student_user)
    store = _StubStore([], [])
    monkeypatch.setattr(question_service, "vector_store", store)
    renderer = _VariantRenderer(tmp_path)
    service = GeometryFigureService(question_service.ai, renderer)
    generate_calls = []

    def _generate(content, **kwargs):
        generate_calls.append(kwargs)
        return GeometryVariant(
            question=f"几何候选 {len(generate_calls)}",
            figures=[
                GeometryFigureDraft(
                    kind="geometry",
                    semantic_spec={"relations": ["题干与图不一致"]},
                    tikz_source=f"bad tikz {len(generate_calls)}",
                )
            ],
        )

    monkeypatch.setattr(question_service.ai, "generate_geometry_variant", _generate)
    monkeypatch.setattr(
        question_service.ai,
        "validate_geometry_variant",
        lambda png_bytes, **kwargs: FigureComparison(
            passed=False,
            issues=["新图与题干不一致"],
            repair_instruction="按题干重画新图",
        ),
    )
    monkeypatch.setattr(
        GeometryFigureService, "from_settings", lambda ai, settings: service
    )

    outcome = question_service.similar_questions(source, user_id=student_user.id, top_k=2)

    assert outcome.items == []
    assert len(generate_calls) == 2
    assert generate_calls[0].get("repair_instruction", "") == ""
    assert generate_calls[1]["repair_instruction"] == "按题干重画新图"
    assert renderer.calls == ["bad tikz 1", "bad tikz 2"]


def test_figure_dependent_variant_regenerates_once_then_omits_figureless_result(
    question_service, student_user, monkeypatch, tmp_path
):
    source = _geometry_source(question_service, student_user)
    monkeypatch.setattr(question_service, "vector_store", _StubStore([], []))
    renderer = _VariantRenderer(tmp_path)
    service = GeometryFigureService(question_service.ai, renderer)
    generate_calls = []

    def _generate(content, **kwargs):
        generate_calls.append(kwargs)
        return GeometryVariant(question=f"无图几何候选 {len(generate_calls)}", figures=[])

    monkeypatch.setattr(question_service.ai, "generate_geometry_variant", _generate)
    monkeypatch.setattr(
        GeometryFigureService, "from_settings", lambda ai, settings: service
    )

    outcome = question_service.similar_questions(source, user_id=student_user.id, top_k=2)

    assert outcome.items == []
    assert len(generate_calls) == 2
    assert generate_calls[0].get("repair_instruction", "") == ""
    assert "至少生成一幅新图" in generate_calls[1]["repair_instruction"]
    assert renderer.calls == []


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
