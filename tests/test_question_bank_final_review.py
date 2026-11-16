from __future__ import annotations

import hashlib
import threading
import time
import uuid
from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from backend.config import Settings
from backend.models.orm import (
    Base,
    CanonicalQuestion,
    Job,
    Question,
    QuestionAsset,
    User,
)
from backend.models.schemas import QuestionAnalysis
from backend.repositories.question_bank import QuestionBankRepository
from backend.services.ai.base import AnalysisContext
from backend.services.ai.mock import MockProvider
from backend.services.document import DocPage
from backend.services.document_service import DocumentService
from backend.services.entry_types import PhotoImage
from backend.services.geometry_figures import FigureOutcome, GeometryFigureService
from backend.services.photo_entry import PhotoEntryService
from backend.services.question_bank import QuestionBankService
from backend.services.question_identity import normalize_stem
from backend.services.question_service import QuestionService
from backend.services.rag import RagHit, SimilarResult


class ControlledVectors:
    def __init__(self, hits: list[RagHit] | None = None):
        self.hits = hits or []
        self.upsert_result = True
        self.delete_result = True
        self.upsert_error: Exception | None = None
        self.delete_error: Exception | None = None
        self.search_calls = 0
        self.upserted: list[str] = []
        self.deleted: list[list[str]] = []

    def similar_canonical_questions(self, query_text, *, constraints, top_k):
        self.search_calls += 1
        return SimilarResult(hits=self.hits[:top_k])

    def upsert_bank_question(self, bank_id, text, **metadata):
        if self.upsert_error is not None:
            raise self.upsert_error
        self.upserted.append(bank_id)
        return self.upsert_result

    def delete_bank_questions(self, canonical_ids):
        if self.delete_error is not None:
            raise self.delete_error
        self.deleted.append(list(canonical_ids))
        return self.delete_result

    def upsert_question(self, *args, **kwargs):
        return True

    def delete_questions(self, question_ids):
        return True


@pytest.fixture
def vectors(monkeypatch):
    store = ControlledVectors()
    monkeypatch.setattr(
        "backend.services.question_bank.QuestionVectorStore", lambda: store
    )
    monkeypatch.setattr(
        "backend.services.question_mixins.QuestionVectorStore", lambda settings: store
    )
    return store


def _analysis(**overrides) -> QuestionAnalysis:
    analysis = QuestionAnalysis(
        knowledge_points=["方程"],
        analysis="移项并计算，最后代入原式检验答案。",
        answer="x=2",
        difficulty="medium",
        tags=["方程"],
        question_type="解答题",
        chapter="一元一次方程",
        is_complete=True,
    )
    return analysis.model_copy(update=overrides)


def _user(db_session, role: str = "student") -> User:
    user = User(
        username=f"final-{role}-{uuid.uuid4().hex[:12]}",
        password_hash="not-used",
        role=role,
    )
    db_session.add(user)
    db_session.commit()
    return user


def _canonical(db_session, stem: str, *, origin: str = "student") -> CanonicalQuestion:
    canonical = QuestionBankRepository(db_session).create(
        stem_markdown=stem,
        analysis_markdown="完整标准解析：计算并检验。",
        answer="标准答案",
        origin=origin,
    )
    db_session.commit()
    return canonical


def _document_job(
    db_session,
    user: User,
    tmp_path: Path,
    stem: str,
    *,
    match_status: str = "new",
    canonical_id: int | None = None,
    match_candidates: list[dict] | None = None,
    suffix: str = ".png",
    with_page_image: bool = True,
) -> tuple[str, Path]:
    source = tmp_path / f"{uuid.uuid4().hex}{suffix}"
    source.write_bytes(b"private-document-evidence")
    job_id = uuid.uuid4().hex
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
            },
            result={
                "confirmed": False,
                "page_images": [str(source)] if with_page_image else [],
                "segments": [
                    {
                        "number": "1",
                        "text": stem,
                        "page_start": 1,
                        "page_end": 1,
                        "needs_review": False,
                        "continued": False,
                        "source": "test",
                        "figure_paths": [],
                        "figure_specs": [],
                        "analysis": _analysis().model_dump(mode="json"),
                        "error": None,
                        "match_status": match_status,
                        "canonical_id": canonical_id,
                        "match_candidates": match_candidates or [],
                    }
                ],
                "total": 1,
                "done": 1,
            },
        )
    )
    db_session.commit()
    return job_id, source


