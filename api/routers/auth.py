"""认证路由：登录 / 注册 / 当前用户，返回 JWT。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from api.deps import get_current_user, get_db, rate_limit
from backend.models.orm import User
from backend.models.schemas import RegisterInput
from backend.services.auth import AuthService
from backend.utils.tokens import create_access_token

router = APIRouter(prefix="/auth", tags=["auth"])


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=64)


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user_id: int
    username: str
    role: str


class UserInfo(BaseModel):
    user_id: int
    username: str
    role: str


@router.post(
    "/login",
    response_model=TokenResponse,
    dependencies=[Depends(rate_limit("auth:login", 10, by="ip"))],
)
def login(payload: LoginRequest, db: Session = Depends(get_db)) -> TokenResponse:
    """登录（每 IP 10 次/分钟，防爆破）。"""
    result = AuthService(db).login(payload.username, payload.password)
    if not result.ok:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, result.message or "登录失败")
    assert result.user_id is not None and result.username is not None and result.role is not None
    return TokenResponse(
        access_token=create_access_token(result.user_id, result.role),
        user_id=result.user_id,
        username=result.username,
        role=result.role,
    )


@router.post(
    "/register",
    response_model=TokenResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(rate_limit("auth:register", 5, by="ip"))],
)
def register(payload: RegisterInput, db: Session = Depends(get_db)) -> TokenResponse:
    """注册（每 IP 5 次/分钟，防批量刷号）。"""
    result = AuthService(db).register(payload)
    if not result.ok:
        raise HTTPException(status.HTTP_409_CONFLICT, result.message or "注册失败")
    assert result.user_id is not None and result.username is not None and result.role is not None
    return TokenResponse(
        access_token=create_access_token(result.user_id, result.role),
        user_id=result.user_id,
        username=result.username,
        role=result.role,
    )


@router.get("/me", response_model=UserInfo)
def me(user: User = Depends(get_current_user)) -> UserInfo:
    """校验令牌并返回当前用户信息（移动端启动时恢复会话用）。"""
    return UserInfo(user_id=user.id, username=user.username, role=user.role)
