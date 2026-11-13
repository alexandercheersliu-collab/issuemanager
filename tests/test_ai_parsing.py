from __future__ import annotations

import pydantic
import pytest

from backend.services.ai.base import AIMessageError, parse_analysis
from backend.services.ai.mock import MockProvider

VALID = {
    "knowledge_points": ["判别式"],
    "analysis": "先写出判别式表达式，再解不等式。" ,
    "answer": "k ≤ 1/2",
    "difficulty": "medium",
    "tags": ["方程"],
    "mistake_cause": "忽略取等条件",
    "followup_question": "变式题",
}


def test_parse_pure_json():
    import json

    analysis = parse_analysis(json.dumps(VALID, ensure_ascii=False))
    assert analysis.answer == "k ≤ 1/2"


def test_parse_fenced_json():
    import json

    fenced = f"```json\n{json.dumps(VALID, ensure_ascii=False)}\n```"
    analysis = parse_analysis(fenced)
    assert analysis.knowledge_points == ["判别式"]


def test_parse_json_with_surrounding_text():
    import json

    noisy = f"好的，以下是解析结果：\n{json.dumps(VALID, ensure_ascii=False)}\n希望对你有帮助"
    analysis = parse_analysis(noisy)
    assert analysis.mistake_cause == "忽略取等条件"


def test_parse_invalid_json_raises():
    with pytest.raises(AIMessageError):
        parse_analysis("完全不是 JSON 的回复")


def test_parse_python_dict_repr_is_rejected():
    # 模型若输出 Python 字典字面量（单引号）而非 JSON，应触发重试而非静默接受
    with pytest.raises(AIMessageError):
        parse_analysis(str(VALID))


def test_parse_invalid_difficulty_falls_to_validation_error():
    import json

    bad = dict(VALID, difficulty="超难")
    with pytest.raises(pydantic.ValidationError):
        parse_analysis(json.dumps(bad, ensure_ascii=False))


def test_merged_tags_dedup_and_order():
    from backend.models.schemas import QuestionAnalysis

    analysis = QuestionAnalysis(
        knowledge_points=["几何"],
        analysis="这是一段足够长的解析文本，超过最短限制。",
        answer="42",
        tags=["几何", "三角形"],
    )
    merged = analysis.merged_tags(["三角形", "几何", "用户标签"])
    assert merged == ["三角形", "几何", "用户标签"]


def test_merged_tags_respect_analysis_schema_limit():
    from backend.models.schemas import QuestionAnalysis

    analysis = QuestionAnalysis(
        knowledge_points=["考点5", "考点6", "考点7"],
        analysis="这是一段足够长的解析文本，超过最短限制。",
        answer="42",
        tags=["标签3", "标签4"],
    )

    merged = analysis.merged_tags(["标签1", "标签2"])

    assert merged == ["标签1", "标签2", "标签3", "标签4", "考点5", "考点6"]


def test_mock_provider_returns_valid_analysis():
    provider = MockProvider()
    raw = provider._complete(b"fake-image", "image/jpeg", "prompt")
    analysis = parse_analysis(raw)
    assert analysis.knowledge_points
    info = provider.provider_info()
    assert info.demo_mode is True


def test_parse_overlong_tags_truncated_not_failed():
    # 回归：真实模型给 7 个 tags 曾导致整题解析失败（too_long），应去重截断而非报错
    import json

    over = dict(VALID, tags=["小数除法", "余数", "求余数", "除法", "小数", "计算", "应用题"])
    analysis = parse_analysis(json.dumps(over, ensure_ascii=False))
    assert len(analysis.tags) == 6
    assert analysis.tags[0] == "小数除法"


def test_parse_overlong_knowledge_points_dedup_then_truncate():
    import json

    over = dict(
        VALID,
        knowledge_points=["除法", " 除法 ", "余数", "小数", "竖式", "近似数", "单位换算", "估算"],
    )
    analysis = parse_analysis(json.dumps(over, ensure_ascii=False))
    assert len(analysis.knowledge_points) == 6
    assert analysis.knowledge_points[0] == "除法"
    assert len(set(analysis.knowledge_points)) == 6


