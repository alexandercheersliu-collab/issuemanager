"""Pydantic 数据契约：AI 结构化输出、API 入参、视图层传输对象。

边界处的数据一律经过 Pydantic 校验（配置 / AI 响应 / 表单入参）。
"""
from __future__ import annotations

import datetime as dt
import mimetypes
from pathlib import Path
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
    # 题目完整性与小题聚焦（模型给不出时用默认值，不阻断解析）
    is_complete: bool = Field(
        default=True, description="图片中的题目是否拍全（题干/选项/小题无缺失）"
    )
    completeness_note: str = Field(
        default="", description="不完整时的说明，如「第(2)问被裁掉」（完整时留空串）"
    )
    focused_sub_question: str = Field(
        default="",
        description="大题含多道小题且仅部分做错时，提取出的做错小题题干（否则留空串）",
    )

    @field_validator("knowledge_points", "tags")
    @classmethod
    def _clean_strings(cls, value: list[str]) -> list[str]:
        cleaned = [v.strip() for v in value if v and v.strip()]
        return cleaned or value

    def merged_tags(self, user_tags: list[str]) -> list[str]:
        """AI 标签与用户手填标签合并去重，保持顺序并遵守标签上限。"""
        seen: list[str] = []
        for tag in [*user_tags, *self.tags, *self.knowledge_points]:
            normalized = tag.strip()
            if normalized and normalized not in seen:
                seen.append(normalized)
        return seen[:6]


class GeometryFigureDraft(BaseModel):
    kind: str
    semantic_spec: dict
    tikz_source: str
    assumptions: list[str] = Field(default_factory=list)


class FigureComparison(BaseModel):
    passed: bool
    issues: list[str] = Field(default_factory=list)
    repair_instruction: str = ""


class GeometryVariant(BaseModel):
    question: str
    figures: list[GeometryFigureDraft] = Field(default_factory=list)


class FigureSpec(BaseModel):
    kind: str
    semantic_spec: dict
    tikz_source: str
    render_path: str | None = None
    crop_path: str | None = None
    validation_status: Literal["ai_passed", "manual_passed", "fallback"]
    validation_issues: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)


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
    evidence_type: Literal["image", "document"] | None = None
    evidence_mime_type: str | None = None
    evidence_filename: str | None = None
    figure_paths: list[str] = Field(default_factory=list)
    figure_specs: list[FigureSpec] = Field(default_factory=list)
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
    canonical_id: int | None = None
    match_method: str = "new"
    reps: int = 0
    ease: float = 2.5
    interval_days: float = 0
    due_at: dt.datetime | None = None
    last_reviewed_at: dt.datetime | None = None
    created_at: dt.datetime | None = None
    # 同类题检测降级状态（P3.2）：normal / demoted
    demotion_state: str = "normal"

    @classmethod
    def from_orm_model(cls, q) -> QuestionOut:  # noqa: ANN001 - ORM 实例
        private_image = getattr(q, "private_image_path", None)
        if private_image:
            from backend.models.orm import resolve_image_path

            resolved = resolve_image_path(private_image, q.user_id)
        else:
            resolved = q.resolved_image_path() if hasattr(q, "resolved_image_path") else None
        resolved_figures = [
            str(path)
            for path in (
                q.resolved_figure_paths() if hasattr(q, "resolved_figure_paths") else []
            )
        ]
        resolved_figure_specs = (
            q.resolved_figure_specs()
            if hasattr(q, "resolved_figure_specs")
            else getattr(q, "figure_specs", None) or []
        )
        canonical = getattr(q, "canonical", None)
        snapshot = getattr(q, "metadata_snapshot", None) or {}
        has_shared_content = canonical is not None or any(
            getattr(q, name, None) is not None
            for name in ("stem_snapshot", "analysis_snapshot", "answer_snapshot")
        )
        if has_shared_content:
            stem_override = getattr(q, "stem_override", None)
            analysis_override = getattr(q, "analysis_override", None)
            answer_override = getattr(q, "answer_override", None)
            stem = stem_override if stem_override is not None else (
                canonical.stem_markdown
                if canonical is not None
                else getattr(q, "stem_snapshot", None) or ""
            )
            analysis = analysis_override if analysis_override is not None else (
                canonical.analysis_markdown
                if canonical is not None
                else getattr(q, "analysis_snapshot", None)
            )
            content_markdown = stem + (f"\n\n---\n\n{analysis}" if analysis else "")
            answer = answer_override if answer_override is not None else (
                canonical.answer
                if canonical is not None
                else getattr(q, "answer_snapshot", None) or ""
            )
            if canonical is not None:
                resolved_figures.extend(
                    asset.storage_path
                    for asset in canonical.assets
                    if asset.visibility == "shared"
                    and asset.storage_path not in resolved_figures
                )
            standard = canonical
        else:
            content_markdown = q.content_markdown
            answer = q.answer
            standard = None

        def standard_value(name: str, default=None):  # noqa: ANN001,ANN202
            if standard is not None:
                return getattr(standard, name)
            return snapshot.get(name, default)

        knowledge_points = list(q.knowledge_points or []) or list(
            standard_value("knowledge_points", []) or []
        )
        evidence_mime = mimetypes.guess_type(str(resolved))[0] if resolved else None
        evidence_type = (
            "image" if (evidence_mime or "").startswith("image/") else "document"
        ) if resolved else None
        return cls(
            id=q.id,
            user_id=q.user_id,
            image_path=str(resolved) if resolved else None,
            evidence_type=evidence_type,
            evidence_mime_type=evidence_mime,
            evidence_filename=Path(resolved).name if resolved else None,
            figure_paths=resolved_figures,
            figure_specs=resolved_figure_specs,
            content_markdown=content_markdown,
            answer=answer,
            knowledge_points=knowledge_points,
            tags=list(q.tags or []),
            difficulty=(
                standard_value("difficulty", "medium") if has_shared_content else q.difficulty
            ),
            followup_question=q.followup_question,
            source=q.source,
            user_note=q.user_note,
            ocr_text=q.ocr_text,
            subject=(
                getattr(q, "subject", None)
                or standard_value("subject", None)
                or "math"
            ),
            grade=(
                getattr(q, "grade", None)
                if getattr(q, "grade", None) is not None
                else standard_value("grade")
            ),
            region=getattr(q, "region", None) or standard_value("region"),
            textbook_version=(
                getattr(q, "textbook_version", None)
                or standard_value("textbook_version")
            ),
            question_type=(
                getattr(q, "question_type", None) or standard_value("question_type")
            ),
            chapter=getattr(q, "chapter", None) or standard_value("chapter"),
            error_category=getattr(q, "error_category", None),
            source_doc=getattr(q, "source_doc", None),
            canonical_id=getattr(q, "canonical_id", None),
            match_method=getattr(q, "match_method", None) or "new",
            reps=q.reps,
            ease=q.ease,
            interval_days=q.interval_days,
            due_at=q.due_at,
            last_reviewed_at=q.last_reviewed_at,
            created_at=q.created_at,
            demotion_state=getattr(q, "demotion_state", None) or "normal",
        )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def stem(self) -> str:
        """从原始 Markdown 内容派生题面。"""
        from backend.services.question_content import split_stem_and_analysis

        return split_stem_and_analysis(self.content_markdown)[0]

    @computed_field  # type: ignore[prop-decorator]
    @property
    def analysis(self) -> str | None:
        """从原始 Markdown 内容派生解析；旧题或无解析时为 None。"""
        from backend.services.question_content import split_stem_and_analysis

        return split_stem_and_analysis(self.content_markdown)[1]

    @computed_field  # type: ignore[prop-decorator]
    @property
    def mastered(self) -> bool:
        """掌握归档：连续记牢 ≥3 次且调度间隔 ≥21 天的题移出每日复习池。"""
        return self.reps >= 3 and self.interval_days >= 21


