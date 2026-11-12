"""AI 提供商抽象基类：统一的错题解析接口 + 共享提示词与解析逻辑。"""
from __future__ import annotations

import abc
import json
import re
from dataclasses import dataclass

from backend.config import Settings, get_settings
from backend.models.schemas import (
    ERROR_CATEGORIES,
    SUBJECT_NAMES,
    AIProviderInfo,
    FigureComparison,
    GeometryFigureDraft,
    GeometryVariant,
    QuestionAnalysis,
)
from backend.services.ai.telemetry import track_ai_call
from backend.utils.logging import get_logger

logger = get_logger("ai")

SYSTEM_PROMPT = (
    "你是一位经验丰富、讲解亲切的数学老师。学生会上传一张写有数学错题的图片，"
    "请严谨地识别题目（包括手写内容与 LaTeX 公式），分析错误原因并给出逐步讲解。"
    "识别时先检查题目是否拍全（题干、选项、各小问有无缺失），再留意卷面上的"
    "批改痕迹（打叉、扣分、红笔批注）：若图片是一道含多道小题的大题，且只有部分"
    "小题被判错或学生指明了做错的小题，只提取做错的那道小题作为错题，"
    "把该小题的完整题干填入 focused_sub_question 字段，"
    "解析、答案与错因都紧紧围绕该小题，不要复述无关小题。"
    "所有讲解使用简体中文，公式使用 LaTeX（$...$ 行内，$$...$$ 独立）。"
    "必须严格输出 JSON，不要输出任何 JSON 以外的内容。"
)

FOLLOWUP_SYSTEM_PROMPT = (
    "你是一位耐心的数学老师，正在就学生的一道错题进行一对一追问讲解。"
    "讲解使用简体中文，公式使用 LaTeX（$...$ 行内，$$...$$ 独立）。"
    "回答紧扣题目本身，先直接回应学生的问题，再按需展开推导；学生理解卡住时给提示而不是直接报答案。"
)

JSON_INSTRUCTION = f"""请只输出一个 JSON 对象，结构如下：
{{
  "knowledge_points": ["3-5 个考察的核心知识点"],
  "analysis": "分步骤的详细解析，Markdown 格式，先指出错误再逐步推导",
  "answer": "最终正确答案",
  "difficulty": "easy | medium | hard",
  "tags": ["2-4 个归档标签，如：几何, 相似三角形"],
  "mistake_cause": "这类题常见的出错原因",
  "followup_question": "一道考查相同知识点、题型与原题一致的变式练习题（只给题目，不给答案；原题是应用题/解答题时必须保留情境与设问形式，不得退化为纯计算题）",
  "question_type": "题型，如：选择题/填空题/判断题/计算题/解答题/证明题（判断不出留空串）",
  "chapter": "所属教材章节，如：一元二次方程（判断不出留空串）",
  "error_category": "学生错因分类，必须是以下枚举之一：{' / '.join(ERROR_CATEGORIES)}；无法判断用 null",
  "is_complete": true 或 false,
  "completeness_note": "题目没拍全时的说明（如「第(2)问只拍到一半」）；拍全了留空串",
  "focused_sub_question": "若这是一道含多道小题的大题，且只有部分小题做错（按批改痕迹或学生说明判断）：只填做错那道小题的完整题干（含必要的大题公共条件）；否则留空串"
}}

规则：
- is_complete=false 只在题干/选项/小问明确缺失时使用，拿不准视为完整；
- focused_sub_question 非空时，analysis、answer、mistake_cause 都必须只针对这道小题；
  反过来，只要你的 analysis/answer 只围绕某一道小题展开，focused_sub_question 就必须填该小题的题干，不得留空；
- 学生说明里指明小题（如「第 2 问错了」）时优先按学生说明聚焦。"""


@dataclass(frozen=True)
class AnalysisContext:
    """录题时的 K12 上下文：学科 + 年级 + 地区 + 教材版本（全部可选）。

    缺省（全空）时提示词与基座行为完全一致；任一字段提供后，
    系统提示词按上下文动态组装，引导模型输出贴合学段与教材的解析。
    """

    subject: str = "math"
    grade: int | None = None
    region: str | None = None
    textbook_version: str | None = None

    def is_default(self) -> bool:
        return (
            self.subject in ("", "math")
            and self.grade is None
            and not self.region
            and not self.textbook_version
        )


