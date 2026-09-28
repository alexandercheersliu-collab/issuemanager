"""间隔重复复习页：闪卡式复习，SM-2 调度。"""
from __future__ import annotations

import datetime as dt
import os

import streamlit as st
import streamlit_antd_components as sac

from backend.services.review import GRADE_ORDER, format_interval
from frontend.common import (
    edit_question_form,
    get_question_service,
    go_to,
    keyboard_shortcuts,
    page_header,
)

_GRADE_LABELS = {"again": "😵 忘了", "hard": "😅 勉强", "good": "🙂 记得", "easy": "😎 秒懂"}
# 评分按钮四色：忘了红 / 勉强橙 / 记得蓝 / 秒懂绿
_GRADE_COLORS = {"again": "red", "hard": "orange", "good": "blue", "easy": "green"}

# 有掌握迹象的评分：推荐发起同类题检测（验证掌握可触发降级拉长间隔）
_GOOD_GRADES = ("good", "easy")
# 降级状态机终态：已降级的题不再推荐检测
_TERMINAL_DEMOTION_STATES = ("demoted",)

# 切题录入的题面/解析分隔符（与 question_mixins.analyze_text_and_save 落库格式一致）
_STEM_SEP = "\n\n---\n\n"


def split_stem_and_analysis(content_markdown: str) -> tuple[str, str | None]:
    """把 content_markdown 拆成（题干, 解析）；无分隔符的旧题返回（原文, None）。

    只按第一个分隔符拆分（解析里若再出现 --- 归解析段）；两段各自 strip，
    解析段为空视为无解析（返回 None），由调用方回落到全文展示。
    """
    if _STEM_SEP not in content_markdown:
        return content_markdown.strip(), None
    stem, _, analysis = content_markdown.partition(_STEM_SEP)
    return stem.strip(), analysis.strip() or None


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
    page_header("今日复习", "按记忆掌握程度评分，自动安排下次复习时间")

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

    # 闪卡居中加大：窄屏下中间列自然占满
    _card_l, card_col, _card_r = st.columns([0.7, 2.6, 0.7])
    with card_col:
        with st.container(border=True):
            reveal_key = f"reveal_{question.id}"  # 按题隔离，避免上一题状态泄漏
            if question.last_reviewed_at:
                last = question.last_reviewed_at
                if last.tzinfo is None:
                    last = last.replace(tzinfo=dt.timezone.utc)
                days_ago = (dt.datetime.now(dt.timezone.utc) - last).days
                st.caption(f"上次复习：{days_ago} 天前 · 已连续记牢 {question.reps} 次")
            stem, analysis_part = split_stem_and_analysis(question.content_markdown)
            has_image = bool(question.image_path and os.path.exists(question.image_path))
            if stem and analysis_part is not None:
                # 切题录入：题面文字优先（完整可读、LaTeX 可渲染），原图折叠收起
                with st.container(key="review_stem_box"):
                    st.markdown(stem)
                if has_image:
                    with st.expander("📷 查看原图", expanded=False):
                        st.image(question.image_path)
            elif has_image:
                # 旧版整图录入：题面只在图片里，限高居中展示
                with st.container(key="review_legacy_image"):
                    st.image(question.image_path)
                st.caption(
                    "本题来自整图录入，未提取题面文字；"
                    "如需文字题面，请到「错题本」对应题目点「编辑」补录题干。"
                )
            else:
                st.markdown(question.content_markdown[:220], unsafe_allow_html=True)
                st.caption("（本题无原图，请根据题面回忆解法）")

            if st.button("显示解析", type="primary", width="stretch"):
                st.session_state[reveal_key] = True

            if st.session_state.get(reveal_key):
                st.divider()
                # 有分隔符：正面已展示题干，这里只放解析段；无分隔符保持全文
                st.markdown(
                    analysis_part if analysis_part is not None else question.content_markdown,
                    unsafe_allow_html=True,
                )
                st.markdown(f"**答案**：{question.answer}")
                # 各档评分的下次间隔预览（一行紧凑展示）
                previews = []
                for grade in GRADE_ORDER:
                    preview = service.scheduler.next_schedule(
                        grade=grade,
                        reps=question.reps,
                        ease=question.ease,
                        interval_days=question.interval_days,
                    )
                    previews.append(f"{_GRADE_LABELS[grade]} {format_interval(preview.next_interval)}")
                clicked = sac.buttons(
                    [
                        sac.ButtonsItem(_GRADE_LABELS[g], color=_GRADE_COLORS[g])
                        for g in GRADE_ORDER
                    ],
                    index=None,
                    label="这道题你掌握得如何？",
                    variant="filled",
                    size="lg",
                    use_container_width=True,
                    return_index=True,
                    key=f"grade_btns_{question.id}",
                )
                st.caption(" · ".join(previews))
                if clicked is not None:
                    grade = GRADE_ORDER[clicked]
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
