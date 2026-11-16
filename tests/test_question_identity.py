from __future__ import annotations

import pytest

from backend.services.question_identity import normalize_stem, stem_hash


def test_normalize_stem_applies_unicode_nfc():
    assert normalize_stem("e\u0301 = x") == "\u00e9 = x"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (r"\(x+1\)", "$x+1$"),
        (r"\[x+1\]", "$$x+1$$"),
    ],
)
def test_normalize_stem_normalizes_latex_delimiters(raw: str, expected: str):
    assert normalize_stem(raw) == expected


def test_normalize_stem_removes_outer_question_number():
    assert normalize_stem("12. 求 x 的值") == "求 x 的值"


def test_normalize_stem_removes_outer_question_number_without_space():
    assert normalize_stem("12.求 x 的值") == "求 x 的值"


def test_normalize_stem_preserves_leading_decimal():
    assert normalize_stem("1.5 + x = 2") == "1.5 + x = 2"


def test_normalize_stem_unifies_newlines_and_collapses_whitespace():
    assert normalize_stem("\r\n  计算   x + 1 \n\n") == "计算 x + 1"


def test_normalize_stem_unifies_formatting_without_changing_math_values():
    left = "(2026秋) 如图，棱长均为 $2$（ ） A、$4$"
    right = "（2026秋）如图，棱长均为2（ ） A、4"
    assert normalize_stem(left) == normalize_stem(right)
    assert stem_hash("$2$") != stem_hash("$3$")


def test_stem_hash_uses_utf8_sha256_of_normalized_stem():
    assert stem_hash("  x=1  ") == "1f206b11c23e28cc250ded7fc0098d3823a8467a54340f1ac4e535cb8544493f"


@pytest.mark.parametrize(
    ("left", "right"),
    [("x=1", "x=2"), ("A", "a"), ("x+y", "x-y")],
)
def test_stem_hash_preserves_mathematical_differences(left: str, right: str):
    assert stem_hash(left) != stem_hash(right)


@pytest.mark.parametrize("raw", ["", " \n\t "])
def test_normalize_stem_rejects_empty_stem(raw: str):
    with pytest.raises(ValueError):
        normalize_stem(raw)
