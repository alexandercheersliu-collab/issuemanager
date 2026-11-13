"""共享拍照录入服务：持久化任务、候选确认、回退、去重与隔离。"""
from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from backend.config import Settings
from backend.models.orm import Job, User
from backend.services.ai.base import AnalysisContext
from backend.services.ai.mock import MockProvider
from backend.services.entry_types import DEFAULT_IMPORT_CONTEXT, PhotoImage
from backend.services.photo_entry import PhotoEntryService


def _service_with_provider(
    monkeypatch, provider: MockProvider, settings: Settings | None = None
) -> PhotoEntryService:
    monkeypatch.setattr(
        "backend.services.question_mixins.get_ai_service", lambda settings: provider
    )
    return PhotoEntryService(settings)


def _wait_for_phase(
    service: PhotoEntryService,
    job_id: str,
    user_id: int,
    *phases: str,
    timeout: float = 15.0,
) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = service.get_job(job_id, user_id)
        assert job is not None
        if job["phase"] in phases:
            return job
        if job["phase"] == "failed" and "failed" not in phases:
            pytest.fail(f"拍照任务失败：{job['error']}")
        time.sleep(0.02)
    raise TimeoutError(f"任务 {job_id} 未进入 {phases}")


def test_default_import_context_matches_shared_mobile_contract():
    assert DEFAULT_IMPORT_CONTEXT == AnalysisContext(
        subject="math", grade=10, region="上海", textbook_version="沪教版"
    )


def test_two_images_produce_stable_candidates_and_observable_defaults(monkeypatch, student_user):
    class Provider(MockProvider):
        def segment_page(self, image_bytes, mime_type="image/png"):
            return [
                {"number": "1", "text": "第一题：计算 1+1。"},
                {
                    "number": "2",
                    "text": "第二题：\n  求 x^2=9 的解。",
                    "likely_wrong": True,
                    "figure_bboxes": [[1, 2, 3, 4]],
                },
            ]

    service = _service_with_provider(monkeypatch, Provider())
    job_id = service.create_job(
        student_user.id,
        [
            PhotoImage("a.jpg", b"photo-a", "image/jpeg"),
            PhotoImage("b.png", b"photo-b", "image/png"),
        ],
    )

    review = _wait_for_phase(service, job_id, student_user.id, "review")
    assert set(review) == {
        "job_id",
        "phase",
        "candidates",
        "fallback_count",
        "total",
        "done",
        "entries",
        "error",
    }
    assert len(review["candidates"]) == 4
    assert [candidate["image"] for candidate in review["candidates"]] == [
        "a.jpg",
        "a.jpg",
        "b.png",
        "b.png",
    ]
    assert [candidate["likely_wrong"] for candidate in review["candidates"]] == [
        False,
        True,
        False,
        True,
    ]
    assert review["candidates"][0]["figure_bboxes"] == []
    assert review["candidates"][1]["figure_bboxes"] == [[1, 2, 3, 4]]
    assert review["candidates"][1]["summary"] == "第二题： 求 x^2=9 的解。"
    assert all(
        set(candidate)
        == {"id", "image", "number", "text", "summary", "likely_wrong", "figure_bboxes"}
        for candidate in review["candidates"]
    )
    assert len({candidate["id"] for candidate in review["candidates"]}) == 4
    assert review["fallback_count"] == 0
    assert review["entries"] == []

    polled = service.get_job(job_id, student_user.id)
    assert polled is not None
    assert [candidate["id"] for candidate in polled["candidates"]] == [
        candidate["id"] for candidate in review["candidates"]
    ]


@pytest.mark.parametrize("outcome", ["single", "empty", "error"])
def test_non_multi_segmentation_auto_imports_without_segmenting_twice(
    monkeypatch, student_user, outcome
):
    class Provider(MockProvider):
        def __init__(self):
            super().__init__()
            self.segment_calls = 0
            self.analyze_calls = 0

        def segment_page(self, image_bytes, mime_type="image/png"):
            self.segment_calls += 1
            if outcome == "single":
                return [{"number": "1", "text": "唯一一道题"}]
            if outcome == "empty":
                return []
            raise RuntimeError("segmentation unavailable")

        def analyze_question(self, *args, **kwargs):
            self.analyze_calls += 1
            return super().analyze_question(*args, **kwargs)

    provider = Provider()
    service = _service_with_provider(monkeypatch, provider)
    job_id = service.create_job(
        student_user.id,
        [PhotoImage(f"{outcome}.jpg", f"fallback-{outcome}".encode(), "image/jpeg")],
    )

    done = _wait_for_phase(service, job_id, student_user.id, "done")
    assert done["candidates"] == []
    assert done["fallback_count"] == 1
    assert done["total"] == 1
    assert done["done"] == 1
    assert len(done["entries"]) == 1
    assert done["entries"][0]["error"] is None
    assert done["entries"][0]["question"] is not None
    assert provider.segment_calls == 1
    assert provider.analyze_calls == 1


