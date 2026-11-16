"""美餐自动订餐 · 本地多用户服务

启动：  ./.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8765
访问：  http://<本机内网IP>:8765
"""
from __future__ import annotations

import asyncio
import random
import time
from datetime import date
from pathlib import Path

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import Cookie, Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import auth, db, keepalive, service
from .meican import MeicanClient, MeicanError

BASE_DIR = Path(__file__).resolve().parent
app = FastAPI(title="美餐自动订餐")
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")

scheduler = AsyncIOScheduler(timezone="Asia/Shanghai")

# 登录过程中的临时状态：phone -> {sent_at}
_pending: dict[str, float] = {}


@app.on_event("startup")
async def startup() -> None:
    db.init_db()
    scheduler.start()
    db.log(0, "app_start", "服务启动")
    reschedule_all()


# ---------------- 页面 ----------------

@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    return HTMLResponse((BASE_DIR / "templates" / "index.html").read_text("utf-8"))


# ---------------- 登录（无需会话）----------------

class PhoneBody(BaseModel):
    phone: str


class VerifyBody(BaseModel):
    phone: str
    code: str


@app.post("/api/login/send-code")
async def send_code(body: PhoneBody):
    if not (len(body.phone) == 11 and body.phone.isdigit()):
        raise HTTPException(400, "手机号格式不对")
    client = MeicanClient()
    try:
        client.send_sms_code(body.phone)
    except MeicanError as exc:
        raise HTTPException(400, exc.message) from exc
    _pending[body.phone] = time.time()
    return {
        "ok": True,
        "hint": "验证码是 4 位。接口返回成功但收不到短信，通常是美餐静默限流，"
                "同号当天发码别超过 4 次。",
    }


@app.post("/api/login/verify")
async def verify(body: VerifyBody, response: Response, request: Request):
    client = MeicanClient()
    try:
        login_data = client.login_with_sms(body.phone, body.code)
    except MeicanError as exc:
        raise HTTPException(400, exc.message) from exc

    users = login_data.get("userList") or []
    members = login_data.get("clientMemberList") or []
    if not users and not members:
        raise HTTPException(400, "该账号没有可用身份")

    chosen = None
    for u in users:
        if u.get("accountVersion") == "AccountFourDotZero":
            chosen = ("user", u)
            break
    if not chosen:
        for u in users:
            if u.get("accountVersion") == "AccountThreeDotZero":
                chosen = ("user", u)
                break
    if not chosen and members:
        chosen = ("member", members[0])

    kind, acct = chosen
    try:
        if kind == "user":
            token = client.choose_account(login_data["ticket"], login_data["signature"],
                                          snowflake_id=acct.get("snowflakeId"))
        else:
            token = client.choose_account(login_data["ticket"], login_data["signature"],
                                          client_id=acct.get("clientId"),
                                          client_member_id=acct.get("id"))
    except MeicanError as exc:
        raise HTTPException(400, exc.message) from exc

    # 登录即注册
    user = db.get_or_create_user(body.phone, acct.get("snowflakeId", ""),
                                 acct.get("name", ""))
    cred = db.Creds(
        access_token=token["accessToken"],
        refresh_token=token.get("refreshToken", ""),
        token_type=token.get("tokenType", "bearer"),
        obtained_at=int(time.time()),
        expires_at=int(time.time() + float(token.get("expiry") or 3600)),
        last_ping_at=int(time.time()),
        snowflake_id=acct.get("snowflakeId", ""),
    )
    db.save_creds(user.id, cred)

    ua = request.headers.get("user-agent", "")
    session, exp = db.issue_session(user.id, ua=ua)
    response.set_cookie(
        auth.COOKIE_NAME, session,
        max_age=auth.COOKIE_MAX_AGE, httponly=True, samesite="lax", path="/",
    )
    db.log(user.id, "login_success", f"登录成功：{user.name}")
    reschedule_user(user.id)

    return {"ok": True, "user": {"name": user.name, "phone_masked": user.phone_masked}}


@app.post("/api/logout")
async def logout(response: Response, mc_session: str | None = Cookie(default=None),
                 user: db.User = Depends(auth.current_user)):
    """只注销当前这台设备的会话，其他设备保持在线。"""
    if mc_session:
        db.revoke_session(mc_session)
    response.delete_cookie(auth.COOKIE_NAME, path="/")
    return {"ok": True}


@app.post("/api/logout-all")
async def logout_all(response: Response, user: db.User = Depends(auth.current_user)):
    """注销该用户的全部会话（所有设备）。"""
    db.clear_session(user.id)
    response.delete_cookie(auth.COOKIE_NAME, path="/")
    return {"ok": True}


@app.get("/api/sessions")
async def sessions(user: db.User = Depends(auth.current_user)):
    return {"sessions": db.list_sessions(user.id)}


@app.get("/api/me")
async def me(user: db.User = Depends(auth.current_user)):
    return {"id": user.id, "name": user.name, "phone_masked": user.phone_masked}


# ---------------- 状态 / 检测 ----------------

def _guard(user_id: int) -> None:
    ok, msg = keepalive.ensure_token(user_id)
    if not ok:
        raise HTTPException(401, f"登录已失效，请重新验证码登录（{msg}）")


