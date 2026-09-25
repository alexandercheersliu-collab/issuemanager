"""文档解析层：把整卷 PDF / DOCX 解析为逐页结构（DocPage），供切题管线使用。

- PDF（PyMuPDF）：逐页提取文本层 + 渲染页图像（≥200 DPI，存入工作目录），
  文本层过薄的页判定为扫描版（只能走视觉切题）。
- DOCX（python-docx）：按文档顺序提取段落文本，内嵌图片落盘并在文本流中
  留下 ``[图片:相对路径]`` 锚点；DOCX 无分页概念，整体视为一页。

任何一步失败抛 ValueError（调用方转成任务失败），不静默吞错。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from backend.utils.logging import get_logger

logger = get_logger("document")

# 页文本层少于此字符数判定为扫描版（残缺的文字层通常只有页眉页脚）
_SCANNED_TEXT_THRESHOLD = 20
# 页图像渲染分辨率（DPI）
_RENDER_DPI = 200

SUPPORTED_SUFFIXES = {".pdf", ".docx"}

IMAGE_ANCHOR_RE = re.compile(r"\[图片:([^\]]+)\]")


@dataclass
class DocPage:
    """一页（PDF）或整篇（DOCX）文档的解析产物。"""

    page_no: int  # 1 起始；DOCX 恒为 1
    text: str  # 文本层 / 段落文本（含 [图片:...] 锚点）
    image_path: Path | None  # 渲染的页图像（DOCX 为 None）
    is_scanned: bool  # True = 扫描版，只能走视觉切题
    images: list[Path] = field(default_factory=list)  # 页内/文档内嵌图片（已落盘）


def parse_document(path: str | Path, work_dir: str | Path) -> list[DocPage]:
    """解析文档为 DocPage 列表；页图像与内嵌图片落 work_dir（自动创建）。"""
    path = Path(path)
    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return _parse_pdf(path, work_dir)
    if suffix == ".docx":
        return _parse_docx(path, work_dir)
    raise ValueError(f"不支持的文档格式: {suffix}（支持 {sorted(SUPPORTED_SUFFIXES)}）")


# ---------------- PDF ----------------

def _parse_pdf(path: Path, work_dir: Path) -> list[DocPage]:
    try:
        import pymupdf
    except ImportError as exc:  # pragma: no cover - 依赖缺失时给出清晰指引
        raise ValueError("解析 PDF 需要安装 pymupdf: pip install pymupdf") from exc

    try:
        doc = pymupdf.open(path)
    except Exception as exc:  # noqa: BLE001 - 统一成可上报的解析错误
        raise ValueError(f"PDF 打开失败: {exc}") from exc

    zoom = _RENDER_DPI / 72.0
    matrix = pymupdf.Matrix(zoom, zoom)
    pages: list[DocPage] = []
    try:
        for index in range(doc.page_count):
            page = doc.load_page(index)
            text = page.get_text("text") or ""
            is_scanned = len(text.strip()) < _SCANNED_TEXT_THRESHOLD

            image_path = work_dir / f"page-{index + 1}.png"
            pixmap = page.get_pixmap(matrix=matrix)
            pixmap.save(image_path)

            pages.append(
                DocPage(
                    page_no=index + 1,
                    text=text.strip(),
                    image_path=image_path,
                    is_scanned=is_scanned,
                )
            )
    finally:
        doc.close()
    logger.info("PDF 解析完成: %s 页（扫描版 %s 页）", len(pages), sum(p.is_scanned for p in pages))
    return pages


# ---------------- DOCX ----------------

def _parse_docx(path: Path, work_dir: Path) -> list[DocPage]:
    try:
        import docx
        from docx.oxml.ns import qn
    except ImportError as exc:  # pragma: no cover
        raise ValueError("解析 DOCX 需要安装 python-docx: pip install python-docx") from exc

    try:
        document = docx.Document(path)
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"DOCX 打开失败: {exc}") from exc

    images_dir = work_dir / "images"
    images_dir.mkdir(parents=True, exist_ok=True)

    lines: list[str] = []
    images: list[Path] = []
    for paragraph in document.paragraphs:
        text = paragraph.text.strip()
        if text:
            lines.append(text)
        # 内嵌图片：按文档顺序落盘并留锚点（保留题目与配图的对应关系）
        for blip in paragraph._element.findall(f".//{qn('a:blip')}"):
            rel_id = blip.get(qn("r:embed"))
            if not rel_id or rel_id not in document.part.related_parts:
                continue
            part = document.part.related_parts[rel_id]
            ext = Path(part.partname).suffix or ".png"
            image_path = images_dir / f"img{len(images) + 1}{ext}"
            image_path.write_bytes(part.blob)
            images.append(image_path)
            lines.append(f"[图片:{image_path.relative_to(work_dir).as_posix()}]")

    text = "\n".join(lines)
    logger.info("DOCX 解析完成: %s 字符，%s 张内嵌图片", len(text), len(images))
    return [
        DocPage(
            page_no=1,
            text=text,
            image_path=None,
            is_scanned=False,
            images=images,
        )
    ]