def test_confirm_imports_only_selected_candidates_plus_fallbacks_with_default_context(
    monkeypatch, student_user, question_service
):
    class Provider(MockProvider):
        def segment_page(self, image_bytes, mime_type="image/png"):
            if image_bytes == b"multi-select":
                return [
                    {"number": "1", "text": "不应导入的第一题", "likely_wrong": True},
                    {"number": "2", "text": "应导入的第二题", "likely_wrong": False},
                ]
            return [{"number": "9", "text": "整图回退题"}]

    service = _service_with_provider(monkeypatch, Provider())
    baseline = len(question_service.list_questions(student_user.id, semantic=False))
    job_id = service.create_job(
        student_user.id,
        [
            PhotoImage("multi.jpg", b"multi-select", "image/jpeg"),
            PhotoImage("single.jpg", b"single-fallback", "image/jpeg"),
        ],
        tags=["共享上下文"],
        hint="只录错题",
    )
    review = _wait_for_phase(service, job_id, student_user.id, "review")
    selected = review["candidates"][1]

    assert service.confirm(job_id, student_user.id, [selected["id"]]) is None
    done = _wait_for_phase(service, job_id, student_user.id, "done")

    assert done["total"] == 2
    assert done["done"] == 2
    assert len(done["entries"]) == 2
    assert [entry["error"] for entry in done["entries"]] == [None, None]
    assert done["entries"][0]["name"] == "multi.jpg · 第 2 题"
    assert done["entries"][1]["name"] == "single.jpg"
    questions = question_service.list_questions(student_user.id, semantic=False)
    assert len(questions) == baseline + 2
    saved = [question_service.get_question(entry["question"]["id"], student_user.id) for entry in done["entries"]]
    assert all(question is not None for question in saved)
    assert all(question.subject == "math" for question in saved)
    assert all(question.grade == 10 for question in saved)
    assert all(question.region == "上海" for question in saved)
    assert all(question.textbook_version == "沪教版" for question in saved)
    assert all("共享上下文" in question.tags for question in saved)
    assert not any("不应导入的第一题" in question.content_markdown for question in questions)


def test_duplicate_fallback_image_reuses_original_record(monkeypatch, student_user):
    class Provider(MockProvider):
        def __init__(self):
            super().__init__()
            self.analyze_calls = 0

        def segment_page(self, image_bytes, mime_type="image/png"):
            return [{"number": "1", "text": "重复整图题"}]

        def analyze_question(self, *args, **kwargs):
            self.analyze_calls += 1
            return super().analyze_question(*args, **kwargs)

    provider = Provider()
    service = _service_with_provider(monkeypatch, provider)
    image = PhotoImage("duplicate.jpg", b"duplicate-photo-entry", "image/jpeg")
    first_id = service.create_job(student_user.id, [image])
    first = _wait_for_phase(service, first_id, student_user.id, "done")
    second_id = service.create_job(student_user.id, [image])
    second = _wait_for_phase(service, second_id, student_user.id, "done")

    assert first["entries"][0]["duplicated"] is False
    assert second["entries"][0]["duplicated"] is True
    assert second["entries"][0]["question"]["id"] == first["entries"][0]["question"]["id"]
    assert provider.analyze_calls == 1


