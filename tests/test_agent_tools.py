"""Agent 工具集测试（服务层真库，不出网）。"""
from __future__ import annotations

import json

import pytest

from backend.database import SessionLocal
from backend.services.agent_tools import build_tools
from backend.services.question_service import QuestionService


@pytest.fixture
def service():
    return QuestionService(session_factory=SessionLocal)


@pytest.fixture
def tools(service, student_user):
    return {t.name: t for t in build_tools(service, student_user.id)}


def test_tools_have_valid_schema(tools):
    assert len(tools) >= 6
    for tool in tools.values():
        schema = tool.openai_schema()
        assert schema["type"] == "function"
        assert schema["function"]["name"] == tool.name
        assert schema["function"]["description"]
        mcp = tool.mcp_schema()
        assert mcp["inputSchema"] == tool.parameters


def test_add_and_search_roundtrip(tools, student_user):
    add = tools["add_text_question"].handler
    result = json.loads(add(content_markdown="已知 a>b，求 a+b 的范围", answer="不确定", tags="不等式"))
    assert result["id"] > 0

    search = tools["search_questions"]
    hits = json.loads(search(keyword="a+b"))
    assert hits["count"] >= 1


def test_grade_and_due_roundtrip(tools, student_user):
    add = tools["add_text_question"].handler
    qid = json.loads(add(content_markdown="去重测试题"))["id"]

    graded = json.loads(tools["grade_question"].handler(question_id=qid, grade="good"))
    assert graded["reps"] == 1

    due = json.loads(tools["list_due_questions"]())
    assert all(q["id"] != qid for q in due["questions"])  # 刚复习过，不再到期


def test_grade_invalid_returns_message(tools, student_user):
    result = tools["grade_question"].handler(question_id=99999, grade="good")
    assert "不存在" in result


def test_weekly_report_and_tag_usage(tools, student_user):
    report = json.loads(tools["get_weekly_report"]())
    assert "created" in report and "reviews" in report

    add = tools["add_text_question"].handler
    add(content_markdown="标签统计题", tags="工具标签")
    usage = json.loads(tools["get_tag_usage"]())
    assert usage.get("工具标签", 0) >= 1


def test_tools_bound_to_user_isolation(tools, service, db_session, student_user):
    from backend.models.orm import User
    from backend.utils.security import hash_password

    other = User(username="agent_other", password_hash=hash_password("x", 4), role="student")
    db_session.add(other)
    db_session.commit()

    saved, _ = service.analyze_and_save(
        student_user.id, b"\xff\xd8" + b"x" * 16, user_tags=["隔离测试"]
    )
    search = tools["search_questions"]
    own_hits = json.loads(search(keyword="隔离测试"))
    assert any(q["id"] == saved.id for q in own_hits["questions"])
    # agent 工具绑定的是 student_user，搜不到其他用户的题
    other_tools = {t.name: t for t in build_tools(service, other.id)}
    other_hits = json.loads(other_tools["search_questions"].handler(keyword="隔离测试"))
    assert all(q["id"] != saved.id for q in other_hits["questions"])


def test_search_results_include_stem_snippet(tools, student_user):
    """搜索结果必须带题干摘要：否则 LLM 分不清哪道题是哪道，更无法取原题。"""
    add = tools["add_text_question"].handler
    add(content_markdown="计算 0.9×19 的竖式过程", answer="17.1", tags="小数乘法")

    hits = json.loads(tools["search_questions"].handler(keyword="小数乘法"))
    assert hits["count"] >= 1
    stems = [q["stem"] for q in hits["questions"]]
    assert any("0.9×19" in s for s in stems)


def test_get_question_returns_full_content(tools, student_user):
    """搜题后按 ID 取原题：完整题面 + 解析 + 答案都要返回。"""
    add = tools["add_text_question"].handler
    qid = json.loads(
        add(content_markdown="已知 x+y=10，求 xy 最大值\n\n---\n\n用均值不等式推导", answer="25")
    )["id"]

    detail = json.loads(tools["get_question"].handler(question_id=qid))
    assert detail["id"] == qid
    assert "已知 x+y=10" in detail["content_markdown"]
    assert "用均值不等式推导" in detail["content_markdown"]
    assert detail["answer"] == "25"


def test_get_question_missing_returns_message(tools, student_user):
    result = tools["get_question"].handler(question_id=99999)
    assert "不存在" in result