class CanonicalQuestionOut(BaseModel):
    id: int
    stem_markdown: str
    normalized_stem: str
    normalized_hash: str
    analysis_markdown: str = ""
    answer: str = ""
    knowledge_points: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    difficulty: str = "medium"
    question_type: str | None = None
    chapter: str | None = None
    subject: str = "math"
    grade: int | None = None
    region: str | None = None
    textbook_version: str | None = None
    origin: Literal["teacher", "student", "seed"] = "student"
    validation_state: str = "machine_validated"
    published_by: int | None = None
    created_at: dt.datetime | None = None
    updated_at: dt.datetime | None = None
    reference_count: int = 0

    @classmethod
    def from_orm_model(
        cls, question, *, reference_count: int = 0
    ) -> CanonicalQuestionOut:  # noqa: ANN001 - ORM 实例
        return cls(
            id=question.id,
            stem_markdown=question.stem_markdown,
            normalized_stem=question.normalized_stem,
            normalized_hash=question.normalized_hash,
            analysis_markdown=question.analysis_markdown,
            answer=question.answer,
            knowledge_points=list(question.knowledge_points or []),
            tags=list(question.tags or []),
            difficulty=question.difficulty,
            question_type=question.question_type,
            chapter=question.chapter,
            subject=question.subject,
            grade=question.grade,
            region=question.region,
            textbook_version=question.textbook_version,
            origin=question.origin,
            validation_state=question.validation_state,
            published_by=question.published_by,
            created_at=question.created_at,
            updated_at=question.updated_at,
            reference_count=reference_count,
        )


class QuestionBankCandidate(BaseModel):
    score: float
    question: CanonicalQuestionOut


class PhotoEntryConfirmation(BaseModel):
    candidate_ids: list[str]
    match_choices: dict[str, int | None] = Field(default_factory=dict)


class CanonicalQuestionUpdate(BaseModel):
    stem_markdown: str | None = None
    analysis_markdown: str | None = None
    answer: str | None = None
    knowledge_points: list[str] | None = None
    tags: list[str] | None = None
    difficulty: Literal["easy", "medium", "hard"] | None = None
    question_type: str | None = None
    chapter: str | None = None
    subject: str | None = None
    grade: int | None = None
    region: str | None = None
    textbook_version: str | None = None


class DeleteImpactOut(BaseModel):
    canonical_id: int
    reference_count: int
    affected_students: list[dict] = Field(default_factory=list)
    affected_question_ids: list[int] = Field(default_factory=list)


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
