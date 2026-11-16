from __future__ import annotations

import threading
import uuid
from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from backend.models.orm import (
    CanonicalQuestion,
    Job,
    Question,
    QuestionAsset,
    QuestionTypeDefinition,
    User,
)
from backend.models.schemas import QuestionAnalysis
from backend.repositories.question_bank import QuestionBankRepository
from backend.services.ai.base import AnalysisContext, build_system_prompt
from backend.services.document_service import DocumentService
from backend.services.question_bank import QuestionBankService
from backend.services.question_identity import stem_hash


class FakeVectorStore:
    def similar_canonical_questions(self, query_text, *, constraints, top_k):
        from backend.services.rag import SimilarResult

        return SimilarResult(hits=[])

    def upsert_bank_question(self, bank_id, text, **metadata):
        return True

    def upsert_question(self, *args, **kwargs):
        return True

    def delete_questions(self, question_ids):
        return True


@pytest.fixture(autouse=True)
def fake_bank_vectors(monkeypatch):
    monkeypatch.setattr(
        "backend.services.question_bank.QuestionVectorStore", lambda: FakeVectorStore()
    )
    monkeypatch.setattr(
        "backend.services.question_mixins.QuestionVectorStore", lambda settings: FakeVectorStore()
    )


def _analysis(**overrides) -> QuestionAnalysis:
    analysis = QuestionAnalysis(
        knowledge_points=["方程"],
        analysis="移项并计算，最后检验答案。",
        answer="x=2",
        difficulty="medium",
        tags=["方程"],
        question_type="AI生成题型",
        chapter="一元一次方程",
        is_complete=True,
    )
    return analysis.model_copy(update=overrides)


def _user(db_session, role: str) -> User:
    user = User(
        username=f"publish-{role}-{uuid.uuid4().hex[:10]}",
        password_hash="not-used",
        role=role,
    )
    db_session.add(user)
    db_session.commit()
    return user


def _document_job(
    db_session,
    user: User,
    tmp_path: Path,
    stem: str,
    *,
    analysis: QuestionAnalysis | None = None,
    match_status: str = "new",
    canonical_id: int | None = None,
    match_candidates: list[dict] | None = None,
    has_page_image: bool = True,
    suffix: str = ".png",
) -> tuple[str, Path]:
    source = tmp_path / f"{uuid.uuid4().hex}{suffix}"
    source.write_bytes(b"private-document-original")
    job_id = uuid.uuid4().hex
    segment = {
        "number": "1",
        "text": stem,
        "page_start": 1,
        "page_end": 1,
        "needs_review": False,
        "continued": False,
        "source": "test",
        "figure_paths": [],
        "figure_specs": [],
        "analysis": (analysis or _analysis()).model_dump(mode="json"),
        "error": None,
        "match_status": match_status,
        "canonical_id": canonical_id,
        "match_candidates": match_candidates or [],
    }
    db_session.add(
        Job(
            id=job_id,
            user_id=user.id,
            type="document_import",
            status="success",
            payload={
                "filename": f"paper{suffix}",
                "doc_path": str(source),
                "subject": "math",
                "grade": 9,
                "region": "北京",
                "textbook_version": "人教版",
            },
            result={
                "confirmed": False,
                "page_images": [str(source)] if has_page_image else [],
                "segments": [segment],
                "total": 1,
                "done": 1,
            },
        )
    )
    db_session.commit()
    return job_id, source


def _canonical(db_session, stem: str, *, origin: str = "student") -> CanonicalQuestion:
    canonical = QuestionBankRepository(db_session).create(
        stem_markdown=stem,
        analysis_markdown="学生标准解析",
        answer="学生标准答案",
        question_type="学生题型",
        origin=origin,
    )
    db_session.commit()
    return canonical


def test_question_types_are_teacher_managed_idempotent_and_subject_scoped(
    db_session, student_user
):
    teacher = _user(db_session, "teacher")
    service = QuestionBankService(db_session, vector_store=FakeVectorStore())

    created = service.create_question_type(teacher.id, "math", " 函数探究 ")
    repeated = service.create_question_type(teacher.id, "math", "函数探究")
    service.create_question_type(teacher.id, "physics", "物理实验题")

    assert isinstance(created, QuestionTypeDefinition)
    assert repeated.id == created.id
    assert service.list_question_types("math") == ["函数探究"]
    assert service.list_question_types("physics") == ["物理实验题"]
    with pytest.raises(PermissionError):
        service.create_question_type(student_user.id, "math", "学生自定义")


