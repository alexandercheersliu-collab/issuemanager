"""AI 录题页上传结果整形的回归测试。

历史 bug：`_process_uploads` 把 `analyze_and_save_dedup()` 返回的 `EntryResult`
当成 `(question, analysis)` 元组解包，界面上一点「开始 AI 解析」就抛
`TypeError: cannot unpack non-iterable EntryResult object`。
服务层测试覆盖不到 UI，故在此补一条纯函数级回归。
"""
from __future__ import annotations

from types import SimpleNamespace

from backend.services.entry_types import EntryResult
from frontend.pages import tutor
from frontend.pages.tutor import _collect_entries


def _entry(*, duplicated: bool = False) -> EntryResult:
    question = SimpleNamespace(id=7, tags=["方程"], difficulty="medium", image_path="x.png")
    analysis = SimpleNamespace(
        knowledge_points=["一元二次方程"],
        analysis="### 解析\n由判别式…",
        answer="x=2 或 x=3",
        mistake_cause="符号错误",
        followup_question="变式：…",
    )
    return EntryResult(question=question, analysis=analysis, duplicated=duplicated)


def test_collect_entries_unpacks_entry_result_fields():
    """核心回归：EntryResult 必须按属性取，而不是当元组解包。"""
    entries = _collect_entries([("a.png", _entry(), None)])

    assert len(entries) == 1
    entry = entries[0]
    assert entry["name"] == "a.png"
    assert entry["error"] is None
    assert entry["duplicated"] is False
    assert entry["question"].id == 7
    assert entry["analysis"].answer == "x=2 或 x=3"


def test_collect_entries_marks_duplicates():
    entries = _collect_entries([("dup.png", _entry(duplicated=True), None)])
    assert entries[0]["duplicated"] is True


def test_collect_entries_keeps_per_file_error():
    entries = _collect_entries(
        [("bad.png", None, "AI 解析失败（已重试 3 次）: Connection error.")]
    )
    assert entries[0]["error"].startswith("AI 解析失败")
    assert "question" not in entries[0]


def test_collect_entries_handles_mixed_batch():
    entries = _collect_entries(
        [
            ("ok.png", _entry(), None),
            ("bad.png", None, "boom"),
            ("dup.png", _entry(duplicated=True), None),
        ]
    )
    assert [e["error"] is None for e in entries] == [True, False, True]
    assert entries[1]["error"] == "boom"
    assert entries[2]["duplicated"] is True


def test_collect_entries_rejects_raw_tuple_contract():
    """若服务层退回元组返回，这里应立刻暴露而不是静默产出错误页面。"""
    import pytest

    with pytest.raises(AttributeError):
        _collect_entries([("a.png", (SimpleNamespace(id=1), SimpleNamespace()), None)])


def test_start_analysis_is_disabled_while_entry_plan_is_processing():
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_string(
        "import io\n"
        "import streamlit as st\n"
        "from PIL import Image\n"
        "from types import SimpleNamespace\n"
        "from frontend.pages import tutor\n"
        "buffer = io.BytesIO()\n"
        "Image.new('RGB', (2, 2), 'white').save(buffer, format='JPEG')\n"
        "image_bytes = buffer.getvalue()\n"
        "class Upload:\n"
        "    name = 'one.jpg'\n"
        "    type = 'image/jpeg'\n"
        "    def getvalue(self):\n"
        "        return image_bytes\n"
        "tutor.st.file_uploader = lambda *args, **kwargs: [Upload()]\n"
        "class FakeAI:\n"
        "    def provider_info(self):\n"
        "        return SimpleNamespace(demo_mode=False)\n"
        "class FakeService:\n"
        "    ai = FakeAI()\n"
        "tutor.get_question_service = lambda: FakeService()\n"
        "tutor._process_plan = lambda *args, **kwargs: None\n"
        "st.session_state['photo_plan'] = {\n"
        "    'upload_names': ['one.jpg'], 'candidates': [],\n"
        "    'fallbacks': [{'name': 'one.jpg'}],\n"
        "}\n"
        "tutor.render_tutor_page({'id': 1})\n",
        default_timeout=15,
    )
    original_file_uploader = tutor.st.file_uploader
    original_get_service = tutor.get_question_service
    original_process_plan = tutor._process_plan
    try:
        at = at.run()
    finally:
        tutor.st.file_uploader = original_file_uploader
        tutor.get_question_service = original_get_service
        tutor._process_plan = original_process_plan

    assert not at.exception
    button = next(button for button in at.button if button.label == "开始 AI 解析")
    assert button.disabled is True


