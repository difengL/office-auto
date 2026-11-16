"""会话认证：把美餐验证码当作本应用的登录凭据。

流程：
  1. 用户输手机号 → 发码 → 输码
  2. 美餐验证通过 = 证明他拥有该账号
  3. 签发 session cookie（30 天滑动续期）
  4. 之后所有业务请求只认 cookie，不再碰美餐验证码
"""
from __future__ import annotations

from fastapi import Cookie, HTTPException

from . import db

COOKIE_NAME = "mc_session"
COOKIE_MAX_AGE = db.SESSION_DAYS * 86400


async def current_user(mc_session: str | None = Cookie(default=None)) -> db.User:
    """FastAPI 依赖：从 cookie 解析当前用户。未登录抛 401。"""
    if not mc_session:
        raise HTTPException(401, "未登录")
    user = db.user_by_session(mc_session)
    if not user:
        raise HTTPException(401, "会话已过期，请重新登录")
    return user


async def optional_user(mc_session: str | None = Cookie(default=None)) -> db.User | None:
    if not mc_session:
        return None
    return db.user_by_session(mc_session)
