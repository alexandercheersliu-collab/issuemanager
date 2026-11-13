"""切题管线测试：规则切题、视觉切题（mock）、双策略对齐、跨页拼接。"""
from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest
from PIL import Image

from backend.services.ai.base import AIProviderInfo, BaseAIProvider
from backend.services.ai.mock import MockProvider
from backend.services.document import DocPage
from backend.services.segment import (
    rule_segment_page,
    segment_document,
    visual_segment_page,
)


def _page(page_no: int, text: str, *, scanned: bool = False, image: Path | None = None) -> DocPage:
    return DocPage(
        page_no=page_no, text=text, image_path=image, is_scanned=scanned
    )


def _page_image(tmp_path: Path, name: str = "page.png") -> Path:
    path = tmp_path / name
    Image.new("RGB", (100, 100), (255, 255, 255)).save(path)
    return path


class _StubSegmentProvider(MockProvider):
    """返回预置切题结果的测试替身。"""

    def __init__(self, pages: list[list[dict]]):
        super().__init__()
        self.pages = pages
        self.calls = 0

    def segment_page(self, image_bytes, mime_type="image/png"):
        self.calls += 1
        return self.pages[min(self.calls - 1, len(self.pages) - 1)]


# ---------- 策略 B：规则切题 ----------

def test_rule_segment_arabic_numbers():
    text = "1. 已知 x^2=9，求 x。\n（1）第一小问\n2. 计算 1+1。\n3、解方程 2x=6"
    segments = rule_segment_page(text, 1)
    assert [s.number for s in segments] == ["1", "2", "3"]
    # （1）不递增，视为第 1 题的小问内容，不切分
    assert "（1）第一小问" in segments[0].text
    assert segments[0].page_start == 1 and segments[0].page_end == 1


def test_rule_segment_parenthesized_top_level_numbers():
    """整页用（1）（2）（3）编号（递增）时正常切分。"""
    text = "（1）解方程 x+1=2\n（2）化简 2x+3x\n（3）证明题"
    segments = rule_segment_page(text, 1)
    assert [s.number for s in segments] == ["1", "2", "3"]


def test_rule_segment_chinese_numbers():
    text = "一、解方程 x+1=2。\n二、化简 2x+3x。\n十一、证明题题干"
    segments = rule_segment_page(text, 2)
    assert [s.number for s in segments] == ["1", "2", "11"]
    assert all(s.page_start == 2 for s in segments)


def test_rule_segment_keeps_image_anchors_and_formulas():
    text = "1. 几何题见下图。\n[图片:images/img1.png]\n$$x^2+y^2=1$$\n2. 下一题"
    segments = rule_segment_page(text, 1)
    assert "[图片:images/img1.png]" in segments[0].text
    assert "$$x^2+y^2=1$$" in segments[0].text


def test_rule_segment_ignores_preamble():
    text = "期末考试卷\n注意事项：请认真作答\n1. 第一题"
    segments = rule_segment_page(text, 1)
    assert len(segments) == 1
    assert "注意事项" not in segments[0].text


# ---------- 策略 A：视觉切题 ----------

def test_visual_segment_with_mock(tmp_path):
    provider = MockProvider()
    segments = visual_segment_page(provider, _page_image(tmp_path), 1)
    assert [s.number for s in segments] == ["1", "2"]
    assert all(s.source == "visual" for s in segments)


# ---------- 双策略合并 ----------

def test_aligned_strategies_use_rule_text(tmp_path):
    """两路题号一致：取规则文本（含锚点），并带上视觉跨页标记。"""
    provider = _StubSegmentProvider([
        [
            {"number": "1", "text": "视觉题1", "continued_from_prev": False, "continues_to_next": False},
            {"number": "2", "text": "视觉题2", "continued_from_prev": False, "continues_to_next": False},
        ]
    ])
    page = _page(1, "1. 规则题1\n[图片:images/a.png]\n2. 规则题2", image=_page_image(tmp_path))
    segments = segment_document([page], provider)
    assert [s.number for s in segments] == ["1", "2"]
    assert "[图片:images/a.png]" in segments[0].text  # 规则文本保留
    assert all(not s.needs_review for s in segments)


