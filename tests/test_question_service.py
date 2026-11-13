from __future__ import annotations

import io
import time

from PIL import Image

from backend.database import SessionLocal
from backend.services.ai.mock import MockProvider
from backend.services.geometry_figures import FigureOutcome
from backend.services.question_service import QuestionService, sanitize_tags


def _tiny_jpeg(color=(200, 210, 255)) -> bytes:
    image = Image.new("RGB", (8, 8), color=color)
    stream = io.BytesIO()
    image.save(stream, format="JPEG")
    return stream.getvalue()


def test_analyze_and_save_full_pipeline(question_service, student_user):
    saved, analysis = question_service.analyze_and_save(
        student_user.id, _tiny_jpeg(), user_tags=["期末复习"], hint=""
    )
    assert saved.id > 0
    assert saved.tags and "期末复习" in saved.tags
    assert analysis.answer
    assert saved.image_path  # 图片已落盘


class _FakeEntryFigures:
    def __init__(self, root):
        self.root = root
        self.calls = []

    def redraw_segment(self, **kwargs):
        self.calls.append(kwargs["number"])
        path = self.root / f"redraw-{kwargs['number']}.png"
        Image.new("RGB", (20, 20), "white").save(path)
        return FigureOutcome(
            figure_paths=[str(path)],
            figure_specs=[
                {
                    "kind": "geometry",
                    "semantic_spec": {"points": ["A", "B"]},
                    "tikz_source": "tikz",
                    "render_path": str(path),
                    "crop_path": None,
                    "validation_status": "ai_passed",
                    "validation_issues": [],
                    "assumptions": [],
                }
            ],
            needs_review=False,
        )


def test_single_question_entry_reuses_segmentation_and_saves_redraw(
    monkeypatch, question_service, student_user, tmp_path
):
    class SingleGeometryProvider(MockProvider):
        def __init__(self):
            super().__init__()
            self.segment_calls = 0

        def segment_page(self, image_bytes, mime_type="image/png"):
            self.segment_calls += 1
            return [
                {
                    "number": "1",
                    "text": "单题几何",
                    "figure_bboxes": [[10, 20, 200, 180]],
                }
            ]

    from backend.services.entry_types import PhotoImage
    from backend.services.photo_entry import PhotoEntryService

    provider = SingleGeometryProvider()
    figures = _FakeEntryFigures(tmp_path)
    question_service.ai = provider
    question_service._geometry_figure_service = figures
    service = PhotoEntryService()
    monkeypatch.setattr(service, "_question_service", lambda: question_service)
    image = _tiny_jpeg(color=(1, 2, 3))
    job_id = service.create_job(
        student_user.id, [PhotoImage("one.jpg", image, "image/jpeg")]
    )

    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        job = service.get_job(job_id, student_user.id)
        if job["phase"] == "done":
            break
        assert job["phase"] != "failed", job["error"]
        time.sleep(0.02)
    assert job["phase"] == "done"

    entry = job["entries"][0]
    assert entry["duplicated"] is False
    assert entry["question"]["content_markdown"].startswith("单题几何\n\n---\n\n")
    assert entry["question"]["figure_paths"] == [str(tmp_path / "redraw-1.png")]
    assert entry["question"]["figure_specs"][0]["tikz_source"] == "tikz"
    assert provider.segment_calls == 1
    assert figures.calls == ["1"]


def test_direct_entry_without_figures_skips_geometry_service(
    question_service, student_user, tmp_path
):
    class PlainProvider(MockProvider):
        def __init__(self):
            super().__init__()
            self.segment_calls = 0

        def segment_page(self, image_bytes, mime_type="image/png"):
            self.segment_calls += 1
            return [{"number": "1", "text": "无图题", "figure_bboxes": []}]

    provider = PlainProvider()
    figures = _FakeEntryFigures(tmp_path)
    question_service.ai = provider
    question_service._geometry_figure_service = figures

    saved, _ = question_service.analyze_and_save(student_user.id, _tiny_jpeg())

    assert saved.figure_paths == []
    assert saved.figure_specs == []
    assert provider.segment_calls == 1
    assert figures.calls == []


def test_list_and_keyword_search(question_service, student_user):
    question_service.analyze_and_save(student_user.id, _tiny_jpeg(), user_tags=["几何"])
    results = question_service.list_questions(student_user.id)
    assert results, "应能列出错题"

    by_tag = question_service.list_questions(student_user.id, tag="几何")
    assert all("几何" in q.tags for q in by_tag)

    by_keyword = question_service.list_questions(student_user.id, keyword="判别式", semantic=False)
    assert all(
        "判别式" in q.content_markdown
        or "判别式" in (q.answer or "")
        or any("判别式" in t for t in q.tags)
        or any("判别式" in t for t in q.knowledge_points)
        for q in by_keyword
    )


