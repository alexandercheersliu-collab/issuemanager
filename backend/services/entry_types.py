"""录入共享类型与默认上下文。"""
from __future__ import annotations

from dataclasses import dataclass

from backend.models.schemas import QuestionAnalysis, QuestionOut
from backend.services.ai.base import AnalysisContext

DEFAULT_IMPORT_CONTEXT = AnalysisContext(
    subject="math", grade=10, region="上海", textbook_version="沪教版"
)


@dataclass(frozen=True)
class PhotoImage:
    name: str
    data: bytes
    mime_type: str


@dataclass
class EntryResult:
    """拍照录题的结果：question 为既有记录时 duplicated=True。"""

    question: QuestionOut
    analysis: QuestionAnalysis
    duplicated: bool = False