def test_identical_fallbacks_in_one_job_keep_dedup_semantics(monkeypatch, student_user):
    class Provider(MockProvider):
        def __init__(self):
            super().__init__()
            self.analyze_calls = 0
            self.lock = threading.Lock()

        def segment_page(self, image_bytes, mime_type="image/png"):
            return [{"number": "1", "text": "同批次重复整图题"}]

        def analyze_question(self, *args, **kwargs):
            with self.lock:
                self.analyze_calls += 1
            time.sleep(0.1)
            return super().analyze_question(*args, **kwargs)

    provider = Provider()
    service = _service_with_provider(monkeypatch, provider, Settings(import_concurrency=2))
    job_id = service.create_job(
        student_user.id,
        [
            PhotoImage("first-copy.jpg", b"same-batch-image", "image/jpeg"),
            PhotoImage("second-copy.jpg", b"same-batch-image", "image/jpeg"),
        ],
    )

    done = _wait_for_phase(service, job_id, student_user.id, "done")

    assert provider.analyze_calls == 1
    assert [entry["duplicated"] for entry in done["entries"]] == [False, True]
    assert done["entries"][0]["question"]["id"] == done["entries"][1]["question"]["id"]


def test_one_failing_candidate_does_not_stop_other_imports(monkeypatch, student_user):
    class Provider(MockProvider):
        def segment_page(self, image_bytes, mime_type="image/png"):
            return [
                {"number": "1", "text": "这题会失败"},
                {"number": "2", "text": "这题应成功"},
            ]

        def analyze_text(self, text, hint="", context=None):
            if text == "这题会失败":
                raise RuntimeError("item failed")
            return super().analyze_text(text, hint, context)

    service = _service_with_provider(monkeypatch, Provider())
    job_id = service.create_job(
        student_user.id, [PhotoImage("partial.jpg", b"partial", "image/jpeg")]
    )
    review = _wait_for_phase(service, job_id, student_user.id, "review")
    service.confirm(
        job_id, student_user.id, [candidate["id"] for candidate in review["candidates"]]
    )

    done = _wait_for_phase(service, job_id, student_user.id, "done")
    assert done["done"] == 2
    assert done["entries"][0]["error"] == "item failed"
    assert done["entries"][0]["question"] is None
    assert done["entries"][1]["error"] is None
    assert done["entries"][1]["question"]["id"] > 0


class _ConcurrentProvider(MockProvider):
    def __init__(self):
        super().__init__()
        self.lock = threading.Lock()
        self.segment_current = 0
        self.segment_max = 0
        self.analyze_current = 0
        self.analyze_max = 0

    def segment_page(self, image_bytes, mime_type="image/png"):
        with self.lock:
            self.segment_current += 1
            self.segment_max = max(self.segment_max, self.segment_current)
        try:
            time.sleep(0.05)
            return [
                {"number": "1", "text": f"{image_bytes.decode()} 第一题"},
                {"number": "2", "text": f"{image_bytes.decode()} 第二题"},
            ]
        finally:
            with self.lock:
                self.segment_current -= 1

    def analyze_text(self, text, hint="", context=None):
        with self.lock:
            self.analyze_current += 1
            self.analyze_max = max(self.analyze_max, self.analyze_current)
        try:
            time.sleep(0.12 if "第一题" in text else 0.02)
            analysis = super().analyze_text(text, hint, context)
            analysis.answer = f"答案:{text}"
            return analysis
        finally:
            with self.lock:
                self.analyze_current -= 1


def test_multi_image_segmentation_uses_bounded_concurrency(monkeypatch, student_user):
    provider = _ConcurrentProvider()
    service = _service_with_provider(
        monkeypatch, provider, Settings(import_concurrency=3)
    )
    job_id = service.create_job(
        student_user.id,
        [PhotoImage(f"{index}.jpg", f"image-{index}".encode(), "image/jpeg") for index in range(3)],
    )

    review = _wait_for_phase(service, job_id, student_user.id, "review")

    assert provider.segment_max > 1
    assert [candidate["text"] for candidate in review["candidates"]] == [
        f"image-{image_index} 第{number}题"
        for image_index in range(3)
        for number in ("一", "二")
    ]


