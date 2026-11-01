"""admin 重置用户密码为初始密码：服务层权限矩阵、限流解锁、前端纯函数、API。

服务层走 db_session（测试后回滚）；API 层用专用账号 apireset 隔离，
避免改动种子用户 demo 的密码影响其他用例。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend.models.orm import User
from backend.services.auth import INITIAL_PASSWORD, AuthService
from backend.utils.rate_limit import InMemoryRateLimiter
from frontend.pages.admin import resettable_users


@pytest.fixture
def admin_user(db_session):
    return db_session.query(User).filter_by(username="admin").one()


@pytest.fixture
def student_user(db_session):
    return db_session.query(User).filter_by(username="demo").one()


# ---------- 服务层 ----------

class TestResetPasswordByAdmin:
    def test_admin_resets_student_password(self, db_session, admin_user, student_user):
        service = AuthService(db_session)
        result = service.reset_password_by_admin(admin_user.id, student_user.id)
        assert result.ok

        login = AuthService(db_session).login("demo", INITIAL_PASSWORD)
        assert login.ok and login.username == "demo"

    def test_non_admin_caller_forbidden(self, db_session, student_user):
        service = AuthService(db_session)
        with pytest.raises(PermissionError):
            service.reset_password_by_admin(student_user.id, student_user.id)

    def test_unknown_caller_rejected(self, db_session, student_user):
        service = AuthService(db_session)
        with pytest.raises(ValueError):
            service.reset_password_by_admin(999999, student_user.id)

    def test_unknown_target_rejected(self, db_session, admin_user):
        service = AuthService(db_session)
        with pytest.raises(ValueError):
            service.reset_password_by_admin(admin_user.id, 999999)

    def test_cannot_reset_admin_account(self, db_session, admin_user):
        service = AuthService(db_session)
        with pytest.raises(ValueError):
            service.reset_password_by_admin(admin_user.id, admin_user.id)

    def test_reset_clears_login_lockout(self, db_session, admin_user, student_user):
        limiter = InMemoryRateLimiter(max_failures=3, window_seconds=300)
        service = AuthService(db_session, limiter=limiter)
        for _ in range(3):
            service.login("demo", "wrong")
        assert limiter.is_locked("demo")

        result = service.reset_password_by_admin(admin_user.id, student_user.id)
        assert result.ok
        assert not limiter.is_locked("demo")

        login = service.login("demo", INITIAL_PASSWORD)
        assert login.ok


# ---------- 前端纯函数 ----------

class TestResettableUsers:
    def test_excludes_admin_accounts(self, db_session):
        users = db_session.query(User).all()
        names = [u.username for u in resettable_users(users)]
        assert "demo" in names
        assert "admin" not in names

    def test_empty_list(self):
        assert resettable_users([]) == []


# ---------- API 权限矩阵 ----------

class TestResetPasswordApi:
    @pytest.fixture(scope="class")
    def client(self):
        from api.main import create_app

        return TestClient(create_app())

    @staticmethod
    def _auth(client: TestClient, username: str, password: str) -> dict:
        resp = client.post(
            "/api/auth/login", json={"username": username, "password": password}
        )
        assert resp.status_code == 200, resp.text
        return {"Authorization": f"Bearer {resp.json()['access_token']}"}

    @pytest.fixture(scope="class")
    def target_user(self, client):
        resp = client.post(
            "/api/auth/register",
            json={"username": "apireset", "password": "orig123456", "role": "student"},
        )
        assert resp.status_code == 201, resp.text
        return resp.json()

    def test_requires_auth(self, client, target_user):
        resp = client.post(f"/api/admin/users/{target_user['user_id']}/reset-password")
        assert resp.status_code == 401

    def test_forbidden_for_student(self, client, target_user):
        headers = self._auth(client, "demo", "demo123")
        resp = client.post(
            f"/api/admin/users/{target_user['user_id']}/reset-password", headers=headers
        )
        assert resp.status_code == 403

    def test_admin_resets_and_new_password_works(self, client, target_user):
        headers = self._auth(client, "admin", "admin123")
        resp = client.post(
            f"/api/admin/users/{target_user['user_id']}/reset-password", headers=headers
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["username"] == "apireset"

        login = client.post(
            "/api/auth/login", json={"username": "apireset", "password": INITIAL_PASSWORD}
        )
        assert login.status_code == 200
