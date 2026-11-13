"""整卷导入：上传去重、异步任务、导入 API 骨架测试。"""
from __future__ import annotations

import hashlib
import time
from pathlib import Path

import pytest
from doc_samples import make_docx, make_image, make_text_pdf
from fastapi.testclient import TestClient

from api.main import create_app
from backend.models.orm import Job
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


def test_import_document_does_not_reuse_empty_success(
    question_service, student_user, db_session, tmp_path
):
    """历史任务成功但切题为 0 时，重新上传必须新建任务而不是复用空结果。"""
    pdf = make_text_pdf(tmp_path / "retry-empty.pdf", ["1. 重新导入后应正常切题"])
    data = pdf.read_bytes()
    db_session.add(
        Job(
            id="empty-result-job",
            user_id=student_user.id,
            type="document_import",
            status="success",
            payload={"doc_hash": hashlib.sha256(data).hexdigest()},
            result={"total": 0, "segments": []},
        )
    )
    db_session.commit()

    service = DocumentService()
    job_id, duplicated = service.import_document(student_user.id, data, "retry-empty.pdf")
    assert not duplicated
    assert job_id != "empty-result-job"
    job = _wait_job(service, job_id, student_user.id)
    assert job["status"] == "success", job["error"]
    assert job["result"]["total"] > 0


def test_import_document_rejects_bad_input(question_service, student_user):
    service = DocumentService()
    with pytest.raises(ValueError, match="不支持的文档格式"):
        service.import_document(student_user.id, b"x", "paper.txt")
    with pytest.raises(ValueError, match="内容为空"):
        service.import_document(student_user.id, b"", "paper.pdf")


def test_import_images_multiple_files_in_order(question_service, student_user, tmp_path):
    """多张试卷照片：按上传顺序组成逐页文档（一页一图），SHA-256 按序去重。"""
    photo1 = make_image(tmp_path / "p1.png", size=(600, 800))
    photo2 = make_image(tmp_path / "p2.png", size=(400, 300))
    service = DocumentService()

    job_id, dup = service.import_document(
        student_user.id,
        photo1.read_bytes(),
        "p1.png",
        extra_files=[(photo2.read_bytes(), "p2.png")],
        subject="math",
        grade=7,
    )
    assert not dup
    job = _wait_job(service, job_id, student_user.id)
    assert job["status"] == "success", job["error"]
    assert job["result"]["pages"] == 2
    assert job["result"]["scanned_pages"] == 2

    # 页序 = 上传顺序：第 1 页是 photo1，第 2 页是 photo2
    page_images = job["result"]["page_images"]
    assert len(page_images) == 2
    assert Path(page_images[0]).read_bytes() == photo1.read_bytes()
    assert Path(page_images[1]).read_bytes() == photo2.read_bytes()

    # 同一组照片（同序）重复上传 → 复用既有任务
    job_id2, dup2 = service.import_document(
        student_user.id,
        photo1.read_bytes(),
        "p1.png",
        extra_files=[(photo2.read_bytes(), "p2.png")],
    )
    assert dup2
    assert job_id2 == job_id

    # 顺序不同 = 另一份文档 → 新任务
    job_id3, dup3 = service.import_document(
        student_user.id,
        photo2.read_bytes(),
        "p2.png",
        extra_files=[(photo1.read_bytes(), "p1.png")],
    )
    assert not dup3
    assert job_id3 != job_id
    job3 = _wait_job(service, job_id3, student_user.id)
    assert job3["status"] == "success", job3["error"]


def test_import_single_image(question_service, student_user, tmp_path):
    """单张照片导入：一页扫描版，兼容单文件路径。"""
    photo = make_image(tmp_path / "single.jpg")
    service = DocumentService()
    job_id, dup = service.import_document(student_user.id, photo.read_bytes(), "single.jpg")
    assert not dup
    job = _wait_job(service, job_id, student_user.id)
    assert job["status"] == "success", job["error"]
    assert job["result"]["pages"] == 1
    assert job["result"]["scanned_pages"] == 1