def test_region_and_textbook_filters(question_service, student_user):
    matching = question_service.create_manual_question(
        student_user.id,
        content_markdown="地区教材过滤·目标题",
        region="上海",
        textbook_version="沪教版",
    )
    question_service.create_manual_question(
        student_user.id,
        content_markdown="地区教材过滤·其他题",
        region="北京",
        textbook_version="人教版",
    )

    results = question_service.list_questions(
        student_user.id,
        keyword="地区教材过滤",
        region="上海",
        textbook_version="沪教版",
        semantic=False,
    )

    assert [q.id for q in results] == [matching.id]
    assert question_service.count_for_user(
        student_user.id,
        keyword="地区教材过滤",
        region="上海",
        textbook_version="沪教版",
    ) == 1


def test_semantic_search_does_not_bypass_metadata_filters(
    question_service, student_user, monkeypatch
):
    from backend.services.rag import RagHit

    matching = question_service.create_manual_question(
        student_user.id,
        content_markdown="语义过滤边界·九年级题",
        grade=9,
    )
    other = question_service.create_manual_question(
        student_user.id,
        content_markdown="语义过滤边界·十年级题",
        grade=10,
    )
    monkeypatch.setattr(
        question_service.vector_store,
        "semantic_search",
        lambda *args, **kwargs: [RagHit(question_id=other.id, distance=0.1)],
    )

    results = question_service.list_questions(
        student_user.id,
        keyword="语义过滤边界",
        grade=9,
        semantic=True,
    )

    assert [q.id for q in results] == [matching.id]


def test_question_figure_paths_persist_in_order(question_service, student_user, tmp_path):
    first = tmp_path / "first.png"
    second = tmp_path / "second.png"
    Image.new("RGB", (10, 10), "white").save(first)
    Image.new("RGB", (10, 10), "black").save(second)

    saved = question_service.create_manual_question(
        student_user.id,
        content_markdown="带图错题",
        figure_paths=[str(first), str(second)],
    )

    assert saved.figure_paths == [str(first), str(second)]
    fetched = question_service.get_question(saved.id, student_user.id)
    assert fetched is not None
    assert fetched.figure_paths == [str(first), str(second)]


def test_resolved_figure_paths_recovers_after_data_dir_move(
    question_service, student_user, tmp_path
):
    from pathlib import Path

    from backend.models.orm import Question

    current = question_service.settings.doc_upload_path / "pages" / "job" / "figures" / "fig.png"
    current.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (10, 10), "white").save(current)
    stale = str(tmp_path / "old-data" / "docs" / "pages" / "job" / "figures" / "fig.png")
    question = Question(
        user_id=student_user.id,
        content_markdown="迁移题",
        figure_paths=[stale, str(tmp_path / "missing.png")],
    )

    assert question.resolved_figure_paths() == [Path(current)]


def test_resolved_tikz_figures_recover_only_from_current_tikz_cache(
    question_service, student_user, tmp_path
):
    from pathlib import Path

    from backend.models.orm import Question

    tikz_dir = question_service.settings.data_dir / "figures" / "tikz"
    tikz_dir.mkdir(parents=True, exist_ok=True)
    render_path = tikz_dir / "render.png"
    crop_path = tikz_dir / "crop.png"
    Image.new("RGB", (10, 10), "white").save(render_path)
    Image.new("RGB", (10, 10), "black").save(crop_path)
    unrelated = question_service.settings.data_dir / "other" / "unrelated.png"
    unrelated.parent.mkdir(parents=True)
    Image.new("RGB", (10, 10), "white").save(unrelated)
    old_tikz = tmp_path / "old-data" / "figures" / "tikz"
    question = Question(
        user_id=student_user.id,
        content_markdown="TikZ 缓存迁移题",
        figure_paths=[
            str(old_tikz / "render.png"),
            str(tmp_path / "old-data" / "other" / "unrelated.png"),
        ],
        figure_specs=[
            {
                "kind": "geometry",
                "semantic_spec": {},
                "tikz_source": "",
                "render_path": str(old_tikz / "render.png"),
                "crop_path": str(old_tikz / "crop.png"),
                "validation_status": "fallback",
                "validation_issues": [],
                "assumptions": [],
            }
        ],
    )

    assert question.resolved_figure_paths() == [Path(render_path)]
    figure = question.resolved_figure_specs()[0]
    assert figure["render_path"] == str(render_path)
    assert figure["crop_path"] == str(crop_path)