def _wait_photo(service: PhotoEntryService, job_id: str, user_id: int, *phases: str) -> dict:
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        job = service.get_job(job_id, user_id)
        assert job is not None
        if job["phase"] in phases:
            return job
        if job["phase"] == "failed":
            pytest.fail(job["error"])
        time.sleep(0.02)
    raise TimeoutError(job_id)


class _PhotoProvider(MockProvider):
    def __init__(self, stem: str):
        super().__init__()
        self.stem = stem
        self.segment_calls = 0
        self.analyze_question_calls = 0
        self.analyze_text_calls = 0

    def segment_page(self, image_bytes, mime_type="image/png"):
        self.segment_calls += 1
        return [{"number": "1", "text": self.stem, "figure_bboxes": []}]

    def analyze_question(self, *args, **kwargs):
        self.analyze_question_calls += 1
        return super().analyze_question(*args, **kwargs)

    def analyze_text(self, *args, **kwargs):
        self.analyze_text_calls += 1
        return super().analyze_text(*args, **kwargs)


def test_photo_student_new_then_other_student_exact_reuses_canonical(
    monkeypatch, db_session, student_user, vectors
):
    second_user = _user(db_session)
    stem = f"学生 A 新建后 B 精确复用-{uuid.uuid4().hex}"
    provider = _PhotoProvider(stem)
    monkeypatch.setattr(
        "backend.services.question_mixins.get_ai_service", lambda settings: provider
    )
    service = PhotoEntryService(Settings(import_concurrency=1))

    first_job = service.create_job(
        student_user.id, [PhotoImage("a.jpg", b"a-private-evidence", "image/jpeg")]
    )
    first_done = _wait_photo(service, first_job, student_user.id, "done")
    second_job = service.create_job(
        second_user.id, [PhotoImage("b.jpg", b"b-private-evidence", "image/jpeg")]
    )
    second_done = _wait_photo(service, second_job, second_user.id, "done")

    first = first_done["entries"][0]["question"]
    second = second_done["entries"][0]["question"]
    assert first is not None, first_done["entries"][0]
    assert second is not None, second_done["entries"][0]
    assert first["canonical_id"] is not None
    assert second["canonical_id"] == first["canonical_id"]
    assert first["match_method"] == "new"
    assert second["match_method"] == "exact"
    assert Path(first["image_path"]).read_bytes() == b"a-private-evidence"
    assert Path(second["image_path"]).read_bytes() == b"b-private-evidence"
    assert db_session.query(func.count(CanonicalQuestion.id)).filter_by(
        normalized_hash=hashlib.sha256(normalize_stem(stem).encode()).hexdigest()
    ).scalar() == 1
    assert provider.analyze_question_calls == 1