def test_import_image_saves_geometry_figure_outcome(
    question_service, student_user, tmp_path, monkeypatch
):
    from backend.services.ai.mock import MockProvider
    from backend.services.geometry_figures import FigureOutcome, GeometryFigureService

    class _FigureProvider(MockProvider):
        def segment_page(self, image_bytes, mime_type="image/png"):
            return [{
                "number": "1",
                "text": "如图，求线段长度。",
                "figure_bboxes": [[0, 0, 500, 500]],
                "continued_from_prev": False,
                "continues_to_next": False,
            }]

    class _FigureService:
        def __init__(self, figure_path, spec):
            self.figure_path = figure_path
            self.spec = spec
            self.calls = []

        def redraw_segment(self, **kwargs):
            self.calls.append(kwargs)
            return FigureOutcome([str(self.figure_path)], [self.spec], False)

    figure_path = tmp_path / "redrawn.png"
    make_image(figure_path)
    spec = {
        "kind": "geometry",
        "semantic_spec": {"shape": "triangle"},
        "tikz_source": "\\draw (0,0)--(1,0);",
        "render_path": str(figure_path),
        "crop_path": None,
        "validation_status": "ai_passed",
        "validation_issues": [],
        "assumptions": [],
    }
    figure_service = _FigureService(figure_path, spec)
    provider = _FigureProvider()
    created_with = []

    def _from_settings(cls, ai, settings):
        created_with.append((ai, settings))
        return figure_service

    monkeypatch.setattr("backend.services.ai.get_ai_service", lambda settings: provider)
    monkeypatch.setattr(GeometryFigureService, "from_settings", classmethod(_from_settings))
    photo = make_image(tmp_path / "figure.png")
    service = DocumentService()
    job_id, _ = service.import_document(
        student_user.id, photo.read_bytes(), "figure.png", subject="math", grade=8
    )
    job = _wait_job(service, job_id, student_user.id)

    assert job["status"] == "success", job["error"]
    entry = job["result"]["segments"][0]
    assert entry["figure_paths"] == [str(figure_path)]
    assert entry["figure_specs"] == [spec]
    assert created_with == [(provider, service.settings)]
    assert len(figure_service.calls) == 1
    assert figure_service.calls[0]["figure_bboxes"] == [[0, 0, 500, 500]]
    assert figure_service.calls[0]["context"].grade == 8


def test_import_document_rejects_multi_non_image(question_service, student_user, tmp_path):
    """多文件仅支持图片：PDF/DOCX 组合或图 + 文档混搭一律拒绝。"""
    pdf = make_text_pdf(tmp_path / "a.pdf", ["1. 题目"])
    photo = make_image(tmp_path / "p.png")
    service = DocumentService()

    with pytest.raises(ValueError, match="多文件仅支持图片"):
        service.import_document(
            student_user.id,
            pdf.read_bytes(),
            "a.pdf",
            extra_files=[(pdf.read_bytes(), "b.pdf")],
        )
    with pytest.raises(ValueError, match="多文件仅支持图片"):
        service.import_document(
            student_user.id,
            photo.read_bytes(),
            "p.png",
            extra_files=[(pdf.read_bytes(), "a.pdf")],
        )
    with pytest.raises(ValueError, match="内容为空"):
        service.import_document(
            student_user.id,
            photo.read_bytes(),
            "p2.png",
            extra_files=[(b"", "empty.png")],
        )


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
    # gif 等非白名单图片格式同样拒绝
    resp_gif = client.post(
        "/api/documents/import",
        headers=headers,
        files={"document": ("notes.gif", b"GIF89a", "image/gif")},
    )
    assert resp_gif.status_code == 415


def test_import_api_accepts_multiple_images(client, headers, tmp_path):
    """多张试卷照片：一次上传多文件，按上传顺序成页（每张 = 一页）。"""
    # 尺寸与服务层用例区分，避免相同字节触发 SHA-256 去重
    photo1 = make_image(tmp_path / "api1.png", size=(610, 810))
    photo2 = make_image(tmp_path / "api2.png", size=(410, 310))
    resp = client.post(
        "/api/documents/import",
        headers=headers,
        files=[
            ("document", ("api1.png", photo1.read_bytes(), "image/png")),
            ("document", ("api2.png", photo2.read_bytes(), "image/png")),
        ],
        data={"subject": "math", "grade": "7"},
    )
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["status"] == "pending"
    assert body["duplicated"] is False

    deadline = time.time() + 30
    seg = {}
    while time.time() < deadline:
        seg = client.get(f"/api/documents/{body['job_id']}/segments", headers=headers).json()
        if seg["status"] in ("success", "failed"):
            break
        time.sleep(0.1)
    assert seg["status"] == "success", seg["error"]
    assert seg["pages"] == 2
    assert seg["scanned_pages"] == 2