def test_misaligned_strategies_mark_needs_review(tmp_path):
    """两路题号不一致：整页标记 needs_review。"""
    provider = _StubSegmentProvider([
        [
            {"number": "1", "text": "视觉题1", "continued_from_prev": False, "continues_to_next": False},
            {"number": "2", "text": "视觉题2", "continued_from_prev": False, "continues_to_next": False},
        ]
    ])
    page = _page(1, "1. 题一\n2. 题二\n3. 题三", image=_page_image(tmp_path))
    segments = segment_document([page], provider)
    assert [s.number for s in segments] == ["1", "2", "3"]
    assert all(s.needs_review for s in segments)


def test_scanned_page_visual_only(tmp_path):
    """扫描版无文本层：只能走视觉路，单路产出标 needs_review。"""
    provider = _StubSegmentProvider([
        [{"number": "1", "text": "扫描页题1", "continued_from_prev": False, "continues_to_next": False}]
    ])
    page = _page(1, "", scanned=True, image=_page_image(tmp_path))
    segments = segment_document([page], provider)
    assert len(segments) == 1
    assert segments[0].source == "visual"
    assert segments[0].needs_review is True


# ---------- 跨页拼接 ----------

def test_cross_page_join(tmp_path):
    """页 1 末题 continues_to_next + 页 2 首段 continued_from_prev → 自动拼接。"""
    # 视觉路并发后无法按调用次序区分页面，页图着色区分、按字节映射到页结果
    image1 = tmp_path / "p1.png"
    Image.new("RGB", (100, 100), (40, 200, 120)).save(image1)
    image2 = tmp_path / "p2.png"
    Image.new("RGB", (100, 100), (80, 200, 120)).save(image2)
    provider = _ConcurrencySegmentProvider({
        image1.read_bytes(): {
            "page_no": 1,
            "items": [
                {"number": "1", "text": "第一题", "continued_from_prev": False, "continues_to_next": False},
                {"number": "2", "text": "第二题上半", "continued_from_prev": False, "continues_to_next": True},
            ],
        },
        image2.read_bytes(): {
            "page_no": 2,
            "items": [
                {"number": "2", "text": "第二题下半", "continued_from_prev": True, "continues_to_next": False},
                {"number": "3", "text": "第三题", "continued_from_prev": False, "continues_to_next": False},
            ],
        },
    })
    pages = [
        _page(1, "1. 第一题\n2. 第二题上半", image=image1),
        _page(2, "2. 第二题下半\n3. 第三题", image=image2),
    ]
    segments = segment_document(pages, provider)
    assert [s.number for s in segments] == ["1", "2", "3"]
    joined = segments[1]
    assert joined.continued is True
    assert joined.page_start == 1 and joined.page_end == 2
    assert "第二题上半" in joined.text and "第二题下半" in joined.text


def test_provider_failure_falls_back_to_rule(tmp_path):
    """视觉路异常：降级为规则单路并标 needs_review。"""

    class _BrokenProvider(MockProvider):
        def segment_page(self, image_bytes, mime_type="image/png"):
            raise RuntimeError("model down")

        def provider_info(self):
            return AIProviderInfo(provider="broken", model="x", configured=True, demo_mode=True)

    page = _page(1, "1. 题一\n2. 题二", image=_page_image(tmp_path))
    segments = segment_document([page], _BrokenProvider())
    assert [s.number for s in segments] == ["1", "2"]
    assert all(s.needs_review for s in segments)


def test_scanned_page_visual_failure_is_not_reported_as_empty(tmp_path):
    """扫描页没有规则路可降级，视觉异常必须向上报告而不是成功返回 0 题。"""

    class _BrokenProvider(MockProvider):
        def segment_page(self, image_bytes, mime_type="image/png"):
            raise RuntimeError("Connection error")

    page = _page(1, "", scanned=True, image=_page_image(tmp_path))
    with pytest.raises(RuntimeError, match="第 1 页视觉切题失败.*Connection error"):
        segment_document([page], _BrokenProvider())


# ---------- 错题预判 likely_wrong ----------

class _RawSegmentProvider(MockProvider):
    """通过 _complete 返回预置原始 JSON，走 BaseAIProvider.segment_page 真实解析。"""

    def __init__(self, raw: str):
        super().__init__()
        self.raw = raw

    def _complete(self, image_bytes, mime_type, prompt, system_prompt=""):
        return self.raw

    def segment_page(self, image_bytes, mime_type="image/png"):
        # MockProvider.segment_page 是固定演示数据，绕开 _complete；
        # 这里显式走回基类实现，才能验证真实解析逻辑。
        return BaseAIProvider.segment_page(self, image_bytes, mime_type)


