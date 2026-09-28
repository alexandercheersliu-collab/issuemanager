"""按学科清空错题本（后台管理）测试：权限矩阵、事务完整性、计数、确认输入、API。

全部走 MockProvider / 测试临时库；purge 目标使用专用学科代码
（alchemy / apialchemy），与其他用例的默认 math 数据隔离。
"""
from __future__ import annotations

import io

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from backend.models.orm import Comment, DetectionLog, Question, ReviewLog, User
from backend.services.auth import is_admin_username
from frontend.pages.admin import purge_confirmed


def _tiny_jpeg() -> bytes:
    image = Image.new("RGB", (8, 8), color=(180, 120, 90))
    stream = io.BytesIO()
    image.save(stream, format="JPEG")
    return stream.getvalue()


@pytest.fixture
def admin_user(db_session):
    return db_session.query(User).filter_by(username="admin").one()


# ---------- admin 判定 ----------

class TestIsAdmin:
    def test_listed_username_is_admin(self):
        assert is_admin_username("admin") is True

    def test_unlisted_username_not_admin(self):
        assert is_admin_username("demo") is False

    def test_blank_or_none_not_admin(self):
        assert is_admin_username("") is False
        assert is_admin_username(None) is False

    def test_whitespace_tolerated(self):
        assert is_admin_username("  admin  ") is True


# ---------- 确认输入纯函数 ----------

class TestPurgeConfirmed:
    def test_exact_match(self):
        assert purge_confirmed("数学", "数学") is True

    def test_whitespace_tolerated(self):
        assert purge_confirmed("  数学 ", "数学") is True

    def test_mismatch_rejected(self):
        assert purge_confirmed("语文", "数学") is False
        assert purge_confirmed("数学2", "数学") is False
        assert purge_confirmed("", "数学") is False


# ---------- 服务层：权限矩阵 + 事务完整性 ----------

class TestPurgeService:
    def test_non_admin_purge_forbidden(self, question_service, student_user):
        with pytest.raises(PermissionError):
            question_service.purge_subject(student_user.id, "alchemy")

    def test_non_admin_overview_forbidden(self, question_service, student_user):
        with pytest.raises(PermissionError):
            question_service.admin_subject_overview(student_user.id)

    def test_unknown_user_rejected(self, question_service):
        with pytest.raises(ValueError):
            question_service.purge_subject(999999, "alchemy")

    def test_blank_subject_rejected(self, question_service, admin_user):
        with pytest.raises(ValueError):
            question_service.purge_subject(admin_user.id, "  ")

    def test_purge_full_pipeline(
        self, question_service, admin_user, student_user, db_session
    ):
        """四表 + 向量库同步删除；其他学科不受影响；其他用户同学科一并清空。"""
        other = User(username="purge_other", password_hash="x", role="student")
        db_session.add(other)
        db_session.commit()  # 先提交，其他连接的 FK 校验才能看到该用户

        q1, _ = question_service.analyze_and_save(
            student_user.id, _tiny_jpeg(), subject="alchemy"
        )
        q2, _ = question_service.analyze_and_save(
            student_user.id, _tiny_jpeg(), subject="alchemy"
        )
        keep, _ = question_service.analyze_and_save(
            student_user.id, _tiny_jpeg(), subject="english"
        )
        q_other, _ = question_service.analyze_and_save(
            other.id, _tiny_jpeg(), subject="alchemy"
        )

        # 在 q1 上造三张关联表数据并提交（服务层是 session-per-operation，需真实落库）
        db_session.add(ReviewLog(
            question_id=q1.id, user_id=student_user.id, grade="good", quality=4,
        ))
        db_session.add(DetectionLog(
            question_id=q1.id, user_id=student_user.id,
            tested_ref=str(q1.id), tested_source="own",
        ))
        db_session.add(Comment(
            question_id=q1.id, author_id=student_user.id, content="注意单位换算",
        ))
        db_session.commit()

        overview = question_service.admin_subject_overview(admin_user.id)
        alchemy = next(o for o in overview if o["subject"] == "alchemy")
        assert alchemy["count"] == 3
        assert alchemy["name"] == "alchemy"  # 未登记学科代码回退原名展示

        result = question_service.purge_subject(admin_user.id, "alchemy")
        assert result == {"subject": "alchemy", "deleted": 3}

        # 主表：alchemy 全清（含其他用户），english 保留
        remaining = db_session.query(Question).filter_by(subject="alchemy").count()
        assert remaining == 0
        assert db_session.get(Question, keep.id) is not None
        # 关联表级联删除
        assert db_session.query(ReviewLog).filter_by(question_id=q1.id).count() == 0
        assert db_session.query(DetectionLog).filter_by(question_id=q1.id).count() == 0
        assert db_session.query(Comment).filter_by(question_id=q1.id).count() == 0
        # 向量库同步删除
        collection = question_service.vector_store._ensure_collection()
        if collection is not None:
            got = collection.get(ids=[str(q) for q in (q1.id, q2.id, q_other.id)])
            assert got["ids"] == []
            assert collection.get(ids=[str(keep.id)])["ids"] == [str(keep.id)]

    def test_purge_nonexistent_subject_zero(self, question_service, admin_user):
        result = question_service.purge_subject(admin_user.id, "alchemy_void")
        assert result["deleted"] == 0


# ---------- API 权限矩阵 ----------

class TestAdminApi:
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

    def test_subjects_requires_auth(self, client):
        assert client.get("/api/admin/subjects").status_code == 401

    def test_subjects_forbidden_for_student(self, client):
        headers = self._auth(client, "demo", "demo123")
        assert client.get("/api/admin/subjects", headers=headers).status_code == 403

    def test_subjects_ok_for_admin(self, client):
        headers = self._auth(client, "admin", "admin123")
        resp = client.get("/api/admin/subjects", headers=headers)
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

    def test_purge_requires_auth(self, client):
        resp = client.delete("/api/admin/questions", params={"subject": "apialchemy"})
        assert resp.status_code == 401

    def test_purge_forbidden_for_student(self, client, question_service, student_user):
        question_service.analyze_and_save(
            student_user.id, _tiny_jpeg(), subject="apialchemy"
        )
        headers = self._auth(client, "demo", "demo123")
        resp = client.delete(
            "/api/admin/questions", params={"subject": "apialchemy"}, headers=headers
        )
        assert resp.status_code == 403
        # 403 不执行删除
        assert (
            question_service.list_questions(student_user.id, subject="apialchemy")
        )

    def test_purge_ok_for_admin(self, client, question_service, student_user):
        saved, _ = question_service.analyze_and_save(
            student_user.id, _tiny_jpeg(), subject="apialchemy"
        )
        headers = self._auth(client, "admin", "admin123")
        resp = client.delete(
            "/api/admin/questions", params={"subject": "apialchemy"}, headers=headers
        )
        assert resp.status_code == 200
        assert resp.json()["deleted"] >= 1
        assert question_service.get_question(saved.id, student_user.id) is None
