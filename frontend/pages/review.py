"""间隔重复复习页：闪卡式复习，SM-2 调度。"""
from __future__ import annotations

import datetime as dt
import os

import streamlit as st

from backend.services.review import GRADE_ORDER, format_interval
from frontend.common import (
    edit_question_form,
    get_question_service,
    go_to,
    keyboard_shortcuts,
    page_header,
)

_GRADE_LABELS = {"again": "😵 忘了", "hard": "😅 勉强", "good": "🙂 记得", "easy": "😎 秒懂"}

# 有掌握迹象的评分：推荐发起同类题检测（验证掌握可触发降级拉长间隔）
_GOOD_GRADES = ("good", "easy")
# 降级状态机终态：已降级的题不再推荐检测
_TERMINAL_DEMOTION_STATES = ("demoted",)


def pick_detection_candidates(
    items: list[dict],
    *,
    good_grades: tuple[str, ...] = _GOOD_GRADES,
    terminal_states: tuple[str, ...] = _TERMINAL_DEMOTION_STATES,
    limit: int = 3,
) -> list[dict]:
    """从本轮复习记录中筛出推荐发起同类题检测的候选（纯函数，可单测）。

    items: [{"id", "grade", "demotion_state", "mastered", ...}, ...]
    规则：评分有掌握迹象（good/easy）+ 未降级（demotion_state 非终态）+ 未掌握归档；
    保持原顺序，最多 limit 个。
    """
    picked: list[dict] = []
    for item in items:
        if item.get("grade") not in good_grades:
            continue
        if item.get("demotion_state") in terminal_states:
            continue
        if item.get("mastered"):
            continue
        picked.append(item)
        if len(picked) >= limit:
            break
    return picked


def _render_detection_recommendations(service, user: dict, summary: dict) -> None:
    """复习完成后的「推荐检测」区块：候选题内联发起检测（不跳页）。"""
    from frontend.pages.notebook import _render_detection

    enriched: list[dict] = []
    for item in summary.get("items", []):
        question = service.get_question(item["id"], user["id"])
        if question is None:
            continue
        enriched.append(
            {
                "id": question.id,
                "grade": item["grade"],
                "demotion_state": question.demotion_state,
                "mastered": question.mastered,
                "question": question,
            }
        )
    picked = pick_detection_candidates(enriched)
    if not picked:
        st.caption("💪 本轮暂无适合检测的题——答错的题已优先排期，巩固后再来挑战同类题检测！")
        return

    st.divider()
    st.markdown("##### 🎯 推荐检测：趁热验证掌握")
    st.caption("对表现好的错题发起同类题检测，连续 2 次通过可自动降级、拉长复习间隔。")
    for cand in picked:
        question = cand["question"]
        with st.container(border=True):
            snippet = " ".join(question.content_markdown.split())[:60]
            st.markdown(f"**#{question.id}** · {snippet}…")
            _render_detection(service, question, user)


