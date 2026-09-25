"""提示词参数化测试：K12 上下文组装、非法错因枚举重试、Mock 新字段。"""
from __future__ import annotations

import json

import pytest

from backend.models.schemas import AIProviderInfo
from backend.services.ai.base import (
    SYSTEM_PROMPT,
    AIMessageError,
    AnalysisContext,
    BaseAIProvider,
    build_system_prompt,
    build_user_prompt,
    parse_analysis,
)
from backend.services.ai.mock import MockProvider

_VALID = {
    "knowledge_points": ["判别式"],
    "analysis": "先写出判别式表达式，再解不等式。",
    "answer": "k ≤ 1/2",
    "question_type": "解答题",
    "chapter": "一元二次方程",
    "error_category": "概念不清",
}


class _StubProvider(BaseAIProvider):
    """记录调用并返回预置响应的测试替身。"""

    def __init__(self, responses: list[str]):
        super().__init__()
        self.responses = responses
        self.calls: list[dict] = []

    def _complete(self, image_bytes, mime_type, prompt, system_prompt=SYSTEM_PROMPT):
        self.calls.append({"prompt": prompt, "system_prompt": system_prompt})
        return self.responses[min(len(self.calls) - 1, len(self.responses) - 1)]

    def chat(self, messages):
        return "stub"

    def provider_info(self):
        return AIProviderInfo(provider="stub", model="stub", configured=True, demo_mode=True)


# ---------- 系统提示词组装 ----------

def test_default_context_matches_legacy_prompt():
    """无上下文 / 全缺省上下文时，提示词与基座逐字一致。"""
    assert build_system_prompt(None) == SYSTEM_PROMPT
    assert build_system_prompt(AnalysisContext()) == SYSTEM_PROMPT
    assert build_user_prompt("").startswith("请分析这张图片中的数学错题。")


def test_contextual_prompt_includes_k12_bits():
    context = AnalysisContext(subject="physics", grade=9, region="北京", textbook_version="人教版")
    prompt = build_system_prompt(context)
    assert "物理老师" in prompt
    assert "9 年级" in prompt
    assert "北京" in prompt
    assert "人教版" in prompt
    assert "严格输出 JSON" in prompt  # 输出约束保留


def test_contextual_prompt_subject_only():
    prompt = build_system_prompt(AnalysisContext(subject="english"))
    assert "英语老师" in prompt
    assert "年级" not in prompt


def test_user_prompt_follows_subject():
    prompt = build_user_prompt("", AnalysisContext(subject="chemistry"))
    assert prompt.startswith("请分析这张图片中的化学错题。")


# ---------- 上下文传入解析管线 ----------

def test_analyze_question_passes_system_prompt_to_provider():
    context = AnalysisContext(subject="physics", grade=8)
    provider = _StubProvider([json.dumps(_VALID, ensure_ascii=False)])
    analysis = provider.analyze_question(b"img", "image/png", context=context)
    assert analysis.error_category == "概念不清"
    assert len(provider.calls) == 1
    sent = provider.calls[0]["system_prompt"]
    assert "物理老师" in sent and "8 年级" in sent


def test_analyze_question_default_context_sends_legacy_prompt():
    provider = _StubProvider([json.dumps(_VALID, ensure_ascii=False)])
    provider.analyze_question(b"img", "image/png")
    assert provider.calls[0]["system_prompt"] == SYSTEM_PROMPT


# ---------- 非法错因枚举 → 重试 ----------

def test_invalid_error_category_triggers_retry():
    """模型输出非法 error_category → ValidationError → 按 ai_max_retries 重试。"""
    bad = json.dumps(dict(_VALID, error_category="题目太难"), ensure_ascii=False)
    good = json.dumps(_VALID, ensure_ascii=False)
    provider = _StubProvider([bad, good])
    analysis = provider.analyze_question(b"img", "image/png")
    assert analysis.error_category == "概念不清"
    assert len(provider.calls) == 2  # 第一次失败，第二次成功


def test_persistent_invalid_error_category_exhausts_retries():
    bad = json.dumps(dict(_VALID, error_category="不会写"), ensure_ascii=False)
    provider = _StubProvider([bad])
    with pytest.raises(AIMessageError):
        provider.analyze_question(b"img", "image/png")
    assert len(provider.calls) == provider.settings.ai_max_retries


# ---------- Mock 演示模式带新字段 ----------

def test_mock_provider_includes_k12_fields():
    provider = MockProvider()
    analysis = parse_analysis(provider._complete(b"fake", "image/jpeg", "prompt"))
    assert analysis.question_type == "解答题"
    assert analysis.chapter == "一元二次方程"
    assert analysis.error_category == "概念不清"


def test_mock_analyze_text_accepts_context():
    provider = MockProvider()
    analysis = provider.analyze_text("任意题目", context=AnalysisContext(subject="math", grade=7))
    assert analysis.error_category == "概念不清"