def build_system_prompt(context: AnalysisContext | None = None) -> str:
    """按 K12 上下文组装系统提示词；无上下文时返回基座原版 SYSTEM_PROMPT。"""
    if context is None or context.is_default():
        return SYSTEM_PROMPT

    subject_name = SUBJECT_NAMES.get(context.subject, context.subject or "数学")
    teacher = f"你是一位经验丰富、讲解亲切的{subject_name}老师。"
    audience: list[str] = []
    if context.grade is not None:
        audience.append(f"{context.grade} 年级")
    if context.region:
        audience.append(f"{context.region}地区")
    if context.textbook_version:
        audience.append(f"使用{context.textbook_version}教材")
    if audience:
        teacher += f"学生是{'，'.join(audience)}的学生，讲解的深度、措辞与引用的知识点要贴合该学段与教材。"
    return (
        f"{teacher}学生会上传一张写有{subject_name}错题的图片，"
        "请严谨地识别题目（包括手写内容与 LaTeX 公式），分析错误原因并给出逐步讲解。"
        "识别时先检查题目是否拍全（题干、选项、各小问有无缺失），再留意卷面上的"
        "批改痕迹（打叉、扣分、红笔批注）：若图片是一道含多道小题的大题，且只有部分"
        "小题被判错或学生指明了做错的小题，只提取做错的那道小题作为错题，"
        "解析、答案与错因都紧紧围绕该小题，不要复述无关小题。"
        "所有讲解使用简体中文，公式使用 LaTeX（$...$ 行内，$$...$$ 独立）。"
        "必须严格输出 JSON，不要输出任何 JSON 以外的内容。"
    )


def build_user_prompt(hint: str, context: AnalysisContext | None = None) -> str:
    subject_name = (
        SUBJECT_NAMES.get(context.subject, context.subject) if context else "数学"
    ) or "数学"
    prompt = f"请分析这张图片中的{subject_name}错题。\n"
    prompt += (
        "特别注意：若图中是一道含多道小题的大题，且只有部分小题被判错"
        "（打叉、扣分、红笔批注等）或学生指明了做错的小题，"
        "必须把做错小题的完整题干填入 focused_sub_question 字段。\n"
    )
    if hint:
        prompt += f"学生的补充说明：{hint}\n"
    return prompt + JSON_INSTRUCTION


class AIMessageError(RuntimeError):
    """模型未返回可解析的结构化结果。"""


SEGMENT_SYSTEM_PROMPT = (
    "你是一位严谨的试卷切题助手。学生会上传一页试卷的图片，"
    "请识别本页所有完整或部分的题目，按题号顺序输出；"
    "同时留意卷面上的作答与批改痕迹，判断哪些题疑似被做错。"
    "必须严格输出 JSON，不要输出任何 JSON 以外的内容。"
)

SEGMENT_INSTRUCTION = """请只输出一个 JSON 数组，每个元素结构如下：
[
  {
    "number": "题号（阿拉伯数字字符串，如 \\"1\\"）",
    "text": "该题在本页的题干文本（含选项；公式用 LaTeX）",
    "figure_bboxes": [[530, 110, 950, 490]],
    "continued_from_prev": false,
    "continues_to_next": false,
    "likely_wrong": false
  }
]
规则：
- continued_from_prev=true 表示本题是上一页末尾题目的延续（此时 number 沿用上一页题号）；
- continues_to_next=true 表示本题题干在本页未结束、延续到下一页；
- likely_wrong=true 表示本题卷面上有作答痕迹且疑似被判错（打叉、扣分、涂改、红笔批注等）；
  没有作答痕迹或作答看似正确的题标 false；无法判断时一律标 false；
- figure_bboxes 只标注本题解答所需的几何图形、函数图像、统计图等图形区域，可有多张；
  每项为 [x0, y0, x1, y1]，相对于页图宽高的 0-1000 归一化坐标，包含必要文字标注；
  坐标必须覆盖图形的完整外接矩形，包括最上方、最下方的线条和全部字母标注；
  不得只框图形的一部分，不得包含页眉、题干、选项或解析，不要框整道题或整页，无图形时输出 []；
  输出前自检每个 bbox：若框内出现完整题干、选项、页眉或解析，必须重新收紧到纯图形及必要标注；
- 页眉页脚、注意事项、答题区提示等非题目内容不要输出。"""

FIGURE_SYSTEM_PROMPT = (
    "你是一位只负责数学图形定位的视觉助手。不要识别题干，不要解题。"
    "必须严格输出 JSON，不要输出任何 JSON 以外的内容。"
)

