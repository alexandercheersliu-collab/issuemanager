"""Pydantic 数据契约：AI 结构化输出、API 入参、视图层传输对象。

边界处的数据一律经过 Pydantic 校验（配置 / AI 响应 / 表单入参）。
"""
from __future__ import annotations

import datetime as dt
from typing import Literal

from pydantic import BaseModel, Field, computed_field, field_validator

# ---------------- K12 元数据常量 ----------------
# 学科代码 → 中文名（存储用代码，提示词与界面展示用中文名）
SUBJECT_NAMES: dict[str, str] = {
    "math": "数学",
    "chinese": "语文",
    "english": "英语",
    "physics": "物理",
    "chemistry": "化学",
    "biology": "生物",
    "history": "历史",
    "geography": "地理",
    "politics": "政治",
}

# 结构化错因枚举（AI 输出与录入界面共用；非法值触发 ValidationError 并重试）
ERROR_CATEGORIES: tuple[str, ...] = ("概念不清", "计算失误", "审题偏差", "方法不会", "粗心其他")
ErrorCategory = Literal["概念不清", "计算失误", "审题偏差", "方法不会", "粗心其他"]


# ---------------- AI 结构化输出 ----------------
class QuestionAnalysis(BaseModel):
    """视觉模型对一道错题的结构化解析结果。"""

    knowledge_points: list[str] = Field(
        default_factory=list, min_length=1, max_length=6, description="考察的核心知识点"
    )
    analysis: str = Field(min_length=10, description="分步骤详细解析，Markdown 格式")
    answer: str = Field(min_length=1, description="最终正确答案")
    difficulty: Literal["easy", "medium", "hard"] = "medium"
    tags: list[str] = Field(default_factory=list, max_length=6, description="归档标签")
    mistake_cause: str = Field(default="", description="常见出错原因分析")
    followup_question: str = Field(default="", description="一道举一反三的变式练习题")
    # K12 结构化字段（二开扩展；模型给不出时留空/None，不阻断解析）
    question_type: str = Field(default="", description="题型，如：选择题/填空题/解答题")
    chapter: str = Field(default="", description="所属章节，如：一元二次方程")
    error_category: ErrorCategory | None = Field(
        default=None, description="结构化错因分类（枚举值之一）"
    )

    @field_validator("knowledge_points", "tags")
    @classmethod
    def _clean_strings(cls, value: list[str]) -> list[str]:
        cleaned = [v.strip() for v in value if v and v.strip()]
        return cleaned or value

    def merged_tags(self, user_tags: list[str]) -> list[str]:
        """AI 标签与用户手填标签合并去重，保持顺序稳定。"""
        seen: list[str] = []
        for tag in [*user_tags, *self.tags, *self.knowledge_points]:
            normalized = tag.strip()
            if normalized and normalized not in seen:
                seen.append(normalized)
        return seen


class AIProviderInfo(BaseModel):
    provider: str
    model: str
    configured: bool
    demo_mode: bool


# ---------------- 视图层传输对象 ----------------
class QuestionOut(BaseModel):
    """错题在界面层的展示形态，隔离 ORM 细节。"""

    id: int
    user_id: int
    image_path: str | None
    content_markdown: str
    answer: str
    knowledge_points: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    difficulty: str = "medium"
    followup_question: str | None = None
    source: str = "ai"
    user_note: str | None = None
    ocr_text: str | None = None
    # K12 元数据（二开扩展）
    subject: str = "math"
    grade: int | None = None
    region: str | None = None
    textbook_version: str | None = None
    question_type: str | None = None
    chapter: str | None = None
    error_category: str | None = None
    source_doc: str | None = None
    reps: int = 0
    ease: float = 2.5
    interval_days: float = 0
    due_at: dt.datetime | None = None
    last_reviewed_at: dt.datetime | None = None
    created_at: dt.datetime | None = None

    @classmethod
    def from_orm_model(cls, q) -> QuestionOut:  # noqa: ANN001 - ORM 实例
        # 原图路径自愈：库里的绝对路径在 DATA_DIR 迁移后可能失效，
        # 这里换成当前真实可用路径（找不到则留空，界面按"无图"处理）。
        resolved = q.resolved_image_path() if hasattr(q, "resolved_image_path") else None
        return cls(
            id=q.id,
            user_id=q.user_id,
            image_path=str(resolved) if resolved else None,
            content_markdown=q.content_markdown,
            answer=q.answer,
            knowledge_points=list(q.knowledge_points or []),
            tags=list(q.tags or []),
            difficulty=q.difficulty,
            followup_question=q.followup_question,
            source=q.source,
            user_note=q.user_note,
            ocr_text=q.ocr_text,
            # K12 元数据：getattr 兜底，兼容无新列的轻量替身对象（老测试桩/第三方模型）
            subject=getattr(q, "subject", None) or "math",
            grade=getattr(q, "grade", None),
            region=getattr(q, "region", None),
            textbook_version=getattr(q, "textbook_version", None),
            question_type=getattr(q, "question_type", None),
            chapter=getattr(q, "chapter", None),
            error_category=getattr(q, "error_category", None),
            source_doc=getattr(q, "source_doc", None),
            reps=q.reps,
            ease=q.ease,
            interval_days=q.interval_days,
            due_at=q.due_at,
            last_reviewed_at=q.last_reviewed_at,
            created_at=q.created_at,
        )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def mastered(self) -> bool:
        """掌握归档：连续记牢 ≥3 次且调度间隔 ≥21 天的题移出每日复习池。"""
        return self.reps >= 3 and self.interval_days >= 21


class TagStat(BaseModel):
    tag: str
    count: int
    mastery: float = Field(ge=0.0, le=1.0, default=0.0)


class RegisterInput(BaseModel):
    username: str = Field(min_length=2, max_length=32)
    password: str = Field(min_length=6, max_length=64)
    role: Literal["student", "teacher"] = "student"

    @field_validator("username")
    @classmethod
    def _username_rules(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("用户名不能为空白")
        return stripped


class LoginResult(BaseModel):
    ok: bool
    user_id: int | None = None
    username: str | None = None
    role: str | None = None
    message: str = ""
