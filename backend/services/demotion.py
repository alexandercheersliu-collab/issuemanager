"""降级规则引擎（P3.2 核心差异化）：同类题检测结果 → 原错题 SM-2 状态联动。

状态机：
    normal ── 连续 detection_pass_threshold 次检测通过 ──▶ demoted
        （降级：按 quality=5 做 SM-2 演进，间隔再乘 demotion_interval_factor 拉长，
          写一条 grade="easy" 的 review_log 留痕，demotion_state 置 demoted）
    demoted ── 复习评 again 或检测失败 ──▶ normal
        （回升：视同一次 again 复习——SM-2 重置 reps/短间隔，立即回到高优先级，
          写一条 grade="again" 的 review_log 留痕，demotion_state 置 normal）

掌握归档沿用基座唯一口径（reps≥3 且 interval≥21 天，QuestionOut.mastered）：
降级通过 SM-2 演进自然抵达归档线，回升的 reps 重置自然退出归档，不另设标准。
"""
from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy import select

from backend.config import Settings, get_settings
from backend.database import SessionLocal
from backend.models.orm import DetectionLog, Question, ReviewLog
from backend.services.review import ReviewScheduler
from backend.utils.logging import get_logger

logger = get_logger("demotion")

RESULT_PENDING = "pending"
RESULT_CORRECT = "correct"
RESULT_WRONG = "wrong"

STATE_NORMAL = "normal"
STATE_DEMOTED = "demoted"

# 掌握归档口径（与 schemas.QuestionOut.mastered 保持一致的唯一标准）
MASTERED_MIN_REPS = 3
MASTERED_MIN_INTERVAL = 21


def is_mastered(reps: int, interval_days: float) -> bool:
    """掌握归档判定：与 QuestionOut.mastered 同一口径，供降级结果回读。"""
    return reps >= MASTERED_MIN_REPS and interval_days >= MASTERED_MIN_INTERVAL


@dataclass
class DetectionOutcome:
    """一次检测结果落库 + 降级/回升联动后的状态快照。"""

    log_id: int
    result: str
    streak: int  # 当前连续通过次数（含本次）
    state: str  # 联动后的降级状态：normal / demoted
    demoted: bool = False  # 本次是否触发降级
    promoted: bool = False  # 本次是否触发回升
    mastered: bool = False  # 联动后是否已达掌握归档线
    next_interval: float | None = None  # 联动后的调度间隔（天）