@pytest.mark.parametrize("role", ["student", "admin"])
def test_only_teacher_role_can_publish_documents(
    db_session, tmp_path, monkeypatch, role
):
    monkeypatch.setattr(
        "backend.services.question_mixins.QuestionVectorStore", lambda settings: FakeVectorStore()
    )
    actor = _user(db_session, role)
    stem = f"禁止{role}发布-{uuid.uuid4().hex}"
    job_id, _ = _document_job(db_session, actor, tmp_path, stem)

    with pytest.raises(PermissionError):
        DocumentService().confirm_import(job_id, actor.id, publish_to_bank=True)

    job = DocumentService().get_job(job_id, actor.id)
    assert job is not None
    assert job["status"] == "success"
    assert job["result"]["confirmed"] is False
    assert db_session.query(func.count(CanonicalQuestion.id)).filter_by(
        normalized_hash=stem_hash(stem)
    ).scalar() == 0


def test_unspecified_type_prompt_prioritizes_same_subject_teacher_types(db_session):
    teacher = _user(db_session, "teacher")
    service = QuestionBankService(db_session, vector_store=FakeVectorStore())
    service.create_question_type(teacher.id, "math", "教师函数探究题")
    service.create_question_type(teacher.id, "physics", "教师物理实验题")

    prompt = build_system_prompt(
        AnalysisContext(subject="math"),
        question_types=service.list_question_types("math"),
    )

    assert "教师函数探究题" in prompt
    assert "优先" in prompt
    assert "教师物理实验题" not in prompt


def test_teacher_explicit_type_overrides_ai_without_creating_personal_question(
    db_session, tmp_path
):
    teacher = _user(db_session, "teacher")
    stem = f"教师题型覆盖-{uuid.uuid4().hex}"
    job_id, _ = _document_job(db_session, teacher, tmp_path, stem)

    outcome = DocumentService().confirm_import(
        job_id,
        teacher.id,
        publish_to_bank=True,
        question_type_overrides={0: "教师指定压轴题"},
    )

    canonical = db_session.execute(
        select(CanonicalQuestion).where(CanonicalQuestion.normalized_hash == stem_hash(stem))
    ).scalar_one()
    assert outcome["imported"] == 1
    assert canonical.question_type == "教师指定压轴题"
    assert canonical.origin == "teacher"
    assert canonical.validation_state == "teacher_published"
    assert db_session.query(Question).filter_by(user_id=teacher.id).count() == 0


def test_teacher_same_hash_upgrade_preserves_student_private_original(
    db_session, question_service, student_user, tmp_path
):
    stem = f"同哈希升级-{uuid.uuid4().hex}"
    canonical = _canonical(db_session, stem)
    student_image = tmp_path / "student-evidence.png"
    student_image.write_bytes(b"student-private-evidence")
    personal = question_service.reference_canonical(
        student_user.id,
        canonical.id,
        private_image_path=str(student_image),
    )
    teacher = _user(db_session, "teacher")
    job_id, teacher_image = _document_job(
        db_session,
        teacher,
        tmp_path,
        f"1. {stem}",
        analysis=_analysis(answer="教师修订答案", analysis="教师修订后的完整解析。"),
        match_status="exact",
        canonical_id=canonical.id,
    )

    DocumentService().confirm_import(
        job_id,
        teacher.id,
        publish_to_bank=True,
        question_type_overrides={0: "教师修订题型"},
    )

    db_session.expire_all()
    upgraded = db_session.get(CanonicalQuestion, canonical.id)
    student_view = question_service.get_question(personal.id, student_user.id)
    assert upgraded is not None
    assert upgraded.answer == "教师修订答案"
    assert upgraded.question_type == "教师修订题型"
    assert upgraded.origin == "teacher"
    assert student_view is not None
    assert student_view.image_path == str(student_image)
    assert student_view.answer == "教师修订答案"
    assert str(teacher_image) not in student_view.figure_paths
    asset = db_session.query(QuestionAsset).filter_by(canonical_id=canonical.id).one()
    assert asset.kind == "teacher_original"
    assert asset.visibility == "teacher_only"
    assert asset.storage_path == str(teacher_image)
    assert db_session.get(Question, personal.id).private_image_path == str(student_image)