def test_parse_normalizes_latex_delimiters():
    # 模型偶发输出 \(...\) / \[...\] 定界符（真实案例：\(0.9\times19=(\quad)\)），
    # 下游渲染只认 $...$，parse_analysis 必须归一化
    import json

    payload = dict(
        VALID,
        analysis="原题：\\(0.9\\times19=(\\quad)\\)，竖式：\\[9\\times19=171\\]",
        answer="\\(17.1\\)",
        followup_question="计算：\\(0.9\\times21=(\\quad)\\)",
        focused_sub_question="\\(0.9\\times19=(\\quad)\\)",
        mistake_cause="小数点位置错，如 \\(0.9\\times19=1.71\\)",
    )
    analysis = parse_analysis(json.dumps(payload, ensure_ascii=False))
    assert analysis.analysis.startswith("原题：$0.9\\times19=(\\quad)$")
    assert "$$9\\times19=171$$" in analysis.analysis
    assert analysis.answer == "$17.1$"
    assert analysis.followup_question == "计算：$0.9\\times21=(\\quad)$"
    assert analysis.focused_sub_question == "$0.9\\times19=(\\quad)$"
    assert "\\(" not in analysis.mistake_cause


def test_parse_keeps_dollar_delimiters_untouched():
    import json

    payload = dict(VALID, analysis="行内 $0.9\\times19$，独立 $$a\\div b=0.5$$")
    analysis = parse_analysis(json.dumps(payload, ensure_ascii=False))
    assert analysis.analysis == "行内 $0.9\\times19$，独立 $$a\\div b=0.5$$"


def test_extract_json_list_repairs_latex_single_backslashes():
    # 视觉模型常把 LaTeX 单反斜杠直接写进 JSON 字符串（\i、\m、\v 等不是合法 JSON 转义），
    # 解析应容错修复，而不是整页切题失败
    from backend.services.ai.base import extract_json_list

    raw = (
        '[{"number": "3", "text": "若 $x\\in\\mathbb{R}$，则 $\\frac{x}{1+x^2}$ 与 '
        '$\\frac{1}{2}$ 的大小关系为____"}, '
        '{"number": "5", "text": "解集为 $\\varnothing$，方程组 '
        '$\\begin{cases}3x-y+2=0\\\\\\\\ax-y+b=0\\end{cases}$"}, '
        '{"number": "7", "text": "则 $\\alpha^2+\\beta^2=$____"}]'
    )
    items = extract_json_list(raw)
    assert items is not None and len(items) == 3
    assert items[0]["text"].count("\\in") == 1
    assert "\\frac{x}{1+x^2}" in items[0]["text"]
    assert "\\varnothing" in items[1]["text"]
    assert "\\begin{cases}" in items[1]["text"]
    assert "\\\\ax-y+b=0" in items[1]["text"]  # 合法转义 \\\\ → LaTeX 换行符不受影响
    assert "\\alpha^2+\\beta^2" in items[2]["text"]


def test_extract_json_block_repairs_latex_single_backslashes():
    from backend.services.ai.base import extract_json_block

    raw = '{"analysis": "由 $x\\geq 0$ 且 $\\log_2 x$ 得", "answer": "$m\\le 3$"}'
    block = extract_json_block(raw)
    assert block is not None
    assert "\\geq" in block["analysis"]
    assert block["answer"] == "$m\\le 3$"


def test_extract_json_list_still_rejects_garbage():
    from backend.services.ai.base import extract_json_list

    assert extract_json_list("完全不是 JSON 的回复") is None


