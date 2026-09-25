"""K12 元数据链路测试：schema 校验、落库、筛选、编辑、AI 录入上下文。"""
from __future__ import annotations

import pydantic
import pytest

from backend.models.schemas import ERROR_CATEGORIES, QuestionAnalysis

PNG_1PX = bytes.fromhex(
    "89504e470d0a1a0a0000000d494844520000000100000001080600000"
    "01f15c4890000000d49444154789c626001000000ffff030000060005"
    "57bfabd40000000049454e44ae426082"
)


def _analysis_payload(**overrides) -> dict:
    payload = {
        "knowledge_points": ["判别式"],
        "analysis": "先写出判别式表达式，再解不等式。",
        "answer": "k ≤ 1/2",
    }
    payload.update(overrides)
    return payload


# ---------- schema 校验 ----------

def test_error_category_accepts_enum_values():
    for category in ERROR_CATEGORIES:
        analysis = QuestionAnalysis(**_analysis_payload(error_category=category))
        assert analysis.error_category == category


def test_error_category_rejects_illegal_value():
    """非法错因枚举必须触发 ValidationError（驱动 AI 重试机制）。"""
    with pytest.raises(pydantic.ValidationError):
        QuestionAnalysis(**_analysis_payload(error_category="题目太难"))


def test_analysis_k12_fields_default_empty():
    analysis = QuestionAnalysis(**_analysis_payload())
    assert analysis.question_type == ""
    assert analysis.chapter == ""
    assert analysis.error_category is None


# ---------- 手动录入落库与筛选 ----------

def test_manual_entry_persists_k12_metadata(question_service, student_user):
    out = question_service.create_manual_question(
        student_user.id,
        content_markdown="已知 x^2=9，求 x。",
        answer="x=±3",
        subject="physics",
        grade=9,
        region="北京",
        textbook_version="人教版",
        question_type="填空题",
        chapter="二次根式",
        error_category="计算失误",
        source_doc="期末卷.pdf#3",
    )
    assert out.subject == "physics"
    assert out.grade == 9
    assert out.region == "北京"
    assert out.textbook_version == "人教版"
    assert out.question_type == "填空题"
    assert out.chapter == "二次根式"
    assert out.error_category == "计算失误"
    assert out.source_doc == "期末卷.pdf#3"

    reloaded = question_service.get_question(out.id, student_user.id)
    assert reloaded is not None
    assert reloaded.subject == "physics"
    assert reloaded.grade == 9


def test_manual_entry_defaults_to_math(question_service, student_user):
    out = question_service.create_manual_question(
        student_user.id, content_markdown="1+1=?"
    )
    assert out.subject == "math"
    assert out.grade is None
    assert out.error_category is None


def test_list_questions_k12_filters(question_service, student_user):
    uid = student_user.id
    question_service.create_manual_question(
        uid, content_markdown="数学七年级题", subject="math", grade=7, error_category="概念不清"
    )
    question_service.create_manual_question(
        uid, content_markdown="物理九年级题", subject="physics", grade=9, error_category="计算失误"
    )
    question_service.create_manual_question(
        uid, content_markdown="数学九年级题", subject="math", grade=9, error_category="粗心其他"
    )

    # 注：测试库为 session 级共享（其他用例也会给 demo 用户录题），
    # 这里用「包含/不包含」断言而非全等，避免用例间相互污染。
    by_subject = {q.content_markdown for q in question_service.list_questions(uid, subject="math", semantic=False)}
    assert {"数学七年级题", "数学九年级题"} <= by_subject
    assert "物理九年级题" not in by_subject

    by_grade = {q.content_markdown for q in question_service.list_questions(uid, grade=9, semantic=False)}
    assert {"物理九年级题", "数学九年级题"} <= by_grade
    assert "数学七年级题" not in by_grade

    by_category = {q.content_markdown for q in question_service.list_questions(uid, error_category="计算失误", semantic=False)}
    assert "物理九年级题" in by_category
    assert "数学七年级题" not in by_category

    combined = {q.content_markdown for q in question_service.list_questions(uid, subject="math", grade=9, semantic=False)}
    assert "数学九年级题" in combined
    assert "数学七年级题" not in combined
    assert "物理九年级题" not in combined

    assert question_service.count_for_user(uid, subject="math") >= 2


# ---------- 编辑 ----------

def test_update_question_k12_metadata(question_service, student_user):
    out = question_service.create_manual_question(
        student_user.id, content_markdown="待编辑的题"
    )
    updated = question_service.update_question(
        out.id,
        student_user.id,
        subject="chemistry",
        grade=11,
        region="上海",
        textbook_version="沪教版",
        question_type="实验题",
        chapter="氧化还原",
        error_category="方法不会",
    )
    assert updated is not None
    assert updated.subject == "chemistry"
    assert updated.grade == 11
    assert updated.chapter == "氧化还原"
    assert updated.error_category == "方法不会"


# ---------- AI 图片录入上下文 ----------

def test_analyze_and_save_stores_k12_context(question_service, student_user):
    """拍照录入：subject/grade/region/textbook_version 随解析落库。"""
    out, _analysis = question_service.analyze_and_save(
        student_user.id,
        PNG_1PX,
        mime_type="image/png",
        subject="math",
        grade=8,
        region="江苏",
        textbook_version="苏科版",
    )
    assert out.subject == "math"
    assert out.grade == 8
    assert out.region == "江苏"
    assert out.textbook_version == "苏科版"

    reloaded = question_service.get_question(out.id, student_user.id)
    assert reloaded is not None
    assert reloaded.grade == 8
    assert reloaded.textbook_version == "苏科版"
