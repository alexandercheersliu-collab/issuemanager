from __future__ import annotations

from pathlib import Path

from PIL import Image
from streamlit.testing.v1 import AppTest


def _image(path: Path) -> str:
    Image.new("RGB", (20, 20), "white").save(path)
    return str(path)


def _run(script: str) -> AppTest:
    return AppTest.from_string(script, default_timeout=15).run()


def test_render_figures_displays_only_existing_images(tmp_path):
    image = _image(tmp_path / "figure.png")
    at = _run(
        "from frontend.components import render_figures\n"
        f"render_figures([{image!r}, '/missing/figure.png'], alt_prefix='题图')\n"
    )

    assert not at.exception
    assert len(at.get("image")) == 1


def test_import_preview_prefers_crops_and_keeps_full_page_in_expander(tmp_path):
    crop = _image(tmp_path / "crop.png")
    page = _image(tmp_path / "page.png")
    segment = {
        "number": "1",
        "text": "如图，求线段长。",
        "page_start": 1,
        "page_end": 1,
        "source": "visual",
        "needs_review": False,
        "error": None,
        "analysis": {"analysis": "解析", "answer": "2"},
        "figure_paths": [crop],
    }
    at = _run(
        "from frontend.pages.import_doc import _render_segment\n"
        f"_render_segment(None, {{'id': 1}}, 'job', 0, {segment!r}, [{page!r}])\n"
    )

    assert not at.exception
    assert len(at.get("image")) == 2
    assert any("查看完整原页" in expander.label for expander in at.expander)


def test_import_preview_without_crops_keeps_legacy_page_image(tmp_path):
    page = _image(tmp_path / "page.png")
    segment = {
        "number": "1",
        "text": "如图",
        "page_start": 1,
        "page_end": 1,
        "source": "visual",
        "needs_review": False,
        "error": None,
        "analysis": {"analysis": "解析"},
    }
    at = _run(
        "from frontend.pages.import_doc import _render_segment\n"
        f"_render_segment(None, {{'id': 1}}, 'job', 0, {segment!r}, [{page!r}])\n"
    )

    assert not at.exception
    assert len(at.get("image")) == 1


def test_question_detail_prefers_figures_and_folds_full_page(tmp_path):
    crop = _image(tmp_path / "crop.png")
    page = _image(tmp_path / "page.png")
    at = _run(
        "from backend.models.schemas import QuestionOut\n"
        "from frontend.components import question_detail_view\n"
        "q = QuestionOut(\n"
        "    id=1, user_id=1, image_path=" + repr(page) + ",\n"
        "    figure_paths=[" + repr(crop) + "],\n"
        "    content_markdown='### 题目\\n如图\\n\\n### 解析\\n解析', answer='2',\n"
        ")\n"
        "question_detail_view(q)\n"
    )

    assert not at.exception
    assert len(at.get("image")) == 2
    assert any("查看完整原页" in expander.label for expander in at.expander)


def test_review_flashcard_displays_stem_and_current_figure(tmp_path):
    crop = _image(tmp_path / "crop.png")
    at = _run(
        "import frontend.pages.review as review\n"
        "from backend.models.schemas import QuestionOut\n"
        "q = QuestionOut(\n"
        "    id=1, user_id=1, image_path=None, figure_paths=[" + repr(crop) + "],\n"
        "    content_markdown='### 题目\\n独立题干\\n\\n### 解析\\n独立解析内容', answer='2',\n"
        ")\n"
        "class FakeScheduler:\n"
        "    pass\n"
        "class FakeService:\n"
        "    scheduler = FakeScheduler()\n"
        "    def due_questions(self, user_id):\n"
        "        return [q]\n"
        "    def recent_reviews(self, user_id, limit=20):\n"
        "        return []\n"
        "review.get_question_service = lambda: FakeService()\n"
        "review.render_review_page({'id': 1, 'role': 'student'})\n"
    )

    assert not at.exception
    assert len(at.get("image")) == 1
    markdown = "\n".join(element.value for element in at.markdown)
    assert "独立题干" in markdown
    assert "独立解析内容" not in markdown


