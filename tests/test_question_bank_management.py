from __future__ import annotations

import hashlib
import threading
import uuid

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker

from backend.config import get_settings
from backend.models.orm import (
    Base,
    CanonicalQuestion,
    Job,
    Question,
    QuestionAsset,
    QuestionBankAudit,
    User,
)
from backend.models.schemas import CanonicalQuestionOut, CanonicalQuestionUpdate
from backend.repositories.question_bank import QuestionBankRepository
from backend.services.question_bank import QuestionBankService
from backend.services.rag import RagHit, SimilarResult


class RecordingVectorStore:
    def __init__(self, hits: list[RagHit] | None=None):
        self.hits = hits or []
        self.upserted: list[tuple[str, str, dict]] = []
        self.deleted: list[list[str]] = []

    def similar_canonical_questions(self, query_text, *, constraints, top_k):
        return SimilarResult(hits=self.hits[:top_k])

    def upsert_bank_question(self, bank_id, text, **metadata):
        self.upserted.append((bank_id, text, metadata))
        return True

    def delete_bank_questions(self, canonical_ids):
        self.deleted.append(list(canonical_ids))


def _user(db_session, role: str="teacher") -> User:
    user = User(
        username=f"management-{role}-{uuid.uuid4().hex[:10]}",
        password_hash="not-used",
        role=role,
    )
    db_session.add(user)
    db_session.commit()
    return user


def _stem(label: str) -> str:
    return f"{label}-{uuid.uuid4().hex[:10]}"


def _canonical(db_session, stem: str | None=None, **overrides) -> CanonicalQuestion:
    values = {
        "stem_markdown": stem or _stem("管理标准题"),
        "analysis_markdown": "标准解析",
        "answer": "标准答案",
        "knowledge_points": ["方程"],
        "tags": ["标准"],
        "difficulty": "medium",
        "question_type": "解答题",
        "chapter": "方程",
        "subject": "math",
        "grade": 9,
        "region": "北京",
        "textbook_version": "人教版",
        "origin": "teacher",
    }
    values.update(overrides)
    question = QuestionBankRepository(db_session).create(**values)
    db_session.commit()
    return question


def _reference(db_session, canonical_id: int, user_id: int, **overrides) -> Question:
    question = Question(
        user_id=user_id,
        canonical_id=canonical_id,
        private_image_path=f"/private/{uuid.uuid4().hex}.png",
        content_markdown="",
        answer="",
        **overrides,
    )
    db_session.add(question)
    db_session.commit()
    return question


def _service(db_session, vectors: RecordingVectorStore | None=None) -> QuestionBankService:
    return QuestionBankService(db_session, vector_store=vectors or RecordingVectorStore())


@pytest.mark.parametrize("role", ["student", "admin"])
def test_management_methods_reject_students_and_non_listed_admin_roles(db_session, role):
    actor = _user(db_session, role)
    canonical = _canonical(db_session)
    service = _service(db_session)
    calls = [
        lambda: service.search_canonical(actor.id),
        lambda: service.update_canonical(actor.id, canonical.id, CanonicalQuestionUpdate(answer="x")),
        lambda: service.delete_unreferenced(actor.id, canonical.id),
        lambda: service.read_canonical_asset(actor.id, 1),
    ]

    for call in calls:
        with pytest.raises(PermissionError):
            call()


def test_management_methods_reject_logged_out_or_unknown_actor(db_session):
    canonical = _canonical(db_session)
    service = _service(db_session)

    for actor_id in (0, 99999999):
        with pytest.raises(PermissionError):
            service.search_canonical(actor_id)
        with pytest.raises(PermissionError):
            service.update_canonical(
                actor_id, canonical.id, CanonicalQuestionUpdate(answer="未授权")
            )
        with pytest.raises(PermissionError):
            service.delete_unreferenced(actor_id, canonical.id)
        with pytest.raises(PermissionError):
            service.read_canonical_asset(actor_id, 1)


def test_listed_admin_is_authorized_independently_of_role(db_session, monkeypatch):
    actor = _user(db_session, "student")
    monkeypatch.setattr(get_settings(), "admin_usernames", [actor.username])
    canonical = _canonical(db_session, stem=_stem("独立管理员权限"))

    result = _service(db_session).search_canonical(actor.id, keyword=canonical.stem_markdown)

    assert [question.id for question in result] == [canonical.id]


