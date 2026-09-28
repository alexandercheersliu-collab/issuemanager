"""后台管理页（仅 admin）：按学科清空错题本，全系统口径的危险维护操作。"""
from __future__ import annotations

import streamlit as st

from frontend.common import get_question_service, page_header


def purge_confirmed(input_text: str, expected_name: str) -> bool:
    """确认输入校验（纯函数）：输入与学科全名完全一致（去首尾空白）才放行。"""
    return bool(expected_name) and input_text.strip() == expected_name


def render_admin_page(user: dict) -> None:
    service = get_question_service()
    page_header("后台管理", "全系统数据维护 · 危险操作区，执行前请确认已备份")

    try:
        overview = service.admin_subject_overview(user["id"])
    except PermissionError:
        st.error("仅管理员可以访问后台管理。")
        st.stop()
        return  # 类型检查器友好；st.stop() 已中断执行

    if not overview:
        st.info("全系统暂无错题，无需清理。")
        return

    st.markdown("#### 🗑️ 按学科清空错题本")
    st.caption("清空范围为**全系统所有用户**在该学科下的错题，而非仅当前账号。")

    # 下拉标签带计数；值取学科代码
    labels = {f"{item['name']}（{item['count']} 道）": item for item in overview}
    choice = st.selectbox("选择要清空的学科", list(labels.keys()), key="admin_purge_subject")
    selected = labels[choice]

    st.markdown(
        f"""
        <div class="mm-danger-zone">
          <div class="mm-danger-zone__title">⚠️ 危险操作，不可恢复</div>
          <div>
            将删除 <b>{selected['name']}</b> 学科下全系统共
            <b>{selected['count']}</b> 道错题，以及这些错题的
            <b>复习记录、检测记录与批注</b>，并同步删除向量库嵌入。
          </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    confirm = st.text_input(
        f"请输入学科全名「{selected['name']}」以确认",
        key="admin_purge_confirm",
        placeholder=selected["name"],
    )
    confirmed = purge_confirmed(confirm, selected["name"])
    if confirm and not confirmed:
        st.caption("输入与学科全名不一致，删除按钮保持锁定。")

    if st.button(
        f"永久删除 {selected['name']} 全部错题",
        key="admin_purge_btn",
        type="primary",
        disabled=not confirmed,
        width="stretch",
    ):
        try:
            result = service.purge_subject(user["id"], selected["subject"])
        except PermissionError:
            st.error("仅管理员可执行该操作。")
        else:
            st.success(
                f"已清空「{selected['name']}」：删除 {result['deleted']} 道错题"
                "及其复习/检测/批注记录，向量索引已同步清理。"
            )
            st.rerun()
