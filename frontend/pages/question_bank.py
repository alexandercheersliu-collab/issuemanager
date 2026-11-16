"""标准题库管理：教师/admin 搜索、编辑和删除未引用标准题。"""
from __future__ import annotations

import streamlit as st

from backend.models.schemas import (
    SUBJECT_NAMES,
    CanonicalQuestionOut,
    CanonicalQuestionUpdate,
)
from backend.services.question_bank import QuestionBankConflictError
from frontend.common import (
    get_question_bank_service,
    is_admin,
    page_header,
    render_markdown,
    safe_truncate_math,
)

_PAGE_SIZE = 20
_ORIGINS = {"teacher": "教师", "student": "学生", "seed": "种子"}
_DIFFICULTIES = {"easy": "简单", "medium": "中等", "hard": "困难"}


def _split_list(value: str) -> list[str]:
    return [item.strip() for item in value.replace("、", ",").split(",") if item.strip()]


def _delete_error(exc: Exception) -> None:
    if isinstance(exc, QuestionBankConflictError):
        st.error(f"删除失败（409）：{exc}")
    else:
        st.error(str(exc))


@st.dialog("确认删除标准题")
def _confirm_delete(user: dict, question: CanonicalQuestionOut) -> None:
    service = get_question_bank_service()
    if is_admin(user) and question.reference_count > 0:
        impact_key = f"qb_delete_impact_{question.id}"
        if impact_key not in st.session_state:
            try:
                st.session_state[impact_key] = service.preview_canonical_delete(
                    user["id"], question.id
                )
            except (ValueError, PermissionError) as exc:
                _delete_error(exc)
                return
        impact = st.session_state[impact_key]
        st.warning("删除标准题不可恢复；所有受影响个人错题将解除引用并保留当前快照。")
        st.markdown(safe_truncate_math(question.stem_markdown, 160))
        st.markdown(
            f"受影响学生：{len(impact.affected_students)} 人 · "
            f"个人错题：{impact.reference_count} 道"
        )
        for student in impact.affected_students:
            question_ids = "、".join(str(qid) for qid in student["question_ids"])
            st.markdown(f"- **{student['username']}** · 个人错题 ID：{question_ids}")
        expected = f"删除标准题 {question.id}"
        st.caption("请输入以下完整确认短语：")
        st.code(expected, language=None)
        confirmation = st.text_input(
            "输入确认短语",
            key=f"qb_delete_confirmation_{question.id}",
            autocomplete="off",
        )
        if st.button(
            "确认删除并保留个人快照",
            key=f"qb_confirm_snapshot_delete_{question.id}",
            type="primary",
            width="stretch",
            disabled=confirmation != expected,
        ):
            try:
                deleted = service.delete_canonical_with_snapshots(
                    user["id"],
                    question.id,
                    expected_reference_count=impact.reference_count,
                    confirmation=confirmation,
                )
            except (QuestionBankConflictError, ValueError, PermissionError) as exc:
                if isinstance(exc, QuestionBankConflictError):
                    st.session_state.pop(impact_key, None)
                _delete_error(exc)
            else:
                vector_pending = service.has_pending_vector_sync(
                    user["id"], question.id, operation="delete"
                )
                asset_pending = service.has_pending_asset_cleanup(user["id"], question.id)
                prefix = f"标准题已删除，{deleted.reference_count} 道个人快照已保留，"
                if asset_pending and vector_pending:
                    notice = prefix + "但资源文件和向量索引清理待重试；已分别记录对账任务。"
                elif asset_pending:
                    notice = prefix + "但资源文件清理待重试；已记录对账任务。"
                elif vector_pending:
                    notice = prefix + "但向量清理待重试；已记录对账任务。"
                else:
                    notice = prefix + "资源和向量索引已同步清理。"
                st.session_state["qb_notice"] = notice
                st.session_state["qb_notice_level"] = (
                    "warning" if asset_pending or vector_pending else "success"
                )
                st.session_state.pop(impact_key, None)
                st.rerun()
        return

    st.warning("此操作只适用于引用数为零的标准题，删除后不可恢复。")
    st.markdown(safe_truncate_math(question.stem_markdown, 160))
    st.caption(f"当前引用数：{question.reference_count}")
    if st.button(
        "确认删除",
        key=f"qb_confirm_delete_{question.id}",
        type="primary",
        width="stretch",
    ):
        try:
            service.delete_unreferenced(user["id"], question.id)
        except (QuestionBankConflictError, ValueError, PermissionError) as exc:
            _delete_error(exc)
        else:
            vector_pending = service.has_pending_vector_sync(
                user["id"], question.id, operation="delete"
            )
            asset_pending = service.has_pending_asset_cleanup(user["id"], question.id)
            if asset_pending and vector_pending:
                notice = "标准题已删除，但资源文件和向量索引清理待重试；已分别记录对账任务。"
            elif asset_pending:
                notice = "标准题已删除，但资源文件清理待重试；已记录对账任务。"
            elif vector_pending:
                notice = "标准题已删除，但向量清理待重试；已记录对账任务。"
            else:
                notice = "标准题已删除，资源和向量索引已同步清理。"
            st.session_state["qb_notice"] = notice
            st.session_state["qb_notice_level"] = (
                "warning" if asset_pending or vector_pending else "success"
            )
            st.rerun()


