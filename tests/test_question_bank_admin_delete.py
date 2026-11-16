from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from backend.config import get_settings
from backend.models.orm import (
    Base,
    CanonicalQuestion,
    Comment,
    DetectionLog,
    Job,
    Question,
    QuestionAsset,
    QuestionBankAudit,
    ReviewLog,
    User,
)
from backend.models.schemas import DeleteImpactOut, QuestionOut
from backend.repositories.question_bank import QuestionBankRepository
from backend.services.question_bank import QuestionBankService


class ToggleVectorStore:
    def __init__(self, *, failing: bool = False):
        self.failing = failing
        self.deleted: list[list[str]] = []

    def delete_bank_questions(self, canonical_ids):
        self.deleted.append(list(canonical_ids))
        return not self.failing


def _user(db_session, role: str = "student") -> User:
    user = User(
        username=f"admin-delete-{role}-{uuid.uuid4().hex[:10]}",
        password_hash="not-used",
        role=role,
    )
    db_session.add(user)
    db_session.commit()
    return user


def _admin(db_session, monkeypatch, *, role: str = "student") -> User:
    admin = _user(db_session, role)
    monkeypatch.setattr(get_settings(), "admin_usernames", [admin.username])
    return admin


def _canonical(db_session, **overrides) -> CanonicalQuestion:
    values = {
        "stem_markdown": f"管理员删除标准题 {uuid.uuid4().hex[:10]}",
        "analysis_markdown": "删除前的标准解析",
        "answer": "删除前的标准答案",
        "knowledge_points": ["二次方程"],
        "tags": ["标准标签"],
        "difficulty": "hard",
        "question_type": "解答题",
        "chapter": "一元二次方程",
        "subject": "math",
        "grade": 9,
        "region": "北京",
        "textbook_version": "人教版",
        "origin": "teacher",
        "validation_state": "teacher_published",
    }
    values.update(overrides)
    canonical = QuestionBankRepository(db_session).create(**values)
    db_session.commit()
    return canonical


def _reference(db_session, canonical_id: int, user_id: int, **overrides) -> Question:
    values = {
        "user_id": user_id,
        "canonical_id": canonical_id,
        "private_image_path": f"/private/{uuid.uuid4().hex}.png",
        "content_markdown": "",
        "answer": "",
    }
    values.update(overrides)
    question = Question(**values)
    db_session.add(question)
    db_session.commit()
    return question


def _service(db_session, vectors: ToggleVectorStore | None = None) -> QuestionBankService:
    return QuestionBankService(db_session, vector_store=vectors or ToggleVectorStore())


def test_only_listed_admin_can_preview_or_snapshot_delete(db_session, monkeypatch):
    teacher = _user(db_session, "teacher")
    admin = _admin(db_session, monkeypatch)
    canonical = _canonical(db_session)
    service = _service(db_session)

    for actor_id in (teacher.id, 0, 99999999):
        with pytest.raises(PermissionError):
            service.preview_canonical_delete(actor_id, canonical.id)
        for confirmation in (f"删除标准题 {canonical.id}", "错误短语"):
            with pytest.raises(PermissionError):
                service.delete_canonical_with_snapshots(
                    actor_id,
                    canonical.id,
                    expected_reference_count=0,
                    confirmation=confirmation,
                )

    assert service.preview_canonical_delete(admin.id, canonical.id).reference_count == 0


def test_preview_returns_exact_students_question_ids_and_reference_count(db_session, monkeypatch):
    admin = _admin(db_session, monkeypatch)
    first = _user(db_session)
    second = _user(db_session)
    canonical = _canonical(db_session)
    unrelated = _canonical(db_session)
    first_question = _reference(db_session, canonical.id, first.id)
    second_question = _reference(db_session, canonical.id, second.id)
    _reference(db_session, unrelated.id, first.id)

    impact = _service(db_session).preview_canonical_delete(admin.id, canonical.id)

    assert isinstance(impact, DeleteImpactOut)
    assert impact.canonical_id == canonical.id
    assert impact.reference_count == 2
    assert impact.affected_question_ids == sorted([first_question.id, second_question.id])
    assert impact.affected_students == [
        {"username": first.username, "question_ids": [first_question.id]},
        {"username": second.username, "question_ids": [second_question.id]},
    ]