def render_review_page(user: dict) -> None:
    service = get_question_service()
    page_header("今日复习", "SM-2 间隔重复调度 · 按记忆掌握程度评分，自动安排下次复习时间")

    due = service.due_questions(user["id"])
    if not due:
        # 完成总结跨 rerun 保留（内联检测会触发 rerun）：首次弹出时另存，
        # 新一轮评分时在评分处理器里清掉。
        fresh = st.session_state.pop("review_session", None)
        if fresh and fresh.get("graded"):
            st.session_state["review_done_summary"] = fresh
        summary = st.session_state.get("review_done_summary")
        if summary and summary.get("graded"):
            grades = summary.get("grades", {})
            strong = grades.get("good", 0) + grades.get("easy", 0)
            rate = round(strong / summary["graded"] * 100) if summary["graded"] else 0
            if fresh:
                st.balloons()
            st.success(
                f"🎉 本轮复习完成！共评分 {summary['graded']} 题，"
                f"记得/秒懂占 {rate}%。错题已按 SM-2 重新排期，明天见。"
            )
            _render_detection_recommendations(service, user, summary)
            if st.button("返回学情看板", type="primary"):
                go_to("dashboard")
        else:
            st.success("🎉 今日复习任务已清空，错题本处于健康状态。")
        return

    idx_key = "review_cursor"
    if idx_key not in st.session_state:
        st.session_state[idx_key] = 0
    session_key = "review_session"  # 本轮复习统计：{"graded": n, "grades": {...}}
    if session_key not in st.session_state:
        st.session_state[session_key] = {"graded": 0, "grades": {}}

    st.markdown(
        f"""
        <div class="mm-stat mm-stat--accent" style="margin-bottom:0.8rem">
          <div class="mm-stat__value">{len(due)}</div>
          <div class="mm-stat__label">道错题待复习 · 当前进度 {st.session_state[idx_key] + 1} / {len(due)} · 本轮已评 {st.session_state[session_key]["graded"]} 题</div>
        </div>
        """,
        unsafe_allow_html=True,
    )
    st.progress((st.session_state[idx_key]) / len(due), text=None)

    cursor = min(st.session_state[idx_key], len(due) - 1)
    question = due[cursor]

    st.markdown(
        f"""<div style="margin-bottom:0.6rem">
        <span class="mm-badge mm-badge--blue">{question.difficulty}</span>
        {''.join(f'<span class="mm-badge">{t}</span>' for t in question.tags)}
        </div>""",
        unsafe_allow_html=True,
    )

    with st.container(border=True):
        reveal_key = f"reveal_{question.id}"  # 按题隔离，避免上一题状态泄漏
        if question.last_reviewed_at:
            last = question.last_reviewed_at
            if last.tzinfo is None:
                last = last.replace(tzinfo=dt.timezone.utc)
            days_ago = (dt.datetime.now(dt.timezone.utc) - last).days
            st.caption(f"上次复习：{days_ago} 天前 · 已连续记牢 {question.reps} 次")
        if question.image_path and os.path.exists(question.image_path):
            st.image(question.image_path, width=460)
        else:
            st.markdown(question.content_markdown[:220], unsafe_allow_html=True)
            st.caption("（本题无原图，请根据题面回忆解法）")

        if st.button("显示解析", type="secondary"):
            st.session_state[reveal_key] = True

        if st.session_state.get(reveal_key):
            st.divider()
            st.markdown(question.content_markdown, unsafe_allow_html=True)
            st.markdown(f"**答案**：{question.answer}")
            st.markdown("##### 这道题你掌握得如何？")
            grade_cols = st.columns(4)
            for col, grade in zip(grade_cols, GRADE_ORDER, strict=False):
                with col:
                    preview = service.scheduler.next_schedule(
                        grade=grade,
                        reps=question.reps,
                        ease=question.ease,
                        interval_days=question.interval_days,
                    )
                    if st.button(_GRADE_LABELS[grade], key=f"grade_{grade}", width="stretch"):
                        updated = service.grade_review(question.id, user["id"], grade)
                        st.session_state[reveal_key] = False
                        session_stats = st.session_state[session_key]
                        session_stats["graded"] += 1
                        session_stats["grades"][grade] = session_stats["grades"].get(grade, 0) + 1
                        # 逐题记录（id + 评分），供完成总结的「推荐检测」筛选
                        session_stats.setdefault("items", []).append(
                            {"id": question.id, "grade": grade}
                        )
                        # 新一轮评分开始，上一轮完成总结作废
                        st.session_state.pop("review_done_summary", None)
                        if updated is not None:
                            when = format_interval(updated.interval_days)
                            msg = f"下次复习：{when}"
                            if updated.mastered:
                                msg += " · 🎉 已掌握归档，移出复习池"
                            st.session_state["last_schedule_msg"] = msg
                        st.session_state[idx_key] = cursor
                        st.rerun()
                    st.caption(format_interval(preview.next_interval))  # 评分后该题移出待复习队列，游标原地指向下一题
        else:
            skip_col, _ = st.columns([1, 2])
            with skip_col:
                if st.button("⏭️ 先跳过这道", width="stretch"):
                    st.session_state[idx_key] = (cursor + 1) % len(due)
                    st.rerun()

    if st.session_state.get("last_schedule_msg"):
        st.caption(st.session_state["last_schedule_msg"])

    with st.expander("✏️ 这道题解析有误？直接修改"):
        edit_question_form(service, question, user)

    keyboard_shortcuts()

    st.divider()
    with st.expander("复习历史（最近 20 次）"):
        history = service.recent_reviews(user["id"], limit=20)
        if not history:
            st.caption("还没有复习记录。")
        else:
            rows = [
                {
                    "时间": h["reviewed_at"].astimezone().strftime("%m-%d %H:%M")
                    if h["reviewed_at"] else "",
                    "评分": _GRADE_LABELS.get(h["grade"], h["grade"]),
                    "下次间隔": f"{h['interval_days']:.0f} 天",
                    "题目": h["snippet"],
                }
                for h in history
            ]
            st.dataframe(rows, width="stretch", hide_index=True)

    with st.expander("SM-2 评分说明"):
        st.markdown(
            "| 评分 | SM-2 质量 q | 效果 |\n|---|---|---|\n"
            "| 😵 忘了 | 0 | 重置进度，10 分钟后重现 |\n"
            "| 😅 勉强 | 3 | 间隔按 1 天重新计算 |\n"
            "| 🙂 记得 | 4 | 间隔 × ease 正常拉长 |\n"
            "| 😎 秒懂 | 5 | 间隔拉长更快，ease 略增 |"
        )
