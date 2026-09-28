"""视图层通用工具：样式加载、会话状态、公共小组件。"""
from __future__ import annotations

import pathlib

import streamlit as st

from backend.services.ai import get_provider_status
from backend.services.question_service import QuestionService

_ASSETS = pathlib.Path(__file__).parent / "assets" / "style.css"


def load_css() -> None:
    if _ASSETS.exists():
        st.markdown(f"<style>{_ASSETS.read_text(encoding='utf-8')}</style>", unsafe_allow_html=True)


def current_user() -> dict | None:
    return st.session_state.get("user")


def is_admin(user: dict | None) -> bool:
    """当前用户是否在 admin 名单（ADMIN_USERNAMES；与 role 独立）。"""
    from backend.services.auth import is_admin_username

    return bool(user) and is_admin_username(user.get("username"))


def login_user(user_id: int, username: str, role: str) -> None:
    st.session_state["user"] = {"id": user_id, "username": username, "role": role}


def logout_user() -> None:
    st.session_state["user"] = None


@st.cache_resource
def get_question_service() -> QuestionService:
    """进程级共享：AI 客户端与向量库句柄复用，避免每页重建。

    QuestionService 内部每次操作独立开短事务，缓存实例是安全的。
    """
    return QuestionService()


def stat_card(
    value,
    label: str,
    accent: bool = False,
    icon: str = "",
    variant: str = "",
) -> None:
    """统计卡：大数字 + 标签；可选图标与 accent 色变体（accent/teal/ok/warn）。"""
    st.markdown(_stat_card_html(value, label, accent=accent, icon=icon, variant=variant), unsafe_allow_html=True)


def _stat_card_html(value, label: str, accent: bool = False, icon: str = "", variant: str = "") -> str:
    variant_class = f" mm-stat--{variant}" if variant else (" mm-stat--accent" if accent else "")
    icon_html = f'<div class="mm-stat__icon">{icon}</div>' if icon else ""
    return f"""
        <div class="mm-stat{variant_class}">
            {icon_html}
            <div class="mm-stat__value">{value}</div>
            <div class="mm-stat__label">{label}</div>
        </div>"""


def stat_grid(cards: list[dict]) -> None:
    """统计卡网格：一次渲染多张卡，桌面 4 列 / 手机 2 列（CSS grid 自适应）。

    cards: [{"value": ..., "label": ..., "icon": "", "variant": ""}, ...]
    相比 st.columns + stat_card，窄屏下不会逐张整行堆叠，主操作按钮保持一屏可见。
    """
    inner = "".join(
        _stat_card_html(
            c["value"], c["label"], icon=c.get("icon", ""), variant=c.get("variant", "")
        )
        for c in cards
    )
    st.markdown(f'<div class="mm-stat-grid">{inner}</div>', unsafe_allow_html=True)


def provider_badges() -> str:
    info = get_provider_status()
    if info.demo_mode:
        mode = '<span class="mm-badge mm-badge--warn">演示模式 · 未配置 API Key</span>'
    else:
        mode = f'<span class="mm-badge mm-badge--ok">AI: {info.model}</span>'
    return mode


def page_header(title: str, subtitle: str = "") -> None:
    st.markdown(
        f"<h2 style='margin-bottom:0.1rem'>{title}</h2>"
        + (f"<p class='mm-muted'>{subtitle}</p>" if subtitle else ""),
        unsafe_allow_html=True,
    )


def initials(name: str) -> str:
    return (name[:2] or "U").upper()


_LABEL_TO_KEY = {
    "学情看板": "dashboard",
    "AI 录题": "tutor",
    "整卷导入": "import_doc",
    "错题本": "notebook",
    "今日复习": "review",
    "知识图谱": "graph",
    "设置": "settings",
}


def go_to(page_key: str, **params) -> None:
    """跨页跳转：记录目标导航项，触发重跑。

    注意：菜单组件的 session key 只能在其「本次实例化之前」修改，
    因此这里仅写入 _pending_nav，由 app.py 在渲染侧边栏前消费。
    """
    label = next(label for label, key in _LABEL_TO_KEY.items() if key == page_key)
    st.session_state["_pending_nav"] = label
    for name, value in params.items():
        st.session_state[f"param_{name}"] = value
    st.rerun()