def test_wrong_confirmation_phrase_is_rejected_without_any_delete(db_session, monkeypatch):
    admin = _admin(db_session, monkeypatch)
    student = _user(db_session)
    canonical = _canonical(db_session)
    personal = _reference(db_session, canonical.id, student.id)
    vectors = ToggleVectorStore()
    service = _service(db_session, vectors)

    for confirmation in ("确认删除", f"删除标准题 {canonical.id} ", f"删除标准题 {canonical.id + 1}"):
        with pytest.raises(ValueError, match=f"删除标准题 {canonical.id}"):
            service.delete_canonical_with_snapshots(
                admin.id,
                canonical.id,
                expected_reference_count=1,
                confirmation=confirmation,
            )

    db_session.expire_all()
    assert db_session.get(CanonicalQuestion, canonical.id) is not None
    assert db_session.get(Question, personal.id).canonical_id == canonical.id
    assert vectors.deleted == []
    assert db_session.query(QuestionBankAudit).filter_by(canonical_id=canonical.id).count() == 0


def test_stale_reference_count_is_rejected_and_requires_new_preview(db_session, monkeypatch):
    admin = _admin(db_session, monkeypatch)
    first = _user(db_session)
    second = _user(db_session)
    canonical = _canonical(db_session)
    personal = _reference(db_session, canonical.id, first.id)
    service = _service(db_session)
    preview = service.preview_canonical_delete(admin.id, canonical.id)
    new_reference = _reference(db_session, canonical.id, second.id)

    with pytest.raises(ValueError, match="引用数.*变化|重新预览") as caught:
        service.delete_canonical_with_snapshots(
            admin.id,
            canonical.id,
            expected_reference_count=preview.reference_count,
            confirmation=f"删除标准题 {canonical.id}",
        )

    assert caught.value.status_code == 409
    db_session.expire_all()
    assert db_session.get(CanonicalQuestion, canonical.id) is not None
    assert db_session.get(Question, personal.id).canonical_id == canonical.id
    assert db_session.get(Question, new_reference.id).canonical_id == canonical.id
    assert service.preview_canonical_delete(admin.id, canonical.id).reference_count == 2


