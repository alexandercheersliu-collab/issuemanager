"""降级规则引擎状态机测试：通过→降级→再错→回升全分支、阈值可配、归档一致。"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from backend.config import get_settings
from backend.database import SessionLocal
from backend.models.orm import Question, ReviewLog
from backend.services.demotion import (
    STATE_DEMOTED,
    STATE_NORMAL,
    DemotionService,
    is_mastered,
)


@pytest.fixture
def demotion():
    return DemotionService(session_factory=SessionLocal)


def _make_question(question_service, student_user, text="降级测试·源题") -> int:
    out = question_service.create_manual_question(
        student_user.id, content_markdown=text, answer="x=1"
    )
    return out.id


def _sm2(question_id: int) -> Question:
    with SessionLocal() as session:
        q = session.get(Question, question_id)
        session.expunge(q)
        return q


def _set_sm2(question_id: int, *, reps: int, interval: float, ease: float = 2.5) -> None:
    with SessionLocal() as session:
        q = session.get(Question, question_id)
        q.reps = reps
        q.interval_days = interval
        q.ease = ease
        session.commit()


def _begin(demotion: DemotionService, question_id: int, user_id: int) -> int:
    return demotion.begin(
        question_id,
        user_id,
        tested_ref="bank:test",
        tested_source="bank",
        tested_content="同类题题干",
        tested_answer="x=1",
    )


def test_full_state_machine(question_service, student_user, demotion):
    """normal →（连续 2 次通过）→ demoted →（检测失败）→ normal 全流转。"""
    qid = _make_question(question_service, student_user)

    # 第 1 次通过：streak=1，未达阈值，不降级
    log1 = _begin(demotion, qid, student_user.id)
    out1 = demotion.record_result(log1, student_user.id, "correct", student_answer="x=1")
    assert out1.streak == 1
    assert out1.demoted is False
    assert out1.state == STATE_NORMAL

    # 第 2 次通过：达阈值 → 降级
    log2 = _begin(demotion, qid, student_user.id)
    out2 = demotion.record_result(log2, student_user.id, "correct", student_answer="x=1")
    assert out2.streak == 2
    assert out2.demoted is True
    assert out2.state == STATE_DEMOTED

    q = _sm2(qid)
    assert q.demotion_state == STATE_DEMOTED
    assert q.reps == 1  # quality=5 演进：reps+1
    # SM-2：reps=1 → 间隔 1 天，× 降级系数 1.5
    assert q.interval_days == pytest.approx(1.0 * 1.5)
    with SessionLocal() as session:
        logs = session.execute(
            select(ReviewLog).where(ReviewLog.question_id == qid)
        ).scalars().all()
    assert [entry.grade for entry in logs] == ["easy"]  # 降级留痕

    # 检测失败 → 回升：SM-2 重置 + 状态回 normal
    log3 = _begin(demotion, qid, student_user.id)
    out3 = demotion.record_result(log3, student_user.id, "wrong", student_answer="x=2")
    assert out3.promoted is True
    assert out3.state == STATE_NORMAL
    assert out3.streak == 0

    q = _sm2(qid)
    assert q.demotion_state == STATE_NORMAL
    assert q.reps == 0  # again 语义：重置进度
    with SessionLocal() as session:
        grades = [
            entry.grade
            for entry in session.execute(
                select(ReviewLog).where(ReviewLog.question_id == qid)
            ).scalars().all()
        ]
    assert grades == ["easy", "again"]  # 降级 + 回升均留痕


def test_wrong_breaks_streak(question_service, student_user, demotion):
    qid = _make_question(question_service, student_user)
    demotion.record_result(_begin(demotion, qid, student_user.id), student_user.id, "correct")
    demotion.record_result(_begin(demotion, qid, student_user.id), student_user.id, "wrong")
    out = demotion.record_result(
        _begin(demotion, qid, student_user.id), student_user.id, "correct"
    )
    assert out.streak == 1  # wrong 中断连续计数
    assert out.demoted is False


def test_threshold_configurable(question_service, student_user, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "detection_pass_threshold", 3)
    demotion = DemotionService(settings=settings, session_factory=SessionLocal)
    qid = _make_question(question_service, student_user, "阈值测试·源题")

    for _ in range(2):
        out = demotion.record_result(
            _begin(demotion, qid, student_user.id), student_user.id, "correct"
        )
        assert out.demoted is False  # 阈值 3，两次通过不降级
    out = demotion.record_result(
        _begin(demotion, qid, student_user.id), student_user.id, "correct"
    )
    assert out.demoted is True


def test_interval_factor_configurable(question_service, student_user, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "demotion_interval_factor", 3.0)
    demotion = DemotionService(settings=settings, session_factory=SessionLocal)
    qid = _make_question(question_service, student_user, "系数测试·源题")

    for _ in range(2):
        out = demotion.record_result(
            _begin(demotion, qid, student_user.id), student_user.id, "correct"
        )
    assert out.demoted is True
    assert _sm2(qid).interval_days == pytest.approx(1.0 * 3.0)


def test_demotion_aligns_with_mastered_archive(question_service, student_user, demotion):
    """归档一致：降级后 reps/interval 达线即 mastered（与基座口径同一标准）。"""
    qid = _make_question(question_service, student_user, "归档测试·源题")
    _set_sm2(qid, reps=2, interval=10.0, ease=2.5)

    for _ in range(2):
        out = demotion.record_result(
            _begin(demotion, qid, student_user.id), student_user.id, "correct"
        )
    assert out.demoted is True
    q = _sm2(qid)
    # reps: 2→3；interval: round(10×2.6)=26 → ×1.5=39 ≥21 → 达归档线
    assert q.reps == 3
    assert q.interval_days == pytest.approx(39.0)
    assert out.mastered is True
    assert is_mastered(q.reps, q.interval_days) is True

    # 回升后自然退出归档（reps 重置），无双标准
    out = demotion.record_result(
        _begin(demotion, qid, student_user.id), student_user.id, "wrong"
    )
    assert out.promoted is True
    assert out.mastered is False


def test_grade_again_promotes_demoted(question_service, student_user, demotion):
    """回升入口①：已降级题在复习中评 again → 自动回升 normal。"""
    qid = _make_question(question_service, student_user, "复习回升·源题")
    for _ in range(2):
        demotion.record_result(
            _begin(demotion, qid, student_user.id), student_user.id, "correct"
        )
    assert _sm2(qid).demotion_state == STATE_DEMOTED

    updated = question_service.grade_review(qid, student_user.id, "again")
    assert updated is not None
    assert _sm2(qid).demotion_state == STATE_NORMAL
    assert updated.mastered is False


def test_record_result_idempotent_and_isolated(question_service, student_user, demotion):
    qid = _make_question(question_service, student_user)
    log_id = _begin(demotion, qid, student_user.id)
    demotion.record_result(log_id, student_user.id, "correct")
    with pytest.raises(ValueError, match="不能重复作答"):
        demotion.record_result(log_id, student_user.id, "correct")
    with pytest.raises(LookupError):
        demotion.record_result(log_id, student_user.id + 999, "wrong")
    with pytest.raises(LookupError):
        demotion.begin(qid, student_user.id + 999, tested_ref="x", tested_source="own")
    with pytest.raises(ValueError, match="非法检测结果"):
        demotion.record_result(_begin(demotion, qid, student_user.id), student_user.id, "skip")


def test_history_returns_streak_and_state(question_service, student_user, demotion):
    qid = _make_question(question_service, student_user, "历史测试·源题")
    demotion.record_result(_begin(demotion, qid, student_user.id), student_user.id, "correct")
    history = demotion.history(qid, student_user.id)
    assert len(history) == 1
    entry = history[0]
    assert entry["result"] == "correct"
    assert entry["streak"] == 1
    assert entry["state"] == STATE_NORMAL
    assert entry["threshold"] == 2
    with pytest.raises(LookupError):
        demotion.history(qid, student_user.id + 999)