def test_selected_candidates_import_concurrently_in_stable_order(monkeypatch, student_user):
    provider = _ConcurrentProvider()
    service = _service_with_provider(
        monkeypatch, provider, Settings(import_concurrency=3)
    )
    job_id = service.create_job(
        student_user.id, [PhotoImage("concurrent.jpg", b"concurrent", "image/jpeg")]
    )
    review = _wait_for_phase(service, job_id, student_user.id, "review")
    service.confirm(
        job_id, student_user.id, [candidate["id"] for candidate in review["candidates"]]
    )

    done = _wait_for_phase(service, job_id, student_user.id, "done")

    assert provider.analyze_max > 1
    assert [entry["name"] for entry in done["entries"]] == [
        "concurrent.jpg · 第 1 题",
        "concurrent.jpg · 第 2 题",
    ]
    assert [entry["question"]["stem"] for entry in done["entries"]] == [
        "concurrent 第一题",
        "concurrent 第二题",
    ]
    assert [entry["question"]["answer"] for entry in done["entries"]] == [
        "答案:concurrent 第一题",
        "答案:concurrent 第二题",
    ]


def test_photo_concurrency_one_preserves_sequential_execution(monkeypatch, student_user):
    provider = _ConcurrentProvider()
    service = _service_with_provider(
        monkeypatch, provider, Settings(import_concurrency=1)
    )
    job_id = service.create_job(
        student_user.id,
        [PhotoImage(f"serial-{index}.jpg", f"serial-{index}".encode(), "image/jpeg") for index in range(2)],
    )
    review = _wait_for_phase(service, job_id, student_user.id, "review")
    service.confirm(
        job_id, student_user.id, [candidate["id"] for candidate in review["candidates"]]
    )

    done = _wait_for_phase(service, job_id, student_user.id, "done")

    assert done["done"] == 4
    assert provider.segment_max == 1
    assert provider.analyze_max == 1


def test_empty_selection_without_fallbacks_is_rejected(monkeypatch, student_user):
    service = _service_with_provider(monkeypatch, MockProvider())
    job_id = service.create_job(
        student_user.id, [PhotoImage("empty.jpg", b"empty-selection", "image/jpeg")]
    )
    _wait_for_phase(service, job_id, student_user.id, "review")

    with pytest.raises(ValueError):
        service.confirm(job_id, student_user.id, [])
    assert service.get_job(job_id, student_user.id)["phase"] == "review"


def test_unknown_candidate_id_is_rejected_without_import(monkeypatch, student_user):
    service = _service_with_provider(monkeypatch, MockProvider())
    job_id = service.create_job(
        student_user.id, [PhotoImage("unknown.jpg", b"unknown-selection", "image/jpeg")]
    )
    _wait_for_phase(service, job_id, student_user.id, "review")

    with pytest.raises(ValueError, match="候选"):
        service.confirm(job_id, student_user.id, ["client-supplied-id"])
    assert service.get_job(job_id, student_user.id)["entries"] == []


def test_repeated_confirmation_returns_original_entries_without_reimport(
    monkeypatch, student_user
):
    class Provider(MockProvider):
        def __init__(self):
            super().__init__()
            self.analyze_calls = 0

        def analyze_text(self, text, hint="", context=None):
            self.analyze_calls += 1
            return super().analyze_text(text, hint, context)

    provider = Provider()
    service = _service_with_provider(monkeypatch, provider)
    job_id = service.create_job(
        student_user.id, [PhotoImage("repeat.jpg", b"repeat-confirmation", "image/jpeg")]
    )
    review = _wait_for_phase(service, job_id, student_user.id, "review")
    service.confirm(job_id, student_user.id, [review["candidates"][0]["id"]])
    original = _wait_for_phase(service, job_id, student_user.id, "done")

    assert service.confirm(job_id, student_user.id, []) is None
    repeated = service.get_job(job_id, student_user.id)
    assert repeated["entries"] == original["entries"]
    assert repeated["total"] == original["total"]
    assert repeated["done"] == original["done"]
    assert provider.analyze_calls == 1


def test_other_user_cannot_read_or_confirm_job(monkeypatch, student_user, db_session):
    service = _service_with_provider(monkeypatch, MockProvider())
    job_id = service.create_job(
        student_user.id, [PhotoImage("owner.jpg", b"owner-only", "image/jpeg")]
    )
    _wait_for_phase(service, job_id, student_user.id, "review")
    other_user = db_session.query(User).filter_by(username="admin").one()

    assert service.get_job(job_id, other_user.id) is None
    with pytest.raises(LookupError):
        service.confirm(job_id, other_user.id, [])