def test_snapshot_delete_preserves_student_content_evidence_history_and_audit(
    db_session, monkeypatch, tmp_path
):
    admin = _admin(db_session, monkeypatch)
    first = _user(db_session)
    second = _user(db_session)
    canonical = _canonical(db_session)
    private_image = tmp_path / "student-original.png"
    private_docx = tmp_path / "student-evidence.docx"
    private_image.write_bytes(b"private-original")
    private_docx.write_bytes(b"private-docx")
    personal = _reference(
        db_session,
        canonical.id,
        first.id,
        private_image_path=str(private_image),
        user_note="个人备注",
        reps=5,
        interval_days=30,
    )
    overridden = _reference(
        db_session,
        canonical.id,
        second.id,
        private_image_path=str(private_docx),
        stem_override="个人题干覆盖",
        analysis_override="个人解析覆盖",
        answer_override="个人答案覆盖",
        source_doc="evidence.docx#2",
    )
    review = ReviewLog(question_id=personal.id, user_id=first.id, grade="good", quality=4)
    comment = Comment(question_id=personal.id, author_id=first.id, content="保留这条评论")
    detection = DetectionLog(
        question_id=personal.id,
        user_id=first.id,
        tested_ref="bank:controlled",
        tested_source="bank",
        result="correct",
    )
    db_session.add_all([review, comment, detection])

    settings = get_settings()
    unique_shared = settings.data_dir / "question-bank" / f"{uuid.uuid4().hex}.png"
    shared_with_survivor = settings.doc_upload_path / f"{uuid.uuid4().hex}.png"
    unique_shared.parent.mkdir(parents=True, exist_ok=True)
    shared_with_survivor.parent.mkdir(parents=True, exist_ok=True)
    unique_shared.write_bytes(b"delete-me")
    shared_with_survivor.write_bytes(b"keep-me")
    survivor = _canonical(db_session)
    db_session.add_all(
        [
            QuestionAsset(
                canonical_id=canonical.id,
                kind="ai_redraw",
                visibility="shared",
                storage_path=str(unique_shared),
            ),
            QuestionAsset(
                canonical_id=canonical.id,
                kind="teacher_original",
                visibility="teacher_only",
                storage_path=str(shared_with_survivor),
            ),
            QuestionAsset(
                canonical_id=survivor.id,
                kind="teacher_original",
                visibility="teacher_only",
                storage_path=str(shared_with_survivor),
            ),
        ]
    )
    db_session.commit()
    vectors = ToggleVectorStore()

    impact = _service(db_session, vectors).delete_canonical_with_snapshots(
        admin.id,
        canonical.id,
        expected_reference_count=2,
        confirmation=f"删除标准题 {canonical.id}",
    )

    assert impact.reference_count == 2
    assert impact.affected_question_ids == sorted([personal.id, overridden.id])
    assert vectors.deleted == [[str(canonical.id)]]
    db_session.expire_all()
    assert db_session.get(CanonicalQuestion, canonical.id) is None
    assert db_session.query(QuestionAsset).filter_by(canonical_id=canonical.id).count() == 0
    assert not unique_shared.exists()
    assert shared_with_survivor.read_bytes() == b"keep-me"
    assert private_image.read_bytes() == b"private-original"
    assert private_docx.read_bytes() == b"private-docx"

    stored = db_session.get(Question, personal.id)
    assert stored.canonical_id is None
    assert stored.detached_reason == "admin_delete"
    assert stored.stem_snapshot == canonical.stem_markdown
    assert stored.analysis_snapshot == "删除前的标准解析"
    assert stored.answer_snapshot == "删除前的标准答案"
    assert stored.metadata_snapshot == {
        "knowledge_points": ["二次方程"],
        "tags": ["标准标签"],
        "difficulty": "hard",
        "question_type": "解答题",
        "chapter": "一元二次方程",
        "subject": "math",
        "grade": 9,
        "region": "北京",
        "textbook_version": "人教版",
    }
    assert stored.private_image_path == str(private_image)
    assert stored.user_note == "个人备注"
    assert stored.reps == 5
    assert stored.interval_days == 30
    assert db_session.get(ReviewLog, review.id) is not None
    assert db_session.get(Comment, comment.id).content == "保留这条评论"
    assert db_session.get(DetectionLog, detection.id).tested_ref == "bank:controlled"
    student_view = QuestionOut.from_orm_model(stored)
    assert canonical.stem_markdown in student_view.content_markdown
    assert "删除前的标准解析" in student_view.content_markdown
    assert student_view.answer == "删除前的标准答案"
    assert student_view.image_path == str(private_image)

    stored_override = db_session.get(Question, overridden.id)
    assert stored_override.canonical_id is None
    assert stored_override.detached_reason == "admin_delete"
    assert stored_override.stem_override == "个人题干覆盖"
    assert stored_override.analysis_override == "个人解析覆盖"
    assert stored_override.answer_override == "个人答案覆盖"
    assert stored_override.private_image_path == str(private_docx)
    override_view = QuestionOut.from_orm_model(stored_override)
    assert override_view.stem == "个人题干覆盖"
    assert override_view.analysis == "个人解析覆盖"
    assert override_view.answer == "个人答案覆盖"

    audit = db_session.execute(
        select(QuestionBankAudit).where(
            QuestionBankAudit.canonical_id == canonical.id,
            QuestionBankAudit.action == "delete_with_snapshots",
        )
    ).scalar_one()
    assert audit.actor == admin.username
    assert audit.actor_id == admin.id
    assert audit.change_summary == {
        "reference_count": 2,
        "detached_reason": "admin_delete",
    }
    assert audit.affected_student_count == 2
    assert audit.affected_question_ids == sorted([personal.id, overridden.id])