FIGURE_INSTRUCTION = """请只输出一个 JSON 数组，按给定题号定位每道题的图形：
[
  {"number": "题号", "figure_bboxes": [[0, 0, 500, 500]]}
]
figure_bboxes 使用相对页图宽高的 0-1000 归一化坐标 [x0, y0, x1, y1]。
只框几何图形、函数图像、统计图等图形及必要字母标注，可有多个；无图形输出 []。
选项字母和公式不属于图形标注，坐标下边缘不得超过图形最低顶点及其必要标注。
每个框必须包含完整图形，且不得包含页眉、题干、选项、答案或解析文字。"""

GEOMETRY_SYSTEM_PROMPT = (
    "你是一位严谨的 K12 数学几何图形助手，负责把题干语义准确转换为 TikZ 图形，"
    "或核对图形与题干是否一致。不要解题，不要编造题干没有给出的关系。"
    "必须严格输出 JSON，不要输出任何 JSON 以外的内容。"
)

GEOMETRY_DRAFT_SCHEMA = r"""{
  "kind": "geometry",
  "semantic_spec": {"points": ["A", "B"], "relations": ["AB ⟂ BC"], "given": ["AB=3"]},
  "tikz_source": "\\begin{tikzpicture}\\draw (0,0)--(1,0);\\end{tikzpicture}",
  "assumptions": ["示意图不按比例"]
}
kind 只能是 geometry；semantic_spec 必须完整记录点、线、角、长度及位置关系；
tikz_source 必须是非空且完整的一个 tikzpicture，不要包含 documentclass 或 usepackage；
assumptions 只记录无法从题干确定、且不影响解题的绘图假设。"""

FIGURE_COMPARISON_SCHEMA = """{
  "passed": true 或 false,
  "issues": ["图形与题干或语义规格不一致的具体问题"],
  "repair_instruction": "可执行的修复要求；通过时留空串"
}
passed 必须是 JSON 布尔值，不能使用字符串；只根据图形语义判断，不要求示意图按比例。"""


def _geometry_system_prompt(context: AnalysisContext | None = None) -> str:
    if context is None or context.is_default():
        return GEOMETRY_SYSTEM_PROMPT
    subject_name = SUBJECT_NAMES.get(context.subject, context.subject or "数学")
    details = [f"学科：{subject_name}"]
    if context.grade is not None:
        details.append(f"{context.grade} 年级")
    if context.region:
        details.append(f"{context.region}地区")
    if context.textbook_version:
        details.append(f"使用{context.textbook_version}教材")
    return (
        f"{GEOMETRY_SYSTEM_PROMPT}\n当前 K12 上下文：{'；'.join(details)}。"
        "图形语义、标注和难度应贴合该学段与教材。"
    )


