from __future__ import annotations

import threading
import time
import uuid
from pathlib import Path

import pytest

from backend.config import Settings
from backend.models.orm import Question, QuestionAsset, User
from backend.repositories.question_bank import QuestionBankRepository
from backend.services.ai.mock import MockProvider
from backend.services.entry_types import PhotoImage
from backend.services.photo_entry import PhotoEntryService
from backend.services.question_identity import normalize_stem
from backend.services.rag import RagHit, SimilarResult


class FakeVectorStore:
    def __init__(self, hits_by_stem: dict[str, list[RagHit]] | None = None):
        self.hits_by_stem = hits_by_stem or {}

    def similar_canonical_questions(
        self, query_text: str, *, constraints: object, top_k: int
    ) -> SimilarResult:
        return SimilarResult(hits=self.hits_by_stem.get(query_text, [])[:top_k])

    def upsert_bank_question(self, bank_id: str, text: str, **metadata) -> bool:
        return True


class RecordingProvider(MockProvider):
    def __init__(self, segments: list[dict]):
        super().__init__()
        self.segments = segments
        self.lock = threading.Lock()
        self.analyze_question_calls = 0
        self.analyze_text_calls = 0
        self.redraw_geometry_calls = 0
        self.segment_page_calls = 0

    def segment_page(self, image_bytes, mime_type="image/png"):
        with self.lock:
            self.segment_page_calls += 1
        return [dict(segment) for segment in self.segments]

    def analyze_question(self, *args, **kwargs):
        with self.lock:
            self.analyze_question_calls += 1
        return super().analyze_question(*args, **kwargs)

    def analyze_text(self, *args, **kwargs):
        with self.lock:
            self.analyze_text_calls += 1
        return super().analyze_text(*args, **kwargs)

    def redraw_geometry(self, *args, **kwargs):
        with self.lock:
            self.redraw_geometry_calls += 1
        return []


def _wait_for_phase(
    service: PhotoEntryService, job_id: str, user_id: int, *phases: str
) -> dict:
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        job = service.get_job(job_id, user_id)
        assert job is not None
        if job["phase"] in phases:
            return job
        if job["phase"] == "failed":
            pytest.fail(f"拍照任务失败：{job['error']}")
        time.sleep(0.02)
    raise TimeoutError(f"任务 {job_id} 未进入 {phases}")


def _canonical(db_session, stem: str, *, origin: str = "student") -> int:
    canonical = QuestionBankRepository(db_session).create(
        stem_markdown=stem,
        analysis_markdown="标准解析：根据已知条件逐步计算并验证结果。",
        answer="标准答案",
        knowledge_points=["标准知识点"],
        difficulty="medium",
        origin=origin,
    )
    db_session.commit()
    return canonical.id


def _service(
    monkeypatch, provider: RecordingProvider, vector_store: FakeVectorStore
) -> PhotoEntryService:
    monkeypatch.setattr(
        "backend.services.question_mixins.get_ai_service", lambda settings: provider
    )
    monkeypatch.setattr(
        "backend.services.question_bank.QuestionVectorStore", lambda: vector_store
    )
    return PhotoEntryService(Settings(import_concurrency=2))


def _two_segment_job(
    service: PhotoEntryService,
    user_id: int,
    first_text: str,
    *,
    image_data: bytes | None = None,
) -> str:
    return service.create_job(
        user_id,
        [
            PhotoImage(
                "evidence.jpg",
                image_data or f"upload-{uuid.uuid4().hex}".encode(),
                "image/jpeg",
            )
        ],
    )