def _render_detail(user: dict, selected: CanonicalQuestionOut) -> None:
    service = get_question_bank_service()
    try:
        question = service.get_canonical(user["id"], selected.id)
        assets = service.list_canonical_assets(user["id"], selected.id)
    except (ValueError, PermissionError) as exc:
        st.error(str(exc))
        return

    st.divider()
    st.subheader("标准题详情")
    st.caption(
        f"ID {question.id} · 引用数 {question.reference_count} · "
        f"来源 {_ORIGINS.get(question.origin, question.origin)}"
    )
    st.markdown("#### 完整题干")
    render_markdown(question.stem_markdown)
    st.markdown("#### 标准解析")
    render_markdown(question.analysis_markdown or "暂无解析")
    st.markdown(f"**答案：** {question.answer or '暂无答案'}")

    original_assets = [asset for asset in assets if asset.kind == "teacher_original"]
    if original_assets:
        st.markdown("#### 教师原始图片")
        for asset in original_assets:
            try:
                content = service.read_canonical_asset(user["id"], asset.id)
            except (ValueError, PermissionError) as exc:
                st.error(str(exc))
            else:
                st.image(
                    content,
                    alt=f"标准题 {question.id} 的教师原始图片",
                    width="content",
                )

    editor = st.expander("编辑标准内容", expanded=False)
    with editor.form(f"qb_edit_form_{question.id}"):
        stem = st.text_area(
            "题干（Markdown）",
            value=question.stem_markdown,
            height=160,
            key=f"qb_edit_stem_{question.id}",
        )
        answer = st.text_input(
            "答案", value=question.answer, key=f"qb_edit_answer_{question.id}"
        )
        analysis = st.text_area(
            "解析（Markdown）",
            value=question.analysis_markdown,
            height=220,
            key=f"qb_edit_analysis_{question.id}",
        )
        knowledge_points = st.text_input(
            "知识点（逗号分隔）",
            value="、".join(question.knowledge_points),
            key=f"qb_edit_points_{question.id}",
        )
        tags = st.text_input(
            "标准标签（逗号分隔）",
            value="、".join(question.tags),
            key=f"qb_edit_tags_{question.id}",
        )
        question_type = st.text_input(
            "题型",
            value=question.question_type or "",
            key=f"qb_edit_type_{question.id}",
        )
        chapter = st.text_input(
            "章节", value=question.chapter or "", key=f"qb_edit_chapter_{question.id}"
        )
        subject_col, grade_col, difficulty_col = st.columns(3)
        with subject_col:
            subject_options = list(SUBJECT_NAMES)
            subject = st.selectbox(
                "学科",
                subject_options,
                index=subject_options.index(question.subject)
                if question.subject in subject_options
                else 0,
                format_func=lambda value: SUBJECT_NAMES.get(value, value),
                key=f"qb_edit_subject_{question.id}",
            )
        with grade_col:
            grade = st.number_input(
                "年级（0 表示未设置）",
                min_value=0,
                max_value=12,
                value=question.grade or 0,
                key=f"qb_edit_grade_{question.id}",
            )
        with difficulty_col:
            difficulty_options = list(_DIFFICULTIES)
            difficulty = st.selectbox(
                "难度",
                difficulty_options,
                index=difficulty_options.index(question.difficulty),
                format_func=lambda value: _DIFFICULTIES[value],
                key=f"qb_edit_difficulty_{question.id}",
            )
        region = st.text_input(
            "地区", value=question.region or "", key=f"qb_edit_region_{question.id}"
        )
        textbook_version = st.text_input(
            "教材版本",
            value=question.textbook_version or "",
            key=f"qb_edit_textbook_{question.id}",
        )
        if st.form_submit_button("保存标准题", type="primary", width="stretch"):
            updates = CanonicalQuestionUpdate(
                stem_markdown=stem,
                answer=answer,
                analysis_markdown=analysis,
                knowledge_points=_split_list(knowledge_points),
                tags=_split_list(tags),
                question_type=question_type.strip() or None,
                chapter=chapter.strip() or None,
                subject=subject,
                grade=int(grade) or None,
                difficulty=difficulty,
                region=region.strip() or None,
                textbook_version=textbook_version.strip() or None,
            )
            try:
                service.update_canonical(user["id"], question.id, updates)
            except (ValueError, PermissionError) as exc:
                st.error(str(exc))
            else:
                pending = service.has_pending_vector_sync(
                    user["id"], question.id, operation="upsert"
                )
                st.session_state["qb_notice"] = (
                    "标准题已保存，但向量同步待重试；已记录对账任务。"
                    if pending
                    else "标准题已保存，向量索引已同步更新。"
                )
                st.session_state["qb_notice_level"] = "warning" if pending else "success"
                st.rerun()

    if st.button(
        "删除当前标准题",
        key=f"qb_detail_delete_{question.id}",
        type="secondary",
        width="stretch",
    ):
        _confirm_delete(user, question)