def pop_params(*names: str) -> dict:
    """读取并清除 go_to 传递的页面参数（一次性）。"""
    return {
        name: st.session_state.pop(f"param_{name}", None)
        for name in names
        if f"param_{name}" in st.session_state
    }


def followup_chat(service, question, user: dict) -> None:
    """围绕一道错题的多轮追问对话组件（历史按题隔离，存于 session_state）。"""
    history_key = f"chat_{question.id}"
    st.session_state.setdefault(history_key, [])

    for message in st.session_state[history_key]:
        with st.chat_message(
            message["role"], avatar="🧑‍🎓" if message["role"] == "user" else "📘"
        ):
            st.markdown(message["content"])

    if prompt := st.chat_input(
        "哪里没看懂？问老师（例如：为什么判别式要大于等于零）",
        key=f"chat_input_{question.id}",
    ):
        st.session_state[history_key].append({"role": "user", "content": prompt})
        with st.chat_message("user", avatar="🧑‍🎓"):
            st.markdown(prompt)
        with st.chat_message("assistant", avatar="📘"):
            try:
                reply = service.answer_followup(
                    question.id, user["id"], st.session_state[history_key], prompt
                )
            except Exception as exc:  # noqa: BLE001 - 对话失败不应崩溃页面
                reply = f"⚠️ 讲师暂时不可用：{exc}"
            st.markdown(reply)
        st.session_state[history_key].append({"role": "assistant", "content": reply})
        st.rerun()


def edit_question_form(service, question, user: dict) -> None:
    """错题编辑表单（错题本与复习页共用）。保存后提示并刷新。"""
    from backend.models.schemas import ERROR_CATEGORIES, SUBJECT_NAMES
    from backend.services.question_service import sanitize_tags

    subject_labels = list(SUBJECT_NAMES.values())
    current_subject_label = SUBJECT_NAMES.get(
        getattr(question, "subject", "math"), subject_labels[0]
    )
    grade_options = ["未设置"] + [f"{g} 年级" for g in range(1, 13)]
    current_grade = getattr(question, "grade", None)
    current_grade_label = f"{current_grade} 年级" if current_grade else "未设置"
    category_options = ["未设置", *ERROR_CATEGORIES]
    current_category = getattr(question, "error_category", None) or "未设置"

    with st.form(f"edit_form_{question.id}"):
        new_tags = st.text_input(
            "标签（逗号分隔）",
            value="、".join(question.tags) if question.tags else "",
        )
        new_content = st.text_area(
            "解析（Markdown）", value=question.content_markdown, height=260
        )
        new_answer = st.text_input("答案", value=question.answer)

        st.caption("K12 元数据")
        col_s, col_g, col_e = st.columns(3)
        with col_s:
            new_subject_label = st.selectbox(
                "学科",
                subject_labels,
                index=subject_labels.index(current_subject_label),
            )
        with col_g:
            new_grade_label = st.selectbox(
                "年级", grade_options, index=grade_options.index(current_grade_label)
            )
        with col_e:
            new_category = st.selectbox(
                "错因",
                category_options,
                index=category_options.index(current_category)
                if current_category in category_options
                else 0,
            )
        col_r, col_t, col_q = st.columns(3)
        with col_r:
            new_region = st.text_input("地区", value=getattr(question, "region", None) or "")
        with col_t:
            new_textbook = st.text_input(
                "教材版本", value=getattr(question, "textbook_version", None) or ""
            )
        with col_q:
            new_qtype = st.text_input(
                "题型", value=getattr(question, "question_type", None) or ""
            )
        new_chapter = st.text_input("章节", value=getattr(question, "chapter", None) or "")

        new_note = st.text_area(
            "我的笔记（易错点、思路备忘）",
            value=question.user_note or "",
            height=80,
            placeholder="例如：下次先看第二问的隐藏条件",
        )
        if st.form_submit_button("保存修改", type="primary"):
            from contextlib import suppress

            new_subject = next(
                (k for k, v in SUBJECT_NAMES.items() if v == new_subject_label), "math"
            )
            updated = service.update_question(
                question.id,
                user["id"],
                content_markdown=new_content,
                answer=new_answer,
                tags=sanitize_tags(new_tags.replace("、", ",")),
                user_note=new_note.strip() or None,
                subject=new_subject,
                grade=int(new_grade_label.split()[0])
                if new_grade_label != "未设置"
                else None,
                region=new_region.strip(),
                textbook_version=new_textbook.strip(),
                question_type=new_qtype.strip(),
                chapter=new_chapter.strip(),
                error_category=None if new_category == "未设置" else new_category,
            )
            if updated is None:
                st.error("保存失败：只能编辑自己的错题（教师可查看但不可修改学生的题）")
            else:
                st.success("已保存，向量索引同步更新")
                with suppress(Exception):
                    st.rerun()