@pytest.mark.parametrize(
    ("filter_name", "overrides"),
    [
        ("keyword", {}),
        ("subject", {"subject": "physics"}),
        ("grade", {"grade": 8}),
        ("question_type", {"question_type": "选择题"}),
        ("origin", {"origin": "student"}),
        ("textbook_version", {"textbook_version": "北师大版"}),
        ("reference_count", {}),
    ],
)
def test_each_search_filter_excludes_a_nearby_non_match(db_session, filter_name, overrides):
    teacher = _user(db_session)
    token = uuid.uuid4().hex[:10]
    target_values = {
        "stem_markdown": f"筛选目标 {token}",
        "subject": "math",
        "grade": 9,
        "question_type": "解答题",
        "origin": "teacher",
        "textbook_version": "人教版",
    }
    target = _canonical(db_session, **target_values)
    other_values = dict(target_values)
    other_values["stem_markdown"] = (
        f"其他题目 {token}" if filter_name != "keyword" else _stem("不含关键词")
    )
    other_values.update(overrides)
    other = _canonical(db_session, **other_values)
    if filter_name == "reference_count":
        student = _user(db_session, "student")
        _reference(db_session, target.id, student.id)
    filters = {
        "keyword": token,
        "subject": "math",
        "grade": 9,
        "question_type": "解答题",
        "origin": "teacher",
        "textbook_version": "人教版",
        "reference_count": 1,
    }

    query = {filter_name: filters[filter_name]}
    query.setdefault("keyword", token)
    result = _service(db_session).search_canonical(teacher.id, **query)

    assert [question.id for question in result] == [target.id]
    assert other.id not in [question.id for question in result]
    assert result[0].reference_count == (1 if filter_name == "reference_count" else 0)


def test_search_paginates_without_join_duplicates_and_returns_reference_counts(db_session):
    teacher = _user(db_session)
    student = _user(db_session, "student")
    token = uuid.uuid4().hex[:10]
    questions = [_canonical(db_session, stem=f"分页 {token} {index}") for index in range(3)]
    _reference(db_session, questions[0].id, student.id)
    _reference(db_session, questions[0].id, student.id)
    service = _service(db_session)

    pages = [
        service.search_canonical(teacher.id, keyword=token, limit=1, offset=offset)
        for offset in range(4)
    ]

    assert [[item.id for item in page] for page in pages] == [
        [questions[2].id],
        [questions[1].id],
        [questions[0].id],
        [],
    ]
    counts = {
        item.id: item.reference_count
        for item in service.search_canonical(teacher.id, keyword=token)
    }
    assert counts == {questions[2].id: 0, questions[1].id: 0, questions[0].id: 2}


def test_semantic_search_uses_vector_recall_then_applies_sql_filters(db_session):
    teacher = _user(db_session)
    token = uuid.uuid4().hex[:10]
    first = _canonical(db_session, stem=f"语义第一 {token}", subject="math")
    _canonical(db_session, stem=f"语义学科不符 {token}", subject="physics")
    second = _canonical(db_session, stem=f"语义第二 {token}", subject="math")
    vectors = RecordingVectorStore(
        [
            RagHit(question_id=-1, distance=0.1, source="bank", bank_id=str(second.id)),
            RagHit(question_id=-1, distance=0.2, source="bank", bank_id=str(first.id)),
        ]
    )

    result = _service(db_session, vectors).search_canonical(
        teacher.id, keyword=token, semantic=True, subject="math"
    )

    assert [question.id for question in result] == [second.id, first.id]
    assert all(isinstance(question, CanonicalQuestionOut) for question in result)


