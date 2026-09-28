"""FastAPI 依赖注入：数据库会话与当前用户。"""
from __future__ import annotations

from collections.abc import Iterator

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from backend.database import SessionLocal
from backend.models.orm import User
from backend.utils.tokens import TokenError, decode_access_token

_bearer = HTTPBearer(auto_error=False)


def get_db() -> Iterator[Session]:
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    db: Session = Depends(get_db),
) -> User:
    if credentials is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "缺少认证令牌")
    try:
        payload = decode_access_token(credentials.credentials)
    except TokenError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(exc)) from exc
    user = db.get(User, int(payload["sub"]))
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "用户不存在")
    return user


def get_admin_user(user: User = Depends(get_current_user)) -> User:
    """后台管理端点鉴权：用户名需在 ADMIN_USERNAMES 名单内（与 role 独立）。"""
    from backend.services.auth import is_admin_username

    if not is_admin_username(user.username):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "仅管理员可执行该操作")
    return user