def test_similar_document_without_segment_choice_returns_409(
    client, db_session, student_user, tmp_path
):
    candidate = _canonical(db_session, f"相似候选-{uuid.uuid4().hex}", origin="teacher")
    stem = f"待确认相似题-{uuid.uuid4().hex}"
    job_id, _ = _document_job(
        db_session,
        student_user,
        tmp_path,
        stem,
        match_status="similar",
        match_candidates=[
            {"score": 0.8, "question": {"id": candidate.id, "stem_markdown": candidate.stem_markdown}}
        ],
    )

    login = client.post("/api/auth/login", json={"username": "demo", "password": "demo123"})
    assert login.status_code == 200, login.text
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
    response = client.post(
        f"/api/documents/{job_id}/confirm", json={"match_choices": {}}, headers=headers
    )

    assert response.status_code == 409, response.text
    assert "选择" in response.json()["detail"]
    job = DocumentService().get_job(job_id, student_user.id)
    assert job is not None and job["result"]["confirmed"] is False
    assert db_session.query(Question).filter_by(canonical_id=candidate.id).count() == 0


def test_student_similar_choice_uses_segment_index_and_keeps_private_original(
    db_session, question_service, student_user, tmp_path
):
    candidate = _canonical(db_session, f"确认引用候选-{uuid.uuid4().hex}", origin="teacher")
    teacher_image = tmp_path / "teacher-only.png"
    teacher_image.write_bytes(b"teacher-only")
    db_session.add(
        QuestionAsset(
            canonical_id=candidate.id,
            kind="teacher_original",
            visibility="teacher_only",
            storage_path=str(teacher_image),
        )
    )
    db_session.commit()
    job_id, student_image = _document_job(
        db_session,
        student_user,
        tmp_path,
        f"学生相似题-{uuid.uuid4().hex}",
        match_status="similar",
        match_candidates=[
            {"score": 0.8, "question": {"id": candidate.id, "stem_markdown": candidate.stem_markdown}}
        ],
    )

    outcome = DocumentService().confirm_import(
        job_id, student_user.id, match_choices={0: candidate.id}
    )

    assert outcome["imported"] == 1
    personal = db_session.query(Question).filter_by(user_id=student_user.id).order_by(
        Question.id.desc()
    ).first()
    assert personal.canonical_id == candidate.id
    assert personal.match_method == "confirmed_similar"
    assert personal.private_image_path == str(student_image)
    student_view = question_service.get_question(personal.id, student_user.id)
    assert student_view is not None
    assert student_view.image_path == str(student_image)
    assert str(teacher_image) not in student_view.figure_paths


@pytest.mark.parametrize(
    ("with_geometry", "tex_available", "expected"),
    [
        (False, True, {"analyze_text": 1}),
        (True, True, {"analyze_text": 1, "redraw_geometry": 1}),
        (True, False, {"analyze_text": 1}),
    ],
)
def test_document_exact_reuse_records_text_and_actual_geometry_savings(
    db_session, student_user, tmp_path, monkeypatch, with_geometry, tex_available, expected
):
    from backend.services.ai import telemetry

    monkeypatch.setattr(telemetry, "_telemetry_path", lambda: tmp_path / "telemetry.jsonl")
    monkeypatch.setattr(
        "backend.services.geometry_figures.GeometryFigureService._tex_available",
        lambda self: tex_available,
    )
    canonical = _canonical(
        db_session, f"文档精确复用-{uuid.uuid4().hex}", origin="teacher"
    )
    job_id, _ = _document_job(
        db_session,
        student_user,
        tmp_path,
        canonical.stem_markdown,
        match_status="exact",
        canonical_id=canonical.id,
    )
    if with_geometry:
        job = db_session.get(Job, job_id)
        result = dict(job.result)
        segment = dict(result["segments"][0])
        segment["figure_specs"] = [{"kind": "geometry", "validation_status": "fallback"}]
        result["segments"] = [segment]
        job.result = result
        db_session.commit()

    outcome = DocumentService().confirm_import(job_id, student_user.id)

    assert outcome["imported"] == 1
    assert telemetry.summarize()["question_bank"]["skipped_ai_calls"] == expected


def test_explicit_none_similar_choice_creates_new_personal_question(
    db_session, student_user, tmp_path
):
    candidate = _canonical(db_session, f"不作为新题复用-{uuid.uuid4().hex}", origin="teacher")
    stem = f"明确选择新题-{uuid.uuid4().hex}"
    job_id, student_image = _document_job(
        db_session,
        student_user,
        tmp_path,
        stem,
        match_status="similar",
        match_candidates=[
            {"score": 0.8, "question": {"id": candidate.id, "stem_markdown": candidate.stem_markdown}}
        ],
    )

    outcome = DocumentService().confirm_import(
        job_id, student_user.id, match_choices={0: None}
    )

    assert outcome["imported"] == 1
    personal = db_session.query(Question).filter_by(user_id=student_user.id).order_by(
        Question.id.desc()
    ).first()
    assert personal.canonical_id is not None
    assert personal.canonical_id != candidate.id
    assert personal.match_method == "new"
    assert personal.image_path == str(student_image)
    assert personal.stem_snapshot == stem


