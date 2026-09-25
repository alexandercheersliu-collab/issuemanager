"""看板学科筛选与 K12 展示摘要的服务层测试。"""
from __future__ import annotations

from types import SimpleNamespace

from frontend.components import k12_meta_line


def test_subjects_for_user(question_service, student_user):
    uid = student_user.id
    question_service.create_manual_question(uid, content_markdown="数学题A", subject="math")
    question_service.create_manual_question(uid, content_markdown="物理题A", subject="physics")

    subjects = question_service.subjects_for_user(uid)
    assert "math" in subjects
    assert "physics" in subjects
    assert subjects == sorted(subjects)  # 升序去重


def test_dashboard_stats_subject_filter(question_service, student_user):
    uid = student_user.id
    question_service.create_manual_question(
        uid, content_markdown="看板筛选-数学题", subject="math", tags=["看板数学"]
    )
    physics_q = question_service.create_manual_question(
        uid, content_markdown="看板筛选-物理题", subject="physics", tags=["看板物理"]
    )
    # 给物理题制造一条复习记录，验证日志也随学科过滤
    question_service.grade_review(physics_q.id, uid, "good")

    full = question_service.dashboard_stats(uid)
    physics = question_service.dashboard_stats(uid, subject="physics")
    math = question_service.dashboard_stats(uid, subject="math")

    contents_full = {q.content_markdown for q in question_service.list_questions(uid, semantic=False)}
    assert {"看板筛选-数学题", "看板筛选-物理题"} <= contents_full

    # 学科过滤后：只含对应学科的题
    physics_tags = {s.tag for s in physics["tag_stats"]}
    assert "看板物理" in physics_tags
    assert "看板数学" not in physics_tags

    math_tags = {s.tag for s in math["tag_stats"]}
    assert "看板数学" in math_tags
    assert "看板物理" not in math_tags

    # 复习记录随学科过滤：物理看板能看到那次复习，数学看板看不到
    assert physics["reviewed"] >= 1
    # 总量口径：各学科过滤结果互不重叠且不超过全量
    # （测试库共享，其他用例可能录入 chemistry/biology 等学科，不用全等断言）
    assert math["total"] >= 1
    assert physics["total"] >= 1
    assert math["total"] + physics["total"] <= full["total"]


def test_dashboard_stats_default_unchanged(question_service, student_user):
    """不传 subject 时行为与基座一致（全量口径）。"""
    uid = student_user.id
    full = question_service.dashboard_stats(uid)
    explicit_none = question_service.dashboard_stats(uid, subject=None)
    assert full["total"] == explicit_none["total"]
    assert full["reviewed"] == explicit_none["reviewed"]


def test_k12_meta_line_full():
    q = SimpleNamespace(
        subject="math", grade=9, region="北京", textbook_version="人教版",
        question_type="解答题", chapter="一元二次方程", error_category="概念不清",
        source_doc="期末卷.pdf#3",
    )
    line = k12_meta_line(q)
    for bit in ("学科：数学", "年级：9 年级", "地区：北京", "教材：人教版",
                "题型：解答题", "章节：一元二次方程", "错因：概念不清", "来源：期末卷.pdf#3"):
        assert bit in line


def test_k12_meta_line_sparse_and_unknown_subject():
    q = SimpleNamespace(
        subject="astronomy", grade=None, region=None, textbook_version=None,
        question_type=None, chapter=None, error_category=None, source_doc=None,
    )
    assert k12_meta_line(q) == "学科：astronomy"  # 未知学科代码原样展示

    empty = SimpleNamespace(
        subject=None, grade=None, region=None, textbook_version=None,
        question_type=None, chapter=None, error_category=None, source_doc=None,
    )
    assert k12_meta_line(empty) == ""