def test_update_recomputes_identity_indexes_and_preserves_personal_state(db_session):
    teacher = _user(db_session)
    student = _user(db_session, "student")
    canonical = _canonical(db_session, stem="修订前题干")
    personal = _reference(
        db_session,
        canonical.id,
        student.id,
        stem_override="学生个人题干",
        analysis_override="学生个人解析",
        answer_override="学生个人答案",
        user_note="学生私有备注",
        reps=7,
        interval_days=21,
    )
    private_image = personal.private_image_path
    vectors = RecordingVectorStore()
    service = _service(db_session, vectors)

    updated = service.update_canonical(
        teacher.id,
        canonical.id,
        CanonicalQuestionUpdate(
            stem_markdown="1.  修订后的   题干",
            analysis_markdown="教师修订解析",
            answer="教师修订答案",
            knowledge_points=["新知识点"],
            question_type="探究题",
            grade=10,
            textbook_version="苏科版",
        ),
    )

    expected_normalized = "修订后的 题干"
    assert updated.normalized_stem == expected_normalized
    assert updated.normalized_hash == hashlib.sha256(expected_normalized.encode()).hexdigest()
    assert updated.answer == "教师修订答案"
    assert updated.reference_count == 1
    assert vectors.upserted[-1][0] == str(canonical.id)
    assert vectors.upserted[-1][1] == "1.  修订后的   题干"
    assert vectors.upserted[-1][2]["grade"] == 10
    db_session.expire_all()
    stored_personal = db_session.get(Question, personal.id)
    assert stored_personal.stem_override == "学生个人题干"
    assert stored_personal.analysis_override == "学生个人解析"
    assert stored_personal.answer_override == "学生个人答案"
    assert stored_personal.user_note == "学生私有备注"
    assert stored_personal.private_image_path == private_image
    assert stored_personal.reps == 7
    assert stored_personal.interval_days == 21
    audit = db_session.execute(
        select(QuestionBankAudit).where(
            QuestionBankAudit.canonical_id == canonical.id,
            QuestionBankAudit.action == "update",
        )
    ).scalar_one()
    assert audit.actor == teacher.username
    assert audit.actor_id == teacher.id
    assert audit.affected_student_count == 0


def test_hash_collision_rejects_update_and_leaves_session_usable(db_session):
    teacher = _user(db_session)
    first = _canonical(db_session, stem="第一道题")
    second = _canonical(db_session, stem="第二道题")
    vectors = RecordingVectorStore()
    service = _service(db_session, vectors)

    with pytest.raises(ValueError, match="冲突|已存在"):
        service.update_canonical(
            teacher.id,
            first.id,
            CanonicalQuestionUpdate(stem_markdown="1. 第二道题"),
        )

    db_session.expire_all()
    assert db_session.get(CanonicalQuestion, first.id).stem_markdown == "第一道题"
    assert db_session.get(CanonicalQuestion, second.id).stem_markdown == "第二道题"
    assert vectors.upserted == []
    assert db_session.query(QuestionBankAudit).filter_by(canonical_id=first.id).count() == 0

    recovered = service.update_canonical(
        teacher.id, first.id, CanonicalQuestionUpdate(answer="冲突后的正常编辑")
    )
    assert recovered.answer == "冲突后的正常编辑"


def test_referenced_delete_returns_409_without_deleting_anything(db_session, tmp_path):
    teacher = _user(db_session)
    student = _user(db_session, "student")
    canonical = _canonical(db_session)
    _reference(db_session, canonical.id, student.id)
    asset_path = tmp_path / "referenced.png"
    asset_path.write_bytes(b"still-needed")
    db_session.add(
        QuestionAsset(
            canonical_id=canonical.id,
            kind="teacher_original",
            visibility="teacher_only",
            storage_path=str(asset_path),
        )
    )
    db_session.commit()
    vectors = RecordingVectorStore()

    with pytest.raises(ValueError) as caught:
        _service(db_session, vectors).delete_unreferenced(teacher.id, canonical.id)

    assert caught.value.status_code == 409
    assert db_session.get(CanonicalQuestion, canonical.id) is not None
    assert asset_path.read_bytes() == b"still-needed"
    assert vectors.deleted == []
    assert db_session.query(QuestionBankAudit).filter_by(canonical_id=canonical.id).count() == 0


def test_listed_admin_also_cannot_use_task8_delete_for_referenced_question(db_session, monkeypatch):
    admin = _user(db_session, "student")
    monkeypatch.setattr(get_settings(), "admin_usernames", [admin.username])
    canonical = _canonical(db_session)
    _reference(db_session, canonical.id, _user(db_session, "student").id)

    with pytest.raises(ValueError) as caught:
        _service(db_session).delete_unreferenced(admin.id, canonical.id)

    assert caught.value.status_code == 409


