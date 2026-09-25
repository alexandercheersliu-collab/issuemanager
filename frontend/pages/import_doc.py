"""整卷导入校对页：上传 PDF/DOCX → 异步切题解构 → 人工校对 → 批量入库。

校对操作（合并/拆分/删除/重解析/字段编辑）全部走 DocumentService，
操作留痕在任务 result.edit_log；确认入库后错题本可按来源文档筛选。
界面交互逻辑抽成纯函数（summarize_segments / segment_headline）便于单测。
"""
from __future__ import annotations

import time

import streamlit as st

from backend.models.schemas import ERROR_CATEGORIES
from backend.services.document_service import DocumentService
from backend.services.question_service import sanitize_tags
from frontend.common import go_to, page_header
from frontend.pages.tutor import _k12_context_inputs

_DIFFICULTY_LABELS = {"easy": "简单", "medium": "中等", "hard": "困难"}


# ---------- 纯函数（可单测） ----------


def summarize_segments(segments: list[dict]) -> dict:
    """切题结果统计：总数 / 待校对 / 解构失败 / 可入库（有解构结果）。"""
    return {
        "total": len(segments),
        "needs_review": sum(1 for s in segments if s.get("needs_review")),
        "failed": sum(1 for s in segments if s.get("error")),
        "importable": sum(1 for s in segments if s.get("analysis")),
    }


def segment_headline(segment: dict, max_len: int = 24) -> str:
    """列表标题：题号 + 题干摘要，待校对题加 ⚠️ 前缀。"""
    text = (segment.get("text") or "").replace("\n", " ").strip()
    digest = text[:max_len] + ("…" if len(text) > max_len else "")
    mark = "⚠️ " if segment.get("needs_review") else ""
    return f"{mark}{segment.get('number') or '?'}. {digest or '（空题干）'}"


# ---------- 页面 ----------


def render_import_doc_page(user: dict) -> None:
    service = DocumentService()
    page_header("整卷导入", "上传整份试卷 PDF/DOCX，自动切题解构，人工校对后批量入库")

    from backend.services.ai import get_provider_status

    if get_provider_status().demo_mode:
        st.warning(
            "当前为演示模式：未配置 AI_API_KEY，切题与解构返回内置示例。"
            "配置后即可调用真实视觉模型。"
        )

    _render_upload(service, user)

    job_id = st.session_state.get("import_doc_job_id")
    if not job_id:
        return
    job = service.get_job(job_id, user["id"])
    if job is None:
        st.error("导入任务不存在（可能已被清理）")
        st.session_state.pop("import_doc_job_id", None)
        return

    if job["status"] in ("pending", "running"):
        _render_progress(job)
        return
    if job["status"] == "failed":
        st.error(f"导入失败：{job.get('error') or '未知错误'}")
        if st.button("重新上传", width="stretch"):
            st.session_state.pop("import_doc_job_id", None)
            st.rerun()
        return
    _render_review(service, user, job)


def _render_upload(service: DocumentService, user: dict) -> None:
    with st.container(border=True):
        col_file, col_meta = st.columns([3, 2])
        with col_file:
            upload = st.file_uploader(
                "试卷文档（PDF / DOCX）",
                type=["pdf", "docx"],
                key="import_doc_file",
                help="PDF 按页解析（扫描版走视觉识别）；DOCX 提取段落与内嵌图片",
            )
        with col_meta:
            context = _k12_context_inputs("import_doc")
        if upload and st.button("开始导入", type="primary", width="stretch"):
            try:
                job_id, duplicated = service.import_document(
                    user["id"], upload.getvalue(), upload.name, **context
                )
            except ValueError as exc:
                st.error(str(exc))
                return
            st.session_state["import_doc_job_id"] = job_id
            if duplicated:
                st.info("该文档此前已导入，直接打开既有任务（SHA-256 去重）")
                time.sleep(1)
            st.rerun()


