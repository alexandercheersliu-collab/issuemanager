"""同类题检测编排（P3.2）：取同类题 → 发题 → 判分 → 联动降级引擎。

作为 QuestionService 的 Mixin 组合（依赖 CoreMixin 的 ai/vector_store/settings），
状态联动委托给 DemotionService（backend/services/demotion.py）。

判分口径：库内题（own/bank）比对参考答案快照；AI 变式题（generated）发题时
即时生成参考答案快照，同样走比对。answers_match 为纯函数，便于单测。
"""
from __future__ import annotations

import hashlib
import unicodedata

from backend.models.schemas import QuestionOut
from backend.services.demotion import RESULT_CORRECT, RESULT_WRONG, DemotionService
from backend.utils.logging import get_logger

logger = get_logger("detection")


def _normalize_answer(text: str | None) -> str:
    """答案规范化：全半角统一、去空白与 LaTeX 装饰、运算符归一。"""
    normalized = unicodedata.normalize("NFKC", text or "").lower()
    for ch in (" ", "　", "\\", "$", "{", "}", ",", "，", "。", ".", "；", ";"):
        normalized = normalized.replace(ch, "")
    return (
        normalized.replace("×", "*")
        .replace("÷", "/")
        .replace("dfrac", "frac")
        .replace("≤", "<=")
        .replace("≥", ">=")
    )


def answers_match(expected: str | None, actual: str | None) -> bool:
    """判分：规范化后全等，或一方包含另一方（长度 ≥2 防单字符误判）。"""
    e, a = _normalize_answer(expected), _normalize_answer(actual)
    if not e or not a:
        return False
    if e == a:
        return True
    return (len(a) >= 2 and a in e) or (len(e) >= 2 and e in a)


def tested_ref_for(tested: QuestionOut) -> str:
    """同类题标识：库内题用 SQL id，bank/generated 用来源前缀 + 内容哈希。"""
    if tested.source in ("bank", "generated"):
        digest = hashlib.sha1(tested.content_markdown.encode("utf-8")).hexdigest()[:12]
        prefix = "bank" if tested.source == "bank" else "gen"
        return f"{prefix}:{digest}"
    return str(tested.id)


class DetectionMixin:
    """同类题检测：发起（约束检索取题）与作答判分（联动降级引擎）。"""

    def _demotion(self) -> DemotionService:
        return DemotionService(self.settings, session_factory=self._session_factory)

    def start_detection(self, question_id: int, user_id: int) -> dict:
        """对一道错题发起同类题检测：按题目自身 K12 元数据约束召回一道同类题。

        宽松模式三级回落（own → bank → generated）保证总有题可测；
        generated 题即时生成参考答案快照。返回 log_id 与题干（答案不下发）。
        """
        question = self.get_question(question_id, user_id)
        if question is None:
            raise LookupError("错题不存在或无权访问")

        outcome = self.similar_questions(
            question,
            user_id=user_id,
            subject=question.subject,
            grade=question.grade,
            region=question.region,
            difficulty=question.difficulty,
            knowledge_points=list(question.knowledge_points or []),
            top_k=1,
        )
        if not outcome.items:
            raise LookupError("暂无可用的同类题（召回与生成均失败）")
        tested = outcome.items[0]

        tested_answer = tested.answer
        if tested.source == "generated":
            tested_answer = self._generate_reference_answer(tested, question)

        log_id = self._demotion().begin(
            question_id,
            user_id,
            tested_ref=tested_ref_for(tested),
            tested_source=tested.source,
            tested_content=tested.content_markdown,
            tested_answer=tested_answer,
        )
        logger.info(
            "发起检测 question=%s user=%s tested=%s source=%s",
            question_id, user_id, log_id, tested.source,
        )
        return {
            "log_id": log_id,
            "question_id": question_id,
            "tested": {
                "source": tested.source,
                "content": tested.content_markdown,
                "knowledge_points": tested.knowledge_points,
                "difficulty": tested.difficulty,
            },
            "relax_level": outcome.level,
        }

    def _generate_reference_answer(self, tested: QuestionOut, source: QuestionOut) -> str:
        """generated 题的参考答案：即时走 AI 文本解析（失败时留空，判分必 wrong）。"""
        from backend.services.ai.base import AnalysisContext

        try:
            analysis = self.ai.analyze_text(
                tested.content_markdown[:4000],
                context=AnalysisContext(
                    subject=source.subject or "math",
                    grade=source.grade,
                    region=source.region,
                    textbook_version=source.textbook_version,
                ),
            )
            return (analysis.answer or "").strip()
        except Exception as exc:  # noqa: BLE001 - 判分按参考答案缺失处理
            logger.warning("变式题参考答案生成失败: %s", exc)
            return ""

    def submit_detection_answer(self, log_id: int, user_id: int, answer: str) -> dict:
        """作答判分：比对参考答案快照 → 落结果 → 降级/回升联动。"""
        demotion = self._demotion()
        log = demotion.get_log(log_id, user_id)
        if log is None:
            raise LookupError("检测记录不存在或无权访问")

        result = RESULT_CORRECT if answers_match(log.tested_answer, answer) else RESULT_WRONG
        outcome = demotion.record_result(log_id, user_id, result, student_answer=answer)
        logger.info("检测判分 log=%s result=%s", log_id, result)
        return {
            "log_id": log_id,
            "result": result,
            "expected_answer": log.tested_answer,
            "streak": outcome.streak,
            "state": outcome.state,
            "demoted": outcome.demoted,
            "promoted": outcome.promoted,
            "mastered": outcome.mastered,
            "next_interval": outcome.next_interval,
        }

    def detection_history(self, question_id: int, user_id: int) -> list[dict]:
        """某题检测历史（新→旧），含连续通过进度与降级状态。"""
        return self._demotion().history(question_id, user_id)
