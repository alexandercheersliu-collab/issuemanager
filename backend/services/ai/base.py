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
- 页眉页脚、注意事项、答题区提示等非题目内容不要输出。"""


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
        return reply.strip()

    def segment_page(self, image_bytes: bytes, mime_type: str = "image/png") -> list[dict]:
        """视觉切题（策略 A）：识别一页试卷图像中的题目列表。

        返回 [{"number": str, "text": str, "continued_from_prev": bool,
        "continues_to_next": bool, "likely_wrong": bool}, ...]；
        likely_wrong 为错题预判（卷面有作答痕迹且疑似被判错），模型不给该字段时默认 False；
        输出非法时抛 AIMessageError（调用方重试/降级）。
        """
        with track_ai_call("segment_page"):
            raw = self._complete(image_bytes, mime_type, SEGMENT_INSTRUCTION, SEGMENT_SYSTEM_PROMPT)
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
                    "text": str(item.get("text", "") or "").strip(),
                    "continued_from_prev": bool(item.get("continued_from_prev", False)),
                    "continues_to_next": bool(item.get("continues_to_next", False)),
                    "likely_wrong": bool(item.get("likely_wrong", False)),
                }
            )
        return segments

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


def parse_analysis(raw: str) -> QuestionAnalysis:
    """从模型响应中稳健地提取 JSON 并校验为 QuestionAnalysis。

    兼容三类输出：纯 JSON、```json 围栏、前后夹杂说明文字。
    列表字段（knowledge_points/tags）超出上限时先去重再截断，不判失败。
    """
    candidate = extract_json_block(raw)
    if candidate is None:
        raise AIMessageError("响应中未找到 JSON 结构")
    _truncate_list_fields(candidate)
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


def extract_json_block(raw: str) -> dict | None:
    if not raw:
        return None
    text = raw.strip()
    # 1) 直接解析
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # 2) 提取 ```json 围栏
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fenced:
        try:
            return json.loads(fenced.group(1))
        except json.JSONDecodeError:
            pass
    # 3) 贪婪匹配第一个平衡的花括号块
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
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
        parsed = json.loads(text)
        return parsed if isinstance(parsed, list) else None
    except json.JSONDecodeError:
        pass
    # 2) 提取 ```json 围栏
    fenced = re.search(r"```(?:json)?\s*(\[.*?\])\s*```", text, re.DOTALL)
    if fenced:
        try:
            parsed = json.loads(fenced.group(1))
            return parsed if isinstance(parsed, list) else None
        except json.JSONDecodeError:
            pass
    # 3) 贪婪匹配第一个方括号块
    match = re.search(r"\[.*\]", text, re.DOTALL)
    if match:
        try:
            parsed = json.loads(match.group(0))
            return parsed if isinstance(parsed, list) else None
        except json.JSONDecodeError:
            pass
    return None
