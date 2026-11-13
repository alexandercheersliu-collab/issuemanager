"""拍照录题页面委托共享任务服务的回归测试。"""
from __future__ import annotations

import io
from types import SimpleNamespace

from PIL import Image
from streamlit.testing.v1 import AppTest

from backend.services.ai.base import AnalysisContext
from frontend.pages import tutor


def _job(phase: str, **overrides) -> dict:
    job = {
        "job_id": "job-1",
        "phase": phase,
        "candidates": [],
        "fallback_count": 0,
        "total": 0,
        "done": 0,
        "entries": [],
        "error": None,
    }
    job.update(overrides)
    return job


class _FakePhotoEntryService:
    def __init__(self, job: dict | None = None):
        self.job = job
        self.create_calls = []
        self.get_calls = []
        self.confirm_calls = []

    def create_job(self, user_id, images, *, tags=None, hint="", context=None):
        self.create_calls.append((user_id, images, tags, hint, context))
        self.job = _job("review")
        return self.job["job_id"]

    def get_job(self, job_id, user_id):
        self.get_calls.append((job_id, user_id))
        return self.job

    def confirm(self, job_id, user_id, candidate_ids):
        self.confirm_calls.append((job_id, user_id, candidate_ids))
        self.job = _job("done")


class _FakeAI:
    def provider_info(self):
        return SimpleNamespace(demo_mode=False)


class _FakeQuestionService:
    ai = _FakeAI()


class _Upload:
    name = "paper.jpg"
    type = "image/jpeg"

    def getvalue(self):
        buffer = io.BytesIO()
        Image.new("RGB", (2, 2), "white").save(buffer, format="JPEG")
        return buffer.getvalue()


def _patch_page(monkeypatch, photo_service, uploads=None):
    monkeypatch.setattr(tutor, "PhotoEntryService", lambda: photo_service, raising=False)
    monkeypatch.setattr(tutor, "get_question_service", lambda: _FakeQuestionService())
    monkeypatch.setattr(
        tutor.st, "file_uploader", lambda *args, **kwargs: list(uploads or [])
    )


def _app() -> AppTest:
    return AppTest.from_string(
        "from frontend.pages import tutor\n"
        "tutor.render_tutor_page({'id': 7})\n",
        default_timeout=15,
    )


def test_start_analysis_creates_shared_photo_job(monkeypatch):
    service = _FakePhotoEntryService()
    upload = _Upload()
    _patch_page(monkeypatch, service, [upload])
    at = _app().run()
    at.text_input(key="photo_tags").set_value("期末, 几何")
    at.text_input[1].set_value("第二问不会")

    at.button[0].click().run()

    assert not at.exception
    assert len(service.create_calls) == 1
    user_id, images, tags, hint, context = service.create_calls[0]
    assert user_id == 7
    assert [(image.name, image.data, image.mime_type) for image in images] == [
        ("paper.jpg", upload.getvalue(), "image/jpeg")
    ]
    assert tags == ["期末", "几何"]
    assert hint == "第二问不会"
    assert context == AnalysisContext(
        subject="math", grade=10, region="上海", textbook_version="沪教版"
    )
    assert at.session_state["photo_job_id"] == "job-1"


def test_active_photo_job_is_polled_until_candidate_review(monkeypatch):
    service = _FakePhotoEntryService(_job("planning"))
    _patch_page(monkeypatch, service)
    monkeypatch.setattr(tutor, "time", SimpleNamespace(sleep=lambda _: None), raising=False)
    monkeypatch.setattr(tutor.st, "rerun", lambda: None)
    at = _app()
    at.session_state["photo_job_id"] = "job-1"

    at.run()
    assert service.get_calls == [("job-1", 7)]
    service.job = _job(
        "review",
        candidates=[
            {
                "id": "0:0",
                "image": "paper.jpg",
                "number": "1",
                "text": "题干",
                "summary": "题干",
                "likely_wrong": True,
                "figure_bboxes": [],
            }
        ],
    )
    at.run()

    assert not at.exception
    assert service.get_calls == [("job-1", 7), ("job-1", 7)]
    assert any("从图中识别出 1 道题" in item.value for item in at.success)
    assert at.checkbox[0].value is True


def test_candidate_confirmation_delegates_stable_ids(monkeypatch):
    candidates = [
        {
            "id": "0:0",
            "image": "paper.jpg",
            "number": "1",
            "text": "第一题",
            "summary": "第一题",
            "likely_wrong": True,
            "figure_bboxes": [],
        },
        {
            "id": "0:1",
            "image": "paper.jpg",
            "number": "2",
            "text": "第二题",
            "summary": "第二题",
            "likely_wrong": False,
            "figure_bboxes": [],
        },
    ]
    service = _FakePhotoEntryService(
        _job("review", candidates=candidates, fallback_count=1)
    )
    _patch_page(monkeypatch, service)
    at = _app()
    at.session_state["photo_job_id"] = "job-1"
    at.run()

    assert [checkbox.value for checkbox in at.checkbox] == [True, False]
    confirm = [button for button in at.button if button.label.startswith("确认录入")]
    assert len(confirm) == 1
    confirm[0].click().run()

    assert not at.exception
    assert service.confirm_calls == [("job-1", 7, ["0:0"])]


def test_photo_job_failure_is_recoverable(monkeypatch):
    service = _FakePhotoEntryService(_job("failed", error="model down"))
    _patch_page(monkeypatch, service, [_Upload()])
    at = _app()
    at.session_state["photo_job_id"] = "job-1"

    at.run()

    assert not at.exception
    assert any("model down" in item.value for item in at.error)
    start = next(button for button in at.button if button.label == "开始 AI 解析")
    assert start.disabled is False
