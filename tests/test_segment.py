"""切题管线测试：规则切题、视觉切题（mock）、双策略对齐、跨页拼接。"""
from __future__ import annotations

from pathlib import Path

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
    provider = _StubSegmentProvider([
        [
            {"number": "1", "text": "第一题", "continued_from_prev": False, "continues_to_next": False},
            {"number": "2", "text": "第二题上半", "continued_from_prev": False, "continues_to_next": True},
        ],
        [
            {"number": "2", "text": "第二题下半", "continued_from_prev": True, "continues_to_next": False},
            {"number": "3", "text": "第三题", "continued_from_prev": False, "continues_to_next": False},
        ],
    ])
    pages = [
        _page(1, "1. 第一题\n2. 第二题上半", image=_page_image(tmp_path, "p1.png")),
        _page(2, "2. 第二题下半\n3. 第三题", image=_page_image(tmp_path, "p2.png")),
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
