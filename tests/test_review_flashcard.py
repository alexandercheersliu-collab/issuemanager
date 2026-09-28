"""闪卡题面/解析拆分纯函数测试：split_stem_and_analysis。"""
from __future__ import annotations

from frontend.pages.review import split_stem_and_analysis

SEP = "\n\n---\n\n"


def test_split_with_separator():
    """切题录入格式：题干 + 分隔线 + 解析 → 两段各归其位。"""
    content = f"计算 $3.7 \\div 3$ 的余数。{SEP}**1. 梳理错误原因** ……"
    stem, analysis = split_stem_and_analysis(content)
    assert stem == "计算 $3.7 \\div 3$ 的余数。"
    assert analysis == "**1. 梳理错误原因** ……"


def test_split_without_separator():
    """旧版整图录入：无分隔符 → （原文, None）。"""
    content = "**1. 梳理错误原因** 很多同学……"
    stem, analysis = split_stem_and_analysis(content)
    assert stem == content
    assert analysis is None


def test_split_only_first_separator():
    """解析段里再出现分隔线归解析段，不二次拆分。"""
    content = f"题干{SEP}解析上段{SEP}解析下段"
    stem, analysis = split_stem_and_analysis(content)
    assert stem == "题干"
    assert analysis == f"解析上段{SEP}解析下段"


def test_split_strips_whitespace():
    stem, analysis = split_stem_and_analysis(f"  题干  {SEP}  解析  ")
    assert stem == "题干"
    assert analysis == "解析"


def test_split_empty_analysis_returns_none():
    """分隔符后无解析内容：解析视为 None，调用方回落全文。"""
    stem, analysis = split_stem_and_analysis(f"题干{SEP}  ")
    assert stem == "题干"
    assert analysis is None


def test_split_empty_string():
    stem, analysis = split_stem_and_analysis("")
    assert stem == ""
    assert analysis is None
