"""SQLAlchemy ORM 模型。

三张核心表：users / questions / review_logs。
questions 内嵌 SM-2 复习调度字段，review_logs 记录每次复习明细用于掌握度分析。
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from backend.config import get_settings


class Base(DeclarativeBase):
    pass


def resolve_image_path(stored: str | None, user_id: int) -> Path | None:
    """把库里存的图片路径解析成真实可用路径。

    绝对路径在 DATA_DIR 迁移后会变成悬空引用（旧目录已不存在），
    因此这里做一次自愈：原路径不存在时，用文件名到当前数据目录找
    {DATA_DIR}/images/u{user_id}/{文件名}。找不到返回 None。

    注意：返回 None 只代表"这张图当前读不到"，不代表记录失效；
    图片属于可重新上传的派生物，不要据此清理或删除任何文件。
    """
    if not stored:
        return None
    path = Path(stored)
    if path.exists():
        return path
    fallback = get_settings().data_dir / "images" / f"u{user_id}" / path.name
    return fallback if fallback.exists() else None


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(128))
    role: Mapped[str] = mapped_column(String(16), default="student")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    questions: Mapped[list[Question]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )

    def display_name(self) -> str:
        return self.username


class Question(Base):
    """一道错题：原图 + AI 结构化解析 + SM-2 复习调度状态。"""

    __tablename__ = "questions"
    __table_args__ = (
        Index("ix_questions_user_created", "user_id", "created_at"),
        Index("ix_questions_due", "user_id", "due_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)

    image_path: Mapped[str | None] = mapped_column(String(512))
    # AI 解析（Markdown）
    content_markdown: Mapped[str] = mapped_column(Text)
    answer: Mapped[str] = mapped_column(Text, default="")
    knowledge_points: Mapped[list] = mapped_column(JSON, default=list)
    tags: Mapped[list] = mapped_column(JSON, default=list)
    difficulty: Mapped[str] = mapped_column(String(16), default="medium")
    followup_question: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(16), default="ai")  # ai / manual
    user_note: Mapped[str | None] = mapped_column(Text)
    ocr_text: Mapped[str | None] = mapped_column(Text)  # 原图 OCR 文字（可选特性）
    image_hash: Mapped[str | None] = mapped_column(String(64), index=True)  # 原图 SHA-256，去重用

    # ---------- K12 元数据（二开扩展）----------
    subject: Mapped[str] = mapped_column(
        String(32), default="math", server_default="math", index=True
    )  # 学科代码：math/chinese/english/physics/...
    grade: Mapped[int | None] = mapped_column(Integer)  # 年级 1-12
    region: Mapped[str | None] = mapped_column(String(64))  # 地区，如：北京
    textbook_version: Mapped[str | None] = mapped_column(String(64))  # 教材版本，如：人教版
    question_type: Mapped[str | None] = mapped_column(String(32))  # 题型，如：选择题/解答题
    chapter: Mapped[str | None] = mapped_column(String(128))  # 章节，如：一元二次方程
    error_category: Mapped[str | None] = mapped_column(String(32))  # 结构化错因（枚举见 schemas.ERROR_CATEGORIES）
    source_doc: Mapped[str | None] = mapped_column(String(256))  # 来源文档#页码（P2 文档输入预留）
    # 降级状态（P3.2）：normal / demoted（连续通过同类题检测后降级，
    # 复习 again 或检测失败自动回升为 normal）
    demotion_state: Mapped[str] = mapped_column(
        String(16), default="normal", server_default="normal"
    )

    def resolved_image_path(self) -> Path | None:
        """当前真实可用的原图路径（DATA_DIR 迁移后自动自愈），无图返回 None。"""
        return resolve_image_path(self.image_path, self.user_id)

    # SM-2 调度状态
    reps: Mapped[int] = mapped_column(Integer, default=0)
    ease: Mapped[float] = mapped_column(Float, default=lambda: get_settings().review_default_ease)
    interval_days: Mapped[float] = mapped_column(Float, default=0)
    due_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    last_reviewed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))

    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    user: Mapped[User] = relationship(back_populates="questions")
    review_logs: Mapped[list[ReviewLog]] = relationship(
        back_populates="question", cascade="all, delete-orphan"
    )
    comments: Mapped[list[Comment]] = relationship(
        back_populates="question", cascade="all, delete-orphan", order_by="Comment.created_at"
    )
    detection_logs: Mapped[list[DetectionLog]] = relationship(
        back_populates="question", cascade="all, delete-orphan"
    )

    def is_due(self, now: dt.datetime | None = None) -> bool:
        now = now or _utcnow()
        if self.due_at is None:
            return True
        due = self.due_at if self.due_at.tzinfo else self.due_at.replace(tzinfo=dt.timezone.utc)
        return due <= now


class Comment(Base):
    """错题批注：教师对学生错题的留言（也可用于学生自注）。"""

    __tablename__ = "comments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    question_id: Mapped[int] = mapped_column(
        ForeignKey("questions.id", ondelete="CASCADE"), index=True
    )
    author_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    question: Mapped[Question] = relationship(back_populates="comments")
    author: Mapped[User] = relationship()


class Job(Base):
    """异步任务记录（图片 AI 解析等耗时操作的队列追踪）。"""

    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)  # uuid4
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    type: Mapped[str] = mapped_column(String(32), default="analyze_image")
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending/running/success/failed
    payload: Mapped[dict] = mapped_column(JSON, default=dict)  # 原始文件名/mime/参数
    result: Mapped[dict | None] = mapped_column(JSON)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))


class ReviewLog(Base):
    """一次复习动作的明细，SM-2 参数演进与掌握度统计的依据。"""

    __tablename__ = "review_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    question_id: Mapped[int] = mapped_column(
        ForeignKey("questions.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)

    grade: Mapped[str] = mapped_column(String(8))  # again / hard / good / easy
    quality: Mapped[int] = mapped_column(Integer)  # SM-2 q: 0/3/4/5
    prev_interval: Mapped[float] = mapped_column(Float, default=0)
    next_interval: Mapped[float] = mapped_column(Float, default=0)
    ease_after: Mapped[Decimal] = mapped_column(Float, default=0)
    reviewed_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    question: Mapped[Question] = relationship(back_populates="review_logs")


class DetectionLog(Base):
    """一次同类题检测明细（P3.2）：原错题 → 同类题作答 → 结果与降级联动。

    tested_ref：同类题标识——库内题为 str(question_id)，公共题库为
    "bank:<内容哈希>"，AI 变式为 "gen:<内容哈希>"。
    result：pending（已发题未作答）/ correct / wrong。
    demoted：本次检测是否触发了原错题降级（报表「降级错题数」口径）。
    """

    __tablename__ = "detection_logs"
    __table_args__ = (
        Index("ix_detection_logs_user_created", "user_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    question_id: Mapped[int] = mapped_column(
        ForeignKey("questions.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)

    tested_ref: Mapped[str] = mapped_column(String(128))
    tested_source: Mapped[str] = mapped_column(String(16))  # own / bank / generated
    tested_content: Mapped[str | None] = mapped_column(Text)  # 同类题题干快照
    tested_answer: Mapped[str | None] = mapped_column(Text)  # 参考答案快照（判分依据）
    result: Mapped[str] = mapped_column(String(8), default="pending")
    student_answer: Mapped[str | None] = mapped_column(Text)
    demoted: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    question: Mapped[Question] = relationship(
        back_populates="detection_logs"
    )
