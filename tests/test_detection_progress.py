"""出题进度回调测试：不同回落路径触发不同的 on_progress 消息序列。

全部走 MockProvider / 向量库替身；回调消息用模块常量断言，避免文案微调即碎。
"""
from __future__ import annotations

from backend.services.detection import PROGRESS_DONE, PROGRESS_VARIANT_ANSWER
from backend.services.question_mixins import (
    PROGRESS_RECALL_BANK,
    PROGRESS_RECALL_GENERATE,
    PROGRESS_RECALL_OWN,
)
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
        snippet="进度回调·公共题库题",
        source="bank",
        bank_id="bank-progress-001",
        metadata={"subject": "math", "grade": 9, "answer": "x=1", "difficulty": "medium"},
    )


def _make(question_service, student_user, text):
    return question_service.create_manual_question(
        student_user.id,
        content_markdown=text,
        answer="x=1",
        knowledge_points=["一元二次方程"],
        subject="math",
        grade=9,
    )


def _start_with_spy(question_service, qid, uid):
    messages: list[str] = []
    challenge = question_service.start_detection(qid, uid, on_progress=messages.append)
    return challenge, messages


def _count_generate(question_service, monkeypatch):
    """给 _generate_variant 装调用计数器，返回计数 dict。"""
    calls = {"n": 0}
    original = question_service._generate_variant

    def spy(question):
        calls["n"] += 1
        return original(question)

    monkeypatch.setattr(question_service, "_generate_variant", spy)
    return calls


def test_progress_own_hit_path(question_service, student_user, monkeypatch):
    """错题库候选充足：只报「检索错题库」与「完成」，不调 AI、不报后续步骤。"""
    source = _make(question_service, student_user, "进度回调·源题A")
    others = [
        _make(question_service, student_user, f"进度回调·同类题A{i}") for i in range(10)
    ]
    monkeypatch.setattr(
        question_service,
        "vector_store",
        _StubStore(own_hits=[RagHit(q.id, 0.4) for q in others]),
    )
    ai_calls = _count_generate(question_service, monkeypatch)

    challenge, messages = _start_with_spy(question_service, source.id, student_user.id)
    assert challenge["tested"]["source"] == "manual"
    assert messages == [PROGRESS_RECALL_OWN, PROGRESS_DONE]
    assert ai_calls["n"] == 0


def test_progress_bank_fallback_path(question_service, student_user, monkeypatch):
    """错题库为空、公共题库命中：报 错题库→公共题库→完成；不再调 AI 生成补位。"""
    source = _make(question_service, student_user, "进度回调·源题B")
    monkeypatch.setattr(
        question_service, "vector_store", _StubStore(bank_hits=[_bank_hit()])
    )
    ai_calls = _count_generate(question_service, monkeypatch)

    challenge, messages = _start_with_spy(question_service, source.id, student_user.id)
    assert challenge["tested"]["source"] == "bank"
    assert messages == [PROGRESS_RECALL_OWN, PROGRESS_RECALL_BANK, PROGRESS_DONE]
    assert ai_calls["n"] == 0  # 库内有候选：lazy 生成不调 AI（秒出题）


def test_progress_generated_path(question_service, student_user, monkeypatch):
    """两级召回全空、AI 变式兜底：完整五步，含参考答案快照生成。"""
    source = _make(question_service, student_user, "进度回调·源题C")
    monkeypatch.setattr(question_service, "vector_store", _StubStore())
    ai_calls = _count_generate(question_service, monkeypatch)

    challenge, messages = _start_with_spy(question_service, source.id, student_user.id)
    assert challenge["tested"]["source"] == "generated"
    assert messages == [
        PROGRESS_RECALL_OWN,
        PROGRESS_RECALL_BANK,
        PROGRESS_RECALL_GENERATE,
        PROGRESS_VARIANT_ANSWER,
        PROGRESS_DONE,
    ]
    assert ai_calls["n"] == 1  # 库内真空才生成一次兜底变式


def test_progress_default_none_still_works(question_service, student_user, monkeypatch):
    """不传 on_progress：行为与既有调用完全一致（静默、正常返回）。"""
    source = _make(question_service, student_user, "进度回调·源题D")
    monkeypatch.setattr(
        question_service, "vector_store", _StubStore(bank_hits=[_bank_hit()])
    )
    challenge = question_service.start_detection(source.id, student_user.id)
    assert challenge["log_id"] > 0
