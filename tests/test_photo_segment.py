"""拍照切题录入（从图中找题）测试：候选整形、回退判定、默认勾选、逐题入库。

全部走 MockProvider / 测试替身，不调真实 API。
界面层逻辑以纯函数（tutor.py 中无 Streamlit 依赖的部分）为目标。
"""
from __future__ import annotations

import io
from pathlib import Path

from PIL import Image

from backend.services.ai.mock import MockProvider
from frontend.pages.tutor import (
    _plan_photo_entry,
    _save_checked_candidates,
    build_candidates,
    default_checked,
    should_use_segmentation,
    summarize_stem,
)


def _jpeg_bytes(color=(255, 255, 255)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (80, 80), color).save(buf, "JPEG")
    return buf.getvalue()


# ---------- 回退判定 ----------

def test_should_use_segmentation_requires_two_or_more():
    assert should_use_segmentation([{"number": "1"}, {"number": "2"}]) is True
    assert should_use_segmentation([{"number": "1"}]) is False
    assert should_use_segmentation([]) is False
    assert should_use_segmentation(None) is False


# ---------- 候选整形与默认勾选 ----------

def test_build_candidates_labels_source_image_and_defaults():
    segments = [
        {"number": "3", "text": "  已知   x^2=9，求 x。 ", "likely_wrong": True},
        {"number": "4", "text": "计算 1+1。"},  # 无 likely_wrong 字段
    ]
    candidates = build_candidates("homework.jpg", segments)

    assert [c["image"] for c in candidates] == ["homework.jpg", "homework.jpg"]
    assert candidates[0]["number"] == "3"
    assert candidates[0]["text"] == "已知   x^2=9，求 x。"  # 仅去首尾空白，内部保留
    assert candidates[0]["summary"] == "已知 x^2=9，求 x。"  # 摘要压缩空白
    assert candidates[0]["likely_wrong"] is True
    assert candidates[1]["likely_wrong"] is False  # 缺省 False
    assert default_checked(candidates[0]) is True
    assert default_checked(candidates[1]) is False


def test_summarize_stem_collapses_whitespace_and_truncates():
    assert summarize_stem("  已知\n x^2=9 ") == "已知 x^2=9"
    long_text = "题" * 100
    summary = summarize_stem(long_text, limit=50)
    assert len(summary) == 51 and summary.endswith("…")


# ---------- 切题计划（多图合并 + 回退） ----------

class _SingleQuestionProvider(MockProvider):
    def segment_page(self, image_bytes, mime_type="image/png"):
        return [{"number": "1", "text": "唯一一道题", "likely_wrong": False}]


class _BrokenProvider(MockProvider):
    def segment_page(self, image_bytes, mime_type="image/png"):
        raise RuntimeError("model down")


def test_plan_multi_question_image_yields_candidates():
    plan = _plan_photo_entry(MockProvider(), [("a.jpg", b"img-a", "image/jpeg")])
    assert len(plan["candidates"]) == 2
    assert plan["fallbacks"] == []
    assert all(c["image"] == "a.jpg" for c in plan["candidates"])
    assert all(c["image_bytes"] == b"img-a" for c in plan["candidates"])
    # MockProvider 演示数据：第 1 题疑似做错
    assert [c["likely_wrong"] for c in plan["candidates"]] == [True, False]


def test_plan_single_question_image_falls_back():
    plan = _plan_photo_entry(
        _SingleQuestionProvider(), [("one.jpg", b"img-1", "image/jpeg")]
    )
    assert plan["candidates"] == []
    assert [f["name"] for f in plan["fallbacks"]] == ["one.jpg"]


def test_plan_segment_exception_falls_back():
    plan = _plan_photo_entry(_BrokenProvider(), [("bad.jpg", b"img-b", "image/jpeg")])
    assert plan["candidates"] == []
    assert [f["name"] for f in plan["fallbacks"]] == ["bad.jpg"]


def test_plan_merges_candidates_across_images():
    plan = _plan_photo_entry(
        MockProvider(),
        [("p1.jpg", b"img-1", "image/jpeg"), ("p2.jpg", b"img-2", "image/jpeg")],
    )
    assert len(plan["candidates"]) == 4
    assert [c["image"] for c in plan["candidates"]] == ["p1.jpg", "p1.jpg", "p2.jpg", "p2.jpg"]


# ---------- 逐题解构入库（MockProvider + 真实落库） ----------

def test_checked_candidates_saved_per_question(question_service, student_user, tmp_path):
    """多题图片 → 切题 → 全选 → 逐题入库：题目数、来源、原图路径正确。"""
    image_bytes = _jpeg_bytes()
    plan = _plan_photo_entry(
        question_service.ai, [("paper.jpg", image_bytes, "image/jpeg")]
    )
    assert len(plan["candidates"]) == 2

    context = {"subject": "math", "grade": 9, "region": None, "textbook_version": None}
    results = _save_checked_candidates(
        question_service, student_user.id, plan["candidates"], ["期末"], "hint", context
    )

    assert [err for _, _, err in results] == [None, None]
    entries = [r for _, r, _ in results]
    questions = [e.question for e in entries]

    # 逐题入库：2 道题、来源 ai、共享同一份落盘原图
    assert len({q.id for q in questions}) == 2
    assert all(q.source == "ai" for q in questions)
    image_paths = {q.image_path for q in questions}
    assert len(image_paths) == 1
    saved_path = image_paths.pop()
    assert saved_path and Path(saved_path).is_file()

    # 题干原文进 content（可回溯原题），K12 元数据随题落库
    assert "【演示切题】" in questions[0].content_markdown
    assert all(q.grade == 9 for q in questions)
    assert all("期末" in q.tags for q in questions)


def test_partial_selection_saves_only_checked(question_service, student_user):
    plan = _plan_photo_entry(question_service.ai, [("p.jpg", _jpeg_bytes(), "image/jpeg")])
    checked = [c for c in plan["candidates"] if default_checked(c)]
    assert len(checked) == 1  # 仅疑似做错的第 1 题

    context = {"subject": "math", "grade": None, "region": None, "textbook_version": None}
    results = _save_checked_candidates(
        question_service, student_user.id, checked, [], "", context
    )
    assert len(results) == 1 and results[0][2] is None
    assert "第 1 题" in results[0][0]


def test_analyze_text_and_save_rejects_empty_text(question_service, student_user):
    import pytest

    with pytest.raises(ValueError):
        question_service.analyze_text_and_save(student_user.id, "   ")