def _render_progress(job: dict) -> None:
    """解析中：进度条 + 自动刷新（rerun 轮询，与 jobs 异步模式配合）。"""
    result = job.get("result") or {}
    total = result.get("total", 0)
    done = result.get("done", 0)
    st.subheader("正在解析…")
    if total:
        st.progress(done / total, text=f"逐题解构中 {done}/{total}")
    else:
        st.progress(0.0, text="文档解析与切题中…")
    st.caption("页面会自动刷新，也可以稍后回到本页继续校对。")
    time.sleep(1)
    st.rerun()


def _render_review(service: DocumentService, user: dict, job: dict) -> None:
    result = job.get("result") or {}
    segments = result.get("segments", [])
    payload = job.get("payload") or {}
    summary = summarize_segments(segments)

    st.subheader(f"校对：{payload.get('filename') or '文档'}")
    cols = st.columns(4)
    cols[0].metric("切出题数", summary["total"])
    cols[1].metric("待校对", summary["needs_review"])
    cols[2].metric("解构失败", summary["failed"])
    cols[3].metric("可入库", summary["importable"])
    if result.get("scanned_pages"):
        st.caption(f"其中 {result['scanned_pages']} 页为扫描版（走视觉切题）")
    edit_log = result.get("edit_log") or []
    if edit_log:
        st.caption(f"最近校对操作：{edit_log[-1]['detail']}")

    if result.get("confirmed"):
        _render_confirm_result(result)
        return

    if not segments:
        st.warning("未切出任何题目，请检查文档内容后重新上传。")
        return

    for index, segment in enumerate(segments):
        _render_segment(service, user, _job_id(job), index, segment)

    st.divider()
    if summary["needs_review"]:
        st.warning(f"还有 {summary['needs_review']} 题待校对，确认入库时会跳过未解构的题。")
    if st.button(
        f"确认入库（{summary['importable']} 题可入库）",
        type="primary",
        width="stretch",
        disabled=summary["importable"] == 0,
    ):
        outcome = service.confirm_import(_job_id(job), user["id"])
        st.session_state["import_doc_outcome"] = outcome
        st.rerun()


def _render_confirm_result(result: dict) -> None:
    imported = result.get("imported", 0)
    skipped = result.get("skipped", [])
    st.success(f"已确认入库 {imported} 题（source=document，可按来源文档筛选）")
    if skipped:
        with st.expander(f"跳过 {len(skipped)} 题及原因", expanded=True):
            for item in skipped:
                st.warning(f"题 {item.get('number')}: {item.get('reason')}")
    col_nb, col_new = st.columns(2)
    with col_nb:
        if st.button("去错题本查看", type="primary", width="stretch"):
            go_to("notebook")
    with col_new:
        if st.button("导入新文档", width="stretch"):
            st.session_state.pop("import_doc_job_id", None)
            st.rerun()


def _job_id(job: dict) -> str:
    return job.get("job_id") or job.get("id")


def _render_segment(
    service: DocumentService, user: dict, job_id: str, index: int, segment: dict
) -> None:
    expanded = bool(segment.get("needs_review"))
    with st.expander(segment_headline(segment), expanded=expanded):
        if segment.get("error"):
            st.error(f"解构失败：{segment['error']}（可手动补齐字段或重新解析）")
        page_start, page_end = segment.get("page_start"), segment.get("page_end")
        page_label = (
            f"第 {page_start} 页"
            if page_start == page_end
            else f"第 {page_start}-{page_end} 页（跨页拼接）"
        )
        st.caption(f"{page_label} · 切题来源：{segment.get('source', '-')}")

        _render_segment_ops(service, user, job_id, index, segment)
        _render_segment_edit_form(service, user, job_id, index, segment)