@pytest.mark.parametrize("origin", ["student", "teacher"])
def test_exact_match_reuses_without_ai_and_keeps_this_upload_as_private_evidence(
    monkeypatch, db_session, student_user, origin
):
    stem = f"精确复用原图证据-{origin}-{uuid.uuid4().hex}"
    canonical_id = _canonical(db_session, stem, origin=origin)
    provider = RecordingProvider(
        [
            {
                "number": "1",
                "text": stem,
                "figure_bboxes": [[0, 0, 20, 20]],
            },
            {"number": "2", "text": f"未选择题-{uuid.uuid4().hex}"},
        ]
    )
    service = _service(monkeypatch, provider, FakeVectorStore())
    monkeypatch.setattr(
        "backend.services.geometry_figures.GeometryFigureService._tex_available", lambda self: True
    )
    teacher_asset_path = service.settings.data_dir / "question-bank" / f"{uuid.uuid4().hex}.jpg"
    teacher_asset_path.parent.mkdir(parents=True, exist_ok=True)
    teacher_asset_path.write_bytes(b"teacher-only-original")
    db_session.add(
        QuestionAsset(
            canonical_id=canonical_id,
            kind="teacher_original",
            visibility="teacher_only",
            storage_path=str(teacher_asset_path),
        )
    )
    db_session.commit()
    upload = f"private-evidence-{uuid.uuid4().hex}".encode()

    job_id = service.create_job(
        student_user.id, [PhotoImage("evidence.jpg", upload, "image/jpeg")]
    )
    review = _wait_for_phase(service, job_id, student_user.id, "review")
    matched = review["candidates"][0]

    assert matched["text"] == stem
    assert matched["match_status"] == "exact"
    assert matched["canonical_id"] == canonical_id
    assert matched["match_candidates"] == []
    assert str(teacher_asset_path) not in str(review)
    assert provider.analyze_question_calls == 0
    assert provider.analyze_text_calls == 0
    assert provider.redraw_geometry_calls == 0

    service.confirm(job_id, student_user.id, [matched["id"]])
    done = _wait_for_phase(service, job_id, student_user.id, "done")
    question = done["entries"][0]["question"]
    private_image = Path(question["image_path"])

    assert question["canonical_id"] == canonical_id
    assert question["match_method"] == "exact"
    assert private_image.read_bytes() == upload
    assert private_image != teacher_asset_path
    assert str(teacher_asset_path) not in question["figure_paths"]
    stored = db_session.get(Question, question["id"])
    assert stored.private_image_path == question["image_path"]
    assert provider.analyze_question_calls == 0
    assert provider.analyze_text_calls == 0
    assert provider.redraw_geometry_calls == 0

    service.confirm(job_id, student_user.id, [], match_choices={})
    repeated = service.get_job(job_id, student_user.id)
    assert repeated["entries"] == done["entries"]
    assert db_session.query(Question).filter_by(canonical_id=canonical_id).count() == 1


def test_single_segment_exact_fallback_reuses_without_visual_or_geometry_ai(
    monkeypatch, db_session, student_user
):
    stem = f"整图精确复用-{uuid.uuid4().hex}"
    canonical_id = _canonical(db_session, stem)
    provider = RecordingProvider(
        [{"number": "1", "text": stem, "figure_bboxes": [[0, 0, 10, 10]]}]
    )
    service = _service(monkeypatch, provider, FakeVectorStore())
    monkeypatch.setattr(
        "backend.services.geometry_figures.GeometryFigureService._tex_available", lambda self: True
    )
    upload = b"single-image-private-evidence"

    job_id = service.create_job(
        student_user.id, [PhotoImage("single.jpg", upload, "image/jpeg")]
    )
    done = _wait_for_phase(service, job_id, student_user.id, "done")

    question = done["entries"][0]["question"]
    assert question["canonical_id"] == canonical_id
    assert question["match_method"] == "exact"
    assert Path(question["image_path"]).read_bytes() == upload
    assert provider.analyze_question_calls == 0
    assert provider.analyze_text_calls == 0
    assert provider.redraw_geometry_calls == 0


def _similar_setup(monkeypatch, db_session, student_user):
    similar_stem = f"相似题题干-{uuid.uuid4().hex}"
    new_stem = f"新题题干-{uuid.uuid4().hex}"
    offered_id = _canonical(db_session, f"已发布相似标准题-{uuid.uuid4().hex}", origin="teacher")
    other_id = _canonical(db_session, f"未提供标准题-{uuid.uuid4().hex}")
    hits = {
        normalize_stem(similar_stem): [
            RagHit(
                question_id=-1,
                distance=0.0,
                source="bank",
                bank_id=str(offered_id),
            )
        ]
    }
    provider = RecordingProvider(
        [{"number": "1", "text": similar_stem}, {"number": "2", "text": new_stem}]
    )
    service = _service(monkeypatch, provider, FakeVectorStore(hits))
    job_id = _two_segment_job(service, student_user.id, similar_stem)
    review = _wait_for_phase(service, job_id, student_user.id, "review")
    return service, provider, job_id, review, offered_id, other_id