def test_followup_rerun_keeps_photo_entry_analysis(monkeypatch):
    import io

    from PIL import Image
    from streamlit.testing.v1 import AppTest

    buffer = io.BytesIO()
    Image.new("RGB", (2, 2), "white").save(buffer, format="JPEG")
    image_bytes = buffer.getvalue()

    class Upload:
        name = "one.jpg"
        type = "image/jpeg"

        def getvalue(self):
            return image_bytes

    question = SimpleNamespace(
        id=7,
        tags=["方程"],
        difficulty="medium",
        image_path=None,
        subject="math",
        grade=10,
        region="上海",
        knowledge_points=["一元二次方程"],
        question_type="解答题",
    )
    analysis = SimpleNamespace(
        question_type="解答题",
        chapter="方程",
        error_category=None,
        knowledge_points=["一元二次方程"],
        analysis="### 解析\n由判别式判断根的情况。",
        answer="x=2",
        mistake_cause=None,
        followup_question=None,
    )

    class FakeAI:
        def provider_info(self):
            return SimpleNamespace(demo_mode=False)

    class FakeService:
        ai = FakeAI()

        def analyze_and_save_dedup(self, *args, **kwargs):
            return EntryResult(question=question, analysis=analysis, duplicated=False)

        def similar_questions(self, *args, **kwargs):
            return SimpleNamespace(items=[])

        def answer_followup(self, *args, **kwargs):
            return "这是老师的追问答复。"

    monkeypatch.setattr(tutor.st, "file_uploader", lambda *args, **kwargs: [Upload()])
    monkeypatch.setattr(tutor, "get_question_service", lambda: FakeService())
    at = AppTest.from_string(
        """
from frontend.pages import tutor
tutor.render_tutor_page({"id": 1})
""",
        default_timeout=15,
    )
    at.session_state["photo_plan"] = {
        "upload_names": ["one.jpg"],
        "candidates": [],
        "fallbacks": [{
            "name": "one.jpg", "bytes": image_bytes,
            "mime": "image/jpeg", "segments": [],
        }],
    }
    at.run()

    assert not at.exception
    assert any("由判别式判断根的情况" in item.value for item in at.markdown)

    at.chat_input(key="chat_input_7").set_value("为什么用判别式？").run()

    assert not at.exception
    assert any("由判别式判断根的情况" in item.value for item in at.markdown)
    assert any("这是老师的追问答复" in item.value for item in at.markdown)


def test_k12_context_inputs_use_default_study_context():
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_string(
        "import streamlit as st\n"
        "from frontend.pages.tutor import _k12_context_inputs\n"
        "st.session_state['context_result'] = _k12_context_inputs('defaults')\n"
    ).run()

    assert not at.exception
    assert at.session_state["context_result"] == {
        "subject": "math",
        "grade": 10,
        "region": "上海",
        "textbook_version": "沪教版",
    }


def test_k12_context_inputs_can_be_locked_after_import():
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_string(
        "from frontend.pages.tutor import _k12_context_inputs\n"
        "_k12_context_inputs('locked', disabled=True)\n"
    )
    at.run()

    assert not at.exception
    assert all(widget.disabled for widget in at.selectbox)
    assert all(widget.disabled for widget in at.text_input)


def test_similar_constraints_come_from_saved_question():
    question = SimpleNamespace(
        subject="math",
        grade=10,
        region="上海",
        difficulty="hard",
        knowledge_points=["函数的单调性"],
        question_type="解答题",
    )

    assert tutor._similar_constraints(question) == {
        "subject": "math",
        "grade": 10,
        "region": "上海",
        "difficulty": "hard",
        "knowledge_points": ["函数的单调性"],
        "question_type": "解答题",
    }