def test_segment_page_retries_transient_connection_error():
    """首次连接失败后按既有 AI 重试配置再次调用，成功响应正常解析。"""

    class _FlakyProvider(_RawSegmentProvider):
        def __init__(self):
            super().__init__('[{"number": "9", "text": "第九题"}]')
            self.attempts = 0

        def _complete(self, image_bytes, mime_type, prompt, system_prompt=""):
            self.attempts += 1
            if self.attempts == 1:
                raise RuntimeError("Connection error")
            return super()._complete(image_bytes, mime_type, prompt, system_prompt)

    provider = _FlakyProvider()
    segments = provider.segment_page(b"img")
    assert [item["number"] for item in segments] == ["9"]
    assert provider.attempts == 2


def test_segment_page_parses_likely_wrong():
    provider = _RawSegmentProvider(
        '[{"number": "1", "text": "题一", "likely_wrong": true},'
        ' {"number": "2", "text": "题二", "likely_wrong": false}]'
    )
    segments = provider.segment_page(b"img")
    assert [s["likely_wrong"] for s in segments] == [True, False]


def test_segment_page_likely_wrong_defaults_false_when_missing():
    """模型不给 likely_wrong 字段时默认 False，不报错。"""
    provider = _RawSegmentProvider('[{"number": "1", "text": "题一"}]')
    segments = provider.segment_page(b"img")
    assert segments[0]["likely_wrong"] is False


def test_mock_provider_segments_carry_likely_wrong():
    """演示模式：第 1 题疑似做错（演示默认勾选），第 2 题正常。"""
    segments = MockProvider().segment_page(b"img")
    assert [s["likely_wrong"] for s in segments] == [True, False]


# ---------- 逐页视觉切题并发 ----------

class _ConcurrencySegmentProvider(MockProvider):
    """按页图字节返回预置结果，并记录 segment_page 实时并发峰值的替身。

    inverse_delay=True 时睡眠随页号递减（后面的页先完成），用于乱序完成场景；
    fail_pages 中的页号抛错，用于单页失败降级场景。
    """

    def __init__(
        self,
        page_results: dict[bytes, dict],
        *,
        delay: float = 0.05,
        inverse_delay: bool = False,
        fail_pages: tuple[int, ...] = (),
    ):
        super().__init__()
        self.page_results = page_results
        self.delay = delay
        self.inverse_delay = inverse_delay
        self.fail_pages = fail_pages
        self._lock = threading.Lock()
        self._current = 0
        self.max_concurrent = 0

    def segment_page(self, image_bytes, mime_type="image/png"):
        info = self.page_results[bytes(image_bytes)]
        with self._lock:
            self._current += 1
            self.max_concurrent = max(self.max_concurrent, self._current)
        try:
            delay = self.delay
            if self.inverse_delay:
                delay = self.delay * (len(self.page_results) - info["page_no"] + 1)
            time.sleep(delay)
            if info["page_no"] in self.fail_pages:
                raise RuntimeError(f"模拟第 {info['page_no']} 页视觉失败")
            return info["items"]
        finally:
            with self._lock:
                self._current -= 1


def _scanned_pages(tmp_path: Path, count: int):
    """造 count 页扫描版页面（页图颜色各异使字节可区分），返回 (pages, page_results)。"""
    pages, results = [], {}
    for page_no in range(1, count + 1):
        image = tmp_path / f"scan-{page_no}.png"
        Image.new("RGB", (100, 100), (page_no * 40, 200, 120)).save(image)
        pages.append(_page(page_no, "", scanned=True, image=image))
        results[image.read_bytes()] = {
            "page_no": page_no,
            "items": [
                {
                    "number": "1",
                    "text": f"第{page_no}页扫描题",
                    "continued_from_prev": False,
                    "continues_to_next": False,
                }
            ],
        }
    return pages, results