def test_similar_match_without_choice_stays_unresolved_without_archiving_or_ai(
    monkeypatch, db_session, student_user
):
    service, provider, job_id, review, offered_id, _ = _similar_setup(
        monkeypatch, db_session, student_user
    )
    similar = review["candidates"][0]

    assert similar["match_status"] == "similar"
    assert similar["canonical_id"] is None
    assert [item["question"]["id"] for item in similar["match_candidates"]] == [offered_id]
    assert similar["match_candidates"][0]["score"] == 1.0
    assert review["candidates"][1]["match_status"] == "new"

    with pytest.raises(ValueError, match="选择"):
        service.confirm(job_id, student_user.id, [similar["id"]])

    assert service.get_job(job_id, student_user.id)["phase"] == "review"
    assert service.get_job(job_id, student_user.id)["entries"] == []
    assert db_session.query(Question).filter_by(canonical_id=offered_id).count() == 0
    assert provider.analyze_question_calls == 0
    assert provider.analyze_text_calls == 0
    assert provider.redraw_geometry_calls == 0


def test_confirmed_similar_choice_reuses_candidate_and_repeated_confirm_is_idempotent(
    monkeypatch, db_session, student_user
):
    service, provider, job_id, review, offered_id, _ = _similar_setup(
        monkeypatch, db_session, student_user
    )
    candidate_id = review["candidates"][0]["id"]

    service.confirm(
        job_id, student_user.id, [candidate_id], match_choices={candidate_id: offered_id}
    )
    done = _wait_for_phase(service, job_id, student_user.id, "done")

    question = done["entries"][0]["question"]
    assert question["canonical_id"] == offered_id
    assert question["match_method"] == "confirmed_similar"
    assert Path(question["image_path"]).is_file()
    assert provider.analyze_question_calls == 0
    assert provider.analyze_text_calls == 0
    assert provider.redraw_geometry_calls == 0

    service.confirm(
        job_id, student_user.id, [], match_choices={candidate_id: offered_id}
    )
    assert service.get_job(job_id, student_user.id)["entries"] == done["entries"]
    assert db_session.query(Question).filter_by(canonical_id=offered_id).count() == 1


def test_explicit_new_choice_runs_text_analysis_instead_of_reusing_similar(
    monkeypatch, db_session, student_user
):
    service, provider, job_id, review, offered_id, _ = _similar_setup(
        monkeypatch, db_session, student_user
    )
    candidate_id = review["candidates"][0]["id"]

    service.confirm(
        job_id, student_user.id, [candidate_id], match_choices={candidate_id: None}
    )
    done = _wait_for_phase(service, job_id, student_user.id, "done")

    question = done["entries"][0]["question"]
    assert question["canonical_id"] != offered_id
    assert question["match_method"] == "new"
    assert question["stem"] == review["candidates"][0]["text"]
    assert provider.analyze_question_calls == 0
    assert provider.analyze_text_calls == 1
    assert provider.redraw_geometry_calls == 0


@pytest.mark.parametrize("choice", ["unknown-candidate", "other-canonical", "missing-canonical"])
def test_forged_similar_choice_ids_are_rejected_without_state_transition(
    monkeypatch, db_session, student_user, choice
):
    service, provider, job_id, review, offered_id, other_id = _similar_setup(
        monkeypatch, db_session, student_user
    )
    candidate_id = review["candidates"][0]["id"]
    choices = {
        "unknown-candidate": {"forged-candidate": offered_id},
        "other-canonical": {candidate_id: other_id},
        "missing-canonical": {candidate_id: 99999999},
    }

    with pytest.raises(ValueError):
        service.confirm(
            job_id, student_user.id, [candidate_id], match_choices=choices[choice]
        )

    assert service.get_job(job_id, student_user.id)["phase"] == "review"
    assert service.get_job(job_id, student_user.id)["entries"] == []
    assert provider.analyze_text_calls == 0
    assert provider.analyze_question_calls == 0


def test_exact_match_rejects_unneeded_canonical_choice(monkeypatch, db_session, student_user):
    stem = f"精确题不接受伪造选择-{uuid.uuid4().hex}"
    canonical_id = _canonical(db_session, stem)
    provider = RecordingProvider(
        [{"number": "1", "text": stem}, {"number": "2", "text": "另一道新题"}]
    )
    service = _service(monkeypatch, provider, FakeVectorStore())
    job_id = service.create_job(
        student_user.id, [PhotoImage("exact.jpg", b"exact-choice", "image/jpeg")]
    )
    review = _wait_for_phase(service, job_id, student_user.id, "review")
    candidate_id = review["candidates"][0]["id"]

    with pytest.raises(ValueError):
        service.confirm(
            job_id,
            student_user.id,
            [candidate_id],
            match_choices={candidate_id: canonical_id},
        )

    assert service.get_job(job_id, student_user.id)["phase"] == "review"
    assert provider.analyze_text_calls == 0


