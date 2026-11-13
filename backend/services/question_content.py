"""错题题面与解析的共享拆分规则。"""
from __future__ import annotations

_STEM_SEP = "\n\n---\n\n"
_DOCUMENT_STEM_PREFIX = "### 题目\n"
_DOCUMENT_ANALYSIS_SEP = "\n\n### 解析\n"


def split_stem_and_analysis(content_markdown: str) -> tuple[str, str | None]:
    """拆分切题录入或整卷导入的（题干, 解析）；无分隔符的旧题返回（原文, None）。

    只按第一个分隔符拆分（解析里若再出现分隔符归解析段）；两段各自 strip，
    解析段为空视为无解析（返回 None），由调用方回落到全文展示。
    """
    if content_markdown.startswith(_DOCUMENT_STEM_PREFIX):
        stem, marker, analysis = content_markdown.partition(_DOCUMENT_ANALYSIS_SEP)
        if marker:
            return stem.removeprefix(_DOCUMENT_STEM_PREFIX).strip(), analysis.strip() or None
    if _STEM_SEP not in content_markdown:
        return content_markdown.strip(), None
    stem, _, analysis = content_markdown.partition(_STEM_SEP)
    return stem.strip(), analysis.strip() or None