class BaseAIProvider(abc.ABC):
    """所有提供商实现同一接口，界面层与提供商解耦。"""

    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()

    @abc.abstractmethod
    def _complete(
        self,
        image_bytes: bytes,
        mime_type: str,
        prompt: str,
        system_prompt: str = SYSTEM_PROMPT,
    ) -> str:
        """调用多模态模型，返回原始文本响应。"""

    @abc.abstractmethod
    def chat(self, messages: list[dict]) -> str:
        """纯文本多轮对话：messages 为 [{"role": ..., "content": ...}, ...]。"""

    @abc.abstractmethod
    def provider_info(self) -> AIProviderInfo:
        """提供商元信息，用于界面展示运行状态。"""

    def answer_followup(
        self,
        question_context: str,
        history: list[dict],
        user_question: str,
    ) -> str:
        """围绕一道已解析错题的多轮追问讲题。"""
        messages: list[dict] = [
            {
                "role": "system",
                "content": f"{FOLLOWUP_SYSTEM_PROMPT}\n\n【题目背景】\n{question_context[:4000]}",
            },
            *history[-12:],  # 限制上下文长度，防止 token 超限
            {"role": "user", "content": user_question},
        ]
        with track_ai_call("followup_chat"):
            reply = self.chat(messages)
        if not reply or not reply.strip():
            raise AIMessageError("模型返回了空响应")
        return normalize_math_delimiters(reply.strip())

    def segment_page(self, image_bytes: bytes, mime_type: str = "image/png") -> list[dict]:
        """视觉切题（策略 A）：识别一页试卷图像中的题目列表。

        返回 [{"number": str, "text": str, "continued_from_prev": bool,
        "continues_to_next": bool, "likely_wrong": bool}, ...]；
        likely_wrong 为错题预判（卷面有作答痕迹且疑似被判错），模型不给该字段时默认 False；
        输出非法时抛 AIMessageError（调用方重试/降级）。
        """
        last_error: Exception | None = None
        for attempt in range(1, self.settings.ai_max_retries + 1):
            with track_ai_call("segment_page"):
                try:
                    raw = self._complete(
                        image_bytes, mime_type, SEGMENT_INSTRUCTION, SEGMENT_SYSTEM_PROMPT
                    )
                    items = extract_json_list(raw)
                    if items is None:
                        raise AIMessageError("切题响应中未找到 JSON 数组")
                    segments: list[dict] = []
                    for item in items:
                        if not isinstance(item, dict) or not str(item.get("number", "")).strip():
                            raise AIMessageError(f"切题结果缺少题号: {item!r}")
                        segments.append(
                            {
                                "number": str(item["number"]).strip(),
                                "text": normalize_math_delimiters(
                                    str(item.get("text", "") or "").strip()
                                ),
                                "figure_bboxes": (
                                    item.get("figure_bboxes")
                                    if isinstance(item.get("figure_bboxes"), list)
                                    else []
                                ),
                                "continued_from_prev": bool(item.get("continued_from_prev", False)),
                                "continues_to_next": bool(item.get("continues_to_next", False)),
                                "likely_wrong": bool(item.get("likely_wrong", False)),
                            }
                        )
                    return segments
                except Exception as exc:  # noqa: BLE001 - 连接与结构化输出错误统一重试
                    last_error = exc
                    logger.warning("视觉切题第 %s 次失败: %s", attempt, exc)
        raise AIMessageError(
            f"视觉切题失败（已重试 {self.settings.ai_max_retries} 次）: {last_error}"
        )

    def locate_figures(
        self, image_bytes: bytes, items: list[dict], mime_type: str = "image/png"
    ) -> list[list]:
        """独立识别图形坐标，返回与 items 同顺序的 figure_bboxes。"""
        prompt = (
            f"{FIGURE_INSTRUCTION}\n需要定位的题号："
            f"{json.dumps([str(item.get('number') or '') for item in items], ensure_ascii=False)}"
        )
        last_error: Exception | None = None
        for attempt in range(1, self.settings.ai_max_retries + 1):
            with track_ai_call("locate_figures"):
                try:
                    raw = self._complete(
                        image_bytes, mime_type, prompt, FIGURE_SYSTEM_PROMPT
                    )
                    located = extract_json_list(raw)
                    if located is None:
                        raise AIMessageError("图形定位响应中未找到 JSON 数组")
                    by_number = {}
                    for entry in located:
                        if not isinstance(entry, dict):
                            continue
                        number = str(entry.get("number") or "").strip()
                        if number:
                            boxes = entry.get("figure_bboxes")
                            by_number[number] = boxes if isinstance(boxes, list) else []
                    return [
                        by_number.get(str(item.get("number") or "").strip(), [])
                        for item in items
                    ]
                except Exception as exc:  # noqa: BLE001 - 连接与结构化输出错误统一重试
                    last_error = exc
                    logger.warning("图形定位第 %s 次失败: %s", attempt, exc)
        raise AIMessageError(
            f"图形定位失败（已重试 {self.settings.ai_max_retries} 次）: {last_error}"
        )

    def redraw_geometry(
        self,
        image_bytes: bytes,
        *,
        text: str,
        figure_bboxes: list,
        mime_type: str = "image/png",
        context: AnalysisContext | None = None,
    ) -> list[GeometryFigureDraft]:
        prompt = f"""请根据原图和题干重绘所有几何图形。坐标是相对于页图宽高的 0-1000 归一化外接矩形，
只用于帮助你定位原图；输出顺序与坐标顺序一致。保留全部字母、数值和几何关系，不要添加解题结论。

题干：{text[:4000]}
图形坐标：{json.dumps(figure_bboxes, ensure_ascii=False)}

请只输出一个 JSON 数组，每个元素结构如下：
{GEOMETRY_DRAFT_SCHEMA}"""
        last_error: Exception | None = None
        for attempt in range(1, self.settings.ai_max_retries + 1):
            with track_ai_call("redraw_geometry"):
                try:
                    raw = self._complete(
                        image_bytes, mime_type, prompt, _geometry_system_prompt(context)
                    )
                    return _parse_geometry_draft_list(raw)
                except Exception as exc:  # noqa: BLE001 - 连接与结构化输出错误统一重试
                    last_error = exc
                    logger.warning("几何原图解析第 %s 次失败: %s", attempt, exc)
        raise AIMessageError(
            f"几何原图解析失败（已重试 {self.settings.ai_max_retries} 次）: {last_error}"
        )

    def compare_geometry(
        self, composite_image_bytes: bytes, *, text: str, draft: GeometryFigureDraft
    ) -> FigureComparison:
        prompt = f"""输入是一张由原图与 TikZ 重绘图合成的对比图。请核对重绘图是否准确保留题干和
semantic_spec 中的几何语义，包括点线连接、位置、平行垂直、角度、长度和全部标注。

题干：{text[:4000]}
图形语义与源码：{json.dumps(draft.model_dump(), ensure_ascii=False)}

请只输出一个 JSON 对象：
{FIGURE_COMPARISON_SCHEMA}"""
        last_error: Exception | None = None
        for attempt in range(1, self.settings.ai_max_retries + 1):
            with track_ai_call("compare_geometry"):
                try:
                    raw = self._complete(
                        composite_image_bytes,
                        "image/png",
                        prompt,
                        _geometry_system_prompt(),
                    )
                    return _parse_figure_comparison(raw)
                except Exception as exc:  # noqa: BLE001 - 连接与结构化输出错误统一重试
                    last_error = exc
                    logger.warning("几何图形对比第 %s 次失败: %s", attempt, exc)
        raise AIMessageError(
            f"几何图形对比失败（已重试 {self.settings.ai_max_retries} 次）: {last_error}"
        )

    def repair_geometry(
        self,
        composite_image_bytes: bytes,
        *,
        text: str,
        draft: GeometryFigureDraft,
        comparison: FigureComparison,
    ) -> GeometryFigureDraft:
        prompt = f"""输入是一张由原图与当前 TikZ 重绘图合成的对比图。请按对比意见修复重绘图，
保留题干和 semantic_spec 中正确的几何语义，只修改必要的语义字段与 TikZ 源码。

题干：{text[:4000]}
当前图形：{json.dumps(draft.model_dump(), ensure_ascii=False)}
对比意见：{json.dumps(comparison.model_dump(), ensure_ascii=False)}

请只输出一个修复后的 JSON 对象：
{GEOMETRY_DRAFT_SCHEMA}"""
        last_error: Exception | None = None
        for attempt in range(1, self.settings.ai_max_retries + 1):
            with track_ai_call("repair_geometry"):
                try:
                    raw = self._complete(
                        composite_image_bytes,
                        "image/png",
                        prompt,
                        _geometry_system_prompt(),
                    )
                    return _parse_geometry_draft(raw)
                except Exception as exc:  # noqa: BLE001 - 连接与结构化输出错误统一重试
                    last_error = exc
                    logger.warning("几何图形修复第 %s 次失败: %s", attempt, exc)
        raise AIMessageError(
            f"几何图形修复失败（已重试 {self.settings.ai_max_retries} 次）: {last_error}"
        )

    def generate_geometry_variant(
        self,
        content: str,
        *,
        knowledge_points: list[str],
        error_category: str,
        question_type: str,
        difficulty: str,
        figure_specs: list,
        context: AnalysisContext | None = None,
        repair_instruction: str = "",
    ) -> GeometryVariant:
        prompt = f"""请基于原错题生成一道新的 K12 几何变式题，只给新题干与所需图形，不给答案。
必须保留考点/错点但可改变图形结构：保留核心知识点、能力要求和学生易错本质，
可以改变点的名称、已知数值、图形形状、拓扑结构和设问情境，但不得把错题退化为无关计算题。
新题干必须与新图形的 semantic_spec 完全一致，题型与难度分别贴近原题要求。

原题干：{content[:4000]}
知识点：{json.dumps(knowledge_points, ensure_ascii=False)}
错因：{error_category}
题型：{question_type}
难度：{difficulty}
原图形规格：{json.dumps(figure_specs, ensure_ascii=False, default=lambda model: model.model_dump())}
"""
        if repair_instruction.strip():
            prompt += f"上一轮校验后的修复要求：{repair_instruction.strip()}\n"
        prompt += f"""
请只输出一个 JSON 对象：
{{
  "question": "完整的新题干，公式使用 LaTeX",
  "figures": ["每个元素是一个图形草稿对象；无图时此数组为 []"]
}}
有图时必须给出全部必要图形，每个图形草稿对象结构如下：
{GEOMETRY_DRAFT_SCHEMA}"""
        last_error: Exception | None = None
        for attempt in range(1, self.settings.ai_max_retries + 1):
            with track_ai_call("generate_geometry_variant"):
                try:
                    raw = self.chat(
                        [
                            {"role": "system", "content": _geometry_system_prompt(context)},
                            {"role": "user", "content": prompt},
                        ]
                    )
                    return _parse_geometry_variant(raw)
                except Exception as exc:  # noqa: BLE001 - 连接与结构化输出错误统一重试
                    last_error = exc
                    logger.warning("几何变式生成第 %s 次失败: %s", attempt, exc)
        raise AIMessageError(
            f"几何变式生成失败（已重试 {self.settings.ai_max_retries} 次）: {last_error}"
        )

    def validate_geometry_variant(
        self,
        rendered_png_bytes: bytes,
        *,
        content: str,
        draft: GeometryFigureDraft,
        original_content: str,
        knowledge_points: list[str],
        error_category: str,
    ) -> FigureComparison:
        prompt = f"""输入是新变式题一张 TikZ 图形的渲染 PNG。请同时核对新题干与图形语义，
确认图中标注、点线关系、已知量与 semantic_spec 和新题干一致；并结合原题确认变式仍保留
相同考点与错点本质。图形结构允许不同，不要求与原图外观一致。

原题干：{original_content[:4000]}
新题干：{content[:4000]}
保留的知识点：{json.dumps(knowledge_points, ensure_ascii=False)}
保留的错因：{error_category}
新图形语义与源码：{json.dumps(draft.model_dump(), ensure_ascii=False)}

请只输出一个 JSON 对象：
{FIGURE_COMPARISON_SCHEMA}"""
        last_error: Exception | None = None
        for attempt in range(1, self.settings.ai_max_retries + 1):
            with track_ai_call("validate_geometry_variant"):
                try:
                    raw = self._complete(
                        rendered_png_bytes,
                        "image/png",
                        prompt,
                        _geometry_system_prompt(),
                    )
                    return _parse_figure_comparison(raw)
                except Exception as exc:  # noqa: BLE001 - 连接与结构化输出错误统一重试
                    last_error = exc
                    logger.warning("几何变式校验第 %s 次失败: %s", attempt, exc)
        raise AIMessageError(
            f"几何变式校验失败（已重试 {self.settings.ai_max_retries} 次）: {last_error}"
        )

    def analyze_text(
        self, text: str, hint: str = "", context: AnalysisContext | None = None
    ) -> QuestionAnalysis:
        """纯文本错题解析（手动录入场景），复用结构化输出约束。"""
        prompt = (
            f"题目内容：\n{text[:4000]}\n\n"
            f"学生补充：{hint or '无'}\n\n"
            f"{JSON_INSTRUCTION}"
        )
        with track_ai_call("analyze_text"):
            raw = self.chat(
                [
                    {"role": "system", "content": build_system_prompt(context)},
                    {"role": "user", "content": prompt},
                ]
            )
        return parse_analysis(raw)

    def analyze_question(
        self,
        image_bytes: bytes,
        mime_type: str = "image/jpeg",
        hint: str = "",
        context: AnalysisContext | None = None,
    ) -> QuestionAnalysis:
        """带重试的结构化错题解析。

        context：K12 录入上下文（学科/年级/地区/教材版本），缺省时提示词与基座一致。
        错因枚举等非法值触发 ValidationError 后按 ai_max_retries 重试。
        """
        prompt = build_user_prompt(hint, context)
        system_prompt = build_system_prompt(context)
        last_error: Exception | None = None

        for attempt in range(1, self.settings.ai_max_retries + 1):
            with track_ai_call("analyze_image") as ctx:
                try:
                    raw = self._complete(image_bytes, mime_type, prompt, system_prompt)
                    analysis = parse_analysis(raw)
                    ctx["ok"] = True
                    return analysis
                except Exception as exc:  # noqa: BLE001 - 统一进入重试
                    last_error = exc
                    logger.warning("AI 调用第 %s 次失败: %s", attempt, exc)
        raise AIMessageError(f"AI 解析失败（已重试 {self.settings.ai_max_retries} 次）: {last_error}")


