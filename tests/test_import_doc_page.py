"""整卷导入校对界面的纯函数测试：统计与列表标题（不启动 Streamlit 运行时）。"""
from __future__ import annotations

from frontend.pages.import_doc import segment_headline, summarize_segments


def _segment(**overrides) -> dict:
    base = {
        "number": "1",
        "text": "已知 x^2=9，求 x 的值。",
        "needs_review": False,
        "error": None,
        "analysis": {"answer": "x=±3"},
    }
    base.update(overrides)
    return base


def test_summarize_segments_counts():
    segments = [
        _segment(),
        _segment(number="2", needs_review=True),
        _segment(number="3", analysis=None, error="模型超时", needs_review=True),
        _segment(number="4", analysis=None),
    ]
    summary = summarize_segments(segments)
    assert summary == {"total": 4, "needs_review": 2, "failed": 1, "importable": 2}


def test_summarize_segments_empty():
    assert summarize_segments([]) == {
        "total": 0,
        "needs_review": 0,
        "failed": 0,
        "importable": 0,
    }


def test_segment_headline_marks_needs_review():
    headline = segment_headline(_segment(needs_review=True))
    assert headline.startswith("⚠️ ")
    assert "1." in headline
    assert "求 x 的值" in headline

    ok = segment_headline(_segment())
    assert not ok.startswith("⚠️")


def test_segment_headline_truncates_and_handles_empty():
    long_text = "题" * 100
    headline = segment_headline(_segment(text=long_text), max_len=24)
    assert "…" in headline
    assert len(headline) < 40

    empty = segment_headline(_segment(text=""))
    assert "（空题干）" in empty
    multiline = segment_headline(_segment(text="第一行\n第二行"))
    assert "\n" not in multiline
