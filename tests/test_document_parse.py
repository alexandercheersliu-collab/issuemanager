"""文档解析层测试：文字版 PDF / 扫描版 PDF / DOCX 三类样本。"""
from __future__ import annotations

import pytest
from doc_samples import make_docx, make_scanned_pdf, make_text_pdf
from PIL import Image

from backend.services.document import parse_document


def test_parse_text_pdf(tmp_path):
    pdf = make_text_pdf(
        tmp_path / "text.pdf",
        [
            "1. 第一题题干：已知 x^2=9，求 x 的值。\n2. 第二题题干：计算 1+1 的结果。",
            "3. 第三题题干：求解方程 2x+3=7 中 x 的值。",
        ],
    )
    pages = parse_document(pdf, tmp_path / "work")

    assert len(pages) == 2
    assert pages[0].page_no == 1
    assert "第一题题干" in pages[0].text
    assert "第三题题干" in pages[1].text
    assert all(not p.is_scanned for p in pages)

    # 页图像已渲染且分辨率 ≥ 200 DPI（A4 宽 595pt → 200DPI 约 1653px）
    for page in pages:
        assert page.image_path is not None and page.image_path.is_file()
        with Image.open(page.image_path) as image:
            assert image.width >= 1600


def test_parse_scanned_pdf(tmp_path):
    pdf = make_scanned_pdf(tmp_path / "scan.pdf", page_count=2)
    pages = parse_document(pdf, tmp_path / "work")

    assert len(pages) == 2
    assert all(p.is_scanned for p in pages)
    assert all(p.text == "" for p in pages)
    assert all(p.image_path is not None and p.image_path.is_file() for p in pages)


def test_parse_docx_with_image_anchor(tmp_path):
    docx_path = make_docx(
        tmp_path / "paper.docx",
        ["1. 已知 x^2=9，求 x。", "2. 几何题见下图。", "3. 计算 1+1。"],
    )
    pages = parse_document(docx_path, tmp_path / "work")

    assert len(pages) == 1  # DOCX 无分页，整体一页
    page = pages[0]
    assert not page.is_scanned
    assert "已知 x^2=9" in page.text
    assert "几何题见下图" in page.text

    # 内嵌图片落盘 + 文本流留锚点
    assert len(page.images) == 1
    assert page.images[0].is_file()
    assert "[图片:images/img1.png]" in page.text


def test_parse_docx_without_image(tmp_path):
    docx_path = make_docx(tmp_path / "plain.docx", ["1. 题目一", "2. 题目二"], with_image=False)
    pages = parse_document(docx_path, tmp_path / "work")
    assert pages[0].images == []
    assert "[图片:" not in pages[0].text


def test_unsupported_suffix_raises(tmp_path):
    bogus = tmp_path / "paper.txt"
    bogus.write_text("hello", encoding="utf-8")
    with pytest.raises(ValueError, match="不支持的文档格式"):
        parse_document(bogus, tmp_path / "work")
