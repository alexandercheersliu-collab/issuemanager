"""整卷导入路由：文档上传（去重）→ 异步切题/解构 → 校对确认入库。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from pydantic import BaseModel

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


# ---------- 校对编辑（P2.3） ----------


class SegmentUpdate(BaseModel):
    """校对编辑请求体：题干与解构字段（白名单内逐字段覆盖）。"""

    text: str | None = None
    analysis: dict[str, Any] | None = None


class SplitRequest(BaseModel):
    """拆分请求体：在题干第 split_at 个字符后拆成两题。"""

    split_at: int


def _run_edit(action, *args, **kwargs) -> Any:
    """统一校对编辑的错误映射：越界/不存在 404，状态冲突 409，重解析失败 502。"""
    try:
        return action(*args, **kwargs)
    except (LookupError, IndexError) as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc


@router.patch("/{job_id}/segments/{index}")
def update_segment(
    job_id: str, index: int, body: SegmentUpdate, user: User = Depends(get_current_user)
) -> dict:
    """编辑题干 / 解构字段；人工编辑即视为已校对（清除 needs_review）。"""
    entry = _run_edit(
        _service().update_segment,
        job_id,
        user.id,
        index,
        text=body.text,
        analysis_updates=body.analysis,
    )
    return {"segment": entry}


@router.post("/{job_id}/segments/{index}/merge")
def merge_segment(job_id: str, index: int, user: User = Depends(get_current_user)) -> dict:
    """合并第 index 题与下一题（解构结果优先保留前者，标 needs_review）。"""
    entry = _run_edit(_service().merge_segments, job_id, user.id, index)
    return {"segment": entry}


@router.post("/{job_id}/segments/{index}/split")
def split_segment(
    job_id: str, index: int, body: SplitRequest, user: User = Depends(get_current_user)
) -> dict:
    """按字符偏移把一题拆成两题（后半题需重新解构）。"""
    first, second = _run_edit(_service().split_segment, job_id, user.id, index, body.split_at)
    return {"segments": [first, second]}


@router.delete("/{job_id}/segments/{index}")
def delete_segment(job_id: str, index: int, user: User = Depends(get_current_user)) -> dict:
    """删除一题（误切的非题目内容、重复题等）。"""
    _run_edit(_service().delete_segment, job_id, user.id, index)
    return {"deleted": index}


@router.post("/{job_id}/segments/{index}/reanalyze")
def reanalyze_segment(job_id: str, index: int, user: User = Depends(get_current_user)) -> dict:
    """单题重新解构（调既有 AI 文本解析管线），失败返回 502。"""
    entry = _run_edit(_service().reanalyze_segment, job_id, user.id, index)
    return {"segment": entry}
