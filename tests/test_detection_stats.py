"""检测报表统计口径测试：通过率、本周降级数、看板数据通路、界面文案纯函数。"""
from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

from backend.services.rag import RagHit, SimilarResult
from backend.services.stats import detection_stats
from frontend.pages.notebook import describe_detection_outcome


def _log(result="correct", demoted=False, days_ago=0, today=None):
    today = today or dt.date.today()
    created = dt.datetime.combine(today - dt.timedelta(days=days_ago), dt.time(12, 0))
    return SimpleNamespace(result=result, demoted=demoted, created_at=created)


# ---------- 统计口径 ----------


def test_pass_rate_excludes_pending():
    logs = [_log("correct"), _log("correct"), _log("wrong"), _log("pending")]
    stats = detection_stats(logs)
    assert stats["detection_total"] == 3  # pending 不计
    assert stats["detection_correct"] == 2
    assert stats["detection_pass_rate"] == 67  # 2/3 ≈ 66.67 → 67


def test_pass_rate_empty_is_none():
    stats = detection_stats([_log("pending")])
    assert stats["detection_pass_rate"] is None
    assert stats["detection_total"] == 0


def test_week_demotions_counts_this_week_only():
    today = dt.date(2026, 9, 26)  # 周五；本周一 = 9-21
    logs = [
        _log("correct", demoted=True, days_ago=0, today=today),  # 本周五 ✓
        _log("correct", demoted=True, days_ago=3, today=today),  # 本周二 ✓
        _log("correct", demoted=True, days_ago=6, today=today),  # 上周日 ✗
        _log("correct", demoted=False, days_ago=0, today=today),  # 未触发降级 ✗
    ]
    stats = detection_stats(logs, today=today)
    assert stats["week_demotions"] == 2


# ---------- 看板数据通路 ----------


class _StubStore:
    def __init__(self, bank_hits):
        self.bank_hits = bank_hits

    def constrained_similar(self, query_text, **kwargs) -> SimilarResult:
        return SimilarResult(hits=[], level=0)

    def similar_from_bank(self, query_text, **kwargs) -> SimilarResult:
        return SimilarResult(hits=self.bank_hits[: kwargs.get("top_k") or 3], level=0)


def _bank_hit() -> RagHit:
    return RagHit(
        question_id=-1,
        distance=0.4,
        snippet="统计通路·公共题库题",
        source="bank",
        bank_id="bank-stats-001",
        metadata={"subject": "math", "grade": 9, "answer": "x=1", "difficulty": "medium"},
    )


def test_detection_overview_reflects_new_logs(question_service, student_user, monkeypatch):
    source = question_service.create_manual_question(
        student_user.id, content_markdown="统计通路·源题", answer="x=1", subject="math", grade=9
    )
    monkeypatch.setattr(
        question_service,
        "vector_store",
        _StubStore(bank_hits=[
            _bank_hit(),
            RagHit(  # 去重随机推题：第二次发起推另一道，答案相同便于判分
                question_id=-2,
                distance=0.5,
                snippet="统计通路·公共题库题二",
                source="bank",
                bank_id="bank-stats-002",
                metadata={"subject": "math", "grade": 9, "answer": "x=1", "difficulty": "medium"},
            ),
        ]),
    )
    before = question_service.detection_overview(student_user.id)

    for _ in range(2):  # 连续两次通过 → 第二次触发降级
        challenge = question_service.start_detection(source.id, student_user.id)
        question_service.submit_detection_answer(
            challenge["log_id"], student_user.id, "x=1"
        )

    after = question_service.detection_overview(student_user.id)
    assert after["detection_total"] == before["detection_total"] + 2
    assert after["detection_correct"] == before["detection_correct"] + 2
    assert after["week_demotions"] == before["week_demotions"] + 1  # 本周降级 +1


# ---------- 界面文案纯函数 ----------


def test_describe_detection_outcome():
    assert "降级" in describe_detection_outcome({"result": "correct", "demoted": True})
    assert "连续通过 1 次" in describe_detection_outcome(
        {"result": "correct", "demoted": False, "streak": 1}
    )
    assert "回升" in describe_detection_outcome({"result": "wrong", "promoted": True})
    assert "清零" in describe_detection_outcome({"result": "wrong", "promoted": False})