# 列表字段的宽松上限：与 QuestionAnalysis 的 max_length 保持一致。
# 模型偶尔多给几个标签/知识点属正常波动，截断优于整题解析失败。
_LIST_FIELD_LIMIT = 6


def normalize_math_delimiters(text: str) -> str:
    """把模型偶发输出的 \\(...\\) / \\[...\\] 定界符归一为 $...$ / $$...$$。

    下游渲染（Streamlit st.markdown、移动端 MathText）只认 $ 定界符；
    提示词约束不可靠（模型时而混用），统一在解析边界归一化。
    """
    text = re.sub(r"\\\[(.*?)\\\]", r"$$\1$$", text, flags=re.DOTALL)
    return re.sub(r"\\\((.*?)\\\)", r"$\1$", text, flags=re.DOTALL)


def _geometry_draft_from_dict(candidate: dict) -> GeometryFigureDraft:
    if not isinstance(candidate, dict):
        raise AIMessageError("几何图形草稿必须是 JSON 对象")
    if candidate.get("kind") != "geometry":
        raise AIMessageError("几何图形草稿的 kind 必须为 geometry")
    tikz_source = candidate.get("tikz_source")
    if not isinstance(tikz_source, str) or not tikz_source.strip():
        raise AIMessageError("几何图形草稿的 TikZ 源码不能为空")
    normalized = dict(
        candidate, tikz_source=normalize_math_delimiters(tikz_source.strip())
    )
    assumptions = normalized.get("assumptions")
    if isinstance(assumptions, list):
        normalized["assumptions"] = [
            normalize_math_delimiters(item) if isinstance(item, str) else item
            for item in assumptions
        ]
    return GeometryFigureDraft.model_validate(normalized)


