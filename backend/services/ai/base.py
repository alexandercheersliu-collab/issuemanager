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
  "followup_question": "一道考查相同知识点的变式练习题（只给题目，不给答案）",
  "question_type": "题型，如：选择题/填空题/判断题/计算题/解答题/证明题（判断不出留空串）",
  "chapter": "所属教材章节，如：一元二次方程（判断不出留空串）",
  "error_category": "学生错因分类，必须是以下枚举之一：{' / '.join(ERROR_CATEGORIES)}；无法判断用 null"
}}"""


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
        "所有讲解使用简体中文，公式使用 LaTeX（$...$ 行内，$$...$$ 独立）。"
        "必须严格输出 JSON，不要输出任何 JSON 以外的内容。"
    )


def build_user_prompt(hint: str, context: AnalysisContext | None = None) -> str:
    subject_name = (
        SUBJECT_NAMES.get(context.subject, context.subject) if context else "数学"
    ) or "数学"
    prompt = f"请分析这张图片中的{subject_name}错题。\n"
    if hint:
        prompt += f"学生的补充说明：{hint}\n"
    return prompt + JSON_INSTRUCTION


class AIMessageError(RuntimeError):
    """模型未返回可解析的结构化结果。"""


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


def parse_analysis(raw: str) -> QuestionAnalysis:
    """从模型响应中稳健地提取 JSON 并校验为 QuestionAnalysis。

    兼容三类输出：纯 JSON、```json 围栏、前后夹杂说明文字。
    """
    candidate = extract_json_block(raw)
    if candidate is None:
        raise AIMessageError("响应中未找到 JSON 结构")
    return QuestionAnalysis.model_validate(candidate)


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
