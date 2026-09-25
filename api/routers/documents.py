"""整卷导入路由：文档上传（去重）→ 异步切题/解构 → 校对确认入库。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status

from api.deps import get_current_user
from backend.models.orm import User
from backend.services.document_service import DocumentService

router = APIRouter(prefix="/documents", tags=["documents"])

_MAX_DOC_BYTES = 50 * 1024 * 1024


def _service() -> DocumentService:
    return DocumentService()


@router.post("/import", status_code=status.HTTP_202_ACCEPTED)
async def import_document(
    document: UploadFile = File(...),
    subject: str = Form(default="math"),
    grade: int | None = Form(default=None),
    region: str = Form(default=""),
    textbook_version: str = Form(default=""),
    user: User = Depends(get_current_user),
) -> dict:
    """上传整卷文档（PDF/DOCX），创建异步导入任务；重复上传返回既有任务。"""
    data = await document.read()
    if not data:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "文档内容为空")
    if len(data) > _MAX_DOC_BYTES:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "文档不能超过 50MB")
    if grade is not None and not 1 <= grade <= 12:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "年级必须在 1-12 之间")

    try:
        job_id, duplicated = _service().import_document(
            user.id,
            data,
            document.filename or "upload.pdf",
            subject=subject or "math",
            grade=grade,
            region=region or None,
            textbook_version=textbook_version or None,
        )
    except ValueError as exc:
        raise HTTPException(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, str(exc)) from exc
    return {"job_id": job_id, "duplicated": duplicated, "status": "pending"}


@router.get("/{job_id}/segments")
def get_segments(job_id: str, user: User = Depends(get_current_user)) -> dict:
    """取切题 + 逐题解构结果（含 needs_review 标记与 total/done 进度）。"""
    job = _service().get_job(job_id, user.id)
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "导入任务不存在")
    result = job["result"] or {}
    segments = result.get("segments", [])
    return {
        "job_id": job_id,
        "status": job["status"],
        "error": job["error"],
        "pages": result.get("pages", 0),
        "scanned_pages": result.get("scanned_pages", 0),
        "total": result.get("total", 0),
        "done": result.get("done", 0),
        "needs_review_count": sum(1 for s in segments if s.get("needs_review")),
        "confirmed": result.get("confirmed", False),
        "segments": segments,
    }


@router.post("/{job_id}/confirm")
def confirm_import(job_id: str, user: User = Depends(get_current_user)) -> dict:
    """确认入库：解构成功的题批量写入错题本（幂等，重复确认返回既有结果）。"""
    try:
        return _service().confirm_import(job_id, user.id)
    except LookupError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