def _parse_geometry_draft(raw: str) -> GeometryFigureDraft:
    candidate = extract_json_block(raw)
    if candidate is None:
        raise AIMessageError("几何图形响应中未找到 JSON 对象")
    return _geometry_draft_from_dict(candidate)


def _parse_geometry_draft_list(raw: str) -> list[GeometryFigureDraft]:
    candidates = extract_json_list(raw)
    if candidates is None:
        raise AIMessageError("几何图形响应中未找到 JSON 数组")
    return [_geometry_draft_from_dict(candidate) for candidate in candidates]


def _parse_figure_comparison(raw: str) -> FigureComparison:
    candidate = extract_json_block(raw)
    if not isinstance(candidate, dict):
        raise AIMessageError("图形对比响应中未找到 JSON 对象")
    if not isinstance(candidate.get("passed"), bool):
        raise AIMessageError("图形对比结果的 passed 必须为布尔值")
    normalized = dict(candidate)
    for key in ("issues", "repair_instruction"):
        value = normalized.get(key)
        if isinstance(value, str):
            normalized[key] = normalize_math_delimiters(value)
        elif isinstance(value, list):
            normalized[key] = [
                normalize_math_delimiters(item) if isinstance(item, str) else item
                for item in value
            ]
    return FigureComparison.model_validate(normalized)