def test_student_creation_registers_only_independent_validated_redraw(
    db_session, student_user, vectors, tmp_path
):
    private = tmp_path / "private.png"
    render = tmp_path / "render.png"
    crop = tmp_path / "crop.png"
    private.write_bytes(b"private")
    render.write_bytes(b"independent-render")
    crop.write_bytes(b"handwritten-crop")
    service = QuestionService()
    specs = [
        {
            "kind": "geometry",
            "semantic_spec": {},
            "tikz_source": "\\draw (0,0)--(1,0);",
            "render_path": str(render),
            "crop_path": None,
            "validation_status": "ai_passed",
            "validation_issues": [],
            "assumptions": [],
        },
        {
            "kind": "geometry",
            "semantic_spec": {},
            "tikz_source": "",
            "render_path": None,
            "crop_path": str(crop),
            "validation_status": "fallback",
            "validation_issues": [],
            "assumptions": [],
        },
    ]

    saved = service.create_ai_reference(
        student_user.id,
        stem=f"只共享独立重绘图-{uuid.uuid4().hex}",
        analysis=_analysis(),
        private_image_path=str(private),
        figure_paths=[str(render), str(crop)],
        figure_specs=specs,
        context=AnalysisContext(),
    )

    assets = db_session.query(QuestionAsset).filter_by(canonical_id=saved.canonical_id).all()
    assert len(assets) == 1
    assert assets[0].kind == "ai_redraw"
    assert assets[0].visibility == "shared"
    assert Path(assets[0].storage_path).read_bytes() == b"independent-render"
    assert str(crop) in saved.figure_paths
    assert all(asset.storage_path != str(crop) for asset in assets)
    assert str(private) == saved.image_path
    assert f"u{student_user.id}" not in assets[0].storage_path


def test_document_student_new_then_other_student_exact_reuses_canonical(
    db_session, student_user, tmp_path, vectors
):
    second_user = _user(db_session)
    stem = f"文档学生 A 新建 B 复用-{uuid.uuid4().hex}"
    first_job, first_source = _document_job(db_session, student_user, tmp_path, stem)

    assert DocumentService().confirm_import(first_job, student_user.id)["imported"] == 1
    canonical = db_session.execute(
        select(CanonicalQuestion).where(CanonicalQuestion.stem_markdown == stem)
    ).scalar_one()
    second_job, second_source = _document_job(
        db_session,
        second_user,
        tmp_path,
        stem,
        match_status="exact",
        canonical_id=canonical.id,
    )
    assert DocumentService().confirm_import(second_job, second_user.id)["imported"] == 1

    refs = db_session.query(Question).filter_by(canonical_id=canonical.id).order_by(Question.id).all()
    assert len(refs) == 2
    assert refs[0].match_method == "new"
    assert refs[1].match_method == "exact"
    assert refs[0].private_image_path == str(first_source)
    assert refs[1].private_image_path == str(second_source)


def test_document_explicit_similar_new_creates_student_canonical(
    db_session, student_user, tmp_path, vectors
):
    candidate = _canonical(db_session, f"明确不引用-{uuid.uuid4().hex}", origin="teacher")
    stem = f"相似但明确作为新题-{uuid.uuid4().hex}"
    job_id, _ = _document_job(
        db_session,
        student_user,
        tmp_path,
        stem,
        match_status="similar",
        match_candidates=[{"score": 0.9, "question": {"id": candidate.id}}],
    )

    assert DocumentService().confirm_import(
        job_id, student_user.id, match_choices={0: None}
    )["imported"] == 1

    personal = db_session.query(Question).filter_by(user_id=student_user.id).order_by(
        Question.id.desc()
    ).first()
    assert personal.canonical_id is not None
    assert personal.canonical_id != candidate.id
    assert personal.match_method == "new"
    assert personal.canonical.origin == "student"