def test_segment_document_parallel_pages(tmp_path):
    """多页视觉切题并发执行（峰值 > 1），合并结果仍按页序排列。"""
    pages, results = _scanned_pages(tmp_path, 4)
    provider = _ConcurrencySegmentProvider(results)
    segments = segment_document(pages, provider, max_workers=3)
    assert provider.max_concurrent > 1, "逐页视觉切题应并发执行"
    assert [s.page_start for s in segments] == [1, 2, 3, 4]
    assert [s.text for s in segments] == [f"第{i}页扫描题" for i in range(1, 5)]


def test_segment_document_page_order_kept_when_later_pages_finish_first(tmp_path):
    """后面的页先完成时，segments 仍保持页序（不随完成顺序漂移）。"""
    pages, results = _scanned_pages(tmp_path, 4)
    provider = _ConcurrencySegmentProvider(results, inverse_delay=True)
    segments = segment_document(pages, provider, max_workers=4)
    assert [s.page_start for s in segments] == [1, 2, 3, 4]
    assert [s.text for s in segments] == [f"第{i}页扫描题" for i in range(1, 5)]


def test_segment_document_sequential_when_max_workers_one(tmp_path):
    """max_workers=1：保持逐页顺序执行（峰值恒为 1）。"""
    pages, results = _scanned_pages(tmp_path, 3)
    provider = _ConcurrencySegmentProvider(results)
    segments = segment_document(pages, provider, max_workers=1)
    assert provider.max_concurrent == 1
    assert [s.page_start for s in segments] == [1, 2, 3]


def test_segment_document_page_failure_fallback_under_concurrency(tmp_path):
    """并发下某页视觉失败：该页降级为规则单路 + needs_review，其余页正常对齐。"""
    pages, results = [], {}
    for page_no in (1, 2, 3):
        image = tmp_path / f"text-{page_no}.png"
        Image.new("RGB", (100, 100), (page_no * 40, 200, 120)).save(image)
        pages.append(_page(page_no, f"1. 第{page_no}页规则题", image=image))
        results[image.read_bytes()] = {
            "page_no": page_no,
            "items": [
                {
                    "number": "1",
                    "text": f"第{page_no}页视觉题",
                    "continued_from_prev": False,
                    "continues_to_next": False,
                }
            ],
        }
    provider = _ConcurrencySegmentProvider(results, fail_pages=(2,))
    segments = segment_document(pages, provider, max_workers=3)
    assert provider.max_concurrent > 1
    assert [s.page_start for s in segments] == [1, 2, 3]
    assert all(s.text.startswith(f"1. 第{s.page_start}页规则题") for s in segments)
    # 第 2 页视觉失败 → 规则单路标 needs_review；其余页双路对齐不标
    assert [s.needs_review for s in segments] == [False, True, False]


class _StubFigureService:
    def __init__(self, outcomes=None):
        self.calls = []
        self.outcomes = outcomes or {}

    def redraw_segment(self, **kwargs):
        from backend.services.geometry_figures import FigureOutcome

        self.calls.append(kwargs)
        key = (Path(kwargs["image_path"]).name, kwargs["number"])
        return self.outcomes.get(
            key,
            FigureOutcome(
                figure_paths=[f"{Path(kwargs['image_path']).stem}-{kwargs['number']}.png"],
                figure_specs=[{
                    "kind": "geometry",
                    "semantic_spec": {"source": key[0]},
                    "tikz_source": kwargs["number"],
                    "render_path": None,
                    "crop_path": None,
                    "validation_status": "ai_passed",
                    "validation_issues": [],
                    "assumptions": [],
                }],
                needs_review=False,
            ),
        )