def _parse_geometry_variant(raw: str) -> GeometryVariant:
    candidate = extract_json_block(raw)
    if not isinstance(candidate, dict):
        raise AIMessageError("几何变式响应中未找到 JSON 对象")
    question = candidate.get("question")
    if not isinstance(question, str) or not question.strip():
        raise AIMessageError("几何变式题干不能为空")
    figures = candidate.get("figures", [])
    if not isinstance(figures, list):
        raise AIMessageError("几何变式 figures 必须为数组")
    normalized = dict(
        candidate,
        question=normalize_math_delimiters(question.strip()),
        figures=[_geometry_draft_from_dict(figure) for figure in figures],
    )
    return GeometryVariant.model_validate(normalized)


def parse_analysis(raw: str) -> QuestionAnalysis:
    """从模型响应中稳健地提取 JSON 并校验为 QuestionAnalysis。

    兼容三类输出：纯 JSON、```json 围栏、前后夹杂说明文字。
    列表字段（knowledge_points/tags）超出上限时先去重再截断，不判失败。
    """
    candidate = extract_json_block(raw)
    if candidate is None:
        raise AIMessageError("响应中未找到 JSON 结构")
    _truncate_list_fields(candidate)
    for key in ("analysis", "answer", "mistake_cause", "followup_question", "focused_sub_question"):
        value = candidate.get(key)
        if isinstance(value, str):
            candidate[key] = normalize_math_delimiters(value)
    return QuestionAnalysis.model_validate(candidate)


