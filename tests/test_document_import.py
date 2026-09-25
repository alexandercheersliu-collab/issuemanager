"""整卷导入：上传去重、异步任务、导入 API 骨架测试。"""
from __future__ import annotations

import time

import pytest
from doc_samples import make_docx, make_text_pdf
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


def test_import_document_parses_and_dedups(question_service, student_user, tmp_path):
    """同一文档重复导入：第二次直接复用既有任务（SHA-256 去重）。"""
    pdf = make_text_pdf(tmp_path / "paper.pdf", ["1. 第一题：求 x^2=4 的解。\n2. 第二题：计算 2+3。"])
    data = pdf.read_bytes()
    service = DocumentService()

    job_id1, dup1 = service.import_document(
        student_user.id, data, "paper.pdf", subject="math", grade=9
    )
    assert not dup1
    job = _wait_job(service, job_id1, student_user.id)
    assert job["status"] == "success", job["error"]
    assert job["result"]["pages"] == 1
    assert job["result"]["scanned_pages"] == 0
    assert job["payload"]["grade"] == 9

    job_id2, dup2 = service.import_document(student_user.id, data, "paper.pdf")
    assert dup2
    assert job_id2 == job_id1  # 返回已有任务

    # 不同内容 → 新任务
    other = make_docx(tmp_path / "other.docx", ["1. 另一份试卷"], with_image=False)
    job_id3, dup3 = service.import_document(student_user.id, other.read_bytes(), "other.docx")
    assert not dup3
    assert job_id3 != job_id1
    job3 = _wait_job(service, job_id3, student_user.id)
    assert job3["status"] == "success", job3["error"]


def test_import_document_rejects_bad_input(question_service, student_user):
    service = DocumentService()
    with pytest.raises(ValueError, match="不支持的文档格式"):
        service.import_document(student_user.id, b"x", "paper.txt")
    with pytest.raises(ValueError, match="内容为空"):
        service.import_document(student_user.id, b"", "paper.pdf")


def test_import_api_endpoint(client, headers, tmp_path):
    pdf = make_text_pdf(tmp_path / "api.pdf", ["1. API 导入测试题：求 1+1。"])
    resp = client.post(
        "/api/documents/import",
        headers=headers,
        files={"document": ("api.pdf", pdf.read_bytes(), "application/pdf")},
        data={"subject": "math", "grade": "7", "region": "北京"},
    )
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["status"] == "pending"
    assert body["duplicated"] is False

    # 重复上传 → 复用任务
    resp2 = client.post(
        "/api/documents/import",
        headers=headers,
        files={"document": ("api.pdf", pdf.read_bytes(), "application/pdf")},
    )
    assert resp2.status_code == 202
    assert resp2.json()["duplicated"] is True
    assert resp2.json()["job_id"] == body["job_id"]


def test_import_api_rejects_unsupported_format(client, headers):
    resp = client.post(
        "/api/documents/import",
        headers=headers,
        files={"document": ("notes.txt", b"hello", "text/plain")},
    )
    assert resp.status_code == 415


def test_import_api_rejects_invalid_grade(client, headers, tmp_path):
    pdf = make_text_pdf(tmp_path / "g.pdf", ["1. 题目"])
    resp = client.post(
        "/api/documents/import",
        headers=headers,
        files={"document": ("g.pdf", pdf.read_bytes(), "application/pdf")},
        data={"grade": "13"},
    )
    assert resp.status_code == 422
