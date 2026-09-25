"""错题本来源筛选测试：list_questions 按 source / source_doc（文档名前缀）过滤。

共享测试库下用「包含/不包含」成员断言，不做全量等值断言。
"""
from __future__ import annotations


def _seed(question_service, user_id: int) -> None:
    question_service.create_manual_question(
        user_id,
        content_markdown="来源筛选种子·整卷题一",
        answer="1",
        source="document",
        source_doc="筛选卷A.pdf#1",
    )
    question_service.create_manual_question(
        user_id,
        content_markdown="来源筛选种子·整卷题二",
        answer="2",
        source="document",
        source_doc="筛选卷A.pdf#2",
    )
    question_service.create_manual_question(
        user_id,
        content_markdown="来源筛选种子·手动题",
        answer="3",
        source="manual",
    )


def test_filter_by_source(question_service, student_user):
    _seed(question_service, student_user.id)

    docs = question_service.list_questions(
        student_user.id, source="document", semantic=False
    )
    contents = [q.content_markdown for q in docs]
    assert any("整卷题一" in c for c in contents)
    assert any("整卷题二" in c for c in contents)
    assert not any("手动题" in c for c in contents)

    manuals = question_service.list_questions(
        student_user.id, source="manual", semantic=False
    )
    assert any("手动题" in q.content_markdown for q in manuals)
    assert not any("整卷题" in q.content_markdown for q in manuals)


def test_filter_by_source_doc_prefix(question_service, student_user):
    _seed(question_service, student_user.id)

    hits = question_service.list_questions(
        student_user.id, source_doc="筛选卷A.pdf", semantic=False
    )
    hit_docs = {q.source_doc for q in hits if "来源筛选种子" in q.content_markdown}
    assert hit_docs == {"筛选卷A.pdf#1", "筛选卷A.pdf#2"}

    misses = question_service.list_questions(
        student_user.id, source_doc="不存在的卷.pdf", semantic=False
    )
    assert not any("来源筛选种子" in q.content_markdown for q in misses)


def test_count_for_user_with_source_filters(question_service, student_user):
    _seed(question_service, student_user.id)

    total = question_service.count_for_user(student_user.id)
    by_doc = question_service.count_for_user(student_user.id, source_doc="筛选卷A.pdf")
    assert 2 <= by_doc <= total
    by_source = question_service.count_for_user(student_user.id, source="document")
    assert by_source >= 2
