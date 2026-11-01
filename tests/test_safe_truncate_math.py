"""定界符感知截断纯函数测试：safe_truncate_math。

列表/闪卡预览截断题干时不能切断 $...$ / $$...$$ 数学区间，
否则 KaTeX 渲染出现未闭合定界符，页面直接显示 LaTeX 源码乱码。
"""
from __future__ import annotations

from frontend.common import safe_truncate_math


def test_short_text_returned_unchanged():
    """长度不超限时原样返回，不追加省略号。"""
    text = "短文本 $x^2$"
    assert safe_truncate_math(text, 220) == text


def test_plain_text_truncation_appends_ellipsis():
    text = "abcdefghij" * 30
    assert safe_truncate_math(text, 220) == text[:220] + "…"


def test_cut_inside_inline_math_backs_off():
    """切点落在 $...$ 内部 → 回退到 $ 之前，避免半公式。"""
    text = "前情提要 " + "$x^2 + y^2 = z^2$" + " 后续展开" + "尾" * 300
    cut_at = text.index(" + y")
    result = safe_truncate_math(text, cut_at)
    assert result == text[: text.index("$")] + "…"
    assert "$" not in result.removesuffix("…")


def test_cut_after_closed_span_unaffected():
    """数学区间已完整闭合，切点在其后的普通文本 → 普通截断。"""
    text = "$a+b$ " + "很长的纯文本说明" * 40
    result = safe_truncate_math(text, 220)
    assert result == text[:220] + "…"


def test_cut_inside_block_math_backs_off():
    """$$...$$ 块级公式同理回退到 $$ 之前。"""
    text = "引入 " + "$$\\int_0^1 x^2 \\, dx$$" + " 与 " + "尾" * 300
    cut_at = text.index("x^2")
    result = safe_truncate_math(text, cut_at)
    assert result == text[: text.index("$$")] + "…"


def test_multiple_spans_backs_off_to_unclosed_one():
    """多个公式区间：前面的已闭合，只回退到未闭合区间的开口。"""
    text = "$a$ 与 $b$ 之后 " + "$x+y$" + "尾" * 300
    cut_at = text.index("+y")
    result = safe_truncate_math(text, cut_at)
    assert result == text[: text.index("$x+y$")] + "…"
    assert result.startswith("$a$ 与 $b$ 之后 ")


def test_escaped_dollar_not_a_delimiter():
    r"""\$ 是字面美元符号，不算数学定界符，切点在其后正常截断。"""
    text = r"价格是 \$5 美元，" + "说明" * 150
    result = safe_truncate_math(text, 220)
    assert result == text[:220] + "…"


def test_formula_at_position_zero_keeps_plain_cut():
    """公式从 0 开始且切点在其中：回退会得到空预览，保留普通截断。"""
    text = "$x^2 + y^2$ " + "尾" * 300
    result = safe_truncate_math(text, 8)
    assert result == text[:8] + "…"


def test_paren_and_bracket_math_forms():
    r"""\(...\) 与 \[...\] 定界符同样识别。"""
    text_paren = "前言 " + r"\(x+1\)" + "尾" * 300
    cut = text_paren.index("x+1")
    assert safe_truncate_math(text_paren, cut) == "前言 " + "…"

    text_bracket = "前言 " + r"\[x+1\]" + "尾" * 300
    cut = text_bracket.index("x+1")
    assert safe_truncate_math(text_bracket, cut) == "前言 " + "…"


def test_custom_ellipsis():
    text = "abcdefghij" * 30
    assert safe_truncate_math(text, 10, ellipsis="...") == "abcdefghij..."


def test_exact_length_boundary_unchanged():
    """长度恰好等于 max_len：未截断，不加省略号。"""
    text = "x" * 220
    assert safe_truncate_math(text, 220) == text
