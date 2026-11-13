"""MathMaster Edu — 应用入口。

Streamlit 运行：streamlit run app.py
"""
from __future__ import annotations

import time
import threading

import bootstrap  # noqa: F401  注入 HOME/HF_HOME 等本地化环境变量，必须在其他 import 之前

import streamlit as st

from backend.config import get_settings
from backend.utils.logging import get_logger
from frontend.common import (
    current_user,
    inject_iframe_responsive,
    load_css,
    logout_user,
)
from frontend.pages.auth import render_auth_page
from frontend.pages.dashboard import render_dashboard
from frontend.pages.notebook import render_notebook_page
from frontend.pages.review import render_review_page
from frontend.pages.settings import render_settings_page
from frontend.pages.tutor import render_tutor_page

logger = get_logger("app")


def _warmup() -> None:
    """启动后台预热：把重初始化从「首次点击」挪到服务启动后。

    - 懒加载页面模块（import_doc/graph/students/assistant）预先 import，
      消除首次切换到这些页时的模块加载停顿；
    - 构造进程级 QuestionService（chromadb 客户端 + AI 客户端）；
    - 发一次微小向量检索，触发 ONNX 嵌入模型加载（首次推理最贵）。
    """
    t0 = time.perf_counter()
    time.sleep(2.0)  # 让登录首屏先渲染完，预热再抢 CPU（首屏优先）
    try:
        from frontend.pages import admin, assistant, graph, import_doc, students  # noqa: F401

        from frontend.common import get_question_service

        svc = get_question_service()
        from backend.services.rag import SimilarConstraints

        svc.vector_store.constrained_similar(
            "warmup",
            user_ids=[-1],  # 不存在的用户：只付嵌入推理，不扫真实数据
            exclude_id=None,
            constraints=SimilarConstraints(),
            top_k=1,
        )
        logger.info("后台预热完成 %.1fs", time.perf_counter() - t0)
    except Exception as exc:  # noqa: BLE001 - 预热失败不影响正常使用（首次访问时按需初始化）
        logger.warning("后台预热失败（忽略，首次访问将按需初始化）: %s", exc)


@st.cache_resource
def _start_warmup_once() -> bool:
    """进程级只启动一次预热线程（cache_resource 保证跨 rerun/会话单例）。"""
    threading.Thread(target=_warmup, name="app-warmup", daemon=True).start()
    return True

settings = get_settings()

st.set_page_config(
    page_title="智能错题本",
    page_icon="static/icon.png",
    layout="wide",
    initial_sidebar_state="auto",  # 窄屏自动折叠，兼顾移动端
)
load_css()

from frontend.i18n import t  # noqa: E402
from frontend.theme import apply_theme  # noqa: E402  需在基础样式之后注入

apply_theme()
inject_iframe_responsive()  # sac 评分按钮窄屏 2×2 换行（同源 iframe 补丁）

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

# 管理员专属页（ADMIN_USERNAMES 名单，与 role 独立）
_ADMIN_PAGES = {
    t("nav.admin"): "admin",
}

# 侧边栏导航统一使用 Material Symbols，保持深浅色下一致的线性风格。
_NAV_ICONS = {
    "dashboard": ":material/dashboard:",
    "tutor": ":material/photo_camera:",
    "import_doc": ":material/description:",
    "notebook": ":material/menu_book:",
    "review": ":material/autorenew:",
    "graph": ":material/account_tree:",
    "students": ":material/group:",
    "admin": ":material/admin_panel_settings:",
    "assistant": ":material/smart_toy:",
    "settings": ":material/settings:",
}


def _render_sidebar(user: dict) -> str:
    from frontend.common import is_admin

    ordered = list(_PAGES.items())
    if user.get("role") == "teacher":
        # 教师专属页插在「知识图谱」之前
        insert_at = next(
            (i for i, (label, key) in enumerate(ordered) if key == "graph"),
            len(ordered),
        )
        ordered[insert_at:insert_at] = list(_TEACHER_PAGES.items())
    if is_admin(user):
        # 管理员专属页插在「设置」之前
        insert_at = next(
            (i for i, (label, key) in enumerate(ordered) if key == "settings"),
            len(ordered),
        )
        ordered[insert_at:insert_at] = list(_ADMIN_PAGES.items())
    visible = dict(ordered)

    with st.sidebar:
        with st.container(key="sidebar_brand"):
            st.image("static/icon.png", width=44, alt="智能错题本图标")
        pending = st.session_state.pop("_pending_nav", None)
        if pending in visible:
            st.session_state["nav"] = pending
        menu = st.session_state.get("nav")
        if menu not in visible:
            menu = next(iter(visible))
            st.session_state["nav"] = menu
        with st.container(key="sidebar_nav", gap=None):
            for label, key in visible.items():
                if st.button(
                    label,
                    icon=_NAV_ICONS[key],
                    key=f"nav_{key}",
                    type="secondary" if label == menu else "tertiary",
                    width="stretch",
                ):
                    st.session_state["nav"] = label
                    st.rerun()
        st.space("small")
        with st.container(key="sidebar_account", border=True):
            st.markdown(
                f"""
                <div class="mm-user-card">
                  <div class="mm-user-card__name">{user['username']}</div>
                  <div class="mm-user-card__role">{"教师" if user['role'] == "teacher" else "学生"}</div>
                </div>
                """,
                unsafe_allow_html=True,
            )
            if st.button(
                "退出登录",
                icon=":material/logout:",
                key="sidebar_logout",
                type="tertiary",
                width="stretch",
            ):
                logout_user()
                st.rerun()
    all_pages = {**visible}
    return all_pages.get(menu or t("nav.dashboard"), "dashboard")


def _dispatch(page: str, user: dict) -> None:
    """路由分发：懒 import 的页面在首次访问时加载模块。"""
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
    elif page == "admin":
        from frontend.pages.admin import render_admin_page

        render_admin_page(user)
    elif page == "assistant":
        from frontend.pages.assistant import render_assistant_page

        render_assistant_page(user)
    elif page == "settings":
        render_settings_page(user)


def main() -> None:
    _t0 = time.perf_counter()
    user = current_user()
    if user is None:
        render_auth_page()
        return

    page = _render_sidebar(user)
    _start_warmup_once()  # 登录后后台预热（线程先睡 2s，不抢看板首渲染的 CPU）
    _t1 = time.perf_counter()
    _dispatch(page, user)
    # 页面级耗时日志：定位首次切换慢的具体页面
    logger.info(
        "render page=%s sidebar=%.2fs page_render=%.2fs",
        page, _t1 - _t0, time.perf_counter() - _t1,
    )


if __name__ == "__main__":
    main()