def test_extract_json_list_preserves_real_newline_escapes():
    # 容错修复不得把模型真正的换行转义 \n 变成字面字符：
    # \n 后接非小写字母（大写/数字/标点）= 换行；后接小写字母 = LaTeX 命令（\ne、\nu）
    from backend.services.ai.base import extract_json_list

    raw = (
        '[{"number": "1", "text": "下列选项正确的是（ ）\\nA. $a+c>b+c$\\nB. $ac>bc$"}, '
        '{"number": "2", "text": "比较 $x$ 与 $y$：$x\\ne y$ 且 $x\\nu$"}]'
    )
    items = extract_json_list(raw)
    assert items is not None and len(items) == 2
    assert "\nA. $a+c>b+c$\nB." in items[0]["text"]  # 真换行
    assert "\\nA" not in items[0]["text"]
    assert "\\ne y" in items[1]["text"]  # LaTeX \ne 原样保留
    assert "\\nu" in items[1]["text"]


def test_segment_page_preserves_figure_bboxes(monkeypatch):
    import json

    from backend.services.ai.base import BaseAIProvider

    provider = MockProvider()
    monkeypatch.setattr(
        provider,
        "_complete",
        lambda *args: json.dumps(
            [{"number": "1", "text": "如图", "figure_bboxes": [[530, 110, 950, 490]]}]
        ),
    )

    items = BaseAIProvider.segment_page(provider, b"page")

    assert items[0]["figure_bboxes"] == [[530, 110, 950, 490]]


def test_segment_page_figure_bboxes_default_and_invalid(monkeypatch):
    import json

    from backend.services.ai.base import BaseAIProvider

    provider = MockProvider()
    monkeypatch.setattr(
        provider,
        "_complete",
        lambda *args: json.dumps(
            [
                {"number": "1", "text": "无图题"},
                {"number": "2", "text": "非法坐标", "figure_bboxes": "not-a-list"},
            ]
        ),
    )

    items = BaseAIProvider.segment_page(provider, b"page")

    assert items[0]["figure_bboxes"] == []
    assert items[1]["figure_bboxes"] == []


def test_segment_instruction_requires_complete_figure_bounds():
    from backend.services.ai.base import SEGMENT_INSTRUCTION

    assert "完整外接矩形" in SEGMENT_INSTRUCTION
    assert "不得只框图形的一部分" in SEGMENT_INSTRUCTION
    assert "不得包含页眉、题干、选项或解析" in SEGMENT_INSTRUCTION
    assert "输出前自检" in SEGMENT_INSTRUCTION


def test_locate_figures_aligns_boxes_with_question_order(monkeypatch):
    import json

    provider = MockProvider()
    monkeypatch.setattr(
        provider,
        "_complete",
        lambda *args: json.dumps(
            [
                {"number": "2", "figure_bboxes": [[500, 500, 1000, 1000]]},
                {"number": "1", "figure_bboxes": [[0, 0, 500, 500]]},
            ]
        ),
    )
    items = [{"number": "1"}, {"number": "2"}]

    boxes = provider.locate_figures(b"page", items)

    assert boxes == [[[0, 0, 500, 500]], [[500, 500, 1000, 1000]]]


def test_figure_instruction_excludes_option_text():
    from backend.services.ai.base import FIGURE_INSTRUCTION

    assert "选项字母和公式不属于图形标注" in FIGURE_INSTRUCTION
    assert "坐标下边缘不得超过图形最低顶点及其必要标注" in FIGURE_INSTRUCTION


def test_redraw_geometry_parses_fenced_json_and_includes_boxes_and_k12(monkeypatch):
    import json

    from backend.models.schemas import GeometryFigureDraft
    from backend.services.ai.base import AnalysisContext

    provider = MockProvider()
    provider.settings = provider.settings.model_copy(update={"ai_max_retries": 1})
    calls = {}

    def complete(image_bytes, mime_type, prompt, system_prompt):
        calls.update(
            image_bytes=image_bytes,
            mime_type=mime_type,
            prompt=prompt,
            system_prompt=system_prompt,
        )
        payload = [
            {
                "kind": "geometry",
                "semantic_spec": {"points": ["A", "B", "C"], "relations": ["AB ⟂ BC"]},
                "tikz_source": "\\begin{tikzpicture}\\draw (0,0)--(1,0);\\end{tikzpicture}",
                "assumptions": ["\\(AB=BC\\)"],
            }
        ]
        return f"```json\n{json.dumps(payload, ensure_ascii=False)}\n```"

    monkeypatch.setattr(provider, "_complete", complete)
    drafts = provider.redraw_geometry(
        b"page",
        text="如图，已知 \\(AB=BC\\)。",
        figure_bboxes=[[530, 110, 950, 490]],
        context=AnalysisContext(grade=8, region="浙江", textbook_version="人教版"),
    )

    assert len(drafts) == 1
    assert isinstance(drafts[0], GeometryFigureDraft)
    assert drafts[0].semantic_spec["relations"] == ["AB ⟂ BC"]
    assert drafts[0].assumptions == ["$AB=BC$"]
    assert calls["image_bytes"] == b"page"
    assert calls["mime_type"] == "image/png"
    assert "[[530, 110, 950, 490]]" in calls["prompt"]
    assert "8 年级" in calls["system_prompt"]
    assert "浙江地区" in calls["system_prompt"]
    assert "人教版" in calls["system_prompt"]


