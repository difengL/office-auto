"""Token 保活与自动续期（多用户版）。

核心假设（2026-11-16 实测）：
  美餐 accessToken 的 TTL 是「最后一次使用后 1 小时」，而不是「签发后 1 小时」。
  手机端之所以数月不用重登，是因为用户持续使用，每次请求都在续命。

每 20 分钟对每个已登录用户请求一次轻量接口。遇到 401 尝试用 refreshToken
续期（实测 v4 签发的 refreshToken 在 v3/v4 都不被接受）。
"""
from __future__ import annotations

import time
from datetime import date

from . import db
from .meican import MeicanClient, MeicanError

KEEPALIVE_MINUTES = 20


def ping(user_id: int) -> tuple[bool, str]:
    """轻量请求，用于续命。成功会更新 last_ping_at。"""
    creds = db.load_creds(user_id)
    if not creds.access_token:
        return False, "未登录"
    try:
        MeicanClient(creds.access_token).calendar_items(date.today(), date.today(),
                                                        with_order=False)
        db.touch_ping(user_id)
        return True, "ok"
    except MeicanError as exc:
        return False, exc.message
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"


def try_refresh(user_id: int) -> tuple[bool, str]:
    """尝试用 refreshToken 续期。实测基本都失败，保留以备美餐后续改动。"""
    from .meican import V4_CLIENT_ID, V4_CLIENT_SECRET, WEB_PC_APP
    import httpx

    creds = db.load_creds(user_id)
    if not creds.refresh_token:
        return False, "没有 refreshToken"

    query = ("mutation RefreshToken($i: UserCenterSsoV2RefreshRequest!)"
             "{refreshToken(input:$i){token{accessToken refreshToken expiry tokenType}}}")
    try:
        r = httpx.post(
            "https://gateway.meican.com/graphql?op=RefreshToken",
            json={"operationName": "RefreshToken", "query": query,
                  "variables": {"i": {"refreshToken": creds.refresh_token}}},
            headers={"content-type": "application/json", "clientid": V4_CLIENT_ID,
                     "clientsecret": V4_CLIENT_SECRET, "x-mc-app": WEB_PC_APP},
            timeout=20,
        )
        payload = r.json()
        if payload.get("errors"):
            return False, payload["errors"][0].get("message", "刷新失败")
        token = (payload.get("data", {}).get("refreshToken") or {}).get("token") or {}
        if not token.get("accessToken"):
            return False, "刷新未返回 token"
        creds.access_token = token["accessToken"]
        creds.refresh_token = token.get("refreshToken", creds.refresh_token)
        creds.obtained_at = int(time.time())
        creds.expires_at = int(time.time() + float(token.get("expiry") or 3600))
        db.save_creds(user_id, creds)
        return True, "已刷新"
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"


def ensure_token(user_id: int) -> tuple[bool, str]:
    """业务接口调用前的守卫：token 不行就尝试续期。"""
    ok, msg = ping(user_id)
    if ok:
        return True, "ok"
    refreshed, rmsg = try_refresh(user_id)
    if refreshed:
        return True, "refreshed"
    return False, f"{msg} / {rmsg}"


def keepalive_tick(user_id: int) -> dict:
    """一次保活检查，供该用户的定时任务调用。"""
    ok, msg = ping(user_id)
    if ok:
        return {"ok": True, "action": "ping", "message": msg}

    db.log(user_id, "keepalive_fail", f"保活请求失败：{msg}")
    refreshed, rmsg = try_refresh(user_id)
    if refreshed:
        db.log(user_id, "keepalive", f"已通过 refreshToken 续期：{rmsg}")
        return {"ok": True, "action": "refresh", "message": rmsg}

    db.log(user_id, "keepalive_dead",
           f"token 已失效且无法续期：{rmsg}，需要重新登录")
    return {"ok": False, "action": "relogin_required", "message": f"{msg} / {rmsg}"}
