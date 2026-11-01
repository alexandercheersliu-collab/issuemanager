"""后台管理路由：重置用户密码、按学科清空错题本（全系统口径，仅 admin）。

admin 判定与 users.role 独立：用户名在 ADMIN_USERNAMES 名单内即管理员。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from api.deps import get_admin_user, get_db
from backend.models.orm import User
from backend.services.auth import AuthService
from backend.services.question_service import QuestionService

router = APIRouter(prefix="/admin", tags=["admin"])


class SubjectCount(BaseModel):
    subject: str
    name: str
    count: int


class PurgeResult(BaseModel):
    subject: str
    deleted: int


class ResetPasswordResult(BaseModel):
    username: str
    message: str


def _service() -> QuestionService:
    return QuestionService()


@router.get("/subjects", response_model=list[SubjectCount])
def list_subjects(admin: User = Depends(get_admin_user)) -> list[dict]:
    """全系统各学科错题数（预览用）。非 admin 由依赖直接 403。"""
    return _service().admin_subject_overview(admin.id)


@router.post("/users/{user_id}/reset-password", response_model=ResetPasswordResult)
def reset_user_password(
    user_id: int,
    admin: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
) -> ResetPasswordResult:
    """把指定用户密码重置为初始密码并解除登录锁定。仅 admin；不能重置 admin 账号。"""
    try:
        result = AuthService(db).reset_password_by_admin(admin.id, user_id)
    except PermissionError as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    return ResetPasswordResult(username=result.username or "", message=result.message)


@router.delete("/questions", response_model=PurgeResult)
def purge_questions_by_subject(
    subject: str = Query(..., min_length=1, description="学科代码，如 math"),
    admin: User = Depends(get_admin_user),
) -> dict:
    """按学科清空错题本：删除该学科全部错题及复习/检测/批注记录，
    并同步删除向量库嵌入。不可恢复，仅 admin。"""
    return _service().purge_subject(admin.id, subject)
