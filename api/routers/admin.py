"""后台管理路由：按学科清空错题本（全系统口径，仅 admin）。

admin 判定与 users.role 独立：用户名在 ADMIN_USERNAMES 名单内即管理员。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from api.deps import get_admin_user
from backend.models.orm import User
from backend.services.question_service import QuestionService

router = APIRouter(prefix="/admin", tags=["admin"])


class SubjectCount(BaseModel):
    subject: str
    name: str
    count: int


class PurgeResult(BaseModel):
    subject: str
    deleted: int


def _service() -> QuestionService:
    return QuestionService()


@router.get("/subjects", response_model=list[SubjectCount])
def list_subjects(admin: User = Depends(get_admin_user)) -> list[dict]:
    """全系统各学科错题数（预览用）。非 admin 由依赖直接 403。"""
    return _service().admin_subject_overview(admin.id)


@router.delete("/questions", response_model=PurgeResult)
def purge_questions_by_subject(
    subject: str = Query(..., min_length=1, description="学科代码，如 math"),
    admin: User = Depends(get_admin_user),
) -> dict:
    """按学科清空错题本：删除该学科全部错题及复习/检测/批注记录，
    并同步删除向量库嵌入。不可恢复，仅 admin。"""
    return _service().purge_subject(admin.id, subject)