def test_unreferenced_delete_cleans_sql_assets_files_vectors_and_writes_audit(db_session):
    teacher = _user(db_session)
    canonical = _canonical(db_session)
    settings = get_settings()
    shared = settings.data_dir / "question-bank" / f"{uuid.uuid4().hex}.png"
    original = settings.doc_upload_path / f"{uuid.uuid4().hex}.png"
    for path, content in ((shared, b"shared"), (original, b"original")):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    db_session.add_all(
        [
            QuestionAsset(
                canonical_id=canonical.id,
                kind="ai_redraw",
                visibility="shared",
                storage_path=str(shared),
            ),
            QuestionAsset(
                canonical_id=canonical.id,
                kind="teacher_original",
                visibility="teacher_only",
                storage_path=str(original),
            ),
        ]
    )
    db_session.commit()
    canonical_id = canonical.id
    vectors = RecordingVectorStore()

    assert _service(db_session, vectors).delete_unreferenced(teacher.id, canonical_id) is None

    db_session.expire_all()
    assert db_session.get(CanonicalQuestion, canonical_id) is None
    assert db_session.query(QuestionAsset).filter_by(canonical_id=canonical_id).count() == 0
    assert not shared.exists()
    assert not original.exists()
    assert vectors.deleted == [[str(canonical_id)]]
    audit = db_session.execute(
        select(QuestionBankAudit).where(
            QuestionBankAudit.canonical_id == canonical_id,
            QuestionBankAudit.action == "delete_unreferenced",
        )
    ).scalar_one()
    assert audit.actor_id == teacher.id
    assert audit.affected_student_count == 0


def test_forged_asset_id_never_returns_teacher_original_to_student(db_session, tmp_path):
    teacher = _user(db_session)
    student = _user(db_session, "student")
    canonical = _canonical(db_session)
    original = tmp_path / "teacher-original.png"
    original.write_bytes(b"teacher-only-original")
    asset = QuestionAsset(
        canonical_id=canonical.id,
        kind="teacher_original",
        visibility="teacher_only",
        storage_path=str(original),
    )
    db_session.add(asset)
    db_session.commit()
    service = _service(db_session)

    assert service.read_canonical_asset(teacher.id, asset.id) == b"teacher-only-original"
    with pytest.raises(PermissionError):
        service.read_canonical_asset(student.id, asset.id)
    with pytest.raises(PermissionError):
        service.read_canonical_asset(99999999, asset.id)