def test_vector_failure_keeps_committed_snapshots_and_real_retry_is_idempotent(
    db_session, monkeypatch
):
    admin = _admin(db_session, monkeypatch)
    student = _user(db_session)
    canonical = _canonical(db_session)
    personal = _reference(db_session, canonical.id, student.id)
    vectors = ToggleVectorStore(failing=True)
    service = _service(db_session, vectors)

    impact = service.delete_canonical_with_snapshots(
        admin.id,
        canonical.id,
        expected_reference_count=1,
        confirmation=f"删除标准题 {canonical.id}",
    )

    assert impact.affected_question_ids == [personal.id]
    db_session.expire_all()
    assert db_session.get(CanonicalQuestion, canonical.id) is None
    assert db_session.get(Question, personal.id).stem_snapshot == canonical.stem_markdown
    job = db_session.execute(
        select(Job).where(
            Job.user_id == admin.id,
            Job.type == "vector_reconciliation",
            Job.status == "pending",
        )
    ).scalars().one()
    assert job.payload == {"operation": "delete", "canonical_id": canonical.id}
    assert service.has_pending_vector_sync(admin.id, canonical.id, operation="delete") is True

    vectors.failing = False
    assert service.retry_vector_reconciliation(admin.id, job.id) is True

    db_session.expire_all()
    assert db_session.get(Job, job.id).status == "success"
    assert db_session.get(Job, job.id).finished_at is not None
    assert service.has_pending_vector_sync(admin.id, canonical.id, operation="delete") is False
    assert vectors.deleted == [[str(canonical.id)], [str(canonical.id)]]
    assert service.retry_vector_reconciliation(admin.id, job.id) is True
    assert vectors.deleted == [[str(canonical.id)], [str(canonical.id)]]


def test_stale_confirmation_releases_sqlite_lock_for_injected_session(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'stale-admin-delete.db'}",
        connect_args={"check_same_thread": False, "timeout": 0.1},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as session:
        admin = User(username="stale-admin", password_hash="x", role="student")
        first = User(username="stale-first", password_hash="x", role="student")
        second = User(username="stale-second", password_hash="x", role="student")
        session.add_all([admin, first, second])
        session.flush()
        canonical = QuestionBankRepository(session).create(
            stem_markdown="过期确认释放锁", origin="teacher"
        )
        session.add_all(
            [
                Question(
                    user_id=first.id,
                    canonical_id=canonical.id,
                    private_image_path="first.png",
                    content_markdown="",
                    answer="",
                ),
                Question(
                    user_id=second.id,
                    canonical_id=canonical.id,
                    private_image_path="second.png",
                    content_markdown="",
                    answer="",
                ),
            ]
        )
        session.commit()
        admin_id, canonical_id = admin.id, canonical.id
    monkeypatch.setattr(get_settings(), "admin_usernames", ["stale-admin"])

    with factory() as injected:
        with pytest.raises(ValueError) as caught:
            QuestionBankService(
                injected, vector_store=ToggleVectorStore()
            ).delete_canonical_with_snapshots(
                admin_id,
                canonical_id,
                expected_reference_count=1,
                confirmation=f"删除标准题 {canonical_id}",
            )
        assert caught.value.status_code == 409

        with factory() as writer:
            writer.add(User(username="post-stale-writer", password_hash="x", role="student"))
            writer.commit()

    with factory() as session:
        assert session.query(User).filter_by(username="post-stale-writer").count() == 1
        assert session.query(Question).filter_by(canonical_id=canonical_id).count() == 2


