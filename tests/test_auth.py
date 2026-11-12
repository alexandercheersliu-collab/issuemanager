from __future__ import annotations

from streamlit.testing.v1 import AppTest

from backend.models.schemas import RegisterInput
from backend.services.auth import AuthService


def test_login_success_and_failure(db_session):
    service = AuthService(db_session)
    ok = service.login("demo", "demo123")
    assert ok.ok and ok.username == "demo"

    bad = service.login("demo", "wrong")
    assert not bad.ok


def test_register_validates_and_persists(db_session):
    service = AuthService(db_session)
    payload = RegisterInput(username="new_student", password="abc12345")
    result = service.register(payload)
    assert result.ok

    login = service.login("new_student", "abc12345")
    assert login.ok and login.role == "student"


def test_register_duplicate_username_rejected(db_session):
    service = AuthService(db_session)
    result = service.register(RegisterInput(username="demo", password="abc12345"))
    assert not result.ok


def test_register_rejects_short_password(db_session):
    import pydantic
    import pytest

    with pytest.raises(pydantic.ValidationError):
        RegisterInput(username="someone", password="123")


def test_change_password_flow(db_session):
    service = AuthService(db_session)
    wrong_old = service.change_password(999999, "x", "abcdef")
    assert not wrong_old.ok

    ok = service.change_password(999999, "", "abcdef")
    assert not ok.ok


def test_successful_login_clears_auth_container_before_rerun():
    at = AppTest.from_string(
        """
import streamlit as st
from frontend.pages.auth import render_auth_page

if 'auth_cleared' not in st.session_state:
    st.session_state['auth_cleared'] = False
original_container = st.container

def container():
    target = original_container()
    original_empty = target.empty
    def empty():
        st.session_state['auth_cleared'] = True
        return original_empty()
    target.empty = empty
    return target

st.container = container
try:
    render_auth_page()
finally:
    st.container = original_container
"""
    ).run()
    assert not at.exception
    at.text_input[0].set_value("demo")
    at.text_input[1].set_value("demo123")
    at.form_submit_button[0].click().run()

    assert not at.exception
    assert at.session_state["auth_cleared"] is True
    assert at.session_state["user"]["username"] == "demo"


def test_dashboard_shows_welcome_before_service_initialization():
    at = AppTest.from_string(
        """
import streamlit as st
from frontend.pages import dashboard

st.session_state['welcome_seen'] = False
original_empty = dashboard.st.empty

def empty():
    placeholder = original_empty()
    original_markdown = placeholder.markdown
    def markdown(text, *args, **kwargs):
        if '你好' in str(text):
            st.session_state['welcome_seen'] = True
        return original_markdown(text, *args, **kwargs)
    placeholder.markdown = markdown
    return placeholder

class FakeService:
    def subjects_for_user(self, *args, **kwargs):
        st.stop()

def get_service():
    st.session_state['service_saw_welcome'] = st.session_state['welcome_seen']
    return FakeService()

dashboard.st.empty = empty
dashboard.get_question_service = get_service
dashboard.render_dashboard({'id': 1, 'role': 'student', 'username': 'demo'})
"""
    ).run()

    assert not at.exception
    assert at.session_state["service_saw_welcome"] is True