def test_redraw_geometry_normalizes_tikz_math_delimiters(monkeypatch):
    import json

    from backend.services.tikz import validate_tikz_source

    provider = MockProvider()
    provider.settings = provider.settings.model_copy(update={"ai_max_retries": 1})
    payload = [
        {
            "kind": "geometry",
            "semantic_spec": {"points": ["A", "B"]},
            "tikz_source": (
                "\\begin{tikzpicture}\\node at (0,0) {\\(A\\)};"
                "\\node at (1,0) {\\(B\\)};\\end{tikzpicture}"
            ),
            "assumptions": [],
        }
    ]
    monkeypatch.setattr(
        provider,
        "_complete",
        lambda *args: f"```json\n{json.dumps(payload)}\n```",
    )

    draft = provider.redraw_geometry(b"page", text="如图", figure_bboxes=[[0, 0, 10, 10]])[0]

    assert "\\(" not in draft.tikz_source
    assert "{\\(A\\)}" not in draft.tikz_source
    assert "{$A$}" in draft.tikz_source
    validate_tikz_source(draft.tikz_source)


def test_compare_and_repair_geometry_parse_fenced_json(monkeypatch):
    import json

    from backend.models.schemas import FigureComparison, GeometryFigureDraft

    provider = MockProvider()
    provider.settings = provider.settings.model_copy(update={"ai_max_retries": 1})
    draft = GeometryFigureDraft(
        kind="geometry",
        semantic_spec={"points": ["A", "B"], "relations": ["AB ⟂ BC"]},
        tikz_source="\\begin{tikzpicture}\\draw (0,0)--(1,0);\\end{tikzpicture}",
    )
    responses = iter(
        [
            {
                "passed": False,
                "issues": ["点 \\(B\\) 位置不符"],
                "repair_instruction": "调整 \\(B\\) 的位置",
            },
            draft.model_dump(),
        ]
    )
    monkeypatch.setattr(
        provider,
        "_complete",
        lambda *args: f"```json\n{json.dumps(next(responses), ensure_ascii=False)}\n```",
    )

    comparison = provider.compare_geometry(b"composite", text="原题干", draft=draft)
    repaired = provider.repair_geometry(
        b"composite", text="原题干", draft=draft, comparison=comparison
    )

    assert isinstance(comparison, FigureComparison)
    assert comparison.issues == ["点 $B$ 位置不符"]
    assert comparison.repair_instruction == "调整 $B$ 的位置"
    assert isinstance(repaired, GeometryFigureDraft)
    assert repaired == draft