def test_import_api_rejects_multi_non_image(client, headers, tmp_path):
    """多文件仅限图片：多份 PDF 或 PDF + 图片混搭返回 400。"""
    pdf = make_text_pdf(tmp_path / "m.pdf", ["1. 题目"])
    photo = make_image(tmp_path / "m.png")

    resp_pdfs = client.post(
        "/api/documents/import",
        headers=headers,
        files=[
            ("document", ("m1.pdf", pdf.read_bytes(), "application/pdf")),
            ("document", ("m2.pdf", pdf.read_bytes(), "application/pdf")),
        ],
    )
    assert resp_pdfs.status_code == 400

    resp_mixed = client.post(
        "/api/documents/import",
        headers=headers,
        files=[
            ("document", ("m.png", photo.read_bytes(), "image/png")),
            ("document", ("m.pdf", pdf.read_bytes(), "application/pdf")),
        ],
    )
    assert resp_mixed.status_code == 400


def test_import_api_rejects_invalid_grade(client, headers, tmp_path):
    pdf = make_text_pdf(tmp_path / "g.pdf", ["1. 题目"])
    resp = client.post(
        "/api/documents/import",
        headers=headers,
        files={"document": ("g.pdf", pdf.read_bytes(), "application/pdf")},
        data={"grade": "13"},
    )
    assert resp.status_code == 422


def _wait_api_document(client, headers, job_id: str) -> dict:
    deadline = time.time() + 30
    body = {}
    while time.time() < deadline:
        resp = client.get(f"/api/documents/{job_id}/segments", headers=headers)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        if body["status"] in ("success", "failed"):
            return body
        time.sleep(0.1)
    raise TimeoutError(f"任务 {job_id} 在 30s 内未完成")


def test_import_api_default_context_is_applied_after_confirm(
    client, headers, question_service, student_user, tmp_path
):
    """Mobile 显式选择默认上下文时，确认入库使用共享的数学/高中默认值的实际行为。"""
    pdf = make_text_pdf(
        tmp_path / "default-context.pdf",
        ["1. 默认上下文题一：求 x。\n2. 默认上下文题二：求 y。"],
    )
    resp = client.post(
        "/api/documents/import",
        headers=headers,
        files={"document": ("default-context.pdf", pdf.read_bytes(), "application/pdf")},
        data={"use_default_context": "true"},
    )
    assert resp.status_code == 202, resp.text
    job_id = resp.json()["job_id"]
    assert _wait_api_document(client, headers, job_id)["status"] == "success"
    confirm = client.post(f"/api/documents/{job_id}/confirm", headers=headers)
    assert confirm.status_code == 200, confirm.text
    assert confirm.json()["imported"] == 2

    docs = [
        q
        for q in question_service.list_questions(student_user.id, semantic=False)
        if (q.source_doc or "").startswith("default-context.pdf#")
    ]
    assert len(docs) == 2
    assert {(q.subject, q.grade, q.region, q.textbook_version) for q in docs} == {
        ("math", 10, "上海", "沪教版")
    }


