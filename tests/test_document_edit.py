"""校对编辑操作测试：合并/拆分/删除/重解析/字段编辑、留痕、confirm 跳过明细。"""
from __future__ import annotations

import time

import pytest
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


def _import_and_wait(student_user, tmp_path, name="edit.pdf"):
    pdf = make_text_pdf(
        tmp_path / name,
        ["1. 校对题一：已知 x^2=9，求 x 的值。\n2. 校对题二：计算 1+1 的结果。"],
    )
    service = DocumentService()
    job_id, _ = service.import_document(
        student_user.id, pdf.read_bytes(), name, subject="math", grade=9
    )
    job = _wait_job(service, job_id, student_user.id)
    assert job["status"] == "success", job["error"]
    assert job["result"]["total"] == 2
    return service, job_id


def _segments(service: DocumentService, job_id: str, user_id: int) -> list[dict]:
    job = service.get_job(job_id, user_id)
    assert job is not None
    return job["result"]["segments"]


def test_update_segment_fields_and_clears_review(student_user, tmp_path):
    service, job_id = _import_and_wait(student_user, tmp_path)

    entry = service.update_segment(
        job_id,
        student_user.id,
        0,
        text="1. 校对题一（已修订）：求 x。",
        analysis_updates={"answer": "x=±3", "非白名单字段": "应被忽略"},
    )
    assert entry["text"] == "1. 校对题一（已修订）：求 x。"
    assert entry["analysis"]["answer"] == "x=±3"
    assert "非白名单字段" not in entry["analysis"]
    assert entry["needs_review"] is False
    assert entry["error"] is None

    job = service.get_job(job_id, student_user.id)
    log = job["result"]["edit_log"]
    assert any(item["action"] == "update" and item["index"] == 0 for item in log)


def test_update_segment_rejects_invalid_job_and_index(student_user, tmp_path):
    service, job_id = _import_and_wait(student_user, tmp_path, "invalid.pdf")
    with pytest.raises(LookupError):
        service.update_segment("no-such-job", student_user.id, 0, text="x")
    with pytest.raises(IndexError):
        service.update_segment(job_id, student_user.id, 99, text="x")


def test_merge_segments(student_user, tmp_path):
    service, job_id = _import_and_wait(student_user, tmp_path, "merge.pdf")

    merged = service.merge_segments(job_id, student_user.id, 0)
    assert "校对题一" in merged["text"] and "校对题二" in merged["text"]
    assert merged["needs_review"] is True  # 结构变动需人工确认
    assert merged["analysis"] is not None  # 解构结果优先保留前者

    segments = _segments(service, job_id, student_user.id)
    assert len(segments) == 1
    job = service.get_job(job_id, student_user.id)
    assert job["result"]["total"] == 1  # total/done 同步
    assert job["result"]["done"] == 1

    with pytest.raises(IndexError):
        service.merge_segments(job_id, student_user.id, 0)  # 已无下一题


def test_split_segment(student_user, tmp_path):
    service, job_id = _import_and_wait(student_user, tmp_path, "split.pdf")
    original = _segments(service, job_id, student_user.id)[0]
    split_at = len(original["text"]) // 2

    first, second = service.split_segment(job_id, student_user.id, 0, split_at)
    assert first["text"] + "" != ""
    assert first["text"] == original["text"][:split_at].rstrip()
    assert second["text"] == original["text"][split_at:].lstrip()
    assert second["number"].startswith(original["number"])
    assert second["analysis"] is None  # 后半题需重新解构
    assert first["needs_review"] is True and second["needs_review"] is True

    segments = _segments(service, job_id, student_user.id)
    assert len(segments) == 3

    with pytest.raises(ValueError):
        service.split_segment(job_id, student_user.id, 0, 0)  # 位置越界


def test_delete_segment(student_user, tmp_path):
    service, job_id = _import_and_wait(student_user, tmp_path, "delete.pdf")

    service.delete_segment(job_id, student_user.id, 0)
    segments = _segments(service, job_id, student_user.id)
    assert len(segments) == 1
    assert "校对题二" in segments[0]["text"]

    job = service.get_job(job_id, student_user.id)
    assert job["result"]["total"] == 1
    assert any(item["action"] == "delete" for item in job["result"]["edit_log"])


def test_reanalyze_segment(student_user, tmp_path):
    service, job_id = _import_and_wait(student_user, tmp_path, "reanalyze.pdf")
    original = _segments(service, job_id, student_user.id)[0]
    split_at = len(original["text"]) // 2
    service.split_segment(job_id, student_user.id, 0, split_at)

    entry = service.reanalyze_segment(job_id, student_user.id, 1)
    assert entry["analysis"] is not None
    assert entry["analysis"]["answer"]
    assert entry["needs_review"] is False
    assert entry["error"] is None


def test_edit_rejected_after_confirm(question_service, student_user, tmp_path):
    service, job_id = _import_and_wait(student_user, tmp_path, "locked.pdf")
    service.confirm_import(job_id, student_user.id)

    with pytest.raises(ValueError, match="已确认入库"):
        service.update_segment(job_id, student_user.id, 0, text="x")
    with pytest.raises(ValueError, match="已确认入库"):
        service.delete_segment(job_id, student_user.id, 0)


def test_confirm_reports_skipped(question_service, student_user, tmp_path):
    """拆分产生的未解构题在 confirm 时进入 skipped 明细，其余正常入库。"""
    service, job_id = _import_and_wait(student_user, tmp_path, "skipped.pdf")
    original = _segments(service, job_id, student_user.id)[0]
    service.split_segment(job_id, student_user.id, 0, len(original["text"]) // 2)

    outcome = service.confirm_import(job_id, student_user.id)
    assert outcome["already_confirmed"] is False
    assert outcome["imported"] == 2  # 原第 1 题前半 + 原第 2 题
    assert len(outcome["skipped"]) == 1
    assert outcome["skipped"][0]["reason"] == "未完成结构化解构"

    docs = [
        q
        for q in question_service.list_questions(student_user.id, semantic=False)
        if q.source == "document" and (q.source_doc or "").startswith("skipped.pdf#")
    ]
    assert len(docs) == 2

    again = service.confirm_import(job_id, student_user.id)
    assert again["already_confirmed"] is True
    assert again["skipped"] == outcome["skipped"]  # 幂等返回既有跳过明细