def test_generate_geometry_variant_prompt_preserves_essence_and_parses(monkeypatch):
    import json

    from backend.models.schemas import GeometryFigureDraft, GeometryVariant
    from backend.services.ai.base import AnalysisContext

    provider = MockProvider()
    provider.settings = provider.settings.model_copy(update={"ai_max_retries": 1})
    draft = GeometryFigureDraft(
        kind="geometry",
        semantic_spec={"points": ["A", "B", "C"]},
        tikz_source="\\begin{tikzpicture}\\draw (0,0)--(1,0);\\end{tikzpicture}",
    )
    messages = []

    def chat(incoming):
        messages.extend(incoming)
        payload = {
            "question": "在新图形中求 \\(BC\\) 的长。",
            "figures": [draft.model_dump()],
        }
        return f"```json\n{json.dumps(payload, ensure_ascii=False)}\n```"

    monkeypatch.setattr(provider, "chat", chat)
    variant = provider.generate_geometry_variant(
        "原题干",
        knowledge_points=["直角三角形"],
        error_category="概念不清",
        question_type="解答题",
        difficulty="medium",
        figure_specs=[draft.model_dump()],
        context=AnalysisContext(grade=9, textbook_version="北师大版"),
        repair_instruction="上一轮图形语义不符",
    )

    assert isinstance(variant, GeometryVariant)
    assert variant.question == "在新图形中求 $BC$ 的长。"
    assert variant.figures == [draft]
    user_prompt = messages[-1]["content"]
    assert "保留考点/错点但可改变图形结构" in user_prompt
    assert "上一轮图形语义不符" in user_prompt
    assert "直角三角形" in user_prompt
    assert "概念不清" in user_prompt
    assert "9 年级" in messages[0]["content"]
    assert "北师大版" in messages[0]["content"]


def test_validate_geometry_variant_prompt_contains_new_question_and_semantics(monkeypatch):
    from backend.models.schemas import FigureComparison, GeometryFigureDraft

    provider = MockProvider()
    provider.settings = provider.settings.model_copy(update={"ai_max_retries": 1})
    draft = GeometryFigureDraft(
        kind="geometry",
        semantic_spec={"relations": ["AB ⟂ BC"], "given": ["AB=BC"]},
        tikz_source="\\begin{tikzpicture}\\draw (0,0)--(1,0);\\end{tikzpicture}",
    )
    calls = {}

    def complete(image_bytes, mime_type, prompt, system_prompt):
        calls.update(image_bytes=image_bytes, mime_type=mime_type, prompt=prompt)
        return "```json\n{\"passed\": true, \"issues\": [], \"repair_instruction\": \"\"}\n```"

    monkeypatch.setattr(provider, "_complete", complete)
    comparison = provider.validate_geometry_variant(
        b"rendered-png",
        content="新题干：求斜边长",
        draft=draft,
        original_content="原题干",
        knowledge_points=["勾股定理"],
        error_category="计算失误",
    )

    assert isinstance(comparison, FigureComparison)
    assert comparison.passed is True
    assert calls["image_bytes"] == b"rendered-png"
    assert calls["mime_type"] == "image/png"
    assert "新题干：求斜边长" in calls["prompt"]
    assert "AB ⟂ BC" in calls["prompt"]
    assert "AB=BC" in calls["prompt"]


@pytest.mark.parametrize("invalid_field", ["kind", "tikz_source", "passed"])
def test_geometry_contract_rejects_invalid_fields(monkeypatch, invalid_field):
    import json

    from backend.models.schemas import FigureComparison, GeometryFigureDraft

    provider = MockProvider()
    provider.settings = provider.settings.model_copy(update={"ai_max_retries": 1})
    draft = GeometryFigureDraft(
        kind="geometry",
        semantic_spec={},
        tikz_source="\\begin{tikzpicture}\\draw (0,0)--(1,0);\\end{tikzpicture}",
    )
    payload = draft.model_dump()
    payload[invalid_field] = {"kind": "unknown", "tikz_source": "  ", "passed": "true"}[
        invalid_field
    ]
    monkeypatch.setattr(
        provider,
        "_complete",
        lambda *args: f"```json\n{json.dumps(payload, ensure_ascii=False)}\n```",
    )

    with pytest.raises(AIMessageError):
        if invalid_field == "kind":
            provider.repair_geometry(
                b"composite",
                text="原题干",
                draft=draft,
                comparison=FigureComparison(passed=False),
            )
        elif invalid_field == "tikz_source":
            provider.repair_geometry(
                b"composite",
                text="原题干",
                draft=draft,
                comparison=FigureComparison(passed=False),
            )
        else:
            provider.compare_geometry(b"composite", text="原题干", draft=draft)