def test_import_api_without_default_context_flag_preserves_legacy_grade(
    client, headers, question_service, student_user, tmp_path
):
    """既有调用方不传标志时，不能在确认入库时静默获得默认年级。"""
    pdf = make_text_pdf(
        tmp_path / "legacy-context.pdf",
        ["1.  legacy 题一：求 x。\n2. legacy 题二：求 y。"],
    )
    resp = client.post(
        "/api/documents/import",
        headers=headers,
        files={"document": ("legacy-context.pdf", pdf.read_bytes(), "application/pdf")},
    )
    assert resp.status_code == 202, resp.text
    job_id = resp.json()["job_id"]
    assert _wait_api_document(client, headers, job_id)["status"] == "success"
    confirm = client.post(f"/api/documents/{job_id}/confirm", headers=headers)
    assert confirm.status_code == 200, confirm.text

    docs = [
        q
        for q in question_service.list_questions(student_user.id, semantic=False)
        if (q.source_doc or "").startswith("legacy-context.pdf#")
    ]
    assert len(docs) == 2
    assert [q.grade for q in docs] == [None, None]


def test_same_bytes_legacy_then_default_context_confirm_separately(
    client, headers, question_service, student_user, tmp_path
):
    """显式默认上下文不能复用同字节但无上下文的历史任务。"""
    pdf = make_text_pdf(
        tmp_path / "legacy-then-default.pdf",
        ["1. 同字节上下文题一：求 x。\n2. 同字节上下文题二：求 y。"],
    )
    files = {
        "document": ("legacy-then-default.pdf", pdf.read_bytes(), "application/pdf")
    }
    legacy = client.post("/api/documents/import", headers=headers, files=files)
    assert legacy.status_code == 202, legacy.text
    legacy_id = legacy.json()["job_id"]
    assert _wait_api_document(client, headers, legacy_id)["status"] == "success"

    defaulted = client.post(
        "/api/documents/import",
        headers=headers,
        files=files,
        data={"use_default_context": "true"},
    )
    assert defaulted.status_code == 202, defaulted.text
    assert defaulted.json()["duplicated"] is False
    default_id = defaulted.json()["job_id"]
    assert default_id != legacy_id
    assert _wait_api_document(client, headers, default_id)["status"] == "success"

    for job_id in (legacy_id, default_id):
        confirmed = client.post(f"/api/documents/{job_id}/confirm", headers=headers)
        assert confirmed.status_code == 200, confirmed.text
        assert confirmed.json()["imported"] == 2

    docs = [
        q
        for q in question_service.list_questions(student_user.id, semantic=False)
        if (q.source_doc or "").startswith("legacy-then-default.pdf#")
    ]
    assert len(docs) == 4
    contexts = {(q.subject, q.grade, q.region, q.textbook_version) for q in docs}
    assert contexts == {
        ("math", None, None, None),
        ("math", 10, "上海", "沪教版"),
    }


@pytest.mark.parametrize("suffix", ["pdf", "docx"])
def test_import_api_accepts_direct_pdf_and_docx(client, headers, tmp_path, suffix):
    """不必先转 PDF：PDF 与 DOCX 原文件都能直接创建并完成任务。"""
    path = tmp_path / f"direct.{suffix}"
    if suffix == "pdf":
        make_text_pdf(path, ["1. 直接 PDF 题：求 x。\n2. 直接 PDF 题二：求 y。"])
    else:
        make_docx(path, ["1. 直接 DOCX 题：求 x。", "2. 直接 DOCX 题二：求 y。"], with_image=False)
    mime = (
        "application/pdf"
        if suffix == "pdf"
        else "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )
    resp = client.post(
        "/api/documents/import",
        headers=headers,
        files={"document": (path.name, path.read_bytes(), mime)},
    )
    assert resp.status_code == 202, resp.text
    body = _wait_api_document(client, headers, resp.json()["job_id"])
    assert body["status"] == "success", body["error"]
    assert body["pages"] == 1
    assert body["total"] == 2


def test_import_api_invalid_and_empty_files_create_no_job(client, headers):
    """格式校验或空文件失败时不能留下 pending 任务。"""
    from backend.database import SessionLocal

    with SessionLocal() as session:
        before = session.query(Job).count()
    invalid = client.post(
        "/api/documents/import",
        headers=headers,
        files={"document": ("invalid.txt", b"not a document", "text/plain")},
    )
    assert invalid.status_code == 415
    empty = client.post(
        "/api/documents/import",
        headers=headers,
        files={"document": ("empty.pdf", b"", "application/pdf")},
    )
    assert empty.status_code == 400
    with SessionLocal() as session:
        assert session.query(Job).count() == before