def test_question_out_defaults_missing_figure_specs_to_empty_list():
    from types import SimpleNamespace

    from backend.models.schemas import QuestionOut

    question = SimpleNamespace(
        id=1,
        user_id=2,
        image_path=None,
        content_markdown="旧题",
        answer="42",
        knowledge_points=[],
        tags=[],
        difficulty="medium",
        followup_question=None,
        source="ai",
        user_note=None,
        ocr_text=None,
        reps=0,
        ease=2.5,
        interval_days=0,
        due_at=None,
        last_reviewed_at=None,
        created_at=None,
    )

    assert QuestionOut.from_orm_model(question).figure_specs == []


def test_question_figure_specs_create_and_read_roundtrip(db_session, student_user, tmp_path):
    from backend.repositories.questions import QuestionRepository, to_out

    render_path = tmp_path / "triangle.png"
    crop_path = tmp_path / "triangle-crop.png"
    Image.new("RGB", (10, 10), "white").save(render_path)
    Image.new("RGB", (10, 10), "white").save(crop_path)
    specs = [
        {
            "kind": "geometry",
            "semantic_spec": {"points": ["A", "B", "C"], "relations": ["AB ⟂ BC"]},
            "tikz_source": "\\begin{tikzpicture}\\draw (0,0)--(1,0);\\end{tikzpicture}",
            "render_path": str(render_path),
            "crop_path": str(crop_path),
            "validation_status": "ai_passed",
            "validation_issues": [],
            "assumptions": ["示意图不按比例"],
        }
    ]
    repository = QuestionRepository(db_session)
    created = repository.create(
        student_user.id,
        content_markdown="带 TikZ 规格的错题",
        figure_specs=specs,
    )
    db_session.commit()
    db_session.expire_all()

    fetched = repository.get_owned(created.id, student_user.id)
    assert fetched is not None
    assert fetched.figure_specs == specs
    assert [spec.model_dump() for spec in to_out(fetched).figure_specs] == specs


def test_geometry_variant_contract_allows_empty_figures():
    from backend.models.schemas import GeometryVariant

    variant = GeometryVariant(question="新题", figures=[])

    assert variant.question == "新题"
    assert variant.figures == []


def test_figure_spec_rejects_unknown_validation_status():
    import pytest
    from pydantic import ValidationError

    from backend.models.schemas import FigureSpec

    with pytest.raises(ValidationError):
        FigureSpec(
            kind="geometry",
            semantic_spec={},
            tikz_source="",
            render_path=None,
            crop_path=None,
            validation_status="unchecked",
            validation_issues=[],
            assumptions=[],
        )


def test_resolved_figure_specs_recovers_render_and_crop_paths(
    question_service, student_user, tmp_path
):
    from backend.models.orm import Question

    figures_dir = question_service.settings.doc_upload_path / "pages" / "job" / "figures"
    figures_dir.mkdir(parents=True, exist_ok=True)
    render_path = figures_dir / "render.png"
    crop_path = figures_dir / "crop.png"
    Image.new("RGB", (10, 10), "white").save(render_path)
    Image.new("RGB", (10, 10), "black").save(crop_path)
    old_docs = tmp_path / "old-data" / "docs" / "pages" / "job" / "figures"
    question = Question(
        user_id=student_user.id,
        content_markdown="迁移题",
        figure_specs=[
            {
                "kind": "geometry",
                "semantic_spec": {},
                "tikz_source": "",
                "render_path": str(old_docs / "render.png"),
                "crop_path": str(old_docs / "crop.png"),
                "validation_status": "fallback",
                "validation_issues": [],
                "assumptions": [],
            }
        ],
    )

    figure = question.resolved_figure_specs()[0]
    assert figure["render_path"] == str(render_path)
    assert figure["crop_path"] == str(crop_path)


def test_isolation_between_users(question_service, db_session, student_user):
    from backend.models.orm import User

    other = User(username="other_user", password_hash="x", role="student")
    db_session.add(other)
    db_session.commit()  # 先提交，另一个连接才能看到该用户（FK 校验）

    mine, _ = question_service.analyze_and_save(student_user.id, _tiny_jpeg())
    visible = question_service.list_questions(student_user.id)
    assert any(q.id == mine.id for q in visible)

    other_view = question_service.list_questions(other.id)
    assert all(q.id != mine.id for q in other_view)