def keyboard_shortcuts() -> None:
    """复习页键盘快捷键：空格/回车=显示解析，1/2/3/4=四种评分。

    通过同源组件 iframe 向父页面注册 keydown 监听（Streamlit 重渲染后自动重绑）。
    """
    import streamlit.components.v1 as components

    components.html(
        """
<script>
(function () {
  var doc = window.parent.document;
  if (doc.__mmKeyHandler) { doc.removeEventListener('keydown', doc.__mmKeyHandler); }
  function findByText(txt) {
    var buttons = doc.querySelectorAll('button');
    for (var i = 0; i < buttons.length; i++) {
      if (buttons[i].innerText && buttons[i].innerText.trim() === txt) return buttons[i];
    }
    return null;
  }
  function clickByText(txt) {
    var b = findByText(txt);
    if (b) { b.click(); return true; }
    return false;
  }
  var handler = function (e) {
    var tag = e.target && e.target.tagName;
    if (tag === 'INPUT' || tag === 'TEXTAREA' || e.target.isContentEditable) return;
    if (e.code === 'Space' || e.code === 'Enter') {
      if (clickByText('显示解析')) e.preventDefault();
      return;
    }
    var map = { '1': '😵 忘了', '2': '😅 勉强', '3': '🙂 记得', '4': '😎 秒懂' };
    var label = map[e.key];
    if (label && clickByText(label)) e.preventDefault();
  };
  doc.__mmKeyHandler = handler;
  doc.addEventListener('keydown', handler);
})();
</script>
""",
        height=0,
    )


_IFRAME_RESPONSIVE_JS = """
<script>
(function () {
  // sac 评分按钮在同源 iframe 里，外层 CSS 媒体查询够不到；
  // 借同源脚本向 iframe 注入窄屏样式：按钮 2×2 换行 + 44px 触控目标。
  var CSS =
    "@media (max-width: 520px){" +
    ".ant-space-horizontal{flex-wrap:wrap!important;row-gap:10px!important}" +
    ".ant-space-item.flex-fill{flex:1 1 42%!important}" +
    ".ant-btn{min-height:44px!important;font-size:1rem!important}}";
  var patch = function () {
    try {
      window.parent.document
        .querySelectorAll('iframe[title*="streamlit_antd_components"]')
        .forEach(function (f) {
          try {
            var doc = f.contentDocument;
            if (!doc || !doc.head || doc.getElementById("mm-rfs-fix")) return;
            var s = doc.createElement("style");
            s.id = "mm-rfs-fix";
            s.textContent = CSS;
            doc.head.appendChild(s);
          } catch (e) {}
        });
    } catch (e) {}
  };
  try {
    new MutationObserver(patch).observe(window.parent.document.body, {
      childList: true,
      subtree: true,
    });
  } catch (e) {}
  patch();
  setTimeout(patch, 500);
  setTimeout(patch, 2000);
})();
</script>
"""


def inject_iframe_responsive() -> None:
    """同源 iframe（sac 评分按钮）响应式补丁：窄屏下按钮 2×2 换行、≥44px 触控。"""
    import streamlit.components.v1 as components

    components.html(_IFRAME_RESPONSIVE_JS, height=0)
