"""切题管线：把 DocPage 列表切成逐题 Segment。

双策略：
- 策略 A（视觉）：页图像送视觉模型，输出题号/题干/跨页标记（扫描版唯一通道）；
- 策略 B（规则）：文字版页用题号正则切分，保留公式与 [图片:...] 锚点。

合并校验：两路按题号序列对齐，一致则取规则文本（信息更全）+ 视觉跨页标记；
不一致的页标记 needs_review=True。最后按跨页标记自动拼接跨页题。
"""
from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

from backend.services.ai.base import BaseAIProvider
from backend.services.document import DocPage
from backend.utils.logging import get_logger

logger = get_logger("segment")

# 题号正则：1. / 1、 / 1． / （1） / (1) / 一、 / 十二、
_NUMBER_PATTERNS = [
    re.compile(r"^\s*(\d{1,2})\s*[.、．]\s*"),
    re.compile(r"^\s*[（(]\s*(\d{1,2})\s*[)）]\s*"),
    re.compile(r"^\s*([一二三四五六七八九十]{1,3})、\s*"),
]

_CN_NUMERALS = {
    "一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
    "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
}


@dataclass
class Segment:
    """切出的一道题（可能是跨页拼接产物）。"""

    number: str  # 规范化题号（阿拉伯数字字符串）
    text: str  # 题干文本（含 [图片:...] 锚点）
    page_start: int
    page_end: int
    needs_review: bool = False  # 双策略不一致 / 仅单路产出，需人工确认
    continued: bool = False  # True = 跨页拼接产物
    continues_to_next: bool = False  # 内部对齐用：题干延续到下一页
    continued_from_prev: bool = False  # 内部对齐用：本段是上一页题目的延续
    source: str = "rule"  # rule / visual
    figure_paths: list[str] = field(default_factory=list)
    figure_specs: list[dict] = field(default_factory=list)


def _cn_to_int(text: str) -> int | None:
    """中文数字（一到二十）转阿拉伯数字。"""
    if text in _CN_NUMERALS:
        return _CN_NUMERALS[text]
    if text.startswith("十") and len(text) == 2:  # 十一..十九
        tail = _CN_NUMERALS.get(text[1])
        return 10 + tail if tail else None
    if text.endswith("十") and len(text) == 2:  # 二十（三十…）
        head = _CN_NUMERALS.get(text[0])
        return head * 10 if head else None
    return None


def _match_number(line: str) -> str | None:
    """行首命中题号模式时返回规范化题号，否则 None。"""
    for pattern in _NUMBER_PATTERNS:
        match = pattern.match(line)
        if not match:
            continue
        token = match.group(1)
        if token.isdigit():
            return str(int(token))
        number = _cn_to_int(token)
        if number is not None:
            return str(number)
    return None


# ---------------- 策略 B：规则切题 ----------------

def rule_segment_page(text: str, page_no: int) -> list[Segment]:
    """按题号正则切分一页文本；公式与 [图片:...] 锚点随行保留在所属题内。

    题号必须单调递增才视为新题：「1. ……（1）第一小问」中的（1）是上题的小问，
    不切分；而整页（1）（2）（3）编号（序号从 1 开始递增）正常切分。
    """
    segments: list[Segment] = []
    current_lines: list[str] = []
    current_number: str | None = None
    last_number = 0

    def _flush() -> None:
        nonlocal current_lines, current_number
        if current_number is not None:
            segments.append(
                Segment(
                    number=current_number,
                    text="\n".join(current_lines).strip(),
                    page_start=page_no,
                    page_end=page_no,
                    source="rule",
                )
            )
        current_lines = []
        current_number = None

    for line in text.split("\n"):
        number = _match_number(line)
        if number is not None and int(number) > last_number:
            _flush()
            current_number = number
            last_number = int(number)
            current_lines = [line.strip()]
        elif current_number is not None:
            current_lines.append(line.rstrip())
        # 题号出现前的页眉/说明文字直接丢弃
    _flush()
    return segments


# ---------------- 策略 A：视觉切题 ----------------

def visual_segment_page(
    provider: BaseAIProvider,
    image_path,
    page_no: int,
    figures_dir=None,
    figure_service=None,
    context=None,
) -> list[Segment]:
    """页图像送视觉模型切题，图形坐标交给几何图形服务处理。"""
    from pathlib import Path

    image_path = Path(image_path)
    items = provider.segment_page(image_path.read_bytes(), "image/png")
    segments = []
    for item in items:
        figure_paths: list[str] = []
        figure_specs: list[dict] = []
        needs_review = False
        if figure_service is not None:
            outcome = figure_service.redraw_segment(
                image_path=image_path,
                number=item["number"],
                text=item["text"],
                figure_bboxes=item.get("figure_bboxes") or [],
                figures_dir=figures_dir,
                context=context,
            )
            figure_paths = list(outcome.figure_paths)
            figure_specs = list(outcome.figure_specs)
            needs_review = outcome.needs_review
        segments.append(
            Segment(
                number=item["number"],
                text=item["text"],
                page_start=page_no,
                page_end=page_no,
                needs_review=needs_review,
                continues_to_next=item.get("continues_to_next", False),
                continued_from_prev=item.get("continued_from_prev", False),
                source="visual",
                figure_paths=figure_paths,
                figure_specs=figure_specs,
            )
        )
    return segments


