"""把用户手册 Markdown 渲染为 PDF（reportlab + 可指定 CJK 字体）。

用法::

    python scripts/build_user_manual_pdf.py                      # 自动探测系统 CJK 字体
    python scripts/build_user_manual_pdf.py --font C:\\Windows\\Fonts\\msyh.ttc
    python scripts/build_user_manual_pdf.py --out dist_assets/USER_MANUAL.pdf

仅实现手册用到的 Markdown 子集：#/##/### 标题、段落、- 与 1. 列表、> 引用、
**加粗**、表格（按等宽文本行渲染）、--- 分隔线。手册编写时不要超出该子集。
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_MD = PROJECT_ROOT / "docs" / "USER_MANUAL.md"
DEFAULT_OUT = PROJECT_ROOT / "dist_assets" / "USER_MANUAL.pdf"

# 常见 CJK 字体候选（Linux Noto / Windows 微软雅黑 / macOS 苹方）
FONT_CANDIDATES = [
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/arphic/uming.ttc",
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/simhei.ttf",
    "/System/Library/Fonts/PingFang.ttc",
]


def _loadable(font_path: str) -> bool:
    """reportlab 只支持 TrueType 轮廓；Noto CJK 等 PostScript(CFF) 轮廓字体会被拒。"""
    from reportlab.pdfbase.ttfonts import TTFont

    try:
        TTFont("probe", font_path, subfontIndex=0)
        return True
    except Exception:  # noqa: BLE001 - 任何解析失败都视为不可用
        return False


def find_font(explicit: str | None) -> str:
    if explicit:
        if not Path(explicit).is_file():
            raise SystemExit(f"指定字体不存在: {explicit}")
        if not _loadable(explicit):
            raise SystemExit(f"指定字体不可用（需 TrueType 轮廓，Noto CJK 等 CFF 字体不支持）: {explicit}")
        return explicit
    for candidate in FONT_CANDIDATES:
        if Path(candidate).is_file() and _loadable(candidate):
            return candidate
    raise SystemExit("未找到可用的 CJK 字体，请用 --font 显式指定（如 msyh.ttc）")


def _inline(text: str) -> str:
    """转义 XML 后还原 **加粗**（platypus 的迷你标记）。"""
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)


def render_pdf(md_path: Path, out_path: Path, font_path: str) -> None:
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import cm
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.platypus import HRFlowable, Paragraph, SimpleDocTemplate, Spacer

    pdfmetrics.registerFont(TTFont("CJK", font_path, subfontIndex=0))
    pdfmetrics.registerFont(TTFont("CJK-Bold", font_path, subfontIndex=0))

    styles = {
        "h1": ParagraphStyle("h1", fontName="CJK-Bold", fontSize=18, spaceAfter=10),
        "h2": ParagraphStyle("h2", fontName="CJK-Bold", fontSize=14, spaceBefore=10, spaceAfter=6),
        "h3": ParagraphStyle("h3", fontName="CJK-Bold", fontSize=12, spaceBefore=8, spaceAfter=4),
        "body": ParagraphStyle("body", fontName="CJK", fontSize=10.5, leading=16, spaceAfter=5),
        "quote": ParagraphStyle(
            "quote", fontName="CJK", fontSize=10, leading=15, leftIndent=12,
            textColor="#555555", spaceAfter=5,
        ),
        "mono": ParagraphStyle("mono", fontName="CJK", fontSize=9.5, leading=14, spaceAfter=2),
    }

    story: list = []
    in_table = False
    for raw in md_path.read_text(encoding="utf-8").splitlines():
        line = raw.rstrip()
        if not line:
            in_table = False
            continue
        if line.startswith("### "):
            story.append(Paragraph(_inline(line[4:]), styles["h3"]))
        elif line.startswith("## "):
            story.append(Paragraph(_inline(line[3:]), styles["h2"]))
        elif line.startswith("# "):
            story.append(Paragraph(_inline(line[2:]), styles["h1"]))
        elif line.startswith("---"):
            story.append(HRFlowable(width="100%", thickness=0.6, color="#999999"))
            story.append(Spacer(1, 0.15 * cm))
        elif line.startswith(">"):
            story.append(Paragraph(_inline(line.lstrip("> ")), styles["quote"]))
        elif line.startswith("|"):
            # 表格按文本行渲染；分隔行（|---|）跳过
            if set(line) <= set("|-: "):
                continue
            story.append(Paragraph(_inline(line), styles["mono"]))
            in_table = True
        elif re.match(r"^(-|\d+\.)\s", line):
            story.append(Paragraph(_inline(line), styles["mono" if in_table else "body"]))
        else:
            story.append(Paragraph(_inline(line), styles["body"]))

    out_path.parent.mkdir(parents=True, exist_ok=True)
    doc = SimpleDocTemplate(
        str(out_path),
        pagesize=A4,
        leftMargin=2 * cm,
        rightMargin=2 * cm,
        title="Issues Manager 用户手册",
    )
    doc.build(story)


def main() -> None:
    # Windows en-US 控制台（cp1252）打印中文会 UnicodeEncodeError，降级替换保证不崩
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except Exception:  # noqa: BLE001 - 非文本流时忽略
            pass

    parser = argparse.ArgumentParser(description="用户手册 Markdown → PDF")
    parser.add_argument("--md", default=str(DEFAULT_MD))
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--font", default=None, help="CJK 字体路径（.ttf/.ttc）")
    args = parser.parse_args()

    font = find_font(args.font)
    render_pdf(Path(args.md), Path(args.out), font)
    print(f"PDF 已生成（字体 {font}）: {args.out}")


if __name__ == "__main__":
    main()
