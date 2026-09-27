"""MathMaster Edu — 应用入口。

Streamlit 运行：streamlit run app.py
"""
from __future__ import annotations

import bootstrap  # noqa: F401  注入 HOME/HF_HOME 等本地化环境变量，必须在其他 import 之前

import streamlit as st

from backend.config import get_settings
from frontend.common import current_user, load_css, logout_user
from frontend.pages.auth import render_auth_page
from frontend.pages.dashboard import render_dashboard
from frontend.pages.notebook import render_notebook_page
from frontend.pages.review import render_review_page
from frontend.pages.settings import render_settings_page
from frontend.pages.tutor import render_tutor_page

settings = get_settings()

st.set_page_config(
    page_title=f"{settings.app_name} · 智能错题本",
    page_icon="📘",
    layout="wide",
    initial_sidebar_state="auto",  # 窄屏自动折叠，兼顾移动端
)
load_css()

from frontend.i18n import t  # noqa: E402
from frontend.theme import apply_theme  # noqa: E402  需在基础样式之后注入

apply_theme()

# PWA：manifest 与 Service Worker（静态目录 static/，Streamlit 只认项目根下的 static）
st.markdown(
    '<link rel="manifest" href="app/static/manifest.json">',
    unsafe_allow_html=True,
)
try:
    import streamlit.components.v1 as components

    components.html(
        """
<script>
if ('serviceWorker' in navigator) {
  navigator.serviceWorker.register('app/static/sw.js').catch(function () {});
}
</script>
""",
        height=0,
    )
except Exception:  # noqa: BLE001 - SW 注册失败不影响应用
    pass


_PAGES = {
    t("nav.dashboard"): "dashboard",
    t("nav.tutor"): "tutor",
    t("nav.import_doc"): "import_doc",
    t("nav.notebook"): "notebook",
    t("nav.review"): "review",
    t("nav.graph"): "graph",
    t("nav.assistant"): "assistant",
    t("nav.settings"): "settings",
}


_TEACHER_PAGES = {
    t("nav.students"): "students",
}

# 侧边栏导航图标（emoji，深浅色通吃；自定义组件 iframe 无法跟随主题故弃用 sac.menu）
_NAV_ICONS = {
    "dashboard": "🏠",
    "tutor": "📸",
    "import_doc": "📄",
    "notebook": "📒",
    "review": "🔁",
    "graph": "🧠",
    "students": "👥",
    "assistant": "🤖",
    "settings": "⚙️",
}


def _render_sidebar(user: dict) -> str:
    from frontend.common import initials

    visible = dict(_PAGES)
    if user.get("role") == "teacher":
        # 教师专属页插在「知识图谱」之前
        ordered = list(visible.items())
        insert_at = next(
            (i for i, (label, key) in enumerate(ordered) if key == "graph"),
            len(ordered),
        )
        ordered[insert_at:insert_at] = list(_TEACHER_PAGES.items())
        visible = dict(ordered)

    with st.sidebar:
        st.markdown(
            """
            <div class="mm-brand">
              <div class="mm-brand__logo">📘</div>
              <div class="mm-brand__title">MathMaster Edu</div>
              <div class="mm-brand__tag">视觉大模型 × RAG 错题本</div>
            </div>
            """,
            unsafe_allow_html=True,
        )
        pending = st.session_state.pop("_pending_nav", None)  # 必须在菜单实例化前写入其 key
        if pending:
            st.session_state["nav"] = pending
        menu = st.radio(
            "导航",
            list(visible.keys()),
            format_func=lambda lbl: f"{_NAV_ICONS.get(visible[lbl], '')} {lbl}",
            key="nav",
            label_visibility="collapsed",
        )
        st.markdown("<hr>", unsafe_allow_html=True)
        st.markdown(
            f"""
            <div class="mm-user-card">
              <div class="user-avatar">{initials(user['username'])}</div>
              <div>
                <div class="mm-user-card__name">{user['username']}</div>
                <div class="mm-user-card__role">{"教师" if user['role'] == "teacher" else "学生"}</div>
              </div>
            </div>
            <div class="mm-version">v{settings.app_version}</div>
            """,
            unsafe_allow_html=True,
        )
        if st.button("退出登录", width="stretch"):
            logout_user()
            st.rerun()
    all_pages = {**visible}
    return all_pages.get(menu or t("nav.dashboard"), "dashboard")


def main() -> None:
    user = current_user()
    if user is None:
        render_auth_page()
        return

    page = _render_sidebar(user)
    if page == "dashboard":
        render_dashboard(user)
    elif page == "tutor":
        render_tutor_page(user)
    elif page == "import_doc":
        from frontend.pages.import_doc import render_import_doc_page

        render_import_doc_page(user)
    elif page == "notebook":
        render_notebook_page(user)
    elif page == "review":
        render_review_page(user)
    elif page == "graph":
        from frontend.pages.graph import render_graph_page

        render_graph_page(user)
    elif page == "students":
        from frontend.pages.students import render_students_page

        render_students_page(user)
    elif page == "assistant":
        from frontend.pages.assistant import render_assistant_page

        render_assistant_page(user)
    elif page == "settings":
        render_settings_page(user)


if __name__ == "__main__":
    main()
