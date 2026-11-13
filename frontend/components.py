"""可复用的展示组件：错题详情视图、重测按钮、变式题入库、命中高亮。"""
from __future__ import annotations

import html
import os

import streamlit as st

from backend.models.schemas import SUBJECT_NAMES
from frontend.common import render_markdown

_GRADE_LABELS = {"again": "😵 忘了", "hard": "😅 勉强", "good": "🙂 记得", "easy": "😎 秒懂"}


def k12_meta_line(q) -> str:
    """K12 元数据摘要行（学科/年级/地区/教材/题型/章节/错因/来源），无数据返回空串。"""
    bits: list[str] = []
    subject = SUBJECT_NAMES.get(getattr(q, "subject", None), getattr(q, "subject", None))
    if subject:
        bits.append(f"学科：{subject}")
    if getattr(q, "grade", None) is not None:
        bits.append(f"年级：{q.grade} 年级")
    if getattr(q, "region", None):
        bits.append(f"地区：{q.region}")
    if getattr(q, "textbook_version", None):
        bits.append(f"教材：{q.textbook_version}")
    if getattr(q, "question_type", None):
        bits.append(f"题型：{q.question_type}")
    if getattr(q, "chapter", None):
        bits.append(f"章节：{q.chapter}")
    if getattr(q, "error_category", None):
        bits.append(f"错因：{q.error_category}")
    if getattr(q, "source_doc", None):
        bits.append(f"来源：{q.source_doc}")
    return " · ".join(bits)


def render_figures(
    paths: list[str], *, alt_prefix: str, width: str | int = "stretch"
) -> None:
    for index, path in enumerate(paths, start=1):
        if os.path.exists(path):
            st.image(path, width=width, alt=f"{alt_prefix} {index}")


def question_detail_view(q, show_hit: bool = True) -> None:
    """错题「查看」视图：原图/元信息/解析/笔记/答案/变式（不含交互）。

    show_hit：搜索场景下展示关键词命中片段预览。
    """
    img_col, content_col = st.columns([2, 3])
    with img_col:
        figures = [path for path in (getattr(q, "figure_paths", None) or []) if os.path.exists(path)]
        has_page_image = bool(q.image_path and os.path.exists(q.image_path))
        if figures:
            render_figures(figures, alt_prefix=f"题 {q.id} 的图形")
            if has_page_image:
                with st.expander("查看完整原页", expanded=False):
                    st.image(q.image_path, width="stretch", alt=f"题 {q.id} 的完整原页")
        elif has_page_image:
            st.image(q.image_path, width="stretch", alt=f"题 {q.id} 的原图")
        else:
            st.caption("无原图（手动录入）")
        badges = " ".join(f"<span class='mm-badge'>{t}</span>" for t in q.tags)
        st.markdown(
            f"<div><span class='mm-badge mm-badge--blue'>{q.difficulty}</span>{badges}</div>",
            unsafe_allow_html=True,
        )
        meta_line = k12_meta_line(q)
        if meta_line:
            st.caption(meta_line)
        if q.reps:
            st.caption(f"已复习 {q.reps} 次 · 间隔 {q.interval_days:.0f} 天 · 难度系数 {q.ease:.2f}")
        else:
            st.caption("尚未复习")
    with content_col:
        if q.user_note:
            st.info(f"📝 我的笔记：{q.user_note}")
        render_markdown(q.content_markdown, unsafe_allow_html=True)
        if q.answer:
            render_markdown(f"**答案**：{q.answer}")
        _hit_preview(q)
        if q.followup_question:
            with st.expander("举一反三 · 变式练习"):
                render_markdown(q.followup_question)


def _hit_preview(q) -> None:
    """展示搜索关键词的首个命中片段（转义后高亮，不改动原解析渲染）。"""
    keyword = st.session_state.get("notebook_search")
    if not keyword:
        return
    kw = keyword.strip()
    if not kw:
        return
    for source in (q.content_markdown, q.answer or "", q.ocr_text or ""):
        lowered = source.lower()
        pos = lowered.find(kw.lower())
        if pos < 0:
            continue
        start = max(0, pos - 30)
        end = min(len(source), pos + len(kw) + 30)
        fragment = source[start:end]
        highlighted = re_escape_and_mark(fragment, kw)
        st.markdown(
            f"🔍 命中：<span style='background:#fef3c7;border-radius:4px'>"
            f"{highlighted}</span>",
            unsafe_allow_html=True,
        )
        return


def re_escape_and_mark(fragment: str, keyword: str) -> str:
    """HTML 转义后高亮关键词（大小写不敏感）。"""
    import re

    escaped_kw = re.escape(keyword)
    marked = re.sub(
        escaped_kw,
        lambda m: f"<mark>{html.escape(m.group(0))}</mark>",
        html.escape(fragment),
        flags=re.IGNORECASE,
    )
    return marked.replace("\n", " ")


def regrade_buttons(service, q, user: dict) -> None:
    """单题重测：按 SM-2 对本题直接评分（教师操作他人题目会得到提示）。"""
    st.markdown("**重测本题**")
    grade_cols = st.columns(4)
    for col, grade in zip(grade_cols, ("again", "hard", "good", "easy"), strict=False):
        with col:
            if st.button(
                _GRADE_LABELS[grade], key=f"nb_grade_{q.id}_{grade}", width="stretch"
            ):
                updated = service.grade_review(q.id, user["id"], grade)
                if updated is None:
                    st.toast("只能重测自己的错题", icon="⚠️")
                else:
                    st.toast("已按 SM-2 重新排期", icon="🔁")
                st.rerun()


def save_followup_button(
    service,
    q,
    user: dict,
    followup: str,
    *,
    figure_paths=None,
    figure_specs=None,
    key_suffix: str | None = None,
) -> None:
    """把变式及其自有图形存为待做错题，保留原题的 K12 元数据。"""
    if not followup:
        return
    button_key = f"save_followup_{q.id}_{key_suffix}" if key_suffix else f"save_followup_{q.id}"
    if st.button("📥 把变式题存入错题本", key=button_key):
        tags = [*(q.tags or []), "变式练习"]
        specs = [
            spec.model_dump(mode="json") if hasattr(spec, "model_dump") else dict(spec)
            for spec in (figure_specs or [])
        ]
        saved = service.create_manual_question(
            user["id"],
            content_markdown=f"### 变式练习（来自错题 #{q.id}）\n\n{followup}",
            knowledge_points=list(getattr(q, "knowledge_points", None) or []),
            tags=tags,
            difficulty=getattr(q, "difficulty", "medium"),
            source="followup",
            subject=getattr(q, "subject", "math"),
            grade=getattr(q, "grade", None),
            region=getattr(q, "region", None),
            textbook_version=getattr(q, "textbook_version", None),
            question_type=getattr(q, "question_type", None),
            chapter=getattr(q, "chapter", None),
            error_category=getattr(q, "error_category", None),
            figure_paths=list(figure_paths or []),
            figure_specs=specs,
        )
        st.success(f"已存入错题本（#{saved.id}），进入正常复习循环。")