def test_review_plain_generated_variant_displays_current_figure(tmp_path):
    crop = _image(tmp_path / "variant.png")
    at = _run(
        "import frontend.pages.review as review\n"
        "from backend.models.schemas import QuestionOut\n"
        "q = QuestionOut(\n"
        "    id=22, user_id=1, image_path=None, figure_paths=[" + repr(crop) + "],\n"
        "    content_markdown='纯文本 AI 变式题干', answer='2', source='generated',\n"
        ")\n"
        "class FakeScheduler:\n"
        "    pass\n"
        "class FakeService:\n"
        "    scheduler = FakeScheduler()\n"
        "    def due_questions(self, user_id):\n"
        "        return [q]\n"
        "    def recent_reviews(self, user_id, limit=20):\n"
        "        return []\n"
        "review.get_question_service = lambda: FakeService()\n"
        "review.render_review_page({'id': 1, 'role': 'student'})\n"
    )

    assert not at.exception
    assert len(at.get("image")) == 1
    captions = "\n".join(element.value for element in at.get("caption"))
    assert "本题无原图" not in captions


def test_detection_challenge_displays_figure(tmp_path):
    crop = _image(tmp_path / "crop.png")
    at = _run(
        "import streamlit as st\n"
        "from backend.models.schemas import QuestionOut\n"
        "from frontend.pages.notebook import _render_detection\n"
        "q = QuestionOut(id=1, user_id=1, image_path=None, content_markdown='原题', answer='2')\n"
        "st.session_state['det_challenge_1'] = {\n"
        "    'tested': {\n"
        "        'source': 'generated', 'content': '同类题干', 'difficulty': 'medium',\n"
        "        'knowledge_points': [], 'figure_paths': [" + repr(crop) + "],\n"
        "    }\n"
        "}\n"
        "_render_detection(None, q, {'id': 1})\n"
    )

    assert not at.exception
    assert len(at.get("image")) == 1


def test_similar_question_candidate_displays_figure(tmp_path):
    crop = _image(tmp_path / "crop.png")
    at = _run(
        "from types import SimpleNamespace\n"
        "from backend.models.schemas import QuestionOut\n"
        "from frontend.pages.tutor import _render_analysis\n"
        "saved = QuestionOut(id=1, user_id=1, image_path=None, content_markdown='原题', answer='2')\n"
        "candidate = QuestionOut(\n"
        "    id=-1, user_id=0, image_path=None, figure_paths=[" + repr(crop) + "],\n"
        "    content_markdown='带图同类题', answer='', source='generated',\n"
        ")\n"
        "class FakeService:\n"
        "    def similar_questions(self, *args, **kwargs):\n"
        "        return SimpleNamespace(items=[candidate])\n"
        "analysis = SimpleNamespace(\n"
        "    question_type='解答题', chapter='几何', error_category=None,\n"
        "    knowledge_points=['几何'], analysis='解析', answer='2', mistake_cause='',\n"
        "    followup_question='',\n"
        ")\n"
        "_render_analysis(saved, analysis, FakeService(), {'id': 1})\n"
    )

    assert not at.exception
    assert len(at.get("image")) == 1


