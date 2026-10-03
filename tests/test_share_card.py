"""错题分享卡片测试。"""
from __future__ import annotations

import io
import os

import pytest
from PIL import Image, ImageFont

from backend.config import get_settings
from backend.models.schemas import QuestionOut
from backend.services import share_card
from backend.services.share_card import has_cjk_font, render_share_card

needs_cjk = pytest.mark.skipif(not has_cjk_font(), reason="无中文字体（Linux CI）")


def _question() -> QuestionOut:
    return QuestionOut(
        id=1,
        user_id=1,
        image_path=None,
        content_markdown="### 题目\n已知 $x^2=9$，求 x 的值。\n### 解析\n开平方得两个解。",
        answer="x=±3",
        tags=["方程", "代数"],
        difficulty="medium",
        created_at=None,
    )


@needs_cjk
def test_share_card_renders_png():
    stream = render_share_card(_question())
    data = stream.getvalue()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    image = Image.open(io.BytesIO(data))
    assert image.width >= 1000
    assert image.height >= 500  # 卡片有实际内容高度


@needs_cjk
def test_share_card_handles_long_content():
    question = _question().model_copy(
        update={"content_markdown": "这是一段很长的题面。" * 60, "answer": "长答案" * 30}
    )
    stream = render_share_card(question)
    image = Image.open(stream)
    # 内容更长 → 卡片自动加高，不截断
    short = Image.open(io.BytesIO(render_share_card(_question()).getvalue()))
    assert image.height > short.height


def _existing_platform_font() -> str | None:
    """当前平台上真实存在的候选字体（用于配置项测试）。"""
    for path in share_card._font_candidates():
        if os.path.exists(path):
            return path
    return None


@pytest.fixture
def _reset_settings_cache():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_share_card_font_path_config_takes_priority(monkeypatch, _reset_settings_cache):
    """SHARE_CARD_FONT_PATH 配置后成为第一候选并被实际加载。"""
    font_path = _existing_platform_font()
    if font_path is None:
        pytest.skip("当前环境无任何候选中文字体")
    monkeypatch.setenv("SHARE_CARD_FONT_PATH", font_path)
    get_settings.cache_clear()

    assert share_card._font_candidates()[0] == str(get_settings().share_card_font_path)
    font = share_card._font(24)
    assert isinstance(font, ImageFont.FreeTypeFont)
    assert font.path == font_path


def test_share_card_font_path_invalid_falls_back(monkeypatch, _reset_settings_cache, tmp_path):
    """配置指向不存在的字体文件时，回退到平台探测/PIL 默认，不抛异常。"""
    monkeypatch.setenv("SHARE_CARD_FONT_PATH", str(tmp_path / "no-such-font.ttf"))
    get_settings.cache_clear()

    font = share_card._font(24)
    assert font is not None  # 回退成功（平台字体或 PIL 默认）
