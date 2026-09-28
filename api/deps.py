"""FastAPI 依赖注入：数据库会话、当前用户、轻量限流。"""
from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Iterator

from fastapi import Depends, HTTPException, Request, status
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


# ---------- 轻量限流（滑动窗口，进程内内存实现） ----------
# 适用：单机双进程部署。多实例水平扩展时需换成 Redis 等共享存储。
_buckets: dict[str, deque[float]] = {}
_buckets_lock = threading.Lock()


def _hit(scope: str, ident: str, max_calls: int, window_seconds: int) -> None:
    from backend.config import get_settings

    if not get_settings().api_rate_limit_enabled:
        return
    now = time.monotonic()
    key = f"{scope}:{ident}"
    with _buckets_lock:
        bucket = _buckets.setdefault(key, deque())
        while bucket and now - bucket[0] > window_seconds:
            bucket.popleft()
        if len(bucket) >= max_calls:
            retry_after = int(window_seconds - (now - bucket[0])) + 1
            raise HTTPException(
                status.HTTP_429_TOO_MANY_REQUESTS,
                "请求过于频繁，请稍后再试",
                headers={"Retry-After": str(retry_after)},
            )
        bucket.append(now)
        if not bucket:
            _buckets.pop(key, None)
        # 防止长时间运行后字典无界增长：定期清理空桶之外的空闲桶
        if len(_buckets) > 10000:
            idle = [k for k, v in _buckets.items() if v and now - v[-1] > window_seconds]
            for k in idle:
                _buckets.pop(k, None)


def rate_limit(scope: str, max_calls: int, window_seconds: int = 60, by: str = "user"):
    """限流依赖工厂：by="user" 按登录用户计数；by="ip" 按客户端 IP 计数（登录/注册用）。

    用法：@router.post("/login", dependencies=[Depends(rate_limit("auth:login", 10, by="ip"))])
    """
    def _limit_by_user(request: Request, user: User = Depends(get_current_user)) -> None:
        _hit(scope, str(user.id), max_calls, window_seconds)

    def _limit_by_ip(request: Request) -> None:
        ident = request.client.host if request.client else "unknown"
        _hit(scope, ident, max_calls, window_seconds)

    return _limit_by_user if by == "user" else _limit_by_ip