def test_single_image_exact_reuse_records_only_visual_analysis_savings(
    monkeypatch, db_session, student_user, tmp_path
):
    from backend.services.ai import telemetry

    monkeypatch.setattr(telemetry, "_telemetry_path", lambda: tmp_path / "telemetry.jsonl")
    stem = f"单图无几何精确复用-{uuid.uuid4().hex}"
    _canonical(db_session, stem)
    provider = RecordingProvider([{"number": "1", "text": stem, "figure_bboxes": []}])
    service = _service(monkeypatch, provider, FakeVectorStore())

    job_id = service.create_job(
        student_user.id, [PhotoImage("single-no-figure.jpg", b"private-image", "image/jpeg")]
    )
    _wait_for_phase(service, job_id, student_user.id, "done")

    summary = telemetry.summarize()
    assert summary["question_bank"]["skipped_ai_calls"] == {"analyze_image": 1}


@pytest.mark.parametrize(
    ("figure_bboxes", "tex_available", "expected"),
    [
        ([], True, {"analyze_text": 1}),
        ([[0, 0, 20, 20]], True, {"analyze_text": 1, "redraw_geometry": 1}),
        ([[0, 0, 20, 20]], False, {"analyze_text": 1}),
    ],
)
def test_segment_exact_reuse_records_text_and_actual_geometry_savings(
    monkeypatch, db_session, student_user, tmp_path, figure_bboxes, tex_available, expected
):
    from backend.services.ai import telemetry

    monkeypatch.setattr(telemetry, "_telemetry_path", lambda: tmp_path / "telemetry.jsonl")
    stem = f"切题精确复用-{uuid.uuid4().hex}"
    _canonical(db_session, stem)
    provider = RecordingProvider(
        [
            {"number": "1", "text": stem, "figure_bboxes": figure_bboxes},
            {"number": "2", "text": f"未选择题-{uuid.uuid4().hex}", "figure_bboxes": []},
        ]
    )
    service = _service(monkeypatch, provider, FakeVectorStore())
    monkeypatch.setattr(
        "backend.services.geometry_figures.GeometryFigureService._tex_available",
        lambda self: tex_available,
    )

    upload = f"private-segments-{uuid.uuid4().hex}".encode()
    job_id = service.create_job(
        student_user.id, [PhotoImage("segments.jpg", upload, "image/jpeg")]
    )
    review = _wait_for_phase(service, job_id, student_user.id, "review")
    service.confirm(job_id, student_user.id, [review["candidates"][0]["id"]])
    _wait_for_phase(service, job_id, student_user.id, "done")

    summary = telemetry.summarize()
    assert summary["question_bank"]["skipped_ai_calls"] == expected


def test_second_student_with_identical_image_skips_segmentation_and_all_ai(
    monkeypatch, db_session, student_user
):
    other = User(username=f"same-image-{uuid.uuid4().hex}", password_hash="x", role="student")
    db_session.add(other)
    db_session.commit()
    provider = RecordingProvider([])
    service = _service(monkeypatch, provider, FakeVectorStore())
    upload = f"identical-source-{uuid.uuid4().hex}".encode()

    first_job = service.create_job(
        student_user.id, [PhotoImage("geometry.jpg", upload, "image/jpeg")]
    )
    first = _wait_for_phase(service, first_job, student_user.id, "done")
    second_job = service.create_job(
        other.id, [PhotoImage("geometry.jpg", upload, "image/jpeg")]
    )
    second = _wait_for_phase(service, second_job, other.id, "done")

    first_question = first["entries"][0]["question"]
    second_question = second["entries"][0]["question"]
    assert second_question["canonical_id"] == first_question["canonical_id"]
    assert second_question["match_method"] == "exact"
    assert second_question["image_path"] != first_question["image_path"]
    assert provider.segment_page_calls == 1
    assert provider.analyze_question_calls == 1
    assert provider.analyze_text_calls == 0
    assert provider.redraw_geometry_calls == 0