@app.get("/api/status")
async def status(user: db.User = Depends(auth.current_user)):
    cred = db.load_creds(user.id)
    cfg = db.load_config(user.id)
    order_job = scheduler.get_job(f"order:{user.id}")
    ka_job = scheduler.get_job(f"keepalive:{user.id}")
    return {
        "logged_in": bool(cred.access_token),
        "user": {"id": user.id, "name": user.name, "phone_masked": user.phone_masked},
        "token_age_hours": round(cred.age_hours, 2),
        "token_idle_minutes": round(cred.idle_minutes, 1),
        "last_ping": time.strftime("%H:%M:%S", time.localtime(cred.last_ping_at))
        if cred.last_ping_at else None,
        "config": {
            "enabled": cfg.enabled, "schedule_time": cfg.schedule_time,
            "order_target": cfg.order_target, "max_price": cfg.max_price,
            "dry_run": cfg.dry_run,
        },
        "next_run": str(order_job.next_run_time) if order_job else None,
        "next_keepalive": str(ka_job.next_run_time) if ka_job else None,
    }


@app.get("/api/check")
async def check(day: str | None = None, user: db.User = Depends(auth.current_user)):
    target = date.fromisoformat(day) if day else None
    _guard(user.id)
    try:
        return service.check_status(user.id, target)
    except MeicanError as exc:
        raise HTTPException(400, exc.message) from exc


@app.get("/api/recommend")
async def recommend(day: str | None = None, limit: int = 5,
                    user: db.User = Depends(auth.current_user)):
    target = date.fromisoformat(day) if day else None
    _guard(user.id)
    try:
        return service.recommend(user.id, target, limit=limit)
    except MeicanError as exc:
        raise HTTPException(400, exc.message) from exc


class OrderBody(BaseModel):
    day: str | None = None
    dish_id: int | str | None = None
    dry_run: bool | None = None


@app.post("/api/order")
async def order(body: OrderBody, user: db.User = Depends(auth.current_user)):
    target = date.fromisoformat(body.day) if body.day else None
    _guard(user.id)
    cfg = db.load_config(user.id)
    dry = cfg.dry_run if body.dry_run is None else body.dry_run
    dish = None
    if body.dish_id is not None:
        rec = service.recommend(user.id, target, limit=50)
        for r in rec.get("recommendations", []):
            if str(r["dish_id"]) == str(body.dish_id):
                dish = r
                break
        if dish is None:
            raise HTTPException(400, "指定的菜品不在推荐列表内")
    try:
        return service.place_order(user.id, target, dish=dish, dry_run=dry)
    except MeicanError as exc:
        raise HTTPException(400, exc.message) from exc


@app.get("/api/history")
async def history(days: int = 45, user: db.User = Depends(auth.current_user)):
    _guard(user.id)
    try:
        records = service.fetch_history(user.id, days)
    except MeicanError as exc:
        raise HTTPException(400, exc.message) from exc
    from .preference import PreferenceModel, summarize
    model = PreferenceModel.from_history(records)
    return {"count": len(records), "records": records, "model": summarize(model)}


# ---------------- 配置 / 日志 ----------------

class ConfigBody(BaseModel):
    enabled: bool | None = None
    schedule_time: str | None = None
    order_target: str | None = None
    max_price: float | None = None
    dry_run: bool | None = None


@app.post("/api/config")
async def update_config(body: ConfigBody, user: db.User = Depends(auth.current_user)):
    cfg = db.load_config(user.id)
    data = body.model_dump(exclude_none=True)
    for k, v in data.items():
        setattr(cfg, k, v)
    db.save_config(user.id, cfg)
    reschedule_user(user.id)
    return {"ok": True, "config": cfg.__dict__}


@app.get("/api/logs")
async def logs(limit: int = 100, user: db.User = Depends(auth.current_user)):
    return {"logs": db.read_logs(user.id, limit)}


@app.post("/api/keepalive/now")
async def keepalive_now(user: db.User = Depends(auth.current_user)):
    return await asyncio.to_thread(keepalive.keepalive_tick, user.id)


@app.post("/api/run-now")
async def run_now(user: db.User = Depends(auth.current_user)):
    try:
        return await asyncio.to_thread(service.run_daily_job, user.id)
    except MeicanError as exc:
        raise HTTPException(400, exc.message) from exc


# ---------------- 调度 ----------------

def reschedule_user(user_id: int) -> None:
    cfg = db.load_config(user_id)

    for jid in (f"order:{user_id}", f"keepalive:{user_id}"):
        if scheduler.get_job(jid):
            scheduler.remove_job(jid)

    if not cfg.enabled:
        return

    try:
        hh, mm = cfg.schedule_time.split(":")
        scheduler.add_job(run_order_job, "cron", hour=int(hh), minute=int(mm),
                          args=[user_id], id=f"order:{user_id}", replace_existing=True)
    except Exception as exc:  # noqa: BLE001
        db.log(user_id, "schedule_error", f"定时设置无效：{exc}")

    # 保活：加随机抖动，避免多人同时整点发请求被风控
    scheduler.add_job(keepalive_job, "interval",
                      minutes=random.randint(15, 25),
                      args=[user_id], id=f"keepalive:{user_id}",
                      replace_existing=True)


def reschedule_all() -> None:
    for uid in db.enabled_user_ids():
        reschedule_user(uid)


async def run_order_job(user_id: int) -> None:
    try:
        await asyncio.to_thread(service.run_daily_job, user_id)
    except Exception as exc:  # noqa: BLE001
        db.log(user_id, "job_error", f"定时任务异常：{exc}")


async def keepalive_job(user_id: int) -> None:
    try:
        await asyncio.to_thread(keepalive.keepalive_tick, user_id)
    except Exception as exc:  # noqa: BLE001
        db.log(user_id, "keepalive_error", f"保活异常：{exc}")


@app.get("/api/admin/users")
async def admin_users(user: db.User = Depends(auth.current_user)):
    """查看已注册用户（内网小范围使用，所有人可见）。"""
    return {"users": [
        {"id": u.id, "name": u.name, "phone_masked": u.phone_masked,
         "logged_in": bool(db.load_creds(u.id).access_token)}
        for u in db.list_users()
    ]}
