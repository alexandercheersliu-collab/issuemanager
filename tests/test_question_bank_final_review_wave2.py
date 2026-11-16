from __future__ import annotations

import hashlib
import time
import uuid
from pathlib import Path

import pytest
from doc_samples import make_text_pdf

from backend.models.orm import CanonicalQuestion, Job, Question, QuestionAsset, User
from backend.models.schemas import QuestionAnalysis
from backend.repositories.question_bank import QuestionBankRepository
from backend.services.ai.base import AnalysisContext
from backend.services.ai.mock import MockProvider
from backend.services.document_service import DocumentService
from backend.services.geometry_figures import FigureOutcome
from backend.services.question_service import QuestionService
from backend.services.rag import SimilarResult
from backend.services.segment import Segment, _join_cross_page


class Wave2Vectors:
    def __init__(self):
        self.upserted: list[str] = []

    def similar_canonical_questions(self, *args, **kwargs):
        return SimilarResult()

    def upsert_bank_question(self, bank_id, text, **metadata):
        self.upserted.append(bank_id)
        return True

    def delete_bank_questions(self, canonical_ids):
        return True

    def upsert_question(self, *args, **kwargs):
        return True

    def delete_questions(self, question_ids):
        return True


@pytest.fixture
def vectors(monkeypatch):
    store = Wave2Vectors()
    monkeypatch.setattr("backend.services.question_bank.QuestionVectorStore", lambda: store)
    monkeypatch.setattr(
        "backend.services.question_mixins.QuestionVectorStore", lambda settings: store
    )
    return store


def _analysis() -> QuestionAnalysis:
    return QuestionAnalysis(
        knowledge_points=["方程"],
        analysis="学生补充的完整解析：移项、计算并检验。",
        answer="学生新答案",
        difficulty="medium",
        tags=["方程"],
        question_type="解答题",
        chapter="一元一次方程",
        is_complete=True,
    )


def _wait_document(service: DocumentService, job_id: str, user_id: int) -> dict:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        job = service.get_job(job_id, user_id)
        assert job is not None
        if job["status"] in {"success", "failed"}:
            assert job["status"] == "success", job["error"]
            return job
        time.sleep(0.05)
    raise TimeoutError(job_id)


def test_teacher_publish_then_identical_student_document_uses_raw_hash_without_exposure(
    db_session, student_user, tmp_path, vectors
):
    teacher = User(
        username=f"wave2-teacher-{uuid.uuid4().hex[:10]}",
        password_hash="not-used",
        role="teacher",
    )
    db_session.add(teacher)
    db_session.commit()
    stem = f"原始文档哈希师生复用-{uuid.uuid4().hex}"
    pdf = make_text_pdf(tmp_path / "shared.pdf", [f"1. {stem}"])
    data = pdf.read_bytes()
    service = DocumentService()

    teacher_job, _ = service.import_document(teacher.id, data, "teacher.pdf")
    _wait_document(service, teacher_job, teacher.id)
    assert service.confirm_import(
        teacher_job, teacher.id, publish_to_bank=True
    )["imported"] == 1
    db_session.expire_all()
    canonical = db_session.query(CanonicalQuestion).order_by(
        CanonicalQuestion.id.desc()
    ).first()
    assert canonical is not None

    student_job, _ = service.import_document(student_user.id, data, "student.pdf")
    student_result = _wait_document(service, student_job, student_user.id)
    segment = student_result["result"]["segments"][0]
    assert segment["source"] == "original_hash"
    assert segment["canonical_id"] == canonical.id
    assert segment["analysis"] is None
    assert service.confirm_import(student_job, student_user.id)["imported"] == 1

    personal = db_session.query(Question).filter_by(
        user_id=student_user.id, canonical_id=canonical.id
    ).one()
    evidence = Path(personal.private_image_path)
    bank_paths = {
        asset.storage_path
        for asset in db_session.query(QuestionAsset).filter_by(canonical_id=canonical.id)
    }
    assert evidence.read_bytes() == data
    assert str(evidence) not in bank_paths
    assert evidence.is_file()
    view = QuestionService().get_question(personal.id, student_user.id)
    assert view is not None
    assert not bank_paths.intersection(view.figure_paths)


@pytest.mark.parametrize("origin", ["student", "teacher"])
def test_valid_student_new_backfills_incomplete_canonical_without_replacing_existing_answer(
    db_session, student_user, tmp_path, vectors, origin
):
    stem = f"学生修复不完整标准题-{origin}-{uuid.uuid4().hex}"
    canonical = QuestionBankRepository(db_session).create(
        stem_markdown=stem,
        analysis_markdown="",
        answer="保留既有答案",
        origin=origin,
        validation_state="teacher_published" if origin == "teacher" else "machine_validated",
    )
    db_session.commit()
    private = tmp_path / f"{origin}-private.png"
    private.write_bytes(b"private")
    canonical_id = canonical.id

    saved = QuestionService().create_ai_reference(
        student_user.id,
        stem=stem,
        analysis=_analysis(),
        private_image_path=str(private),
        context=AnalysisContext(subject="math", grade=9),
    )

    db_session.expire_all()
    repaired = db_session.get(CanonicalQuestion, canonical_id)
    assert saved.canonical_id == canonical_id
    assert saved.match_method == "new"
    assert repaired.analysis_markdown == "学生补充的完整解析：移项、计算并检验。"
    assert repaired.answer == "保留既有答案"
    assert repaired.origin == origin
    assert repaired.validation_state == (
        "teacher_published" if origin == "teacher" else "machine_validated"
    )
    assert str(canonical_id) in vectors.upserted
    assert QuestionBankRepository(db_session).count_references(canonical_id) == 1