class DemotionService:
    """降级规则引擎：只依赖 DB 与 SM-2 调度器，不碰 AI/向量库（便于单测）。"""

    def __init__(
        self,
        settings: Settings | None = None,
        session_factory: Callable | None = None,
    ):
        self.settings = settings or get_settings()
        self._session_factory = session_factory or SessionLocal
        self.scheduler = ReviewScheduler(self.settings)

    # ---------- 检测记录 ----------
    def begin(
        self,
        question_id: int,
        user_id: int,
        *,
        tested_ref: str,
        tested_source: str,
        tested_content: str | None = None,
        tested_answer: str | None = None,
    ) -> int:
        """发起检测：写入 pending 行，返回 log_id（作答后由 record_result 更新）。"""
        with self._session_factory() as session:
            question = session.execute(
                select(Question).where(
                    Question.id == question_id, Question.user_id == user_id
                )
            ).scalar_one_or_none()
            if question is None:
                raise LookupError("错题不存在或无权访问")
            log = DetectionLog(
                question_id=question_id,
                user_id=user_id,
                tested_ref=tested_ref,
                tested_source=tested_source,
                tested_content=tested_content,
                tested_answer=tested_answer,
                result=RESULT_PENDING,
            )
            session.add(log)
            session.commit()
            return log.id

    def record_result(
        self,
        log_id: int,
        user_id: int,
        result: str,
        *,
        student_answer: str | None = None,
    ) -> DetectionOutcome:
        """录入作答结果并执行降级/回升联动（同一事务）。

        result 只允许 correct/wrong；pending 行只能落一次结果（幂等保护：
        已落结果的 log 再次提交抛 ValueError）。
        """
        if result not in (RESULT_CORRECT, RESULT_WRONG):
            raise ValueError(f"非法检测结果: {result}")
        with self._session_factory() as session:
            log = session.get(DetectionLog, log_id)
            if log is None or log.user_id != user_id:
                raise LookupError("检测记录不存在或无权访问")
            if log.result != RESULT_PENDING:
                raise ValueError(f"该检测已落结果（{log.result}），不能重复作答")
            question = session.get(Question, log.question_id)
            if question is None:
                raise LookupError("原错题已被删除")

            log.result = result
            log.student_answer = student_answer

            streak = self._streak(session, log.question_id)
            demoted = promoted = False
            if (
                result == RESULT_CORRECT
                and question.demotion_state != STATE_DEMOTED
                and streak >= self.settings.detection_pass_threshold
            ):
                self._apply_demotion(session, question, log)
                demoted = True
            elif result == RESULT_WRONG and question.demotion_state == STATE_DEMOTED:
                self._apply_promotion(session, question)
                promoted = True

            session.commit()
            outcome = DetectionOutcome(
                log_id=log.id,
                result=result,
                streak=streak,
                state=question.demotion_state,
                demoted=demoted,
                promoted=promoted,
                mastered=is_mastered(question.reps, question.interval_days),
                next_interval=question.interval_days,
            )
        logger.info(
            "检测结果落库 log=%s question=%s result=%s streak=%s demoted=%s promoted=%s",
            log_id, log.question_id, result, streak, demoted, promoted,
        )
        return outcome

    @staticmethod
    def _streak(session, question_id: int) -> int:
        """当前连续通过次数（最新一条起向前，遇到非 correct 即停）。"""
        logs = session.execute(
            select(DetectionLog)
            .where(
                DetectionLog.question_id == question_id,
                DetectionLog.result != RESULT_PENDING,
            )
            .order_by(DetectionLog.created_at.desc(), DetectionLog.id.desc())
        ).scalars()
        streak = 0
        for entry in logs:
            if entry.result != RESULT_CORRECT:
                break
            streak += 1
        return streak

    # ---------- 状态机动作 ----------
    def _apply_demotion(self, session, question: Question, log: DetectionLog) -> None:
        """降级：quality=5 的 SM-2 演进 + 间隔 × demotion_interval_factor。"""
        schedule = self.scheduler.next_schedule(
            grade="easy",
            reps=question.reps,
            ease=question.ease,
            interval_days=question.interval_days,
        )
        next_interval = schedule.next_interval * self.settings.demotion_interval_factor
        now = dt.datetime.now(dt.timezone.utc)
        question.reps = question.reps + 1  # quality=5 ≥ 3，SM-2 记一次通过
        question.ease = schedule.ease_after
        question.interval_days = next_interval
        question.due_at = now + dt.timedelta(days=next_interval)
        question.last_reviewed_at = now
        question.demotion_state = STATE_DEMOTED
        log.demoted = True
        session.add(
            ReviewLog(
                question_id=question.id,
                user_id=question.user_id,
                grade="easy",
                quality=schedule.quality,
                prev_interval=schedule.prev_interval,
                next_interval=next_interval,
                ease_after=schedule.ease_after,
            )
        )

    def _apply_promotion(self, session, question: Question) -> None:
        """回升：视同一次 again 复习（SM-2 重置，立即回到高优先级复习池）。"""
        schedule = self.scheduler.next_schedule(
            grade="again",
            reps=question.reps,
            ease=question.ease,
            interval_days=question.interval_days,
        )
        now = dt.datetime.now(dt.timezone.utc)
        question.reps = 0  # quality<3：SM-2 重置进度，自然退出掌握归档
        question.ease = schedule.ease_after
        question.interval_days = schedule.next_interval
        question.due_at = now + dt.timedelta(days=schedule.next_interval)
        question.last_reviewed_at = now
        question.demotion_state = STATE_NORMAL
        session.add(
            ReviewLog(
                question_id=question.id,
                user_id=question.user_id,
                grade="again",
                quality=schedule.quality,
                prev_interval=schedule.prev_interval,
                next_interval=schedule.next_interval,
                ease_after=schedule.ease_after,
            )
        )

    # ---------- 查询 ----------
    def get_log(self, log_id: int, user_id: int) -> DetectionLog | None:
        """取一条检测记录（按用户隔离，分离态返回）。"""
        with self._session_factory() as session:
            log = session.get(DetectionLog, log_id)
            if log is None or log.user_id != user_id:
                return None
            session.expunge(log)
            return log

    def history(self, question_id: int, user_id: int, limit: int = 20) -> list[dict]:
        """某题检测历史（新→旧），供界面展示连续通过进度。"""
        with self._session_factory() as session:
            question = session.execute(
                select(Question).where(
                    Question.id == question_id, Question.user_id == user_id
                )
            ).scalar_one_or_none()
            if question is None:
                raise LookupError("错题不存在或无权访问")
            logs = session.execute(
                select(DetectionLog)
                .where(DetectionLog.question_id == question_id)
                .order_by(DetectionLog.created_at.desc(), DetectionLog.id.desc())
                .limit(limit)
            ).scalars()
            streak = self._streak(session, question_id)
            return [
                {
                    "log_id": log.id,
                    "tested_ref": log.tested_ref,
                    "tested_source": log.tested_source,
                    "result": log.result,
                    "demoted": log.demoted,
                    "created_at": log.created_at,
                    "streak": streak,
                    "state": question.demotion_state,
                    "threshold": self.settings.detection_pass_threshold,
                }
                for log in logs
            ]

    def logs_for_user(self, user_id: int) -> list[DetectionLog]:
        """用户全部检测记录（报表统计口径：含 pending，由调用方过滤）。"""
        with self._session_factory() as session:
            logs = list(
                session.execute(
                    select(DetectionLog)
                    .where(DetectionLog.user_id == user_id)
                    .order_by(DetectionLog.created_at)
                ).scalars()
            )
            session.expunge_all()  # 分离态返回，跨会话只读使用
            return logs