def test_teacher_similar_choice_upgrades_selected_identity_and_hash(db_session):
    teacher = _user(db_session, "teacher")
    selected = _canonical(db_session, f"教师选择的相似题-{uuid.uuid4().hex}", origin="student")
    revised_stem = f"教师修订后的题干-{uuid.uuid4().hex}"

    upgraded = QuestionBankService(db_session, vector_store=FakeVectorStore()).create_or_upgrade(
        revised_stem,
        _analysis(answer="教师答案"),
        actor=teacher,
        context=AnalysisContext(subject="math"),
        canonical_id=selected.id,
    )

    assert upgraded.id == selected.id
    assert upgraded.normalized_hash == stem_hash(revised_stem)
    assert upgraded.answer == "教师答案"
    assert upgraded.origin == "teacher"


def test_split_segments_cannot_keep_the_original_exact_match(
    db_session, student_user, tmp_path
):
    canonical = _canonical(db_session, f"拆分前精确题-{uuid.uuid4().hex}")
    job_id, _ = _document_job(
        db_session,
        student_user,
        tmp_path,
        "拆分前题干需要变化",
        match_status="exact",
        canonical_id=canonical.id,
    )

    split = DocumentService().split_segment(job_id, student_user.id, 0, 4)

    assert len(split) == 2
    assert all(entry["match_status"] == "new" for entry in split)
    assert all(entry["canonical_id"] is None for entry in split)
    assert all(entry["match_candidates"] == [] for entry in split)


def test_repeated_teacher_publish_is_idempotent_for_question_and_assets(
    db_session, tmp_path
):
    teacher = _user(db_session, "teacher")
    stem = f"重复发布-{uuid.uuid4().hex}"
    job_id, _ = _document_job(db_session, teacher, tmp_path, stem)
    service = DocumentService()

    first = service.confirm_import(job_id, teacher.id, publish_to_bank=True)
    second = service.confirm_import(job_id, teacher.id, publish_to_bank=True)

    canonical = db_session.execute(
        select(CanonicalQuestion).where(CanonicalQuestion.normalized_hash == stem_hash(stem))
    ).scalar_one()
    assert first == {"imported": 1, "skipped": [], "already_confirmed": False}
    assert second == {"imported": 1, "skipped": [], "already_confirmed": True}
    assert db_session.query(QuestionAsset).filter_by(canonical_id=canonical.id).count() == 1
    assert db_session.query(Question).filter_by(user_id=teacher.id).count() == 0


@pytest.mark.parametrize("match_status", ["exact", "similar"])
def test_docx_match_preserves_persistent_private_document_and_cleans_it_up(
    db_session, question_service, student_user, tmp_path, match_status
):
    candidate = _canonical(
        db_session, f"DOCX 标准题-{match_status}-{uuid.uuid4().hex}", origin="teacher"
    )
    job_id, source = _document_job(
        db_session,
        student_user,
        tmp_path,
        f"DOCX 学生题-{match_status}-{uuid.uuid4().hex}",
        match_status=match_status,
        canonical_id=candidate.id if match_status == "exact" else None,
        match_candidates=(
            [{"score": 0.8, "question": {"id": candidate.id}}]
            if match_status == "similar"
            else []
        ),
        has_page_image=False,
        suffix=".docx",
    )

    outcome = DocumentService().confirm_import(
        job_id,
        student_user.id,
        match_choices={0: candidate.id} if match_status == "similar" else {},
    )

    assert outcome == {"imported": 1, "skipped": [], "already_confirmed": False}
    personal = db_session.query(Question).filter_by(user_id=student_user.id).order_by(
        Question.id.desc()
    ).first()
    evidence = Path(personal.private_image_path)
    assert personal.canonical_id == candidate.id
    assert evidence.suffix == ".docx"
    assert evidence != source
    assert evidence.read_bytes() == b"private-document-original"
    assert evidence.is_relative_to(
        question_service.settings.data_dir / "images" / f"u{student_user.id}"
    )
    assert source.exists()

    assert question_service.delete_questions([personal.id], student_user.id) == 1
    assert not evidence.exists()


