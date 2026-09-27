"""主题：深色模式（会话级切换，注入 CSS 变量覆盖）。

设计系统的颜色全部走 style.css 的 --mm-* CSS 变量；
深色模式只需重定义同名变量 + 少量 Streamlit 原生组件覆盖。
"""
from __future__ import annotations

import streamlit as st

_DARK_CSS = """
<style>
/* 深色主题：变量覆盖（浅色定义见 assets/style.css :root） */
:root {
  --mm-primary: #3b82f6;
  --mm-primary-strong: #60a5fa;
  --mm-primary-soft: #172554;
  --mm-primary-border: #1e40af;
  --mm-accent: #2dd4bf;
  --mm-accent-soft: #134e4a;

  --mm-navy: #f1f5f9;
  --mm-text: #e2e8f0;
  --mm-text-2: #94a3b8;
  --mm-muted: #7d8fa8;
  --mm-bg: #0f172a;
  --mm-card: #1e293b;
  --mm-border: #334155;
  --mm-border-strong: #475569;

  --mm-ok: #34d399;
  --mm-ok-bg: #064e3b;
  --mm-ok-border: #065f46;
  --mm-warn: #fbbf24;
  --mm-warn-bg: #451a03;
  --mm-warn-border: #92400e;
  --mm-danger: #f87171;
  --mm-danger-bg: #450a0a;
  --mm-danger-border: #991b1b;
  --mm-orange: #fb923c;

  --mm-shadow-sm: 0 1px 2px rgba(0, 0, 0, 0.3);
  --mm-shadow-md: 0 6px 20px rgba(0, 0, 0, 0.35);
  --mm-shadow-lg: 0 12px 32px rgba(0, 0, 0, 0.45);
}

/* Streamlit 原生组件的深色补丁（变量覆盖不到的部分） */
section[data-testid="stSidebar"] { border-right-color: var(--mm-border); }
.mm-welcome { background: linear-gradient(135deg, #1e3a8a 0%, #172554 60%, #134e4a 100%) !important; }
div[data-testid="stExpander"] details > summary { color: var(--mm-text); }
.stTextInput input, .stTextArea textarea, textarea {
  background: var(--mm-bg) !important;
  color: var(--mm-text) !important;
}
section[data-testid="stFileUploaderDropzone"] { background: var(--mm-card); }
section[data-testid="stFileUploaderDropzone"]:hover { background: var(--mm-primary-soft); }
.mm-user-card { background: var(--mm-bg); }
</style>
"""


def apply_theme() -> None:
    """按会话开关注入深色 CSS（app.py 在 load_css() 之后调用）。"""
    if st.session_state.get("dark_mode"):
        st.markdown(_DARK_CSS, unsafe_allow_html=True)