def test_multiple_generated_similar_questions_have_unique_save_keys():
    at = _run(
        "from types import SimpleNamespace\n"
        "from backend.models.schemas import QuestionOut\n"
        "from frontend.pages.tutor import _render_analysis\n"
        "saved = QuestionOut(id=1, user_id=1, image_path=None, content_markdown='原题', answer='2')\n"
        "candidates = [\n"
        "    QuestionOut(id=-9999, user_id=0, image_path=None, content_markdown='变式 A', answer='', source='generated'),\n"
        "    QuestionOut(id=-9999, user_id=0, image_path=None, content_markdown='变式 B', answer='', source='generated'),\n"
        "]\n"
        "class FakeService:\n"
        "    def similar_questions(self, *args, **kwargs):\n"
        "        return SimpleNamespace(items=candidates)\n"
        "analysis = SimpleNamespace(\n"
        "    question_type='解答题', chapter='几何', error_category=None,\n"
        "    knowledge_points=['几何'], analysis='解析', answer='2', mistake_cause='',\n"
        "    followup_question='',\n"
        ")\n"
        "_render_analysis(saved, analysis, FakeService(), {'id': 1})\n"
    )

    assert not at.exception
    buttons = [button for button in at.button if "变式题存入错题本" in button.label]
    assert len(buttons) == 2
    assert len({button.key for button in buttons}) == 2


def test_import_review_shows_reference_redraw_status_and_tikz_editor(tmp_path):
    reference = _image(tmp_path / "reference.png")
    redraw = _image(tmp_path / "redraw.png")
    page = _image(tmp_path / "review-page.png")
    spec = {
        "kind": "geometry",
        "semantic_spec": {"points": ["A", "B"]},
        "tikz_source": "\\begin{tikzpicture}\\draw (0,0)--(1,1);\\end{tikzpicture}",
        "render_path": redraw,
        "crop_path": reference,
        "validation_status": "fallback",
        "validation_issues": ["角度需核对"],
        "assumptions": [],
    }
    segment = {
        "number": "1",
        "text": "如图，求角度。",
        "page_start": 1,
        "page_end": 1,
        "source": "visual",
        "needs_review": True,
        "error": None,
        "analysis": {"analysis": "解析", "answer": "30°"},
        "figure_paths": [redraw],
        "figure_specs": [spec],
    }
    at = _run(
        "from frontend.pages.import_doc import _render_segment\n"
        f"_render_segment(None, {{'id': 1}}, 'job', 0, {segment!r}, [{page!r}])\n"
    )

    assert not at.exception
    captions_and_markdown = "\n".join(
        [element.value for element in at.get("caption")]
        + [element.value for element in at.markdown]
    )
    assert "原图参考" in captions_and_markdown
    assert "TikZ 重绘" in captions_and_markdown
    assert any("TikZ" in area.label for area in at.text_area)
    assert "待人工确认" in captions_and_markdown
    assert "角度需核对" in captions_and_markdown
    alt_values = [
        image.alt
        for element in at.get("image")
        for image in element.proto.imgs
    ]
    assert "题 1 的第 1 张原图参考" in alt_values
    assert "题 1 的第 1 张 TikZ 重绘图" in alt_values


def test_import_review_recompile_and_accept_buttons_call_document_service(tmp_path):
    reference = _image(tmp_path / "interaction-reference.png")
    redraw = _image(tmp_path / "interaction-redraw.png")
    spec = {
        "kind": "geometry",
        "semantic_spec": {"points": ["A", "B"]},
        "tikz_source": "\\begin{tikzpicture}\\draw (0,0)--(1,1);\\end{tikzpicture}",
        "render_path": redraw,
        "crop_path": reference,
        "validation_status": "fallback",
        "validation_issues": ["角度需核对"],
        "assumptions": [],
    }
    segment = {
        "number": "1",
        "text": "如图，求角度。",
        "page_start": 1,
        "page_end": 1,
        "source": "visual",
        "needs_review": True,
        "error": None,
        "analysis": {"analysis": "解析", "answer": "30°"},
        "figure_paths": [reference],
        "figure_specs": [spec],
    }
    at = _run(
        "import streamlit as st\n"
        "from frontend.pages.import_doc import _render_segment\n"
        "class FakeService:\n"
        "    def recompile_segment_figure(self, job_id, user_id, index, figure_index, *, tikz_source):\n"
        "        st.session_state['recompile_call'] = (job_id, user_id, index, figure_index, tikz_source)\n"
        "    def accept_segment_figure(self, job_id, user_id, index, figure_index):\n"
        "        st.session_state['accept_call'] = (job_id, user_id, index, figure_index)\n"
        f"_render_segment(FakeService(), {{'id': 1}}, 'job', 0, {segment!r}, [None])\n"
    )

    assert not at.exception
    recompile = next(
        button for button in at.form_submit_button if "重新编译并对比" in button.label
    )
    recompile.click().run()

    assert not at.exception
    assert at.session_state["recompile_call"] == (
        "job",
        1,
        0,
        0,
        spec["tikz_source"],
    )

    at.button(key="accept_figure_job_0_0").click().run()

    assert not at.exception
    assert at.session_state["accept_call"] == ("job", 1, 0, 0)