def test_visual_segment_delegates_boxes_without_eager_locate_or_crop(tmp_path, monkeypatch):
    from backend.services.ai.base import AnalysisContext
    from backend.services.geometry_figures import FigureOutcome

    class _NoSecondLocate(_StubSegmentProvider):
        def locate_figures(self, image_bytes, items, mime_type="image/png"):
            raise AssertionError("生产切题路径不应立即二次定位")

    def _no_eager_crop(*args, **kwargs):
        raise AssertionError("生产切题路径不应绕过图形服务立即裁剪")

    monkeypatch.setattr("backend.services.figures.crop_figures", _no_eager_crop)
    provider = _NoSecondLocate([
        [{"number": "1", "text": "如图", "figure_bboxes": [[0, 0, 500, 500]]}]
    ])
    spec = {
        "kind": "geometry",
        "semantic_spec": {"shape": "triangle"},
        "tikz_source": "\\draw (0,0)--(1,0);",
        "render_path": "rendered.png",
        "crop_path": None,
        "validation_status": "fallback",
        "validation_issues": ["需人工确认"],
        "assumptions": [],
    }
    service = _StubFigureService({
        ("page.png", "1"): FigureOutcome(["rendered.png"], [spec], True)
    })
    context = AnalysisContext(subject="math", grade=8, region="北京")
    figures_dir = tmp_path / "figures"

    segments = visual_segment_page(
        provider,
        _page_image(tmp_path),
        1,
        figures_dir=figures_dir,
        figure_service=service,
        context=context,
    )

    assert segments[0].figure_paths == ["rendered.png"]
    assert segments[0].figure_specs == [spec]
    assert segments[0].needs_review is True
    assert len(service.calls) == 1
    call = service.calls[0]
    assert call["image_path"] == tmp_path / "page.png"
    assert call["number"] == "1"
    assert call["text"] == "如图"
    assert call["figure_bboxes"] == [[0, 0, 500, 500]]
    assert call["figures_dir"] == figures_dir
    assert call["context"] is context


def test_aligned_strategies_keep_visual_figure_data(tmp_path):
    from backend.services.geometry_figures import FigureOutcome

    provider = _StubSegmentProvider([
        [{"number": "1", "text": "视觉题", "figure_bboxes": [[0, 0, 500, 500]]}]
    ])
    spec = {
        "kind": "geometry",
        "semantic_spec": {},
        "tikz_source": "aligned",
        "render_path": "aligned.png",
        "crop_path": None,
        "validation_status": "fallback",
        "validation_issues": [],
        "assumptions": [],
    }
    service = _StubFigureService({
        ("page.png", "1"): FigureOutcome(["aligned.png"], [spec], True)
    })
    page = _page(1, "1. 规则题", image=_page_image(tmp_path))

    segments = segment_document(
        [page], provider, figures_dir=tmp_path / "figures", figure_service=service
    )

    assert segments[0].figure_paths == ["aligned.png"]
    assert segments[0].figure_specs == [spec]
    assert segments[0].needs_review is True
    assert segments[0].text == "1. 规则题"


def test_cross_page_join_preserves_figure_order(tmp_path):
    from backend.services.geometry_figures import FigureOutcome

    image1 = tmp_path / "first.png"
    image2 = tmp_path / "second.png"
    Image.new("RGB", (100, 100), "white").save(image1)
    Image.new("RGB", (100, 100), "black").save(image2)
    provider = _ConcurrencySegmentProvider({
        image1.read_bytes(): {
            "page_no": 1,
            "items": [{
                "number": "1",
                "text": "上半",
                "continues_to_next": True,
                "figure_bboxes": [[0, 0, 400, 400]],
            }],
        },
        image2.read_bytes(): {
            "page_no": 2,
            "items": [{
                "number": "1",
                "text": "下半",
                "continued_from_prev": True,
                "figure_bboxes": [[500, 500, 1000, 1000]],
            }],
        },
    })
    first_spec = {
        "kind": "geometry",
        "semantic_spec": {"part": 1},
        "tikz_source": "first",
        "render_path": "first-figure.png",
        "crop_path": None,
        "validation_status": "ai_passed",
        "validation_issues": [],
        "assumptions": [],
    }
    second_spec = {
        "kind": "geometry",
        "semantic_spec": {"part": 2},
        "tikz_source": "second",
        "render_path": None,
        "crop_path": "second-figure.png",
        "validation_status": "fallback",
        "validation_issues": [],
        "assumptions": [],
    }
    service = _StubFigureService({
        ("first.png", "1"): FigureOutcome(["first-figure.png"], [first_spec], False),
        ("second.png", "1"): FigureOutcome(["second-figure.png"], [second_spec], True),
    })

    segments = segment_document(
        [_page(1, "", scanned=True, image=image1), _page(2, "", scanned=True, image=image2)],
        provider,
        figures_dir=tmp_path / "figures",
        figure_service=service,
    )

    assert len(segments) == 1
    assert segments[0].figure_paths == ["first-figure.png", "second-figure.png"]
    assert segments[0].figure_specs == [first_spec, second_spec]
    assert segments[0].needs_review is True