# ---------------- 双策略合并 ----------------

def _merge_page(
    rule_segments: list[Segment], visual_segments: list[Segment]
) -> list[Segment]:
    """单页双策略对齐：题号序列一致取规则文本 + 视觉跨页标记；不一致标 needs_review。"""
    if not visual_segments:
        return rule_segments
    if not rule_segments:
        for segment in visual_segments:
            segment.needs_review = True  # 规则路无产出，仅视觉单路
        return visual_segments

    rule_numbers = [s.number for s in rule_segments]
    visual_numbers = [s.number for s in visual_segments]
    if rule_numbers == visual_numbers:
        flags = {s.number: s for s in visual_segments}
        for segment in rule_segments:
            twin = flags[segment.number]
            segment.continues_to_next = twin.continues_to_next
            segment.continued_from_prev = twin.continued_from_prev
            segment.figure_paths = list(twin.figure_paths)
            segment.figure_specs = list(twin.figure_specs)
            segment.needs_review = segment.needs_review or twin.needs_review
        return rule_segments

    logger.warning(
        "切题双策略不一致: rule=%s visual=%s", rule_numbers, visual_numbers
    )
    for segment in rule_segments:
        segment.needs_review = True
    return rule_segments


def _join_cross_page(segments: list[Segment]) -> list[Segment]:
    """按跨页标记拼接：前段 continues_to_next 且后段 continued_from_prev 时合并。"""
    merged: list[Segment] = []
    for segment in segments:
        if (
            merged
            and merged[-1].continues_to_next
            and segment.continued_from_prev
            and merged[-1].number == segment.number
        ):
            head = merged[-1]
            head.text = f"{head.text}\n{segment.text}".strip()
            head.page_end = segment.page_end
            head.continued = True
            head.continues_to_next = segment.continues_to_next
            head.figure_paths.extend(segment.figure_paths)
            head.figure_specs.extend(segment.figure_specs)
            if segment.needs_review:
                head.needs_review = True
        else:
            merged.append(segment)
    return merged


def segment_document(
    pages: list[DocPage],
    provider: BaseAIProvider | None,
    max_workers: int = 3,
    figures_dir=None,
    figure_service=None,
    context=None,
) -> list[Segment]:
    """整卷切题：逐页双策略合并（视觉路按页并发），再跨页拼接。

    provider 为 None 时只走规则路（扫描页将无产出）。
    max_workers：视觉切题并发度（1 = 逐页顺序执行）；规则切题是本地正则，
    开销极小，保持逐页内联。视觉结果按页序回填，合并与跨页拼接的页序不变。
    """
    rule_results = [
        [] if page.is_scanned or not page.text.strip() else rule_segment_page(page.text, page.page_no)
        for page in pages
    ]
    # 视觉路结果：[] = 未走视觉路 / 成功但无产出；None = 调用失败（降级标记）
    visual_results: list[list[Segment] | None] = [[] for _ in pages]
    if provider is not None:
        targets = [i for i, page in enumerate(pages) if page.image_path is not None]
        if targets:
            with ThreadPoolExecutor(max_workers=max(1, max_workers)) as pool:
                futures = {
                    pool.submit(
                        visual_segment_page,
                        provider,
                        pages[i].image_path,
                        pages[i].page_no,
                        figures_dir,
                        figure_service,
                        context,
                    ): i
                    for i in targets
                }
                for future in as_completed(futures):
                    i = futures[future]
                    try:
                        visual_results[i] = future.result()
                    except Exception as exc:  # noqa: BLE001 - 文字页可降级为规则单路
                        logger.warning("视觉切题失败 page=%s: %s", pages[i].page_no, exc)
                        if pages[i].is_scanned:
                            raise RuntimeError(
                                f"第 {pages[i].page_no} 页视觉切题失败: {exc}"
                            ) from exc
                        visual_results[i] = None

    all_segments: list[Segment] = []
    for rule_segments, visual_segments in zip(rule_results, visual_results, strict=True):
        if visual_segments is None:  # 视觉路失败的页：规则单路并标 needs_review
            visual_segments = []
            for segment in rule_segments:
                segment.needs_review = True
        all_segments.extend(_merge_page(rule_segments, visual_segments))
    return _join_cross_page(all_segments)