def test_sqlite_reference_commit_during_zero_count_delete_returns_409(
    tmp_path, monkeypatch
):
    from backend.repositories.questions import QuestionRepository
    from backend.services.question_service import QuestionService

    engine = create_engine(
        f"sqlite:///{tmp_path / 'delete-reference-race.db'}",
        connect_args={"check_same_thread": False, "timeout": 5},
    )

    @event.listens_for(engine, "connect")
    def _foreign_keys(dbapi_connection, _record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as session:
        teacher = User(username="race-teacher", password_hash="x", role="teacher")
        student = User(username="race-student", password_hash="x", role="student")
        session.add_all([teacher, student])
        session.flush()
        canonical = QuestionBankRepository(session).create(
            stem_markdown="并发引用与删除",
            analysis_markdown="完整解析",
            answer="完整答案",
            origin="teacher",
        )
        session.commit()
        teacher_id, student_id, canonical_id = teacher.id, student.id, canonical.id

    reference_flushed = threading.Event()
    deletion_counted = threading.Event()
    original_create_reference = QuestionRepository.create_reference
    original_count_references = QuestionBankRepository.count_references

    def paused_reference(self, *args, **kwargs):
        question = original_create_reference(self, *args, **kwargs)
        reference_flushed.set()
        deletion_counted.wait(timeout=0.5)
        return question

    def observed_count(self, canonical_id):
        count = original_count_references(self, canonical_id)
        if threading.current_thread() is threading.main_thread():
            deletion_counted.set()
        return count

    monkeypatch.setattr(QuestionRepository, "create_reference", paused_reference)
    monkeypatch.setattr(QuestionBankRepository, "count_references", observed_count)
    reference_errors: list[BaseException] = []

    def create_reference():
        try:
            QuestionService(session_factory=factory).reference_canonical(
                student_id, canonical_id, private_image_path="race-private.png"
            )
        except BaseException as exc:
            reference_errors.append(exc)

    thread = threading.Thread(target=create_reference)
    thread.start()
    assert reference_flushed.wait(timeout=2)
    delete_error = None
    with factory() as session:
        try:
            QuestionBankService(
                session, vector_store=RecordingVectorStore()
            ).delete_unreferenced(teacher_id, canonical_id)
        except ValueError as exc:
            delete_error = exc
    thread.join(timeout=5)

    assert not thread.is_alive()
    assert reference_errors == []
    assert delete_error is not None
    assert delete_error.status_code == 409
    with factory() as session:
        assert session.get(CanonicalQuestion, canonical_id) is not None
        reference = session.query(Question).filter_by(user_id=student_id).one()
        assert reference.canonical_id == canonical_id


def test_delete_retains_teacher_original_shared_with_surviving_asset(db_session):
    teacher = _user(db_session)
    first = _canonical(db_session, stem=_stem("共享原图第一题"))
    second = _canonical(db_session, stem=_stem("共享原图第二题"))
    original = get_settings().doc_upload_path / f"{uuid.uuid4().hex}.png"
    original.parent.mkdir(parents=True, exist_ok=True)
    original.write_bytes(b"shared-page-original")
    db_session.add_all(
        [
            QuestionAsset(
                canonical_id=canonical.id,
                kind="teacher_original",
                visibility="teacher_only",
                storage_path=str(original),
            )
            for canonical in (first, second)
        ]
    )
    db_session.commit()
    service = _service(db_session)

    service.delete_unreferenced(teacher.id, first.id)

    assert original.read_bytes() == b"shared-page-original"
    surviving_asset = db_session.query(QuestionAsset).filter_by(canonical_id=second.id).one()
    assert service.read_canonical_asset(teacher.id, surviving_asset.id) == b"shared-page-original"

    service.delete_unreferenced(teacher.id, second.id)
    assert not original.exists()


def test_update_vector_failure_commits_sql_and_records_retryable_job(db_session):
    class FailingVectors(RecordingVectorStore):
        def upsert_bank_question(self, bank_id, text, **metadata):
            return False

    teacher = _user(db_session)
    canonical = _canonical(db_session)
    service = _service(db_session, FailingVectors())

    updated = service.update_canonical(
        teacher.id, canonical.id, CanonicalQuestionUpdate(answer="SQL 已提交的新答案")
    )

    assert updated.answer == "SQL 已提交的新答案"
    db_session.expire_all()
    assert db_session.get(CanonicalQuestion, canonical.id).answer == "SQL 已提交的新答案"
    jobs = db_session.query(Job).filter_by(
        type="vector_reconciliation", status="pending", user_id=teacher.id
    ).all()
    job = next(job for job in jobs if job.payload.get("canonical_id") == canonical.id)
    assert job.user_id == teacher.id
    assert job.payload == {"operation": "upsert", "canonical_id": canonical.id}
    assert job.error
    assert service.has_pending_vector_sync(teacher.id, canonical.id) is True


def test_delete_vector_failure_keeps_sql_deleted_and_records_retryable_job(db_session):
    class FailingVectors(RecordingVectorStore):
        def delete_bank_questions(self, canonical_ids):
            return False

    teacher = _user(db_session)
    canonical = _canonical(db_session)
    canonical_id = canonical.id
    service = _service(db_session, FailingVectors())

    assert service.delete_unreferenced(teacher.id, canonical_id) is None

    db_session.expire_all()
    assert db_session.get(CanonicalQuestion, canonical_id) is None
    jobs = db_session.query(Job).filter_by(
        type="vector_reconciliation", status="pending", user_id=teacher.id
    ).all()
    job = next(job for job in jobs if job.payload.get("canonical_id") == canonical_id)
    assert job.user_id == teacher.id
    assert job.payload == {"operation": "delete", "canonical_id": canonical_id}
    assert job.error
    assert service.has_pending_vector_sync(
        teacher.id, canonical_id, operation="delete"
    ) is True


def test_bank_vector_delete_reports_failure(monkeypatch):
    from backend.services.rag import QuestionVectorStore

    class FailingCollection:
        def delete(self, ids):
            raise RuntimeError("controlled delete failure")

    store = QuestionVectorStore()
    monkeypatch.setattr(store, "_ensure_bank_collection", lambda: FailingCollection())

    assert store.delete_bank_questions(["41"]) is False


def test_referenced_delete_409_releases_sqlite_write_lock_for_injected_session(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'delete-conflict-lock.db'}",
        connect_args={"check_same_thread": False, "timeout": 0.1},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as session:
        teacher = User(username="lock-teacher", password_hash="x", role="teacher")
        student = User(username="lock-student", password_hash="x", role="student")
        session.add_all([teacher, student])
        session.flush()
        canonical = QuestionBankRepository(session).create(
            stem_markdown="409 后释放写锁", origin="teacher"
        )
        session.flush()
        session.add(
            Question(
                user_id=student.id,
                canonical_id=canonical.id,
                private_image_path="lock-private.png",
                content_markdown="",
                answer="",
            )
        )
        session.commit()
        teacher_id, canonical_id = teacher.id, canonical.id

    with factory() as injected:
        with pytest.raises(ValueError) as caught:
            QuestionBankService(
                injected, vector_store=RecordingVectorStore()
            ).delete_unreferenced(teacher_id, canonical_id)
        assert caught.value.status_code == 409

        with factory() as writer:
            writer.add(User(username="post-409-writer", password_hash="x", role="student"))
            writer.commit()

    with factory() as session:
        assert session.query(User).filter_by(username="post-409-writer").count() == 1


def test_successful_update_settles_only_that_questions_pending_upsert_jobs(db_session):
    class ToggleVectors(RecordingVectorStore):
        failing = True

        def upsert_bank_question(self, bank_id, text, **metadata):
            super().upsert_bank_question(bank_id, text, **metadata)
            return not self.failing

    teacher = _user(db_session)
    target = _canonical(db_session, stem=_stem("失败后恢复同步"))
    other = _canonical(db_session, stem=_stem("仍待同步的其他题"))
    vectors = ToggleVectors()
    service = _service(db_session, vectors)
    for canonical in (target, other):
        service.update_canonical(
            teacher.id, canonical.id, CanonicalQuestionUpdate(answer="失败答案")
        )
    assert service.has_pending_vector_sync(teacher.id, target.id) is True
    assert service.has_pending_vector_sync(teacher.id, other.id) is True

    vectors.failing = False
    service.update_canonical(
        teacher.id, target.id, CanonicalQuestionUpdate(answer="成功答案")
    )

    assert service.has_pending_vector_sync(teacher.id, target.id) is False
    assert service.has_pending_vector_sync(teacher.id, other.id) is True
    jobs = db_session.query(Job).filter_by(type="vector_reconciliation").all()
    target_jobs = [
        job
        for job in jobs
        if job.user_id == teacher.id
        and job.payload.get("canonical_id") == target.id
        and job.payload.get("operation") == "upsert"
    ]
    other_jobs = [
        job
        for job in jobs
        if job.user_id == teacher.id
        and job.payload.get("canonical_id") == other.id
        and job.payload.get("operation") == "upsert"
    ]
    assert target_jobs and all(job.status == "success" for job in target_jobs), [
        (job.payload, job.status) for job in target_jobs
    ]
    assert other_jobs and all(job.status == "pending" for job in other_jobs)


def test_successful_delete_ignores_pending_upsert_without_settling_it(db_session):
    class UpsertFailureVectors(RecordingVectorStore):
        def upsert_bank_question(self, bank_id, text, **metadata):
            return False

        def delete_bank_questions(self, canonical_ids):
            super().delete_bank_questions(canonical_ids)
            return True

    teacher = _user(db_session)
    canonical = _canonical(db_session)
    canonical_id = canonical.id
    service = _service(db_session, UpsertFailureVectors())
    service.update_canonical(
        teacher.id, canonical_id, CanonicalQuestionUpdate(answer="更新失败但 SQL 已保存")
    )
    assert service.has_pending_vector_sync(
        teacher.id, canonical_id, operation="upsert"
    ) is True

    service.delete_unreferenced(teacher.id, canonical_id)

    assert service.has_pending_vector_sync(
        teacher.id, canonical_id, operation="delete"
    ) is False
    assert service.has_pending_vector_sync(
        teacher.id, canonical_id, operation="upsert"
    ) is True
    jobs = db_session.query(Job).filter_by(
        type="vector_reconciliation", user_id=teacher.id
    ).all()
    upsert_job = next(
        job
        for job in jobs
        if job.payload.get("canonical_id") == canonical_id
        and job.payload.get("operation") == "upsert"
    )
    assert upsert_job.status == "pending"