def test_persist_image_compresses_large_images(question_service, student_user):
    from PIL import Image

    big = Image.new("RGB", (2400, 1200), (10, 60, 130))
    stream = io.BytesIO()
    big.save(stream, format="JPEG", quality=95)
    raw_size = len(stream.getvalue())

    saved, _ = question_service.analyze_and_save(student_user.id, stream.getvalue())
    with Image.open(saved.image_path) as stored:
        assert max(stored.size) <= 1600
    import os

    assert os.path.getsize(saved.image_path) < raw_size


def test_analyze_and_save_dedup_returns_existing(question_service, student_user, db_session):
    jpeg = _tiny_jpeg()
    first_result = question_service.analyze_and_save_dedup(student_user.id, jpeg)
    assert first_result.duplicated is False

    second_result = question_service.analyze_and_save_dedup(student_user.id, jpeg)
    assert second_result.duplicated is True
    assert second_result.question.id == first_result.question.id  # 复用既有记录

    all_questions = question_service.list_questions(student_user.id, semantic=False)
    assert len([q for q in all_questions if q.id == first_result.question.id]) == 1

    # 其他用户上传同图是独立错题（按用户隔离）
    from backend.models.orm import User

    other = User(username="dedup_other", password_hash="x", role="student")
    db_session.add(other)
    db_session.commit()
    other_result = question_service.analyze_and_save_dedup(other.id, jpeg)
    assert other_result.duplicated is False
    assert other_result.question.id != first_result.question.id


def test_analyze_and_save_dedup_reuses_legacy_overlong_metadata(
    question_service, student_user, db_session
):
    from backend.models.orm import Question

    jpeg = _tiny_jpeg()
    first = question_service.analyze_and_save_dedup(student_user.id, jpeg)
    stored = db_session.get(Question, first.question.id)
    stored.tags = [f"标签{i}" for i in range(8)]
    stored.knowledge_points = [f"考点{i}" for i in range(8)]
    db_session.commit()

    second = question_service.analyze_and_save_dedup(student_user.id, jpeg)

    assert second.duplicated is True
    assert second.question.id == first.question.id
    assert second.analysis.tags == [f"标签{i}" for i in range(6)]
    assert second.analysis.knowledge_points == [f"考点{i}" for i in range(6)]


def test_update_and_delete(question_service, student_user):
    saved, _ = question_service.analyze_and_save(student_user.id, _tiny_jpeg())
    updated = question_service.update_question(
        saved.id, student_user.id, answer="修正后的答案", tags=["更新标签"]
    )
    assert updated is not None and updated.answer == "修正后的答案"

    noted = question_service.update_question(
        saved.id, student_user.id, user_note="自己的易错备注"
    )
    assert noted is not None and noted.user_note == "自己的易错备注"

    deleted = question_service.delete_questions([saved.id], student_user.id)
    assert deleted == 1
    assert question_service.get_question(saved.id, student_user.id) is None


def test_review_grading_flow(question_service, student_user):
    saved, _ = question_service.analyze_and_save(student_user.id, _tiny_jpeg())

    due_before = question_service.due_questions(student_user.id)
    assert any(q.id == saved.id for q in due_before)  # 新错题立即可复习

    graded = question_service.grade_review(saved.id, student_user.id, "good")
    assert graded is not None and graded.reps == 1

    again = question_service.grade_review(saved.id, student_user.id, "again")
    assert again is not None and again.reps == 0


def test_dashboard_stats_shape(question_service, student_user):
    stats = question_service.dashboard_stats(student_user.id)
    assert {
        "total", "reviewed", "due", "streak",
        "calendar", "accuracy_trend", "tag_stats", "weak_tags", "activity",
    } <= set(stats)
    assert stats["total"] >= 1
    assert stats["streak"] >= 1  # 今天录入了错题，连击至少 1 天
    assert stats["calendar"]["max"] >= 1
    assert len(stats["activity"]) == 14


def test_sanitize_tags_variants():
    assert sanitize_tags("a, b，c； d") == ["a", "b", "c", "d"]
    assert sanitize_tags("") == []
    assert sanitize_tags("重复,重复") == ["重复"]


def test_service_accepts_session_factory_singleton():
    service = QuestionService(session_factory=SessionLocal)
    assert service.vector_store is not None
    assert service.ai is not None