def test_generated_variant_save_uses_new_figures_and_preserves_k12_metadata(tmp_path):
    source_figure = _image(tmp_path / "source.png")
    variant_figure = _image(tmp_path / "variant.png")
    variant_spec = {
        "kind": "geometry",
        "semantic_spec": {"points": ["D", "E"]},
        "tikz_source": "variant tikz",
        "render_path": variant_figure,
        "crop_path": None,
        "validation_status": "ai_passed",
        "validation_issues": [],
        "assumptions": ["变式示意图"],
    }
    at = _run(
        "import streamlit as st\n"
        "from types import SimpleNamespace\n"
        "from backend.models.schemas import QuestionOut\n"
        "from backend.services.geometry_figures import GeneratedVariant\n"
        "from backend.services.question_mixins import QueryMixin\n"
        "from frontend.pages.tutor import _render_analysis\n"
        f"source_figure = {source_figure!r}\n"
        f"variant_figure = {variant_figure!r}\n"
        f"variant_spec = {variant_spec!r}\n"
        "saved = QuestionOut(\n"
        "    id=1, user_id=1, image_path=None, figure_paths=[source_figure],\n"
        "    content_markdown='原题', answer='2', knowledge_points=['几何'],\n"
        "    difficulty='hard', subject='math', grade=8, region='北京',\n"
        "    textbook_version='人教版', question_type='解答题', chapter='三角形',\n"
        "    error_category='概念不清',\n"
        ")\n"
        "candidate = QueryMixin._generated_as_question(\n"
        "    saved,\n"
        "    GeneratedVariant(\n"
        "        content='带新图的 AI 变式',\n"
        "        figure_paths=[variant_figure],\n"
        "        figure_specs=[variant_spec],\n"
        "    ),\n"
        ")\n"
        "class FakeService:\n"
        "    def similar_questions(self, *args, **kwargs):\n"
        "        return SimpleNamespace(items=[candidate])\n"
        "    def create_manual_question(self, user_id, **kwargs):\n"
        "        st.session_state['followup_save'] = {'user_id': user_id, **kwargs}\n"
        "        return SimpleNamespace(id=88)\n"
        "analysis = SimpleNamespace(\n"
        "    question_type='解答题', chapter='几何', error_category=None,\n"
        "    knowledge_points=['几何'], analysis='解析', answer='2', mistake_cause='',\n"
        "    followup_question='',\n"
        ")\n"
        "_render_analysis(saved, analysis, FakeService(), {'id': 1})\n"
    )
    assert not at.exception
    save_button = next(button for button in at.button if "变式题存入错题本" in button.label)
    save_button.click().run()

    assert not at.exception
    saved = at.session_state["followup_save"]
    assert saved["figure_paths"] == [variant_figure]
    assert saved["figure_specs"] == [variant_spec]
    assert saved["figure_paths"] != [source_figure]
    assert saved["knowledge_points"] == ["几何"]
    assert saved["difficulty"] == "hard"
    assert saved["subject"] == "math"
    assert saved["grade"] == 8
    assert saved["region"] == "北京"
    assert saved["textbook_version"] == "人教版"
    assert saved["question_type"] == "解答题"
    assert saved["chapter"] == "三角形"
    assert saved["error_category"] == "概念不清"
