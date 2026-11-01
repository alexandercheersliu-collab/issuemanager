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
