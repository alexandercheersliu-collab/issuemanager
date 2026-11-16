"""共享标准题仓储：稳定身份查询与标准内容持久化。"""
from __future__ import annotations

import json

from sqlalchemy import String, delete, func, or_, select
from sqlalchemy.orm import Session

from backend.models.orm import (
    CanonicalQuestion,
    Question,
    QuestionAsset,
    QuestionBankAudit,
    QuestionSourceFingerprint,
)
from backend.services.question_identity import normalize_stem, stem_hash


class QuestionBankRepository:
    def __init__(self, session: Session):
        self.session = session

    def create(
        self,
        *,
        stem_markdown: str,
        analysis_markdown: str = "",
        answer: str = "",
        knowledge_points: list[str] | None = None,
        tags: list[str] | None = None,
        difficulty: str = "medium",
        question_type: str | None = None,
        chapter: str | None = None,
        subject: str = "math",
        grade: int | None = None,
        region: str | None = None,
        textbook_version: str | None = None,
        origin: str = "student",
        validation_state: str = "machine_validated",
        published_by: int | None = None,
    ) -> CanonicalQuestion:
        question = CanonicalQuestion(
            stem_markdown=stem_markdown,
            normalized_stem=normalize_stem(stem_markdown),
            normalized_hash=stem_hash(stem_markdown),
            analysis_markdown=analysis_markdown,
            answer=answer,
            knowledge_points=knowledge_points or [],
            tags=tags or [],
            difficulty=difficulty,
            question_type=question_type,
            chapter=chapter,
            subject=subject,
            grade=grade,
            region=region,
            textbook_version=textbook_version,
            origin=origin,
            validation_state=validation_state,
            published_by=published_by,
        )
        self.session.add(question)
        self.session.flush()
        return question

    def get(self, canonical_id: int) -> CanonicalQuestion | None:
        return self.session.get(CanonicalQuestion, canonical_id)

    def get_many(self, canonical_ids: list[int]) -> list[CanonicalQuestion]:
        if not canonical_ids:
            return []
        return list(
            self.session.execute(
                select(CanonicalQuestion).where(CanonicalQuestion.id.in_(canonical_ids))
            ).scalars()
        )

    def get_by_normalized_hash(self, normalized_hash: str) -> CanonicalQuestion | None:
        return self.session.execute(
            select(CanonicalQuestion).where(
                CanonicalQuestion.normalized_hash == normalized_hash
            )
        ).scalar_one_or_none()

    def find_by_stem(self, stem: str) -> CanonicalQuestion | None:
        return self.get_by_normalized_hash(stem_hash(stem))

    def add_source_fingerprint(self, canonical_id: int, content_hash: str) -> None:
        exists = self.session.execute(
            select(QuestionSourceFingerprint).where(
                QuestionSourceFingerprint.canonical_id == canonical_id,
                QuestionSourceFingerprint.content_hash == content_hash,
            )
        ).scalar_one_or_none()
        if exists is None:
            self.session.add(
                QuestionSourceFingerprint(
                    canonical_id=canonical_id, content_hash=content_hash
                )
            )

    @staticmethod
    def is_reusable(question: CanonicalQuestion) -> bool:
        return all(
            str(value or "").strip()
            for value in (
                question.stem_markdown,
                question.analysis_markdown,
                question.answer,
            )
        )

    def acquire_write_lock(self) -> None:
        if self.session.get_bind().dialect.name == "sqlite":
            self.session.execute(delete(Question).where(False))

    def get_for_update(self, canonical_id: int) -> CanonicalQuestion | None:
        return self.session.execute(
            select(CanonicalQuestion)
            .where(CanonicalQuestion.id == canonical_id)
            .with_for_update()
        ).scalar_one_or_none()

    def get_asset(self, asset_id: int) -> QuestionAsset | None:
        return self.session.get(QuestionAsset, asset_id)

    def list_assets(self, canonical_id: int) -> list[QuestionAsset]:
        return list(
            self.session.execute(
                select(QuestionAsset)
                .where(QuestionAsset.canonical_id == canonical_id)
                .order_by(QuestionAsset.is_preferred.desc(), QuestionAsset.id)
            ).scalars()
        )

    def search(
        self,
        *,
        keyword: str = "",
        subject: str | None = None,
        grade: int | None = None,
        question_type: str | None = None,
        origin: str | None = None,
        textbook_version: str | None = None,
        reference_count: int | None = None,
        canonical_ids: list[int] | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[tuple[CanonicalQuestion, int]]:
        references = func.count(Question.id)
        statement = (
            select(CanonicalQuestion, references)
            .outerjoin(Question, Question.canonical_id == CanonicalQuestion.id)
            .group_by(CanonicalQuestion.id)
        )
        if keyword:
            statement = statement.where(
                or_(
                    CanonicalQuestion.stem_markdown.contains(keyword, autoescape=True),
                    CanonicalQuestion.analysis_markdown.contains(keyword, autoescape=True),
                    CanonicalQuestion.answer.contains(keyword, autoescape=True),
                )
            )
        for column, value in (
            (CanonicalQuestion.subject, subject),
            (CanonicalQuestion.grade, grade),
            (CanonicalQuestion.question_type, question_type),
            (CanonicalQuestion.origin, origin),
            (CanonicalQuestion.textbook_version, textbook_version),
        ):
            if value is not None:
                statement = statement.where(column == value)
        if canonical_ids is not None:
            if not canonical_ids:
                return []
            statement = statement.where(CanonicalQuestion.id.in_(canonical_ids))
        if reference_count is not None:
            statement = statement.having(references == reference_count)
        statement = statement.order_by(
            CanonicalQuestion.updated_at.desc(), CanonicalQuestion.id.desc()
        ).limit(limit).offset(offset)
        return [(question, int(count)) for question, count in self.session.execute(statement)]

    def count_references(self, canonical_id: int) -> int:
        return int(
            self.session.execute(
                select(func.count(Question.id)).where(Question.canonical_id == canonical_id)
            ).scalar_one()
        )

    def list_references(self, canonical_id: int) -> list[Question]:
        return list(
            self.session.execute(
                select(Question)
                .join(Question.user)
                .where(Question.canonical_id == canonical_id)
                .order_by(Question.user_id, Question.id)
            ).scalars()
        )

    def _asset_path_in_use(self, path: str) -> bool:
        if self.session.execute(
            select(QuestionAsset.id).where(QuestionAsset.storage_path == path).limit(1)
        ).first() is not None:
            return True
        return self.session.execute(
            select(Question.id)
            .where(
                or_(
                    Question.private_image_path == path,
                    Question.image_path == path,
                    Question.figure_paths.cast(String).contains(
                        json.dumps(path, ensure_ascii=False)
                    ),
                )
            )
            .limit(1)
        ).first() is not None

    def delete_with_snapshots(
        self,
        canonical_id: int,
        *,
        expected_reference_count: int,
        actor_id: int,
        actor_name: str,
    ) -> tuple[list[Question], list[str]] | None:
        self.acquire_write_lock()
        question = self.get_for_update(canonical_id)
        if question is None:
            raise ValueError("标准题不存在")
        references = self.list_references(canonical_id)
        if len(references) != expected_reference_count:
            return None
        metadata = {
            name: getattr(question, name)
            for name in (
                "knowledge_points",
                "tags",
                "difficulty",
                "question_type",
                "chapter",
                "subject",
                "grade",
                "region",
                "textbook_version",
            )
        }
        for personal in references:
            personal.stem_snapshot = question.stem_markdown
            personal.analysis_snapshot = question.analysis_markdown
            personal.answer_snapshot = question.answer
            personal.metadata_snapshot = dict(metadata)
            personal.canonical_id = None
            personal.detached_reason = "admin_delete"
        asset_paths = [asset.storage_path for asset in question.assets]
        self.session.add(
            QuestionBankAudit(
                actor=actor_name,
                actor_id=actor_id,
                canonical_id=canonical_id,
                action="delete_with_snapshots",
                change_summary={
                    "reference_count": len(references),
                    "detached_reason": "admin_delete",
                },
                affected_student_count=len({personal.user_id for personal in references}),
                affected_question_ids=[personal.id for personal in references],
            )
        )
        self.session.delete(question)
        self.session.flush()
        return references, [
            path
            for path in asset_paths
            if not self._asset_path_in_use(path)
        ]

    def lock_many(self, canonical_ids: list[int]) -> list[CanonicalQuestion]:
        if not canonical_ids:
            return []
        return list(
            self.session.execute(
                select(CanonicalQuestion)
                .where(CanonicalQuestion.id.in_(canonical_ids))
                .with_for_update()
            ).scalars()
        )

    def delete_unreferenced(
        self, canonical_id: int, *, actor_id: int, actor_name: str
    ) -> list[str] | None:
        self.acquire_write_lock()
        question = self.get_for_update(canonical_id)
        if question is None:
            raise ValueError("标准题不存在")
        if self.count_references(canonical_id) != 0:
            return None
        asset_paths = [asset.storage_path for asset in question.assets]
        self.session.add(
            QuestionBankAudit(
                actor=actor_name,
                actor_id=actor_id,
                canonical_id=canonical_id,
                action="delete_unreferenced",
                change_summary={"reference_count": 0},
                affected_student_count=0,
                affected_question_ids=[],
            )
        )
        self.session.delete(question)
        self.session.flush()
        return [
            path
            for path in asset_paths
            if not self._asset_path_in_use(path)
        ]

    def delete_student_orphan(self, canonical_id: int) -> list[str] | None:
        question = self.get(canonical_id)
        if (
            question is None
            or question.origin != "student"
            or self.count_references(canonical_id) != 0
        ):
            return None
        shared_paths = [
            asset.storage_path for asset in question.assets if asset.visibility == "shared"
        ]
        self.session.add(
            QuestionBankAudit(
                actor="system",
                actor_id=None,
                canonical_id=canonical_id,
                action="student_orphan_cleanup",
                change_summary={"reason": "zero_references"},
                affected_student_count=0,
                affected_question_ids=[],
            )
        )
        self.session.delete(question)
        self.session.flush()
        return [path for path in shared_paths if not self._asset_path_in_use(path)]
