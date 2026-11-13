from __future__ import annotations

import io

from streamlit.testing.v1 import AppTest


def test_notebook_defers_export_generation_until_download(
    question_service, student_user, monkeypatch
):
    question_service.create_manual_question(
        student_user.id,
        content_markdown="解方程 $2x=6$。",
        answer="x=3",
        tags=["代数"],
    )

    def unexpected_export(*args, **kwargs):
        raise AssertionError("打开错题本时不应预生成导出文件")

    monkeypatch.setattr(
        "frontend.pages.notebook.generate_word_exam", unexpected_export
    )
    monkeypatch.setattr("frontend.pages.notebook.generate_pdf_exam", unexpected_export)
    monkeypatch.setattr(
        "frontend.pages.notebook.get_question_service", lambda: question_service
    )
    at = AppTest.from_string(
        f"""
from frontend.pages.notebook import render_notebook_page
render_notebook_page({{"id": {student_user.id}, "username": "demo", "role": "student"}})
"""
    ).run()

    assert not at.exception
    assert any("代数" in item.label for item in at.expander)


def test_notebook_defers_collapsed_question_details(
    question_service, student_user, monkeypatch
):
    question_service.create_manual_question(
        student_user.id,
        content_markdown="解方程 $3x=9$。",
        answer="x=3",
        tags=["折叠详情"],
    )

    def unexpected_detail(*args, **kwargs):
        raise AssertionError("折叠题目不应加载完整详情")

    monkeypatch.setattr(
        "frontend.pages.notebook._render_question_detail", unexpected_detail
    )
    monkeypatch.setattr(
        "frontend.pages.notebook.get_question_service", lambda: question_service
    )
    at = AppTest.from_string(
        f"""
from frontend.pages.notebook import render_notebook_page
render_notebook_page({{"id": {student_user.id}, "username": "demo", "role": "student"}})
"""
    ).run()

    assert not at.exception
    assert any("折叠详情" in item.label for item in at.expander)


def test_share_card_button_is_disabled_while_rendering(
    question_service, student_user, monkeypatch
):
    question = question_service.create_manual_question(
        student_user.id,
        content_markdown="解方程 $4x=12$。",
        answer="x=3",
        tags=["分享"],
    )

    monkeypatch.setattr(
        "backend.services.share_card.render_share_card",
        lambda q: io.BytesIO(b"png"),
    )
    monkeypatch.setattr("frontend.pages.notebook.st.rerun", lambda: None)
    at = AppTest.from_string(
        f"""
from types import SimpleNamespace
from frontend.pages.notebook import _share_card_button
_share_card_button(SimpleNamespace(id={question.id}))
"""
    ).run()

    at.button(key=f"share_{question.id}").click().run()

    assert not at.exception
    assert at.button(key=f"share_{question.id}").disabled
    assert at.download_button(key=f"share_dl_{question.id}")

    at.run()

    assert not at.button(key=f"share_{question.id}").disabled
