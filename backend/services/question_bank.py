"""共享标准题的精确匹配、相似候选与创建升级。"""
from __future__ import annotations

import datetime as dt
import hashlib
import mimetypes
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.config import get_settings
from backend.database import SessionLocal
from backend.models.orm import (
    CanonicalQuestion,
    Job,
    Question,
    QuestionAsset,
    QuestionBankAudit,
    QuestionSourceFingerprint,
    QuestionTypeDefinition,
    User,
)
from backend.models.schemas import (
    CanonicalQuestionOut,
    CanonicalQuestionUpdate,
    DeleteImpactOut,
    QuestionAnalysis,
    QuestionBankCandidate,
)
from backend.repositories.question_bank import QuestionBankRepository
from backend.services.ai.base import AnalysisContext
from backend.services.ai.telemetry import record_bank_match
from backend.services.question_identity import normalize_stem, stem_hash
from backend.services.rag import QuestionVectorStore, SimilarConstraints
from backend.utils.logging import get_logger

logger = get_logger("question_bank")


class QuestionBankConflictError(ValueError):
    status_code = 409


class QuestionBankService:
    def __init__(
        self,
        session: Session | None = None,
        *,
        session_factory: Callable[[], Session] | None = None,
        vector_store: QuestionVectorStore | None = None,
    ):
        if session is not None and session_factory is not None:
            raise ValueError("session 与 session_factory 不能同时提供")
        self._session = session
        self._session_factory = session_factory or SessionLocal
        self.settings = get_settings()
        self.vector_store = vector_store or QuestionVectorStore()

    @contextmanager
    def _session_scope(self) -> Iterator[Session]:
        if self._session is not None:
            try:
                yield self._session
            except Exception:
                self._session.rollback()
                raise
            return
        with self._session_factory() as session:
            yield session

    @staticmethod
    def _require_manager(session: Session, actor_id: int) -> User:
        from backend.services.auth import is_admin_username

        actor = session.get(User, actor_id)
        if actor is None or not (actor.role == "teacher" or is_admin_username(actor.username)):
            raise PermissionError("仅教师或管理员可管理标准题库")
        return actor

    def search_canonical(
        self,
        actor_id: int,
        *,
        keyword: str = "",
        semantic: bool = False,
        subject: str | None = None,
        grade: int | None = None,
        question_type: str | None = None,
        origin: str | None = None,
        textbook_version: str | None = None,
        reference_count: int | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[CanonicalQuestionOut]:
        if limit <= 0 or offset < 0:
            raise ValueError("分页参数无效")
        if reference_count is not None and reference_count < 0:
            raise ValueError("引用数不能为负数")
        clean_keyword = keyword.strip()
        canonical_ids: list[int] | None = None
        ranks: dict[int, int] = {}
        with self._session_scope() as session:
            self._require_manager(session, actor_id)
            if semantic and clean_keyword:
                result = self.vector_store.similar_canonical_questions(
                    normalize_stem(clean_keyword),
                    constraints=SimilarConstraints(subject=subject, grade=grade),
                    top_k=max(offset + limit, 50),
                )
                canonical_ids = []
                for hit in result.hits:
                    if hit.bank_id and hit.bank_id.isdigit():
                        canonical_id = int(hit.bank_id)
                        if canonical_id not in ranks:
                            ranks[canonical_id] = len(canonical_ids)
                            canonical_ids.append(canonical_id)
            repository = QuestionBankRepository(session)
            rows = repository.search(
                keyword="" if semantic and clean_keyword else clean_keyword,
                subject=subject,
                grade=grade,
                question_type=question_type,
                origin=origin,
                textbook_version=textbook_version,
                reference_count=reference_count,
                canonical_ids=canonical_ids,
                limit=len(canonical_ids) if canonical_ids is not None else limit,
                offset=0 if canonical_ids is not None else offset,
            )
        if canonical_ids is not None:
            rows.sort(key=lambda row: ranks[row[0].id])
            rows = rows[offset : offset + limit]
        return [
            CanonicalQuestionOut.from_orm_model(question, reference_count=count)
            for question, count in rows
        ]

    def get_canonical(self, actor_id: int, canonical_id: int) -> CanonicalQuestionOut:
        with self._session_scope() as session:
            self._require_manager(session, actor_id)
            repository = QuestionBankRepository(session)
            question = repository.get(canonical_id)
            if question is None:
                raise ValueError("标准题不存在")
            return CanonicalQuestionOut.from_orm_model(
                question, reference_count=repository.count_references(canonical_id)
            )

    def list_canonical_assets(
        self, actor_id: int, canonical_id: int
    ) -> list[QuestionAsset]:
        with self._session_scope() as session:
            self._require_manager(session, actor_id)
            repository = QuestionBankRepository(session)
            if repository.get(canonical_id) is None:
                raise ValueError("标准题不存在")
            return repository.list_assets(canonical_id)

    def read_canonical_asset(self, actor_id: int, asset_id: int) -> bytes:
        with self._session_scope() as session:
            self._require_manager(session, actor_id)
            asset = QuestionBankRepository(session).get_asset(asset_id)
            if asset is None:
                raise ValueError("题库资源不存在")
            path = Path(asset.storage_path)
            if not path.is_file():
                raise ValueError("题库资源不存在")
            return path.read_bytes()

    def update_canonical(
        self, actor_id: int, canonical_id: int, updates: CanonicalQuestionUpdate
    ) -> CanonicalQuestionOut:
        with self._session_scope() as session:
            actor = self._require_manager(session, actor_id)
            repository = QuestionBankRepository(session)
            question = repository.get_for_update(canonical_id)
            if question is None:
                raise ValueError("标准题不存在")
            changes = updates.model_dump(exclude_unset=True)
            if "stem_markdown" in changes:
                stem = changes["stem_markdown"]
                if stem is None:
                    raise ValueError("题干不能为空")
                normalized = normalize_stem(stem)
                normalized_hash = stem_hash(stem)
                collision = repository.get_by_normalized_hash(normalized_hash)
                if collision is not None and collision.id != canonical_id:
                    raise ValueError("题干规范化哈希与其他标准题冲突")
                question.stem_markdown = stem
                question.normalized_stem = normalized
                question.normalized_hash = normalized_hash
                changes.pop("stem_markdown")
            for name in ("subject", "difficulty"):
                if name in changes and changes[name] is None:
                    raise ValueError(f"{name} 不能为空")
            for name, value in changes.items():
                setattr(question, name, value)
            session.add(
                QuestionBankAudit(
                    actor=actor.username,
                    actor_id=actor.id,
                    canonical_id=canonical_id,
                    action="update",
                    change_summary={"fields": sorted(updates.model_fields_set)},
                    affected_student_count=0,
                    affected_question_ids=[],
                )
            )
            try:
                session.commit()
            except IntegrityError as exc:
                session.rollback()
                raise ValueError("题干规范化哈希与其他标准题冲突") from exc
            reference_count = repository.count_references(canonical_id)
            result = CanonicalQuestionOut.from_orm_model(
                question, reference_count=reference_count
            )
        self.reconcile_canonical_vector(actor_id, canonical_id, "upsert", question=question)
        return result

    @staticmethod
    def _require_admin(session: Session, actor_id: int) -> User:
        from backend.services.auth import is_admin_username

        actor = session.get(User, actor_id)
        if actor is None or not is_admin_username(actor.username):
            raise PermissionError("仅管理员可删除被引用的标准题")
        return actor

    @staticmethod
    def _delete_impact(canonical_id: int, references: list[Question]) -> DeleteImpactOut:
        students: dict[int, dict] = {}
        for personal in references:
            entry = students.setdefault(
                personal.user_id,
                {"username": personal.user.username, "question_ids": []},
            )
            entry["question_ids"].append(personal.id)
        return DeleteImpactOut(
            canonical_id=canonical_id,
            reference_count=len(references),
            affected_students=list(students.values()),
            affected_question_ids=[personal.id for personal in references],
        )

    def preview_canonical_delete(self, actor_id: int, canonical_id: int) -> DeleteImpactOut:
        with self._session_scope() as session:
            self._require_admin(session, actor_id)
            repository = QuestionBankRepository(session)
            if repository.get(canonical_id) is None:
                raise ValueError("标准题不存在")
            return self._delete_impact(canonical_id, repository.list_references(canonical_id))

    def delete_canonical_with_snapshots(
        self,
        actor_id: int,
        canonical_id: int,
        *,
        expected_reference_count: int,
        confirmation: str,
    ) -> DeleteImpactOut:
        with self._session_scope() as session:
            actor = self._require_admin(session, actor_id)
            expected_confirmation = f"删除标准题 {canonical_id}"
            if confirmation != expected_confirmation:
                raise ValueError(f"确认短语不匹配，请输入：{expected_confirmation}")
            outcome = QuestionBankRepository(session).delete_with_snapshots(
                canonical_id,
                expected_reference_count=expected_reference_count,
                actor_id=actor.id,
                actor_name=actor.username,
            )
            if outcome is None:
                raise QuestionBankConflictError("标准题引用数已变化，请重新预览后再确认")
            references, asset_paths = outcome
            impact = self._delete_impact(canonical_id, references)
            session.commit()
        self._cleanup_asset_files(actor_id, canonical_id, asset_paths)
        self.reconcile_canonical_vector(actor_id, canonical_id, "delete")
        return impact

    def delete_unreferenced(self, actor_id: int, canonical_id: int) -> None:
        with self._session_scope() as session:
            actor = self._require_manager(session, actor_id)
            repository = QuestionBankRepository(session)
            if repository.get_for_update(canonical_id) is None:
                raise ValueError("标准题不存在")
            asset_paths = repository.delete_unreferenced(
                canonical_id, actor_id=actor.id, actor_name=actor.username
            )
            if asset_paths is None:
                raise QuestionBankConflictError("标准题仍被引用，不能删除")
            session.commit()
        self._cleanup_asset_files(actor_id, canonical_id, asset_paths)
        self.reconcile_canonical_vector(actor_id, canonical_id, "delete")

    def retry_vector_reconciliation(self, actor_id: int, job_id: str) -> bool:
        with self._session_scope() as session:
            self._require_manager(session, actor_id)
            job = session.get(Job, job_id)
            if job is None or job.type != "vector_reconciliation":
                raise ValueError("向量对账任务不存在")
            if job.user_id != actor_id:
                raise PermissionError("不能重试其他用户的向量对账任务")
            if job.status == "success":
                return True
            if job.status != "pending":
                raise ValueError("只有待处理的向量对账任务可以重试")
            payload = job.payload or {}
            canonical_id = payload.get("canonical_id")
            operation = payload.get("operation")
            try:
                if operation == "delete":
                    completed = self.vector_store.delete_bank_questions([str(canonical_id)])
                elif operation == "upsert":
                    question = session.get(CanonicalQuestion, canonical_id)
                    if question is None:
                        raise ValueError("标准题不存在，无法重建向量索引")
                    completed = self._index(question)
                else:
                    raise ValueError("未知向量对账操作")
            except Exception as exc:  # noqa: BLE001 - 保留任务供下次重试
                completed = False
                error = str(exc)
            else:
                error = None if completed else "向量服务返回失败"
            if not completed:
                job.error = error
                session.commit()
                return False
            jobs = session.execute(
                select(Job).where(
                    Job.user_id == actor_id,
                    Job.type == "vector_reconciliation",
                    Job.status == "pending",
                )
            ).scalars()
            for pending in jobs:
                pending_payload = pending.payload or {}
                if (
                    pending_payload.get("canonical_id") == canonical_id
                    and pending_payload.get("operation") == operation
                ):
                    pending.status = "success"
                    pending.result = {"resolved_by": "retry", "operation": operation}
                    pending.error = None
                    pending.finished_at = dt.datetime.now(dt.timezone.utc)
            session.commit()
            return True

    def has_pending_vector_sync(
        self, actor_id: int, canonical_id: int, *, operation: str = "upsert"
    ) -> bool:
        with self._session_scope() as session:
            self._require_manager(session, actor_id)
            jobs = session.execute(
                select(Job).where(
                    Job.user_id == actor_id,
                    Job.type == "vector_reconciliation",
                    Job.status == "pending",
                )
            ).scalars()
            return any(
                (job.payload or {}).get("canonical_id") == canonical_id
                and (job.payload or {}).get("operation") == operation
                for job in jobs
            )

    def has_pending_asset_cleanup(self, actor_id: int, canonical_id: int) -> bool:
        with self._session_scope() as session:
            self._require_manager(session, actor_id)
            jobs = session.execute(
                select(Job).where(
                    Job.user_id == actor_id,
                    Job.type == "asset_cleanup",
                    Job.status == "pending",
                )
            ).scalars()
            return any(
                (job.payload or {}).get("canonical_id") == canonical_id for job in jobs
            )

    def reconcile_canonical_vector(
        self,
        actor_id: int,
        canonical_id: int,
        operation: str,
        *,
        question: CanonicalQuestion | None = None,
    ) -> bool:
        if operation not in {"upsert", "delete"}:
            raise ValueError("未知向量对账操作")
        try:
            if operation == "delete":
                completed = self.vector_store.delete_bank_questions([str(canonical_id)])
            else:
                if question is None:
                    with self._session_scope() as session:
                        question = session.get(CanonicalQuestion, canonical_id)
                if question is None:
                    raise ValueError("标准题不存在，无法重建向量索引")
                completed = self._index(question)
        except Exception as exc:  # noqa: BLE001 - SQL 已提交，失败进入对账
            completed = False
            error = str(exc)
        else:
            error = None if completed else "向量服务返回失败"
        if not completed:
            logger.warning(
                "标准题 %s 的向量 %s 失败，已记录对账: %s",
                canonical_id,
                operation,
                error,
            )
            self._record_vector_failure(actor_id, canonical_id, operation, error or "未知错误")
            return False
        self._resolve_vector_jobs(actor_id, canonical_id, operation)
        return True

    def _record_vector_failure(
        self, actor_id: int, canonical_id: int, operation: str, error: str
    ) -> None:
        with self._session_scope() as session:
            session.add(
                Job(
                    id=str(uuid.uuid4()),
                    user_id=actor_id,
                    type="vector_reconciliation",
                    status="pending",
                    payload={"operation": operation, "canonical_id": canonical_id},
                    error=error,
                )
            )
            session.commit()

    def _resolve_vector_jobs(
        self, actor_id: int, canonical_id: int, operation: str
    ) -> None:
        with self._session_scope() as session:
            jobs = session.execute(
                select(Job).where(
                    Job.user_id == actor_id,
                    Job.type == "vector_reconciliation",
                    Job.status == "pending",
                )
            ).scalars()
            for job in jobs:
                payload = job.payload or {}
                if (
                    payload.get("canonical_id") == canonical_id
                    and payload.get("operation") == operation
                ):
                    job.status = "success"
                    job.result = {"resolved_by": operation}
                    job.finished_at = dt.datetime.now(dt.timezone.utc)
            session.commit()

    def _cleanup_asset_files(
        self, actor_id: int, canonical_id: int, paths: list[str]
    ) -> bool:
        settings = get_settings()
        roots = [
            (settings.data_dir / "question-bank").resolve(),
            (settings.data_dir / "figures" / "tikz").resolve(),
            settings.doc_upload_path.resolve(),
        ]
        completed = True
        for stored_path in paths:
            path = Path(stored_path).expanduser().resolve()
            if not any(path != root and path.is_relative_to(root) for root in roots):
                logger.warning("拒绝清理题库目录外的资源: %s", path)
                continue
            try:
                path.unlink(missing_ok=True)
            except OSError as exc:
                completed = False
                logger.warning("题库资源清理失败 path=%s: %s", path, exc)
                with self._session_scope() as session:
                    session.add(
                        Job(
                            id=str(uuid.uuid4()),
                            user_id=actor_id,
                            type="asset_cleanup",
                            status="pending",
                            payload={
                                "paths": [stored_path],
                                "roots": [str(root) for root in roots],
                                "canonical_id": canonical_id,
                            },
                            error=str(exc),
                        )
                    )
                    session.commit()
        return completed

    def list_question_types(self, subject: str) -> list[str]:
        with self._session_scope() as session:
            definitions = session.execute(
                select(QuestionTypeDefinition)
                .where(
                    QuestionTypeDefinition.subject == (subject.strip() or "math"),
                    QuestionTypeDefinition.enabled.is_(True),
                )
                .order_by(QuestionTypeDefinition.name)
            ).scalars()
            return [definition.name for definition in definitions]

    def create_question_type(
        self, actor_id: int, subject: str, name: str
    ) -> QuestionTypeDefinition:
        clean_subject = subject.strip() or "math"
        clean_name = name.strip()
        if not clean_name:
            raise ValueError("题型名称不能为空")
        with self._session_scope() as session:
            actor = session.get(User, actor_id)
            if actor is None or actor.role != "teacher":
                raise PermissionError("仅教师可维护题型")
            existing = session.execute(
                select(QuestionTypeDefinition).where(
                    QuestionTypeDefinition.subject == clean_subject,
                    QuestionTypeDefinition.name == clean_name,
                )
            ).scalar_one_or_none()
            if existing is not None:
                return existing
            definition = QuestionTypeDefinition(
                subject=clean_subject, name=clean_name, created_by=actor_id
            )
            session.add(definition)
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
                definition = session.execute(
                    select(QuestionTypeDefinition).where(
                        QuestionTypeDefinition.subject == clean_subject,
                        QuestionTypeDefinition.name == clean_name,
                    )
                ).scalar_one_or_none()
                if definition is None:
                    raise
            return definition

    def register_shared_redraws(
        self, canonical_id: int, figure_specs: list[dict]
    ) -> dict[str, str]:
        validated = [
            str(spec.get("render_path") or "")
            for spec in figure_specs
            if spec.get("validation_status") in {"ai_passed", "manual_passed"}
            and spec.get("render_path")
        ]
        if not validated:
            return {}
        registered: dict[str, str] = {}
        with self._session_scope() as session:
            repository = QuestionBankRepository(session)
            repository.acquire_write_lock()
            if repository.get_for_update(canonical_id) is None:
                raise ValueError("标准题不存在")
            for original in validated:
                source = Path(original)
                if not source.is_file():
                    continue
                data = source.read_bytes()
                content_hash = hashlib.sha256(data).hexdigest()
                existing = session.execute(
                    select(QuestionAsset).where(
                        QuestionAsset.canonical_id == canonical_id,
                        QuestionAsset.kind == "ai_redraw",
                        QuestionAsset.content_hash == content_hash,
                    )
                ).scalars().first()
                if existing is not None:
                    registered[original] = existing.storage_path
                    continue
                suffix = source.suffix.lower() if source.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"} else ".png"
                target_dir = self.settings.data_dir / "question-bank" / "shared" / str(canonical_id)
                target_dir.mkdir(parents=True, exist_ok=True)
                target = target_dir / f"{content_hash}{suffix}"
                if not target.is_file():
                    target.write_bytes(data)
                session.add(
                    QuestionAsset(
                        canonical_id=canonical_id,
                        kind="ai_redraw",
                        visibility="shared",
                        storage_path=str(target),
                        content_hash=content_hash,
                        mime_type=mimetypes.guess_type(target.name)[0] or "image/png",
                        is_preferred=True,
                    )
                )
                registered[original] = str(target)
            session.commit()
        return registered

    def shared_asset_savings(self, canonical_id: int) -> tuple[int, int]:
        with self._session_scope() as session:
            paths = list(
                session.execute(
                    select(QuestionAsset.storage_path).where(
                        QuestionAsset.canonical_id == canonical_id,
                        QuestionAsset.kind == "ai_redraw",
                        QuestionAsset.visibility == "shared",
                    )
                ).scalars()
            )
        sizes = []
        for path in dict.fromkeys(paths):
            try:
                sizes.append(Path(path).stat().st_size)
            except OSError:
                continue
        return len(sizes), sum(sizes)

    def get_reusable(self, canonical_id: int) -> CanonicalQuestion:
        with self._session_scope() as session:
            repository = QuestionBankRepository(session)
            question = repository.get(canonical_id)
            if question is None or not repository.is_reusable(question):
                raise ValueError("标准题不存在或不完整")
            return question

    def match_exact(self, stem: str) -> CanonicalQuestion | None:
        with self._session_scope() as session:
            repository = QuestionBankRepository(session)
            question = repository.find_by_stem(stem)
            if question is not None and not repository.is_reusable(question):
                question = None
        if question is not None:
            record_bank_match("exact")
        return question

    def match_original_hash(self, content_hash: str) -> CanonicalQuestion | None:
        with self._session_scope() as session:
            teacher_ids = set(
                session.execute(
                    select(QuestionAsset.canonical_id).where(
                        QuestionAsset.kind == "teacher_original",
                        QuestionAsset.visibility == "teacher_only",
                        QuestionAsset.content_hash == content_hash,
                    )
                ).scalars()
            )
            source_ids = set(
                session.execute(
                    select(QuestionSourceFingerprint.canonical_id).where(
                        QuestionSourceFingerprint.content_hash == content_hash
                    )
                ).scalars()
            )
            repository = QuestionBankRepository(session)
            reusable = [
                row
                for row in repository.get_many(list(teacher_ids | source_ids))
                if repository.is_reusable(row)
            ]
            question = reusable[0] if len(reusable) == 1 else None
        if question is not None:
            record_bank_match("exact")
        return question

    def find_similar(
        self,
        stem: str,
        *,
        context: AnalysisContext,
        limit: int = 3,
    ) -> list[QuestionBankCandidate]:
        result = self.vector_store.similar_canonical_questions(
            normalize_stem(stem),
            constraints=SimilarConstraints(
                subject=context.subject,
                grade=context.grade,
                region=context.region,
            ),
            top_k=limit,
        )
        hits = [
            hit
            for hit in result.hits
            if hit.bank_id is not None
            and hit.bank_id.isdigit()
            and 1.0 - hit.distance >= self.settings.bank_similarity_threshold
        ]
        canonical_ids = [int(hit.bank_id) for hit in hits if hit.bank_id is not None]
        if not canonical_ids:
            record_bank_match("new")
            return []
        with self._session_scope() as session:
            repository = QuestionBankRepository(session)
            questions = {
                question.id: question
                for question in repository.get_many(canonical_ids)
                if repository.is_reusable(question)
            }
            candidates = [
                QuestionBankCandidate(
                    score=1.0 - hit.distance,
                    question=CanonicalQuestionOut.from_orm_model(questions[int(hit.bank_id)]),
                )
                for hit in hits
                if hit.bank_id is not None and int(hit.bank_id) in questions
            ]
        candidates.sort(
            key=lambda candidate: (
                candidate.question.origin != "teacher",
                -candidate.score,
            )
        )
        record_bank_match("similar" if candidates else "new")
        return candidates[:limit]

    def create_or_upgrade(
        self,
        stem: str,
        analysis: QuestionAnalysis,
        *,
        actor: User,
        context: AnalysisContext,
        question_type: str | None = None,
        canonical_id: int | None = None,
    ) -> CanonicalQuestion:
        is_teacher = actor.role == "teacher"
        actor_id = actor.id
        changed = False

        with self._session_scope() as session:
            repository = QuestionBankRepository(session)
            question = (
                repository.get(canonical_id)
                if canonical_id is not None
                else repository.find_by_stem(stem)
            )
            if canonical_id is not None and question is None:
                raise ValueError("选择的标准题不存在")
            if question is not None:
                if is_teacher:
                    self._apply_teacher_content(
                        question,
                        stem,
                        analysis,
                        actor_id=actor_id,
                        context=context,
                        question_type=question_type,
                    )
                    session.commit()
                    changed = True
                elif not repository.is_reusable(question):
                    self._validate_student_content(stem, analysis)
                    self._backfill_student_content(question, analysis, context=context)
                    session.commit()
                    changed = True
            else:
                if not is_teacher:
                    self._validate_student_content(stem, analysis)
                try:
                    question = repository.create(
                        stem_markdown=stem,
                        analysis_markdown=analysis.analysis,
                        answer=analysis.answer,
                        knowledge_points=analysis.knowledge_points,
                        tags=analysis.tags,
                        difficulty=analysis.difficulty,
                        question_type=(
                            question_type
                            if question_type is not None
                            else analysis.question_type or None
                        ),
                        chapter=analysis.chapter or None,
                        subject=context.subject or "math",
                        grade=context.grade,
                        region=context.region,
                        textbook_version=context.textbook_version,
                        origin="teacher" if is_teacher else "student",
                        validation_state=(
                            "teacher_published" if is_teacher else "machine_validated"
                        ),
                        published_by=actor_id if is_teacher else None,
                    )
                    session.commit()
                    changed = True
                except IntegrityError:
                    session.rollback()
                    question = repository.find_by_stem(stem)
                    if question is None:
                        raise
                    if is_teacher:
                        self._apply_teacher_content(
                            question,
                            stem,
                            analysis,
                            actor_id=actor_id,
                            context=context,
                            question_type=question_type,
                        )
                        session.commit()
                        changed = True
                    elif not repository.is_reusable(question):
                        self._validate_student_content(stem, analysis)
                        self._backfill_student_content(question, analysis, context=context)
                        session.commit()
                        changed = True
        if changed:
            self.reconcile_canonical_vector(actor_id, question.id, "upsert", question=question)
        return question

    @staticmethod
    def _validate_student_content(stem: str, analysis: QuestionAnalysis) -> None:
        if not analysis.is_complete:
            raise ValueError("题目不完整，不能创建标准题")
        if not stem.strip():
            raise ValueError("题干不能为空")
        if not analysis.answer.strip():
            raise ValueError("答案不能为空")
        if not analysis.analysis.strip():
            raise ValueError("解析不能为空")

    @staticmethod
    def _backfill_student_content(
        question: CanonicalQuestion,
        analysis: QuestionAnalysis,
        *,
        context: AnalysisContext,
    ) -> None:
        values = {
            "analysis_markdown": analysis.analysis,
            "answer": analysis.answer,
            "knowledge_points": list(analysis.knowledge_points),
            "tags": list(analysis.tags),
            "difficulty": analysis.difficulty,
            "question_type": analysis.question_type or None,
            "chapter": analysis.chapter or None,
            "subject": context.subject or "math",
            "grade": context.grade,
            "region": context.region,
            "textbook_version": context.textbook_version,
        }
        for name, value in values.items():
            current = getattr(question, name)
            if current is None or current == [] or (
                isinstance(current, str) and not current.strip()
            ):
                setattr(question, name, value)

    @staticmethod
    def _apply_teacher_content(
        question: CanonicalQuestion,
        stem: str,
        analysis: QuestionAnalysis,
        *,
        actor_id: int,
        context: AnalysisContext,
        question_type: str | None,
    ) -> None:
        question.stem_markdown = stem
        question.normalized_stem = normalize_stem(stem)
        question.normalized_hash = stem_hash(stem)
        question.analysis_markdown = analysis.analysis
        question.answer = analysis.answer
        question.knowledge_points = list(analysis.knowledge_points)
        question.tags = list(analysis.tags)
        question.difficulty = analysis.difficulty
        question.question_type = (
            question_type if question_type is not None else analysis.question_type or None
        )
        question.chapter = analysis.chapter or None
        question.subject = context.subject or "math"
        question.grade = context.grade
        question.region = context.region
        question.textbook_version = context.textbook_version
        question.origin = "teacher"
        question.validation_state = "teacher_published"
        question.published_by = actor_id

    def _index(self, question: CanonicalQuestion) -> bool:
        return self.vector_store.upsert_bank_question(
            str(question.id),
            question.stem_markdown,
            subject=question.subject,
            grade=question.grade,
            region=question.region,
            knowledge_points=list(question.knowledge_points or []),
            difficulty=question.difficulty,
            answer=question.answer,
            question_type=question.question_type or "",
            chapter=question.chapter or "",
        )
