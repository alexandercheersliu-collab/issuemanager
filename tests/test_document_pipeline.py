"""整卷导入完整管线测试：解析 → 切题 → 逐题解构（进度与结果）。"""
from __future__ import annotations

import time

from doc_samples import make_text_pdf

from backend.services.document_service import DocumentService


def _wait_job(service: DocumentService, job_id: str, user_id: int, timeout: float = 30.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = service.get_job(job_id, user_id)
        assert job is not None
        if job["status"] in ("success", "failed"):
            return job
        time.sleep(0.1)
    raise TimeoutError(f"任务 {job_id} 在 {timeout}s 内未完成")


def test_pipeline_segments_and_analysis(question_service, student_user, tmp_path):
    """文字版 PDF：双策略对齐（规则 1,2 = mock 视觉 1,2），逐题解构带 K12 字段。"""
    pdf = make_text_pdf(
        tmp_path / "exam.pdf",
        ["1. 第一题：已知 x^2=9，求 x 的值。\n2. 第二题：计算 1+1 的结果。"],
    )
    service = DocumentService()
    job_id, duplicated = service.import_document(
        student_user.id, pdf.read_bytes(), "exam.pdf", subject="math", grade=9, region="北京"
    )
    assert not duplicated

    job = _wait_job(service, job_id, student_user.id)
    assert job["status"] == "success", job["error"]

    result = job["result"]
    assert result["pages"] == 1
    assert result["total"] == 2
    assert result["done"] == 2  # 进度最终走满
    assert result["confirmed"] is False

    segments = result["segments"]
    assert [s["number"] for s in segments] == ["1", "2"]
    for segment in segments:
        assert segment["needs_review"] is False  # 双策略一致
        assert segment["error"] is None
        analysis = segment["analysis"]
        assert analysis is not None
        # Mock 演示解构：K12 结构化字段齐备
        assert analysis["error_category"] == "概念不清"
        assert analysis["question_type"] == "解答题"
        assert analysis["chapter"] == "一元二次方程"
        assert analysis["answer"]


def test_pipeline_mismatch_marks_needs_review(question_service, student_user, tmp_path):
    """规则切出 3 题 vs mock 视觉 2 题：不一致标 needs_review，解构仍完成。"""
    pdf = make_text_pdf(
        tmp_path / "exam3.pdf",
        ["1. 题一：求 x。\n2. 题二：求 y。\n3. 题三：求 z。"],
    )
    service = DocumentService()
    job_id, _ = service.import_document(student_user.id, pdf.read_bytes(), "exam3.pdf")
    job = _wait_job(service, job_id, student_user.id)
    assert job["status"] == "success", job["error"]

    result = job["result"]
    assert result["total"] == 3
    assert result["done"] == 3
    assert all(s["needs_review"] for s in result["segments"])
    assert all(s["analysis"] is not None for s in result["segments"])


def test_pipeline_context_in_payload(question_service, student_user, tmp_path):
    """K12 录入上下文随任务 payload 保存（解构与确认入库共用）。"""
    pdf = make_text_pdf(tmp_path / "ctx.pdf", ["1. 上下文测试题：求 1+1。"])
    service = DocumentService()
    job_id, _ = service.import_document(
        student_user.id,
        pdf.read_bytes(),
        "ctx.pdf",
        subject="physics",
        grade=8,
        region="江苏",
        textbook_version="苏科版",
    )
    job = _wait_job(service, job_id, student_user.id)
    payload = job["payload"]
    assert payload["subject"] == "physics"
    assert payload["grade"] == 8
    assert payload["region"] == "江苏"
    assert payload["textbook_version"] == "苏科版"
