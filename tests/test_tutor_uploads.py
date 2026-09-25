"""AI 录题页上传结果整形的回归测试。

历史 bug：`_process_uploads` 把 `analyze_and_save_dedup()` 返回的 `EntryResult`
当成 `(question, analysis)` 元组解包，界面上一点「开始 AI 解析」就抛
`TypeError: cannot unpack non-iterable EntryResult object`。
服务层测试覆盖不到 UI，故在此补一条纯函数级回归。
"""
from __future__ import annotations

from types import SimpleNamespace

from backend.services.entry_types import EntryResult
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
