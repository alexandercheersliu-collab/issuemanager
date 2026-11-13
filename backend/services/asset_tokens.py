"""Short-lived signed tokens for detection figures that have no database path field."""
from __future__ import annotations

import datetime as dt
from pathlib import Path

import jwt

from backend.config import get_settings

_ALGORITHM = "HS256"
_KIND = "detection_figure"
_LIFETIME = dt.timedelta(minutes=10)
_CLAIMS = ("kind", "user", "log", "index", "path", "iat", "exp")


def issue_detection_figure_token(
    *,
    log_id: int,
    user_id: int,
    figure_index: int,
    path: str,
    now: dt.datetime | None = None,
) -> str:
    issued_at = now or dt.datetime.now(dt.timezone.utc)
    payload = {
        "kind": _KIND,
        "user": user_id,
        "log": log_id,
        "index": figure_index,
        "path": path,
        "iat": issued_at,
        "exp": issued_at + _LIFETIME,
    }
    return jwt.encode(payload, get_settings().auth_secret, algorithm=_ALGORITHM)


def resolve_detection_figure_path(
    token: str,
    *,
    log_id: int,
    user_id: int,
    figure_index: int,
    now: dt.datetime | None = None,
) -> Path:
    try:
        claims = jwt.decode(
            token,
            get_settings().auth_secret,
            algorithms=[_ALGORITHM],
            require=list(_CLAIMS),
            options={"verify_exp": False},
        )
        if (
            claims["kind"] != _KIND
            or type(claims["user"]) is not int
            or type(claims["log"]) is not int
            or type(claims["index"]) is not int
            or not isinstance(claims["path"], str)
            or not claims["path"]
            or isinstance(claims["exp"], bool)
            or not isinstance(claims["exp"], (int, float))
        ):
            raise ValueError("Invalid detection figure claims")
        if (
            claims["user"] != user_id
            or claims["log"] != log_id
            or claims["index"] != figure_index
            or figure_index < 0
        ):
            raise ValueError("Detection figure token does not match the requested resource")
        current_time = now or dt.datetime.now(dt.timezone.utc)
        if current_time.timestamp() >= claims["exp"]:
            raise ValueError("Detection figure token has expired")
        return Path(claims["path"])
    except (jwt.InvalidTokenError, KeyError, TypeError, ValueError) as exc:
        raise ValueError("Invalid detection figure token") from exc