def render_question_bank_page(user: dict) -> None:
    service = get_question_bank_service()
    page_header("标准题库", "搜索并维护全系统共享标准题 · 仅教师和管理员可用")
    if notice := st.session_state.pop("qb_notice", None):
        if st.session_state.pop("qb_notice_level", "success") == "warning":
            st.warning(notice)
        else:
            st.success(notice)
    st.session_state.setdefault("qb_page", 0)

    with st.form("qb_search_form", border=False):
        keyword = st.text_input("关键词", key="qb_keyword", type="search")
        semantic = st.checkbox("使用语义搜索", key="qb_semantic")
        subject_col, grade_col, type_col = st.columns(3)
        with subject_col:
            subject_options = [None, *SUBJECT_NAMES.keys()]
            subject = st.selectbox(
                "学科筛选",
                subject_options,
                format_func=lambda value: "全部学科" if value is None else SUBJECT_NAMES[value],
                key="qb_subject",
            )
        with grade_col:
            grade_text = st.text_input("年级筛选（可留空）", key="qb_grade")
        with type_col:
            question_type = st.text_input("题型筛选（可留空）", key="qb_question_type")
        textbook_col, origin_col, reference_col = st.columns(3)
        with textbook_col:
            textbook_version = st.text_input("教材版本筛选（可留空）", key="qb_textbook")
        with origin_col:
            origin_options = [None, *_ORIGINS]
            origin = st.selectbox(
                "来源筛选",
                origin_options,
                format_func=lambda value: "全部来源" if value is None else _ORIGINS[value],
                key="qb_origin",
            )
        with reference_col:
            reference_text = st.text_input("引用数筛选（精确，可留空）", key="qb_references")
        submitted = st.form_submit_button(
            "搜索标准题", type="primary", icon=":material/search:"
        )

    if submitted:
        st.session_state["qb_page"] = 0
    try:
        grade = int(grade_text.strip()) if grade_text.strip() else None
        reference_count = int(reference_text.strip()) if reference_text.strip() else None
    except ValueError:
        st.error("年级和引用数筛选必须填写整数。")
        return

    page = st.session_state["qb_page"]
    try:
        results = service.search_canonical(
            user.get("id", 0),
            keyword=keyword,
            semantic=semantic,
            subject=subject,
            grade=grade,
            question_type=question_type.strip() or None,
            origin=origin,
            textbook_version=textbook_version.strip() or None,
            reference_count=reference_count,
            limit=_PAGE_SIZE + 1,
            offset=page * _PAGE_SIZE,
        )
    except PermissionError:
        st.error("仅教师或管理员可以访问标准题库管理。")
        st.stop()
    except ValueError as exc:
        st.error(str(exc))
        return

    has_more = len(results) > _PAGE_SIZE
    questions = results[:_PAGE_SIZE]
    if not questions:
        st.info("没有符合条件的标准题。")
    else:
        st.table(
            [
                {
                    "题干": safe_truncate_math(question.stem_markdown, 100),
                    "题型": question.question_type or "—",
                    "来源": _ORIGINS.get(question.origin, question.origin),
                    "更新时间": question.updated_at.strftime("%Y-%m-%d %H:%M")
                    if question.updated_at
                    else "—",
                    "引用数": question.reference_count,
                }
                for question in questions
            ],
            hide_index=True,
            alt="标准题搜索结果，包含题干、题型、来源、更新时间和引用数",
        )
        selected_id = st.selectbox(
            "选择标准题查看详情",
            [question.id for question in questions],
            format_func=lambda canonical_id: f"第 {canonical_id} 题",
            key="qb_detail_id",
        )
        with st.container(horizontal=True):
            for question in questions:
                if st.button(
                    f"删除第 {question.id} 题",
                    key=f"qb_list_delete_{question.id}",
                    type="tertiary",
                ):
                    _confirm_delete(user, question)
        selected = next(question for question in questions if question.id == selected_id)
        _render_detail(user, selected)

    with st.container(horizontal=True, vertical_alignment="center"):
        if st.button(
            "上一页",
            key="qb_previous_page",
            disabled=page == 0,
            icon=":material/chevron_left:",
        ):
            st.session_state["qb_page"] = page - 1
            st.rerun()
        st.markdown(f"第 {page + 1} 页")
        if st.button(
            "下一页",
            key="qb_next_page",
            disabled=not has_more,
            icon=":material/chevron_right:",
        ):
            st.session_state["qb_page"] = page + 1
            st.rerun()