def _render_segment_ops(
    service: DocumentService, user: dict, job_id: str, index: int, segment: dict
) -> None:
    col_merge, col_del, col_re, col_split = st.columns([1, 1, 1, 2])
    with col_merge:
        if st.button("⬇️ 合并下一题", key=f"merge_{job_id}_{index}", width="stretch"):
            try:
                service.merge_segments(job_id, user["id"], index)
            except (LookupError, IndexError, ValueError) as exc:
                st.error(str(exc))
                return
            st.toast("已合并，标为待校对", icon="🔗")
            st.rerun()
    with col_del:
        if st.button("🗑️ 删除", key=f"del_{job_id}_{index}", width="stretch"):
            service.delete_segment(job_id, user["id"], index)
            st.toast("已删除", icon="🗑️")
            st.rerun()
    with col_re:
        if st.button("🔄 重新解析", key=f"re_{job_id}_{index}", width="stretch"):
            try:
                service.reanalyze_segment(job_id, user["id"], index)
            except (LookupError, IndexError, ValueError, RuntimeError) as exc:
                st.error(str(exc))
                return
            st.toast("已重新解析", icon="🔄")
            st.rerun()
    with col_split:
        text_len = len(segment.get("text") or "")
        if text_len > 1:
            split_col, btn_col = st.columns([2, 1])
            with split_col:
                split_at = st.number_input(
                    "在第 N 个字符后拆分",
                    min_value=1,
                    max_value=text_len - 1,
                    value=text_len // 2,
                    key=f"split_at_{job_id}_{index}",
                    label_visibility="collapsed",
                )
            with btn_col:
                if st.button("✂️ 拆分", key=f"split_{job_id}_{index}", width="stretch"):
                    try:
                        service.split_segment(job_id, user["id"], index, int(split_at))
                    except (LookupError, IndexError, ValueError) as exc:
                        st.error(str(exc))
                        return
                    st.toast("已拆分为两题", icon="✂️")
                    st.rerun()


def _render_segment_edit_form(
    service: DocumentService, user: dict, job_id: str, index: int, segment: dict
) -> None:
    analysis = segment.get("analysis") or {}
    with st.form(f"seg_form_{job_id}_{index}"):
        new_text = st.text_area("题干", value=segment.get("text") or "", height=120)
        col_a, col_d = st.columns(2)
        with col_a:
            new_answer = st.text_input("答案", value=analysis.get("answer") or "")
            new_points = st.text_input(
                "知识点（逗号分隔）",
                value="、".join(analysis.get("knowledge_points") or []),
            )
            new_tags = st.text_input(
                "标签（逗号分隔）", value="、".join(analysis.get("tags") or [])
            )
        with col_d:
            difficulty_labels = list(_DIFFICULTY_LABELS.values())
            current_difficulty = _DIFFICULTY_LABELS.get(
                analysis.get("difficulty") or "medium", "中等"
            )
            new_difficulty_label = st.selectbox(
                "难度",
                difficulty_labels,
                index=difficulty_labels.index(current_difficulty),
            )
            category_options = ["未设置", *ERROR_CATEGORIES]
            current_category = analysis.get("error_category") or "未设置"
            new_category = st.selectbox(
                "错因",
                category_options,
                index=category_options.index(current_category)
                if current_category in category_options
                else 0,
            )
            new_qtype = st.text_input("题型", value=analysis.get("question_type") or "")
        new_chapter = st.text_input("章节", value=analysis.get("chapter") or "")
        new_analysis = st.text_area(
            "解析（Markdown）", value=analysis.get("analysis") or "", height=160
        )
        new_followup = st.text_input(
            "变式练习（可选）", value=analysis.get("followup_question") or ""
        )

        if st.form_submit_button("保存校对", type="primary"):
            new_difficulty = next(
                k for k, v in _DIFFICULTY_LABELS.items() if v == new_difficulty_label
            )
            updates = {
                "answer": new_answer.strip(),
                "knowledge_points": sanitize_tags(new_points.replace("、", ",")),
                "tags": sanitize_tags(new_tags.replace("、", ",")),
                "difficulty": new_difficulty,
                "error_category": None if new_category == "未设置" else new_category,
                "question_type": new_qtype.strip(),
                "chapter": new_chapter.strip(),
                "analysis": new_analysis.strip(),
                "followup_question": new_followup.strip(),
            }
            try:
                service.update_segment(
                    job_id,
                    user["id"],
                    index,
                    text=new_text,
                    analysis_updates=updates,
                )
            except (LookupError, IndexError, ValueError) as exc:
                st.error(str(exc))
                return
            st.toast("已保存校对", icon="✅")
            st.rerun()