def test_student_reference_creation_unique_race_creates_two_references(
    tmp_path, monkeypatch
):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'student-reference-race.db'}",
        connect_args={"check_same_thread": False, "timeout": 30},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as session:
        session.add_all(
            [
                User(id=1, username="race-a", password_hash="x", role="student"),
                User(id=2, username="race-b", password_hash="x", role="student"),
            ]
        )
        session.commit()
    gate = threading.Barrier(2)
    original_create = QuestionBankRepository.create

    def concurrent_create(self, **kwargs):
        gate.wait(timeout=5)
        return original_create(self, **kwargs)

    monkeypatch.setattr(QuestionBankRepository, "create", concurrent_create)
    vectors = ControlledVectors()
    service = QuestionService(session_factory=factory)
    service.vector_store = vectors
    monkeypatch.setattr(
        "backend.services.question_bank.QuestionVectorStore", lambda: vectors
    )
    errors: list[BaseException] = []
    saved: list[int] = []

    def create(user_id: int):
        try:
            out = service.create_ai_reference(
                user_id,
                stem="并发学生新建只保留一个标准题",
                analysis=_analysis(),
                private_image_path=str(tmp_path / f"private-{user_id}.png"),
                context=AnalysisContext(),
            )
            saved.append(out.id)
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=create, args=(user_id,)) for user_id in (1, 2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
        assert not thread.is_alive()

    assert errors == []
    assert len(saved) == 2
    with factory() as session:
        assert session.query(func.count(CanonicalQuestion.id)).scalar() == 1
        assert session.query(func.count(Question.id)).scalar() == 2


def test_new_docx_keeps_private_original_and_last_delete_cleans_orphan(
    db_session, question_service, student_user, tmp_path, vectors
):
    stem = f"新 DOCX 也保留原始证据-{uuid.uuid4().hex}"
    job_id, source = _document_job(
        db_session,
        student_user,
        tmp_path,
        stem,
        suffix=".docx",
        with_page_image=False,
    )

    assert DocumentService().confirm_import(job_id, student_user.id)["imported"] == 1

    personal = db_session.query(Question).filter_by(user_id=student_user.id).order_by(
        Question.id.desc()
    ).first()
    evidence = Path(personal.private_image_path)
    assert personal.canonical_id is not None
    assert evidence.suffix == ".docx"
    assert evidence != source
    assert evidence.read_bytes() == b"private-document-evidence"
    canonical_id = personal.canonical_id

    assert question_service.delete_questions([personal.id], student_user.id) == 1
    assert not evidence.exists()
    assert db_session.get(CanonicalQuestion, canonical_id) is None


def _pipeline_job(db_session, user: User, tmp_path: Path) -> str:
    source = tmp_path / f"{uuid.uuid4().hex}.pdf"
    source.write_bytes(b"pdf")
    job_id = uuid.uuid4().hex
    db_session.add(
        Job(
            id=job_id,
            user_id=user.id,
            type="document_import",
            status="pending",
            payload={"doc_path": str(source), "filename": source.name, "subject": "math"},
        )
    )
    db_session.commit()
    return job_id


@pytest.mark.parametrize("match_status", ["exact", "similar"])
def test_document_matching_skips_analysis_and_geometry_until_similar_choice(
    monkeypatch, db_session, student_user, tmp_path, vectors, match_status
):
    stem = f"先匹配再解析-{match_status}-{uuid.uuid4().hex}"
    canonical = _canonical(db_session, stem if match_status == "exact" else f"候选-{stem}")
    if match_status == "similar":
        vectors.hits = [
            RagHit(question_id=-1, distance=0.05, source="bank", bank_id=str(canonical.id))
        ]
    job_id = _pipeline_job(db_session, student_user, tmp_path)
    provider = MockProvider()
    analysis_calls = []
    redraw_calls = []
    monkeypatch.setattr("backend.services.ai.get_ai_service", lambda settings: provider)
    monkeypatch.setattr(provider, "analyze_text", lambda *args, **kwargs: analysis_calls.append(args))
    monkeypatch.setattr(
        "backend.services.document_service.parse_document",
        lambda path, work_dir: [DocPage(1, stem, None, False)],
    )

    from backend.services.segment import Segment

    def segment_once(pages, ai, **kwargs):
        assert kwargs["figure_service"] is None
        return [Segment("1", stem, 1, 1, figure_bboxes=[[0, 0, 1, 1]])]

    monkeypatch.setattr("backend.services.segment.segment_document", segment_once)
    monkeypatch.setattr(
        GeometryFigureService,
        "from_settings",
        classmethod(lambda cls, ai, settings: SimpleRedraw(redraw_calls)),
    )

    DocumentService()._run_import(job_id, student_user.id)

    result = db_session.get(Job, job_id).result
    assert result["segments"][0]["match_status"] == match_status
    assert result["segments"][0]["analysis"] is None
    assert analysis_calls == []
    assert redraw_calls == []


class SimpleRedraw:
    def __init__(self, calls):
        self.calls = calls

    def redraw_segment(self, **kwargs):
        self.calls.append(kwargs)
        return FigureOutcome([], [], False)


def test_document_new_runs_analysis_and_geometry_after_match(
    monkeypatch, db_session, student_user, tmp_path, vectors
):
    stem = f"只有新题执行解析和几何-{uuid.uuid4().hex}"
    job_id = _pipeline_job(db_session, student_user, tmp_path)
    page_image = tmp_path / "new-page.png"
    page_image.write_bytes(b"page")
    provider = MockProvider()
    analysis_calls = []
    redraw_calls = []

    def analyze(*args, **kwargs):
        analysis_calls.append(args)
        return _analysis()

    monkeypatch.setattr("backend.services.ai.get_ai_service", lambda settings: provider)
    monkeypatch.setattr(provider, "analyze_text", analyze)
    monkeypatch.setattr(
        "backend.services.document_service.parse_document",
        lambda path, work_dir: [DocPage(1, stem, page_image, False)],
    )
    from backend.services.segment import Segment

    monkeypatch.setattr(
        "backend.services.segment.segment_document",
        lambda pages, ai, **kwargs: [
            Segment("1", stem, 1, 1, figure_bboxes=[[0, 0, 1, 1]])
        ],
    )
    monkeypatch.setattr(
        GeometryFigureService,
        "from_settings",
        classmethod(lambda cls, ai, settings: SimpleRedraw(redraw_calls)),
    )

    DocumentService()._run_import(job_id, student_user.id)

    result = db_session.get(Job, job_id).result
    assert result["segments"][0]["match_status"] == "new"
    assert result["segments"][0]["analysis"]["answer"] == "x=2"
    assert len(analysis_calls) == 1
    assert len(redraw_calls) == 1


@pytest.mark.parametrize("failure", ["false", "exception"])
@pytest.mark.parametrize("operation", ["create", "upgrade"])
def test_create_and_upgrade_vector_failures_are_durable(
    db_session, student_user, vectors, failure, operation
):
    vectors.upsert_result = failure != "false"
    vectors.upsert_error = RuntimeError("controlled upsert") if failure == "exception" else None
    teacher = _user(db_session, "teacher")
    actor = student_user if operation == "create" else teacher
    stem = f"向量{operation}{failure}-{uuid.uuid4().hex}"
    service = QuestionBankService(vector_store=vectors)

    created = service.create_or_upgrade(
        stem, _analysis(), actor=actor, context=AnalysisContext()
    )

    assert created.id is not None
    jobs = db_session.query(Job).filter_by(
        type="vector_reconciliation", status="pending", user_id=actor.id
    ).all()
    job = next(job for job in jobs if job.payload.get("canonical_id") == created.id)
    assert job.payload == {"operation": "upsert", "canonical_id": created.id}
    assert job.error


def test_orphan_vector_delete_failure_records_reconciliation_without_restoring_sql(
    db_session, question_service, student_user, vectors, tmp_path
):
    canonical = _canonical(db_session, f"孤儿向量删除失败-{uuid.uuid4().hex}")
    canonical_id = canonical.id
    private = tmp_path / "orphan-private.png"
    private.write_bytes(b"private")
    personal = question_service.reference_canonical(
        student_user.id, canonical.id, private_image_path=str(private)
    )
    question_service.vector_store = vectors
    vectors.delete_result = False

    assert question_service.delete_questions([personal.id], student_user.id) == 1

    db_session.expire_all()
    assert db_session.get(CanonicalQuestion, canonical_id) is None
    jobs = db_session.query(Job).filter_by(
        type="vector_reconciliation", status="pending", user_id=student_user.id
    ).all()
    job = next(job for job in jobs if job.payload.get("canonical_id") == canonical_id)
    assert job.payload == {"operation": "delete", "canonical_id": canonical_id}


def test_orphan_cleanup_retains_path_used_by_another_canonical(db_session, tmp_path):
    orphan = _canonical(db_session, f"孤儿资源跨题引用-{uuid.uuid4().hex}")
    survivor = _canonical(db_session, f"存活资源-{uuid.uuid4().hex}")
    shared = tmp_path / "shared.png"
    shared.write_bytes(b"shared")
    db_session.add_all(
        [
            QuestionAsset(
                canonical_id=orphan.id,
                kind="ai_redraw",
                visibility="shared",
                storage_path=str(shared),
            ),
            QuestionAsset(
                canonical_id=survivor.id,
                kind="ai_redraw",
                visibility="shared",
                storage_path=str(shared),
            ),
        ]
    )
    db_session.commit()

    assert QuestionBankRepository(db_session).delete_student_orphan(orphan.id) == []


def test_orphan_cleanup_retains_path_used_by_surviving_personal_reference(
    db_session, student_user, tmp_path
):
    orphan = _canonical(db_session, f"孤儿资源个人引用-{uuid.uuid4().hex}")
    survivor = _canonical(db_session, f"个人仍引用-{uuid.uuid4().hex}")
    shared = tmp_path / "shared-personal.png"
    shared.write_bytes(b"shared")
    db_session.add(
        QuestionAsset(
            canonical_id=orphan.id,
            kind="ai_redraw",
            visibility="shared",
            storage_path=str(shared),
        )
    )
    personal = Question(
        user_id=student_user.id,
        canonical_id=survivor.id,
        private_image_path=str(shared),
        image_path=str(shared),
        content_markdown="",
        answer="",
    )
    db_session.add(personal)
    db_session.commit()

    assert QuestionBankRepository(db_session).delete_student_orphan(orphan.id) == []


def test_orphan_cleanup_retains_path_in_surviving_personal_figures(
    db_session, student_user, tmp_path
):
    orphan = _canonical(db_session, f"孤儿图形资源-{uuid.uuid4().hex}")
    survivor = _canonical(db_session, f"存活图形引用-{uuid.uuid4().hex}")
    shared = tmp_path / "shared-figure.png"
    shared.write_bytes(b"shared")
    private = tmp_path / "private.png"
    private.write_bytes(b"private")
    db_session.add_all(
        [
            QuestionAsset(
                canonical_id=orphan.id,
                kind="ai_redraw",
                visibility="shared",
                storage_path=str(shared),
            ),
            Question(
                user_id=student_user.id,
                canonical_id=survivor.id,
                private_image_path=str(private),
                image_path=str(private),
                figure_paths=[str(shared)],
                content_markdown="",
                answer="",
            ),
        ]
    )
    db_session.commit()

    assert QuestionBankRepository(db_session).delete_student_orphan(orphan.id) == []


def test_unauthorized_semantic_search_never_calls_vector_store(db_session, student_user, vectors):
    service = QuestionBankService(vector_store=vectors)

    with pytest.raises(PermissionError):
        service.search_canonical(student_user.id, keyword="几何", semantic=True)

    assert vectors.search_calls == 0


def test_original_hash_reuses_only_one_complete_canonical(db_session):
    repository = QuestionBankRepository(db_session)
    unique = _canonical(db_session, f"唯一原图哈希-{uuid.uuid4().hex}")
    other = _canonical(db_session, f"歧义原图哈希-{uuid.uuid4().hex}")
    incomplete = repository.create(stem_markdown=f"不完整原图-{uuid.uuid4().hex}")
    db_session.add_all(
        [
            QuestionAsset(
                canonical_id=unique.id,
                kind="teacher_original",
                visibility="teacher_only",
                storage_path="/teacher/unique.png",
                content_hash="unique-hash",
            ),
            QuestionAsset(
                canonical_id=unique.id,
                kind="teacher_original",
                visibility="teacher_only",
                storage_path="/teacher/ambiguous-a.png",
                content_hash="ambiguous-hash",
            ),
            QuestionAsset(
                canonical_id=other.id,
                kind="teacher_original",
                visibility="teacher_only",
                storage_path="/teacher/ambiguous-b.png",
                content_hash="ambiguous-hash",
            ),
            QuestionAsset(
                canonical_id=incomplete.id,
                kind="teacher_original",
                visibility="teacher_only",
                storage_path="/teacher/incomplete.png",
                content_hash="incomplete-hash",
            ),
        ]
    )
    db_session.commit()
    service = QuestionBankService(db_session, vector_store=ControlledVectors())

    assert service.match_original_hash("unique-hash").id == unique.id
    assert service.match_original_hash("ambiguous-hash") is None
    assert service.match_original_hash("incomplete-hash") is None
    assert service.match_original_hash("missing-hash") is None


def test_photo_original_hash_match_skips_segmentation_and_keeps_private_evidence(
    monkeypatch, db_session, student_user, vectors
):
    stem = f"拍照整图哈希命中-{uuid.uuid4().hex}"
    canonical = _canonical(db_session, stem, origin="teacher")
    upload = b"same-original-bytes"
    db_session.add(
        QuestionAsset(
            canonical_id=canonical.id,
            kind="teacher_original",
            visibility="teacher_only",
            storage_path="/teacher/original.png",
            content_hash=hashlib.sha256(upload).hexdigest(),
        )
    )
    db_session.commit()
    provider = _PhotoProvider("不应切题")
    monkeypatch.setattr(
        "backend.services.question_mixins.get_ai_service", lambda settings: provider
    )
    service = PhotoEntryService(Settings(import_concurrency=1))

    job_id = service.create_job(
        student_user.id, [PhotoImage("same.png", upload, "image/png")]
    )
    done = _wait_photo(service, job_id, student_user.id, "done")

    question = done["entries"][0]["question"]
    assert question["canonical_id"] == canonical.id
    assert question["match_method"] == "exact"
    assert Path(question["image_path"]).read_bytes() == upload
    assert provider.segment_calls == 0
    assert provider.analyze_question_calls == 0


def test_document_original_hash_match_skips_parsing_segmentation_and_analysis(
    monkeypatch, db_session, student_user, tmp_path, vectors
):
    stem = f"文档整图哈希命中-{uuid.uuid4().hex}"
    canonical = _canonical(db_session, stem, origin="teacher")
    source = tmp_path / "hash-source.pdf"
    source.write_bytes(b"same-document")
    job_id = uuid.uuid4().hex
    db_session.add_all(
        [
            QuestionAsset(
                canonical_id=canonical.id,
                kind="teacher_original",
                visibility="teacher_only",
                storage_path="/teacher/source.pdf",
                content_hash=hashlib.sha256(source.read_bytes()).hexdigest(),
            ),
            Job(
                id=job_id,
                user_id=student_user.id,
                type="document_import",
                status="pending",
                payload={
                    "doc_path": str(source),
                    "doc_hash": hashlib.sha256(source.read_bytes()).hexdigest(),
                    "filename": source.name,
                    "subject": "math",
                },
            ),
        ]
    )
    db_session.commit()

    def forbidden(*args, **kwargs):
        raise AssertionError("原图哈希命中后不应继续文档解析或 AI 分析")

    monkeypatch.setattr("backend.services.document_service.parse_document", forbidden)
    monkeypatch.setattr("backend.services.ai.get_ai_service", lambda settings: forbidden())

    DocumentService()._run_import(job_id, student_user.id)

    job = db_session.get(Job, job_id)
    assert job.status == "success"
    assert job.result["segments"][0]["match_status"] == "exact"
    assert job.result["segments"][0]["canonical_id"] == canonical.id
    assert job.result["segments"][0]["analysis"] is None


def test_similar_candidates_apply_threshold_and_exclude_incomplete(db_session):
    complete = _canonical(db_session, f"完整相似候选-{uuid.uuid4().hex}")
    incomplete = QuestionBankRepository(db_session).create(
        stem_markdown=f"不完整相似候选-{uuid.uuid4().hex}", analysis_markdown="", answer=""
    )
    db_session.commit()
    vectors = ControlledVectors(
        [
            RagHit(question_id=-1, distance=0.1, source="bank", bank_id=str(complete.id)),
            RagHit(question_id=-1, distance=0.0, source="bank", bank_id=str(incomplete.id)),
        ]
    )
    service = QuestionBankService(db_session, vector_store=vectors)
    service.settings = Settings(bank_similarity_threshold=0.85)

    candidates = service.find_similar("查询", context=AnalysisContext())

    assert [candidate.question.id for candidate in candidates] == [complete.id]


def test_similar_decisions_correction_and_shared_asset_savings_are_measured(
    monkeypatch, db_session, student_user, tmp_path, vectors
):
    from backend.services.ai import telemetry

    monkeypatch.setattr(telemetry, "_telemetry_path", lambda: tmp_path / "telemetry.jsonl")
    candidate = _canonical(db_session, f"观测相似候选-{uuid.uuid4().hex}", origin="teacher")
    shared = tmp_path / "shared.png"
    shared.write_bytes(b"12345678")
    db_session.add_all(
        [
            QuestionAsset(
                canonical_id=candidate.id,
                kind="ai_redraw",
                visibility="shared",
                storage_path=str(shared),
            ),
            QuestionAsset(
                canonical_id=candidate.id,
                kind="ai_redraw",
                visibility="shared",
                storage_path=str(tmp_path / "already-missing.png"),
            ),
        ]
    )
    db_session.commit()
    accept_job, _ = _document_job(
        db_session,
        student_user,
        tmp_path,
        f"接受相似-{uuid.uuid4().hex}",
        match_status="similar",
        match_candidates=[{"score": 0.9, "question": {"id": candidate.id}}],
    )
    new_job, _ = _document_job(
        db_session,
        student_user,
        tmp_path,
        f"纠正相似-{uuid.uuid4().hex}",
        match_status="similar",
        match_candidates=[{"score": 0.9, "question": {"id": candidate.id}}],
    )

    DocumentService().confirm_import(
        accept_job, student_user.id, match_choices={0: candidate.id}
    )
    DocumentService().confirm_import(new_job, student_user.id, match_choices={0: None})

    summary = telemetry.summarize()["question_bank"]
    assert summary["similar_decisions"] == {"accepted": 1, "new": 1}
    assert summary["corrections"] == {"similar_rejected": 1}
    assert summary["shared_asset_savings"] == {
        "generations_avoided": 1,
        "storage_bytes_avoided": 8,
    }
    assert telemetry.summarize()["token_usage"] is None


def test_docx_evidence_type_mime_and_authenticated_download_contract(
    client, db_session, question_service, student_user, tmp_path
):
    canonical = _canonical(db_session, f"DOCX API 合同-{uuid.uuid4().hex}", origin="teacher")
    evidence = question_service.settings.data_dir / "images" / f"u{student_user.id}" / "evidence.docx"
    evidence.parent.mkdir(parents=True, exist_ok=True)
    evidence.write_bytes(b"docx-private-content")
    personal = question_service.reference_canonical(
        student_user.id, canonical.id, private_image_path=str(evidence)
    )
    login = client.post("/api/auth/login", json={"username": "demo", "password": "demo123"})
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    response = client.get(f"/api/questions/{personal.id}", headers=headers)
    assert response.status_code == 200
    assert response.json()["evidence_type"] == "document"
    assert response.json()["evidence_mime_type"] == (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )
    assert response.json()["evidence_filename"] == "evidence.docx"

    download = client.get(f"/api/questions/{personal.id}/image", headers=headers)
    assert download.status_code == 200
    assert download.content == b"docx-private-content"
    assert download.headers["content-type"].startswith(
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )
    assert "attachment" in download.headers["content-disposition"]