def test_student_backfill_treats_whitespace_only_standard_fields_as_missing(
    db_session, student_user, tmp_path, vectors
):
    stem = f"空白字段也视为缺失-{uuid.uuid4().hex}"
    canonical = QuestionBankRepository(db_session).create(
        stem_markdown=stem,
        analysis_markdown=" ",
        answer="\t",
    )
    db_session.commit()
    private = tmp_path / "whitespace-private.png"
    private.write_bytes(b"private")

    saved = QuestionService().create_ai_reference(
        student_user.id,
        stem=stem,
        analysis=_analysis(),
        private_image_path=str(private),
        context=AnalysisContext(subject="math"),
    )

    db_session.expire_all()
    repaired = db_session.get(CanonicalQuestion, canonical.id)
    assert saved.canonical_id == canonical.id
    assert repaired.analysis_markdown == "学生补充的完整解析：移项、计算并检验。"
    assert repaired.answer == "学生新答案"
    assert QuestionBankRepository(db_session).is_reusable(repaired)


def test_cross_page_deferred_redraw_uses_second_page_for_second_page_figure(tmp_path):
    first = Segment(
        "1",
        "跨页题前半",
        1,
        1,
        continues_to_next=True,
    )
    box = [10, 20, 100, 120]
    second = Segment(
        "1",
        "跨页题后半，见图",
        2,
        2,
        continued_from_prev=True,
        figure_bboxes=[box],
        figure_pages=[2],
    )
    joined = _join_cross_page([first, second])[0]
    first_page = tmp_path / "page-1.png"
    second_page = tmp_path / "page-2.png"
    first_page.write_bytes(b"first")
    second_page.write_bytes(b"second")
    entry = {
        "number": joined.number,
        "text": joined.text,
        "page_start": joined.page_start,
        "page_end": joined.page_end,
        "figure_bboxes": list(joined.figure_bboxes),
        "figure_pages": list(joined.figure_pages),
    }
    calls = []

    class Figures:
        def redraw_segment(self, **kwargs):
            calls.append(kwargs)
            crop = tmp_path / f"{kwargs['image_path'].stem}-crop.png"
            crop.write_bytes(kwargs["image_path"].read_bytes())
            spec = {
                "kind": "geometry",
                "semantic_spec": {},
                "tikz_source": "",
                "render_path": None,
                "crop_path": str(crop),
                "validation_status": "fallback",
                "validation_issues": [],
                "assumptions": [],
            }
            return FigureOutcome([str(crop)], [spec], True)

    _, outcome = DocumentService()._analyze_and_redraw(
        entry,
        ai=MockProvider(),
        figure_service=Figures(),
        context=AnalysisContext(),
        page_images=[str(first_page), str(second_page)],
        figures_dir=tmp_path / "figures",
    )

    assert len(calls) == 1
    assert calls[0]["image_path"] == second_page
    assert calls[0]["figure_bboxes"] == [box]
    assert outcome.figure_paths == [str(tmp_path / "page-2-crop.png")]
    assert Path(outcome.figure_paths[0]).read_bytes() == b"second"


def _docx_job(db_session, user_id: int, tmp_path: Path) -> str:
    source = tmp_path / "retry-evidence.docx"
    source.write_bytes(b"private-docx")
    job_id = uuid.uuid4().hex
    db_session.add(
        Job(
            id=job_id,
            user_id=user_id,
            type="document_import",
            status="success",
            payload={
                "filename": source.name,
                "doc_path": str(source),
                "doc_hash": hashlib.sha256(source.read_bytes()).hexdigest(),
                "subject": "math",
            },
            result={
                "confirmed": False,
                "page_images": [],
                "segments": [
                    {
                        "number": "1",
                        "text": f"DOCX 复制失败后重试-{uuid.uuid4().hex}",
                        "page_start": 1,
                        "page_end": 1,
                        "needs_review": False,
                        "continued": False,
                        "source": "test",
                        "figure_paths": [],
                        "figure_specs": [],
                        "analysis": _analysis().model_dump(mode="json"),
                        "error": None,
                        "match_status": "new",
                        "canonical_id": None,
                        "match_candidates": [],
                    }
                ],
                "total": 1,
                "done": 1,
            },
        )
    )
    db_session.commit()
    return job_id


def test_docx_evidence_copy_failure_is_retryable_and_does_not_strand_confirmation(
    db_session, student_user, tmp_path, monkeypatch, vectors
):
    job_id = _docx_job(db_session, student_user.id, tmp_path)
    service = DocumentService()
    original_write = Path.write_bytes
    failed = False

    def fail_private_copy_once(self, data, *args, **kwargs):
        nonlocal failed
        if (
            not failed
            and self.suffix == ".docx"
            and self.is_relative_to(service.settings.data_dir / "images")
        ):
            failed = True
            raise OSError("controlled evidence copy failure")
        return original_write(self, data, *args, **kwargs)

    monkeypatch.setattr(Path, "write_bytes", fail_private_copy_once)

    with pytest.raises(ValueError, match="重试"):
        service.confirm_import(job_id, student_user.id)
    job = service.get_job(job_id, student_user.id)
    assert job is not None
    assert job["status"] == "success"
    assert job["result"]["confirmed"] is False

    outcome = service.confirm_import(job_id, student_user.id)
    assert outcome == {"imported": 1, "skipped": [], "already_confirmed": False}
    personal = db_session.query(Question).filter_by(user_id=student_user.id).order_by(
        Question.id.desc()
    ).first()
    assert Path(personal.private_image_path).read_bytes() == b"private-docx"
