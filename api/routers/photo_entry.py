"""拍照录入路由：多图任务、候选轮询与确认入库。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from pydantic import BaseModel

from api.deps import get_current_user, rate_limit
from backend.models.orm import User
from backend.services.ai.base import AnalysisContext
from backend.services.photo_entry import (
    DEFAULT_IMPORT_CONTEXT,
    PhotoEntryService,
    PhotoImage,
)
from backend.services.question_service import sanitize_tags

router = APIRouter(prefix="/photo-entry", tags=["photo-entry"])

_ALLOWED_MIME = {"image/jpeg", "image/png", "image/webp"}
_MAX_IMAGE_BYTES = 10 * 1024 * 1024


class Confirmation(BaseModel):
    candidate_ids: list[str]


def _service() -> PhotoEntryService:
    return PhotoEntryService()


@router.post(
    "/jobs",
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(rate_limit("photo-entry:jobs", 20))],
)
async def create_job(
    images: list[UploadFile] = File(...),
    tags: str = Form(default=""),
    hint: str = Form(default=""),
    subject: str | None = Form(default=None),
    grade: int | None = Form(default=None),
    region: str | None = Form(default=None),
    textbook_version: str | None = Form(default=None),
    user: User = Depends(get_current_user),
) -> dict:
    uploads = []
    for image in images:
        if image.content_type not in _ALLOWED_MIME:
            raise HTTPException(
                status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
                f"仅支持 {', '.join(sorted(_ALLOWED_MIME))}",
            )
        data = await image.read()
        if not data:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "图片内容为空")
        if len(data) > _MAX_IMAGE_BYTES:
            raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "图片不能超过 10MB")
        uploads.append(PhotoImage(image.filename or "upload.jpg", data, image.content_type))
    if grade is not None and not 1 <= grade <= 12:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "年级必须在 1-12 之间")

    context = None
    if any(value is not None for value in (subject, grade, region, textbook_version)):
        context = AnalysisContext(
            subject=subject or DEFAULT_IMPORT_CONTEXT.subject,
            grade=grade if grade is not None else DEFAULT_IMPORT_CONTEXT.grade,
            region=region or DEFAULT_IMPORT_CONTEXT.region,
            textbook_version=textbook_version or DEFAULT_IMPORT_CONTEXT.textbook_version,
        )
    job_id = _service().create_job(
        user.id,
        uploads,
        tags=sanitize_tags(tags),
        hint=hint,
        context=context,
    )
    return {"job_id": job_id, "status": "pending"}


@router.get("/jobs/{job_id}")
def get_job(job_id: str, user: User = Depends(get_current_user)) -> dict:
    job = _service().get_job(job_id, user.id)
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "拍照录入任务不存在")
    return job


@router.post("/jobs/{job_id}/confirm")
def confirm_job(
    job_id: str, body: Confirmation, user: User = Depends(get_current_user)
) -> dict:
    service = _service()
    try:
        service.confirm(job_id, user.id, body.candidate_ids)
    except LookupError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    job = service.get_job(job_id, user.id)
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "拍照录入任务不存在")
    return job