def test_concurrent_question_type_creation_returns_one_existing_definition(
    tmp_path, monkeypatch
):
    from backend.models.orm import Base

    engine = create_engine(
        f"sqlite:///{tmp_path / 'question-type-race.db'}",
        connect_args={"check_same_thread": False, "timeout": 30},
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    with session_factory() as session:
        session.add(User(id=1, username="teacher", password_hash="x", role="teacher"))
        session.commit()

    gate = threading.Barrier(2)
    lock = threading.Lock()
    initial_checks = 0
    original_execute = Session.execute

    def gated_execute(self, statement, *args, **kwargs):
        nonlocal initial_checks
        if "question_type_definitions" in str(statement):
            with lock:
                initial_checks += 1
                should_wait = initial_checks <= 2
            if should_wait:
                gate.wait(timeout=5)
        return original_execute(self, statement, *args, **kwargs)

    monkeypatch.setattr(Session, "execute", gated_execute)
    service = QuestionBankService(
        session_factory=session_factory, vector_store=FakeVectorStore()
    )
    outcomes: list[int] = []
    errors: list[BaseException] = []

    def create():
        try:
            definition = service.create_question_type(1, "math", "并发题型")
            outcomes.append(definition.id)
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=create) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
        assert not thread.is_alive()

    assert errors == []
    assert len(outcomes) == 2
    assert outcomes[0] == outcomes[1]
    with session_factory() as session:
        assert session.query(QuestionTypeDefinition).filter_by(
            subject="math", name="并发题型"
        ).count() == 1


def test_shared_docx_evidence_survives_until_last_question_is_deleted(
    db_session, question_service, student_user, tmp_path
):
    exact = _canonical(db_session, f"共享 DOCX 精确题-{uuid.uuid4().hex}", origin="teacher")
    similar = _canonical(db_session, f"共享 DOCX 相似题-{uuid.uuid4().hex}", origin="teacher")
    source = tmp_path / "shared-evidence.docx"
    source.write_bytes(b"shared-private-docx")

    def segment(number, text, status, canonical_id, candidates=None):
        return {
            "number": number,
            "text": text,
            "page_start": 1,
            "page_end": 1,
            "needs_review": False,
            "continued": False,
            "source": "test",
            "figure_paths": [],
            "figure_specs": [],
            "analysis": _analysis().model_dump(mode="json"),
            "error": None,
            "match_status": status,
            "canonical_id": canonical_id,
            "match_candidates": candidates or [],
        }

    job_id = uuid.uuid4().hex
    db_session.add(
        Job(
            id=job_id,
            user_id=student_user.id,
            type="document_import",
            status="success",
            payload={
                "filename": "shared.docx",
                "doc_path": str(source),
                "subject": "math",
                "grade": 9,
            },
            result={
                "confirmed": False,
                "page_images": [],
                "segments": [
                    segment("1", "共享 DOCX 精确题", "exact", exact.id),
                    segment(
                        "2",
                        "共享 DOCX 相似题",
                        "similar",
                        None,
                        [{"score": 0.8, "question": {"id": similar.id}}],
                    ),
                ],
                "total": 2,
                "done": 2,
            },
        )
    )
    db_session.commit()

    outcome = DocumentService().confirm_import(
        job_id, student_user.id, match_choices={1: similar.id}
    )

    assert outcome == {"imported": 2, "skipped": [], "already_confirmed": False}
    personal = db_session.query(Question).filter(
        Question.user_id == student_user.id,
        Question.canonical_id.in_([exact.id, similar.id]),
    ).order_by(Question.id).all()
    assert len(personal) == 2
    evidence = Path(personal[0].private_image_path)
    assert personal[1].private_image_path == str(evidence)
    assert evidence.read_bytes() == b"shared-private-docx"
    first_id, surviving_id = personal[0].id, personal[1].id
    personal[1].canonical_id = None
    personal[1].detached_reason = "admin_delete"
    db_session.commit()

    assert question_service.delete_questions([first_id], student_user.id) == 1

    assert evidence.exists()
    surviving = question_service.get_question(surviving_id, student_user.id)
    assert surviving is not None
    assert surviving.canonical_id is None
    assert surviving.image_path == str(evidence)
    assert Path(surviving.image_path).read_bytes() == b"shared-private-docx"

    assert question_service.delete_questions([surviving_id], student_user.id) == 1
    assert not evidence.exists()
