"""整卷导入校对界面的纯函数测试：统计与列表标题（不启动 Streamlit 运行时）。"""
from __future__ import annotations

from frontend.pages import import_doc
from frontend.pages.import_doc import (
    segment_headline,
    segment_preview_markdown,
    summarize_segments,
    validate_upload_selection,
)


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


def test_validate_upload_selection_allows_single_doc_and_multi_images():
    assert validate_upload_selection(["paper.pdf"]) is None
    assert validate_upload_selection(["paper.docx"]) is None
    assert validate_upload_selection(["page1.jpg"]) is None
    # 整卷拍照：多张图片（每张 = 一页）
    assert validate_upload_selection(["p1.jpg", "p2.png", "p3.webp"]) is None


def test_validate_upload_selection_rejects_multi_non_image():
    # PDF/DOCX 仅支持单文件；图 + 文档混搭同样拒绝
    assert validate_upload_selection(["a.pdf", "b.pdf"]) is not None
    assert validate_upload_selection(["p.png", "a.pdf"]) is not None
    assert validate_upload_selection(["a.docx", "b.docx"]) is not None


def test_validate_upload_selection_rejects_empty_and_unsupported():
    assert validate_upload_selection([]) is not None
    assert validate_upload_selection(["notes.txt"]) is not None
    assert validate_upload_selection(["p1.png", "notes.gif"]) is not None


def test_segment_headline_truncates_and_handles_empty():
    long_text = "题" * 100
    headline = segment_headline(_segment(text=long_text), max_len=24)
    assert "…" in headline
    assert len(headline) < 40

    empty = segment_headline(_segment(text=""))
    assert "（空题干）" in empty
    multiline = segment_headline(_segment(text="第一行\n第二行"))
    assert "\n" not in multiline


def test_segment_headline_does_not_break_math_delimiter():
    # 高中题干含 $...$，硬截断会切断定界符导致标题公式渲染破裂
    text = "若关于 $x$ 的不等式 $x^2+2(m-1)x+m^2-m<0$ 的解集为 $(x_1,x_2)$"
    headline = segment_headline(_segment(text=text), max_len=24)
    # 标题中的 $ 定界符必须成对出现（可渲染），不允许残留半个公式
    assert headline.count("$") % 2 == 0


def test_segment_preview_markdown_includes_all_rendered_sections():
    segment = _segment(
        text="若关于 $x$ 的不等式 $x^2<0$",
        analysis={
            "answer": "$m=-1$",
            "analysis": "由 $x\\in[m/3,n/2)$ 可得",
            "followup_question": "不等式组 $\\begin{cases}3x-a\\\\ge0\\end{cases}$ 的整数解",
        },
    )
    preview = segment_preview_markdown(segment)
    assert "若关于 $x$ 的不等式 $x^2<0$" in preview
    assert "**答案**：$m=-1$" in preview
    assert "**解析**：由 $x\\in[m/3,n/2)$ 可得" in preview
    assert "**变式练习**：不等式组" in preview


def test_segment_preview_markdown_skips_missing_fields():
    # 无解析结果（解构失败）时只渲染题干；完全为空返回空串
    assert segment_preview_markdown(_segment(analysis=None)) == "已知 x^2=9，求 x 的值。"
    assert segment_preview_markdown(_segment(text="", analysis=None)) == ""


def test_segment_page_image_uses_start_page():
    segment = _segment(page_start=2, page_end=2)
    assert import_doc.segment_page_image(["p1.jpg", "p2.jpg"], segment) == "p2.jpg"


def test_segment_page_image_for_cross_page_question_uses_start_page():
    segment = _segment(page_start=2, page_end=3)
    assert import_doc.segment_page_image(["p1.jpg", "p2.jpg", "p3.jpg"], segment) == "p2.jpg"


def test_segment_page_image_returns_none_when_unavailable():
    assert import_doc.segment_page_image([], _segment(page_start=1, page_end=1)) is None
    assert import_doc.segment_page_image([None], _segment(page_start=1, page_end=1)) is None
    assert import_doc.segment_page_image(["p1.jpg"], _segment(page_start=2, page_end=2)) is None