@pytest.mark.parametrize("evidence_field", ["private_image_path", "image_path"])
@pytest.mark.parametrize("suffix", ["png", "docx"])
def test_same_path_personal_evidence_survives_canonical_asset_cleanup(
    db_session, monkeypatch, evidence_field, suffix
):
    admin = _admin(db_session, monkeypatch)
    student = _user(db_session)
    canonical = _canonical(db_session)
    evidence = get_settings().doc_upload_path / f"same-path-{uuid.uuid4().hex}.{suffix}"
    evidence.parent.mkdir(parents=True, exist_ok=True)
    content = f"shared-personal-evidence-{evidence_field}-{suffix}".encode()
    evidence.write_bytes(content)
    personal = _reference(
        db_session,
        canonical.id,
        student.id,
        **{evidence_field: str(evidence)},
    )
    db_session.add(
        QuestionAsset(
            canonical_id=canonical.id,
            kind="teacher_original",
            visibility="teacher_only",
            storage_path=str(evidence),
        )
    )
    db_session.commit()

    _service(db_session).delete_canonical_with_snapshots(
        admin.id,
        canonical.id,
        expected_reference_count=1,
        confirmation=f"删除标准题 {canonical.id}",
    )

    db_session.expire_all()
    stored = db_session.get(Question, personal.id)
    assert stored.canonical_id is None
    assert getattr(stored, evidence_field) == str(evidence)
    assert evidence.read_bytes() == content
    cleanup_jobs = db_session.query(Job).filter_by(
        user_id=admin.id, type="asset_cleanup"
    ).all()
    assert all(str(evidence) not in (job.payload.get("paths") or []) for job in cleanup_jobs)


def test_asset_cleanup_failure_preserves_sql_and_records_separate_retryable_job(
    db_session, monkeypatch
):
    admin = _admin(db_session, monkeypatch)
    student = _user(db_session)
    canonical = _canonical(db_session)
    personal = _reference(db_session, canonical.id, student.id)
    asset = get_settings().data_dir / "question-bank" / f"unlink-fails-{uuid.uuid4().hex}.png"
    asset.parent.mkdir(parents=True, exist_ok=True)
    asset.write_bytes(b"cleanup-must-retry")
    db_session.add(
        QuestionAsset(
            canonical_id=canonical.id,
            kind="ai_redraw",
            visibility="shared",
            storage_path=str(asset),
        )
    )
    db_session.commit()
    original_unlink = Path.unlink

    def fail_for_asset(path, *args, **kwargs):
        if path == asset:
            raise OSError("controlled admin asset cleanup failure")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_for_asset)

    impact = _service(db_session).delete_canonical_with_snapshots(
        admin.id,
        canonical.id,
        expected_reference_count=1,
        confirmation=f"删除标准题 {canonical.id}",
    )

    assert impact.affected_question_ids == [personal.id]
    db_session.expire_all()
    assert db_session.get(CanonicalQuestion, canonical.id) is None
    stored = db_session.get(Question, personal.id)
    assert stored.canonical_id is None
    assert stored.detached_reason == "admin_delete"
    assert asset.read_bytes() == b"cleanup-must-retry"
    cleanup_jobs = db_session.query(Job).filter_by(
        user_id=admin.id, type="asset_cleanup", status="pending"
    ).all()
    cleanup = next(
        (job for job in cleanup_jobs if job.payload.get("canonical_id") == canonical.id),
        None,
    )
    assert cleanup is not None
    assert cleanup.payload["paths"] == [str(asset)]
    assert cleanup.payload["roots"]
    assert "controlled admin asset cleanup failure" in cleanup.error
    service = _service(db_session)
    assert service.has_pending_asset_cleanup(admin.id, canonical.id) is True
    assert service.has_pending_vector_sync(admin.id, canonical.id, operation="delete") is False