def test_original_inputs_are_stored_under_job_directory(monkeypatch, student_user, db_session):
    service = _service_with_provider(monkeypatch, MockProvider())
    job_id = service.create_job(
        student_user.id,
        [
            PhotoImage("../../first.jpg", b"first-original", "image/jpeg"),
            PhotoImage("second.png", b"second-original", "image/png"),
        ],
    )
    _wait_for_phase(service, job_id, student_user.id, "review")

    db_session.expire_all()
    job = db_session.get(Job, job_id)
    assert job is not None
    paths = [Path(item["path"]) for item in job.payload["images"]]
    assert len(paths) == 2
    assert len({path.parent for path in paths}) == 1
    assert job_id in paths[0].parent.parts
    assert all(path.is_file() for path in paths)
    assert [path.read_bytes() for path in paths] == [b"first-original", b"second-original"]
    assert paths[0].name != "first.jpg"
    assert all("path" not in candidate for candidate in job.result["candidates"])


def test_import_progress_is_persisted_while_item_is_running(monkeypatch, student_user):
    started = threading.Event()
    release = threading.Event()

    class Provider(MockProvider):
        def analyze_text(self, text, hint="", context=None):
            started.set()
            assert release.wait(timeout=5)
            return super().analyze_text(text, hint, context)

    service = _service_with_provider(monkeypatch, Provider())
    job_id = service.create_job(
        student_user.id, [PhotoImage("progress.jpg", b"progress", "image/jpeg")]
    )
    review = _wait_for_phase(service, job_id, student_user.id, "review")
    service.confirm(job_id, student_user.id, [review["candidates"][0]["id"]])
    try:
        assert started.wait(timeout=5)
        importing = service.get_job(job_id, student_user.id)
        assert importing["phase"] == "importing"
        assert importing["total"] == 1
        assert importing["done"] == 0
    finally:
        release.set()

    done = _wait_for_phase(service, job_id, student_user.id, "done")
    assert done["done"] == 1


def test_concurrent_confirmations_import_candidate_only_once(
    monkeypatch, student_user, question_service
):
    from sqlalchemy import Select

    from backend.services import photo_entry as photo_entry_module

    class Provider(MockProvider):
        def __init__(self):
            super().__init__()
            self.analyze_calls = 0
            self.lock = threading.Lock()

        def analyze_text(self, text, hint="", context=None):
            with self.lock:
                self.analyze_calls += 1
            return super().analyze_text(text, hint, context)

    provider = Provider()
    service = _service_with_provider(monkeypatch, provider)
    baseline = len(question_service.list_questions(student_user.id, semantic=False))
    job_id = service.create_job(
        student_user.id, [PhotoImage("race.jpg", b"concurrent-confirmation", "image/jpeg")]
    )
    review = _wait_for_phase(service, job_id, student_user.id, "review")
    candidate_id = review["candidates"][0]["id"]

    read_barrier = threading.Barrier(2)
    real_session_factory = photo_entry_module.SessionLocal

    class ReadBarrierSession:
        def __init__(self):
            self.session = real_session_factory()

        def __enter__(self):
            self.session.__enter__()
            return self

        def __exit__(self, *args):
            return self.session.__exit__(*args)

        def execute(self, statement, *args, **kwargs):
            result = self.session.execute(statement, *args, **kwargs)
            if isinstance(statement, Select) and threading.current_thread().name.startswith(
                "confirm-race-"
            ):
                read_barrier.wait(timeout=5)
            return result

        def __getattr__(self, name):
            return getattr(self.session, name)

    monkeypatch.setattr(photo_entry_module, "SessionLocal", ReadBarrierSession)
    errors: list[BaseException] = []

    def confirm():
        try:
            service.confirm(job_id, student_user.id, [candidate_id])
        except BaseException as exc:
            errors.append(exc)

    threads = [
        threading.Thread(target=confirm, name=f"confirm-race-{index}")
        for index in range(2)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert all(not thread.is_alive() for thread in threads)
    assert errors == []

    done = _wait_for_phase(service, job_id, student_user.id, "done")
    assert provider.analyze_calls == 1
    assert len(question_service.list_questions(student_user.id, semantic=False)) == baseline + 1
    assert done["total"] == 1
    assert done["done"] == 1
    assert len(done["entries"]) == 1
    original_entries = done["entries"]
    service.confirm(job_id, student_user.id, [])
    assert service.get_job(job_id, student_user.id)["entries"] == original_entries
