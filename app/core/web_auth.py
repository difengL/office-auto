"""系统账号认证：cookie 会话 + FastAPI 依赖。"""
from __future__ import annotations

from fastapi import Cookie, HTTPException

from . import db

COOKIE_NAME = "mc_session"
COOKIE_MAX_AGE = db.SESSION_DAYS * 86400


def current_user(mc_session: str | None = Cookie(default=None)) -> db.User:
    user = db.user_by_session(mc_session or "")
    if not user:
        raise HTTPException(status_code=401, detail="未登录或会话已过期")
    return user