"""确认入库测试：segments 端点、confirm 落库（source/source_doc）、幂等。"""
from __future__ import annotations

import time

import pytest
from doc_samples import make_text_pdf
from fastapi.testclient import TestClient

from api.main import create_app
from backend.services.document_service import DocumentService


@pytest.fixture(scope="module")
def client():
    return TestClient(create_app())


@pytest.fixture(scope="module")
def headers(client):
    resp = client.post(
        "/api/auth/login", json={"username": "demo", "password": "demo123"}
    )
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


def _wait_job(service: DocumentService, job_id: str, user_id: int, timeout: float = 30.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = service.get_job(job_id, user_id)
        assert job is not None
        if job["status"] in ("success", "failed"):
            return job
        time.sleep(0.1)
    raise TimeoutError(f"任务 {job_id} 在 {timeout}s 内未完成")


def _import_and_wait(question_service, student_user, tmp_path, name="confirm.pdf"):
    pdf = make_text_pdf(
        tmp_path / name,
        ["1. 第一题：已知 x^2=9，求 x 的值。\n2. 第二题：计算 1+1 的结果。"],
    )
    service = DocumentService()
    job_id, _ = service.import_document(
        student_user.id, pdf.read_bytes(), name, subject="math", grade=9, region="北京"
    )
    job = _wait_job(service, job_id, student_user.id)
    assert job["status"] == "success", job["error"]
    return service, job_id


def test_confirm_import_persists_source_fields(question_service, student_user, tmp_path):
    service, job_id = _import_and_wait(question_service, student_user, tmp_path)

    outcome = service.confirm_import(job_id, student_user.id)
    assert outcome == {"imported": 2, "already_confirmed": False}

    docs = [
        q
        for q in question_service.list_questions(student_user.id, semantic=False)
        if q.source == "document" and (q.source_doc or "").startswith("confirm.pdf#")
    ]
    assert len(docs) == 2
    # 两题同处第 1 页，source_doc 均为 文档名#1
    assert {q.source_doc for q in docs} == {"confirm.pdf#1"}

    first = next(q for q in docs if "第一题" in q.content_markdown)
    assert first.subject == "math"
    assert first.grade == 9
    assert first.region == "北京"
    # 结构化字段来自逐题解构（Mock 演示输出）
    assert first.error_category == "概念不清"
    assert first.question_type == "解答题"
    assert first.chapter == "一元二次方程"
    assert "第一题" in first.content_markdown
    assert first.answer


def test_confirm_import_is_idempotent(question_service, student_user, tmp_path):
    service, job_id = _import_and_wait(question_service, student_user, tmp_path, "again.pdf")
    first = service.confirm_import(job_id, student_user.id)
    assert first["imported"] == 2

    before = len(question_service.list_questions(student_user.id, semantic=False))
    second = service.confirm_import(job_id, student_user.id)
    assert second == {"imported": 2, "already_confirmed": True}
    after = len(question_service.list_questions(student_user.id, semantic=False))
    assert after == before  # 重复确认不产生新题


def test_confirm_import_rejects_missing_or_unfinished(student_user):
    service = DocumentService()
    import pytest as _pytest

    with _pytest.raises(LookupError):
        service.confirm_import("no-such-job", student_user.id)


def test_segments_endpoint(client, headers, tmp_path):
    pdf = make_text_pdf(
        tmp_path / "seg.pdf", ["1. 端点测试题一：求 x。\n2. 端点测试题二：求 y。"]
    )
    resp = client.post(
        "/api/documents/import",
        headers=headers,
        files={"document": ("seg.pdf", pdf.read_bytes(), "application/pdf")},
        data={"subject": "math", "grade": "8"},
    )
    assert resp.status_code == 202
    job_id = resp.json()["job_id"]

    deadline = time.time() + 30
    body = {}
    while time.time() < deadline:
        seg_resp = client.get(f"/api/documents/{job_id}/segments", headers=headers)
        assert seg_resp.status_code == 200
        body = seg_resp.json()
        if body["status"] in ("success", "failed"):
            break
        time.sleep(0.1)
    assert body["status"] == "success", body["error"]
    assert body["total"] == 2
    assert body["done"] == 2
    assert body["needs_review_count"] == 0
    assert body["confirmed"] is False
    assert [s["number"] for s in body["segments"]] == ["1", "2"]
    assert all(s["analysis"] for s in body["segments"])


def test_confirm_endpoint(client, headers, tmp_path):
    pdf = make_text_pdf(
        tmp_path / "cfm.pdf", ["1. 确认端点题一：求 x。\n2. 确认端点题二：求 y。"]
    )
    resp = client.post(
        "/api/documents/import",
        headers=headers,
        files={"document": ("cfm.pdf", pdf.read_bytes(), "application/pdf")},
    )
    job_id = resp.json()["job_id"]

    deadline = time.time() + 30
    while time.time() < deadline:
        body = client.get(f"/api/documents/{job_id}/segments", headers=headers).json()
        if body["status"] in ("success", "failed"):
            break
        time.sleep(0.1)
    assert body["status"] == "success"

    confirm = client.post(f"/api/documents/{job_id}/confirm", headers=headers)
    assert confirm.status_code == 200, confirm.text
    assert confirm.json() == {"imported": 2, "already_confirmed": False}

    again = client.post(f"/api/documents/{job_id}/confirm", headers=headers)
    assert again.status_code == 200
    assert again.json()["already_confirmed"] is True


def test_segments_endpoint_404(client, headers):
    resp = client.get("/api/documents/no-such-job/segments", headers=headers)
    assert resp.status_code == 404
    confirm = client.post("/api/documents/no-such-job/confirm", headers=headers)
    assert confirm.status_code == 404
