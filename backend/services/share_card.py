"""错题分享卡片：把一道错题渲染为精美的可分享 PNG。"""
from __future__ import annotations

import io
import os
import re
import sys

from PIL import Image, ImageDraw, ImageOps

from backend.config import get_settings
from backend.services.ai.base import normalize_math_delimiters
from backend.utils.logging import get_logger

logger = get_logger("share_card")

_CARD_W = 1080
_MARGIN = 60
_COLORS = {
    "bg": "#ffffff",
    "title": "#1a365d",
    "text": "#334155",
    "muted": "#94a3b8",
    "accent": "#2563eb",
    "soft": "#eff6ff",
    "border": "#e2e8f0",
}


def _font_candidates(bold: bool = False) -> list[str]:
    """字体候选清单：配置项优先，其次按平台探测常见中文字体路径。"""
    candidates: list[str] = []
    configured = get_settings().share_card_font_path
    if configured is not None:
        candidates.append(str(configured))
    if sys.platform.startswith("win"):
        candidates += [
            r"C:\Windows\Fonts\msyhbd.ttc" if bold else r"C:\Windows\Fonts\msyh.ttc",
            r"C:\Windows\Fonts\simhei.ttf",
        ]
    elif sys.platform == "darwin":
        candidates.append("/System/Library/Fonts/PingFang.ttc")
    else:
        candidates += [
            "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        ]
    return candidates


def _font(size: int, bold: bool = False):
    """按候选清单挑选可用中文字体；找不到则用 PIL 默认（可能不支持中文）。"""
    from PIL import ImageFont

    for path in _font_candidates(bold):
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                logger.warning("字体加载失败，尝试下一个候选: %s", path)
                continue
    return ImageFont.load_default(size=size)


def has_cjk_font() -> bool:
    """当前环境是否有可渲染中文的字体（决定测试断言与卡片回退样式）。"""
    return any(os.path.exists(p) for p in _font_candidates())


_MATH_RE = re.compile(r"\$\$(.+?)\$\$|\$(.+?)\$", re.DOTALL)
_MATH_SCALE = 1.5
_TEXT_LINE_H = 46
_MATH_LINE_PADDING = 12
_LATEX_SYMBOLS = {
    "circ": "°", "cdot": "·", "times": "×", "div": "÷", "pm": "±",
    "le": "≤", "leq": "≤", "ge": "≥", "geq": "≥", "ne": "≠", "neq": "≠",
    "pi": "π", "infty": "∞", "angle": "∠", "perp": "⊥", "parallel": "∥",
    "sin": "sin", "cos": "cos", "tan": "tan", "log": "log", "ln": "ln",
}


def _readable_math(latex: str) -> str:
    text = latex.replace(r"^\circ", "°").replace(r"^{\circ}", "°")
    text = re.sub(r"\\[dt]?frac\{([^{}]*)\}\{([^{}]*)\}", r"(\1)/(\2)", text)
    text = re.sub(r"\\sqrt\{([^{}]*)\}", r"√(\1)", text)
    text = re.sub(r"\\(?:overrightarrow|vec)\s*\{([^{}]*)\}", r"\1⃗", text)
    text = re.sub(r"\\(?:overrightarrow|vec)\s+([A-Za-z0-9]+)", r"\1⃗", text)
    text = re.sub(
        r"\\([A-Za-z]+)",
        lambda match: _LATEX_SYMBOLS.get(match.group(1), match.group(1)),
        text,
    )
    return text.replace("{", "").replace("}", "").replace(r"\,", " ")


class _MathRenderer:
    def __init__(self) -> None:
        self._renderer = None
        self._cache: dict[tuple[str, int], Image.Image] = {}

    def render(self, latex: str, max_width: int) -> Image.Image:
        key = (latex, max_width)
        if key in self._cache:
            return self._cache[key]
        if self._renderer is None:
            from backend.services.tikz import TikzRenderer

            self._renderer = TikzRenderer.from_settings(get_settings())
        source = (
            "\\begin{tikzpicture}\n"
            "\\node[inner sep=0pt, outer sep=0pt] {$" + latex + "$};\n"
            "\\end{tikzpicture}"
        )
        rendered = self._renderer.render(source)
        with Image.open(rendered.png_path) as source_image:
            alpha = ImageOps.invert(source_image.convert("L"))
        bbox = alpha.getbbox()
        if bbox is None:
            raise ValueError("公式渲染结果为空")
        alpha = alpha.crop(bbox)
        formula = Image.new("RGBA", alpha.size, _COLORS["text"])
        formula.putalpha(alpha)
        scale = min(_MATH_SCALE, max_width / formula.width)
        formula = formula.resize(
            (
                max(1, round(formula.width * scale)),
                max(1, round(formula.height * scale)),
            ),
            Image.Resampling.LANCZOS,
        )
        self._cache[key] = formula
        return formula


def _line_height(line: list[tuple[str, object]]) -> int:
    return max(
        [_TEXT_LINE_H]
        + [
            value.height + _MATH_LINE_PADDING
            for kind, value in line
            if kind == "math"
        ]
    )


def _layout_rich(
    draw: ImageDraw.ImageDraw,
    text: str,
    font,
    max_width: int,
    math_renderer: _MathRenderer,
) -> list[list[tuple[str, object]]]:
    text = normalize_math_delimiters(text)
    for token in ("###", "**", "`"):
        text = text.replace(token, "")
    lines: list[list[tuple[str, object]]] = []
    for paragraph in text.split("\n"):
        if not paragraph.strip() or paragraph.strip() == "---":
            continue
        atoms: list[str | Image.Image] = []
        cursor = 0
        for match in _MATH_RE.finditer(paragraph):
            atoms.extend(paragraph[cursor : match.start()])
            latex = (match.group(1) or match.group(2) or "").strip()
            try:
                atoms.append(math_renderer.render(latex, max_width))
            except Exception as exc:  # noqa: BLE001 - 无 TeX 时仍生成可读卡片
                logger.warning("分享卡片公式渲染失败，使用文本回退: %s", exc)
                atoms.extend(_readable_math(latex))
            cursor = match.end()
        atoms.extend(paragraph[cursor:])

        line: list[tuple[str, object]] = []
        line_width = 0.0
        for atom in atoms:
            is_text = isinstance(atom, str)
            width = (
                draw.textlength(atom, font=font) if is_text else atom.width
            )
            if line and line_width + width > max_width:
                lines.append(line)
                line = []
                line_width = 0.0
            if is_text:
                if line and line[-1][0] == "text":
                    line[-1] = ("text", str(line[-1][1]) + atom)
                else:
                    line.append(("text", atom))
            else:
                line.append(("math", atom))
            line_width += width
        lines.append(line)
    return lines or [[]]


def _rich_height(lines: list[list[tuple[str, object]]]) -> int:
    return sum(_line_height(line) for line in lines)


def _draw_rich(
    image: Image.Image,
    lines: list[list[tuple[str, object]]],
    x: int,
    y: int,
    font,
) -> None:
    draw = ImageDraw.Draw(image)
    for line in lines:
        height = _line_height(line)
        cursor_x = x
        for kind, value in line:
            if kind == "text":
                text = str(value)
                draw.text(
                    (cursor_x, y + (height - _TEXT_LINE_H) // 2),
                    text,
                    font=font,
                    fill=_COLORS["text"],
                )
                cursor_x += round(draw.textlength(text, font=font))
            else:
                formula = value
                image.paste(
                    formula,
                    (cursor_x, y + (height - formula.height) // 2),
                    formula,
                )
                cursor_x += formula.width
        y += height


def render_share_card(question, base_url: str = "") -> io.BytesIO:
    """渲染错题分享卡片，返回 PNG 字节流。"""
    draw_probe = ImageDraw.Draw(Image.new("RGB", (10, 10)))
    font_title = _font(44, bold=True)
    font_meta = _font(26)
    font_body = _font(30)
    font_small = _font(22)

    math_renderer = _MathRenderer()
    content_lines = _layout_rich(
        draw_probe,
        (question.content_markdown or "").strip() or "（无题面）",
        font_body,
        _CARD_W - 2 * _MARGIN,
        math_renderer,
    )
    answer_lines = _layout_rich(
        draw_probe,
        question.answer or "—",
        font_body,
        _CARD_W - 2 * _MARGIN - 48,
        math_renderer,
    )
    footer = "智能错题本 · AI 学习助手"

    body_h = _rich_height(content_lines) + 24
    answer_h = _rich_height(answer_lines) + 70
    card_h = _MARGIN * 3 + 60 + 44 + body_h + answer_h + 60 + 40

    image = Image.new("RGB", (_CARD_W, card_h), _COLORS["bg"])
    draw = ImageDraw.Draw(image)

    # 顶部品牌条
    draw.rectangle([0, 0, _CARD_W, 10], fill=_COLORS["accent"])
    draw.text((_MARGIN, _MARGIN), "📘 今日错题", font=font_title, fill=_COLORS["title"])
    meta = f"{question.difficulty} · {' / '.join((question.tags or [])[:4])} · {question.created_at:%Y-%m-%d}" if question.created_at else question.difficulty
    draw.text((_MARGIN, _MARGIN + 62), meta, font=font_meta, fill=_COLORS["muted"])

    y = _MARGIN + 130
    _draw_rich(image, content_lines, _MARGIN, y, font_body)
    y += body_h - 8

    # 答案区块
    draw.rounded_rectangle(
        [_MARGIN, y, _CARD_W - _MARGIN, y + answer_h - 40],
        radius=14,
        fill=_COLORS["soft"],
        outline=_COLORS["border"],
    )
    draw.text((_MARGIN + 24, y + 18), "✅ 答案", font=font_meta, fill=_COLORS["accent"])
    _draw_rich(image, answer_lines, _MARGIN + 24, y + 60, font_body)

    # 底部
    draw.text((_MARGIN, card_h - _MARGIN - 26), footer, font=font_small, fill=_COLORS["muted"])

    stream = io.BytesIO()
    image.save(stream, "PNG")
    stream.seek(0)
    return stream
