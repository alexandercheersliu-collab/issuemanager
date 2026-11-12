from __future__ import annotations

import json
from pathlib import Path

from PIL import Image
from streamlit.testing.v1 import AppTest


def _run_sidebar() -> AppTest:
    return AppTest.from_string(
        "import streamlit as st\n"
        "import app\n"
        "st.session_state['rendered_page'] = app._render_sidebar("
        "{'id': 1, 'username': 'demo', 'role': 'student'})\n",
        default_timeout=15,
    ).run()


def test_sidebar_uses_icon_only_brand_and_material_navigation():
    at = _run_sidebar()

    assert not at.exception
    markdown = "\n".join(element.value for element in at.markdown)
    assert "智能错题本" not in markdown
    assert "AI 学习助手" not in markdown
    images = [
        image
        for element in at.get("image")
        for image in element.proto.imgs
    ]
    assert any(image.alt == "智能错题本图标" for image in images)
    notebook = next(
        (button for button in at.button if button.key == "nav_notebook"), None
    )
    assert notebook is not None
    assert notebook.icon == ":material/menu_book:"


def test_sidebar_user_card_omits_initials():
    at = _run_sidebar()

    assert not at.exception
    markdown = "\n".join(element.value for element in at.markdown)
    assert "user-avatar" not in markdown
    assert "demo" in markdown
    assert "学生" in markdown


def test_sidebar_account_area_omits_standalone_version():
    at = _run_sidebar()

    assert not at.exception
    assert not at.get("caption")


def test_sidebar_button_navigation_updates_current_page():
    at = _run_sidebar()

    assert not at.exception
    notebook = next(
        (button for button in at.button if button.key == "nav_notebook"), None
    )
    assert notebook is not None
    notebook.click().run()

    assert not at.exception
    assert at.session_state["rendered_page"] == "notebook"


def test_pwa_manifest_uses_chinese_brand_and_existing_icons():
    root = Path(__file__).resolve().parents[1]
    manifest = json.loads((root / "static" / "manifest.json").read_text(encoding="utf-8"))

    assert manifest["name"] == "智能错题本"
    assert manifest["short_name"] == "智能错题本"
    for icon in manifest["icons"]:
        path = root / "static" / Path(icon["src"]).name
        with Image.open(path) as image:
            expected = tuple(int(value) for value in icon["sizes"].split("x"))
            assert image.size == expected