def _truncate_list_fields(candidate: dict) -> None:
    """把超限的列表字段去重后截断到 _LIST_FIELD_LIMIT（原地修改）。"""
    for key in ("knowledge_points", "tags"):
        value = candidate.get(key)
        if isinstance(value, list) and len(value) > _LIST_FIELD_LIMIT:
            seen: list[str] = []
            for item in value:
                if isinstance(item, str):
                    normalized = item.strip()
                    if normalized and normalized not in seen:
                        seen.append(normalized)
            candidate[key] = seen[:_LIST_FIELD_LIMIT]


# 视觉模型常把 LaTeX 单反斜杠直接写进 JSON 字符串，造成两类故障：
# 1) \i、\m、\v 等非法转义让 json.loads 整体失败（整页切题丢失）；
# 2) \f(\frac)、\b(\beta)、\t(\tan\to)、\n(\ne\nu)、\r(\rho) 恰好是合法转义，
#    被静默解析成换页符/退格/制表符，公式内容被篡改。
# 修复策略：保留结构性转义（\\、\"、\/、\uXXXX）；\b\f\n\r\t 后接小写字母视为
# LaTeX 命令（\frac、\beta、\tan、\ne 等）加倍，否则视为真正的控制符转义保留；
# 其余孤立反斜杠一律加倍。交替分支按顺序匹配，已配对的 \\ 被整体消费。
_JSON_ESC_OR_LONE_RE = re.compile(r'\\\\|\\[\"/]|\\u[0-9a-fA-F]{4}|\\[bfnrt](?![a-z])|\\')

# 已配对的 \\ 之后的第二个 \ 不匹配（负向后顾），避免误伤规范转义的文档
_SUSPICIOUS_LATEX_ESC_RE = re.compile(r'(?<!\\)\\[bfnrt][a-zA-Z]')


def _escape_latex_backslashes(text: str) -> str:
    r"""把疑似 LaTeX 源码的反斜杠加倍：保留 \\、\"、\/、\uXXXX，其余 \ 加倍。"""
    return _JSON_ESC_OR_LONE_RE.sub(
        lambda m: m.group(0) if len(m.group(0)) > 1 else "\\\\", text
    )


def _json_loads_lenient(text: str):
    r"""json.loads 容错版（LaTeX 源码友好）。

    1) 直接解析失败（非法转义）：加倍疑似 LaTeX 反斜杠后重试；
    2) 直接解析成功但原文含 \<字母> 形态的可疑转义（\frac、\beta、\tan 等会被
       静默篡改）：同样改用加倍后重试。
    失败时照常抛 json.JSONDecodeError，由调用方按策略降级。
    """
    try:
        parsed = json.loads(text)
        if not _SUSPICIOUS_LATEX_ESC_RE.search(text):
            return parsed
    except json.JSONDecodeError:
        pass
    return json.loads(_escape_latex_backslashes(text))


def extract_json_block(raw: str) -> dict | None:
    if not raw:
        return None
    text = raw.strip()
    # 1) 直接解析
    try:
        return _json_loads_lenient(text)
    except json.JSONDecodeError:
        pass
    # 2) 提取 ```json 围栏
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fenced:
        try:
            return _json_loads_lenient(fenced.group(1))
        except json.JSONDecodeError:
            pass
    # 3) 贪婪匹配第一个平衡的花括号块
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        try:
            return _json_loads_lenient(match.group(0))
        except json.JSONDecodeError:
            pass
    return None


def extract_json_list(raw: str) -> list | None:
    """从模型响应中提取 JSON 数组（兼容纯 JSON / ```json 围栏 / 夹杂说明文字）。"""
    if not raw:
        return None
    text = raw.strip()
    # 1) 直接解析
    try:
        parsed = _json_loads_lenient(text)
        return parsed if isinstance(parsed, list) else None
    except json.JSONDecodeError:
        pass
    # 2) 提取 ```json 围栏
    fenced = re.search(r"```(?:json)?\s*(\[.*?\])\s*```", text, re.DOTALL)
    if fenced:
        try:
            parsed = _json_loads_lenient(fenced.group(1))
            return parsed if isinstance(parsed, list) else None
        except json.JSONDecodeError:
            pass
    # 3) 贪婪匹配第一个方括号块
    match = re.search(r"\[.*\]", text, re.DOTALL)
    if match:
        try:
            parsed = _json_loads_lenient(match.group(0))
            return parsed if isinstance(parsed, list) else None
        except json.JSONDecodeError:
            pass
    return None
