"""办公自动化平台 · 多功能多用户服务

启动：  ./.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8765
访问：  http://<本机内网IP>:8765

注意：所有路由用 def（不是 async def），因为 runner 内部用 asyncio.run。
"""
from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path

from fastapi import Cookie, Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from .core import db, registry, runner, scheduler, security, web_auth
from .core.feature import FeatureLoginError

BASE_DIR = Path(__file__).resolve().parent
app = FastAPI(title="办公自动化平台")
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
# 用 Jinja2 渲染，模板里才能用 {% include '_style.html' %}
templates = Jinja2Templates(directory=BASE_DIR / "templates")


# ---------------- 启动装配 ----------------

def _register_features() -> None:
    from .features.meican.feature import MeicanFeature
    from .features.timesheet.feature import TimesheetFeature
    for f in (MeicanFeature(), TimesheetFeature()):
        registry.register(f)


@app.on_event("startup")
def startup() -> None:
    db.init_db()
    if not registry.all():
        _register_features()
    scheduler.start()
    scheduler.reschedule_all()


# ---------------- 页面 ----------------

@app.get("/", response_class=HTMLResponse)
def index(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "index.html")


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "login.html")


@app.get("/feature/{key}", response_class=HTMLResponse)
def feature_page(request: Request, key: str) -> HTMLResponse:
    return templates.TemplateResponse(request, "feature.html", {"feature_key": key})


# ---------------- 账号 ----------------

class AuthBody(BaseModel):
    username: str
    password: str


@app.post("/api/register")
def register(body: AuthBody, response: Response):
    if os.environ.get("ALLOW_REGISTER", "1") == "0":
        raise HTTPException(403, "注册已关闭")
    username = body.username.strip()
    if len(username) < 2:
        raise HTTPException(400, "用户名至少 2 个字符")
    if len(body.password) < 6:
        raise HTTPException(400, "密码至少 6 位")
    if db.get_user_by_username(username):
        raise HTTPException(400, "用户名已存在")
    user = db.create_user(username, security.hash_password(body.password), username)
    token, _ = db.issue_session(user.id)
    response.set_cookie(web_auth.COOKIE_NAME, token, max_age=web_auth.COOKIE_MAX_AGE,
                        httponly=True, samesite="lax", path="/")
    return {"ok": True, "username": user.username}


@app.post("/api/login")
def login(body: AuthBody, response: Response, request: Request):
    user = db.get_user_by_username(body.username.strip())
    if not user or not security.verify_password(body.password, user.password_hash):
        time.sleep(1)                       # 防暴力猜解
        raise HTTPException(401, "用户名或密码错误")
    token, _ = db.issue_session(user.id, ua=request.headers.get("user-agent", ""))
    response.set_cookie(web_auth.COOKIE_NAME, token, max_age=web_auth.COOKIE_MAX_AGE,
                        httponly=True, samesite="lax", path="/")
    scheduler.reschedule_user(user.id)
    return {"ok": True, "username": user.username}


@app.post("/api/logout")
def logout(response: Response, mc_session: str | None = Cookie(default=None),
           user: db.User = Depends(web_auth.current_user)):
    if mc_session:
        db.revoke_session(mc_session)
    response.delete_cookie(web_auth.COOKIE_NAME, path="/")
    return {"ok": True}


@app.get("/api/me")
def me(user: db.User = Depends(web_auth.current_user)):
    return {"id": user.id, "username": user.username, "display_name": user.display_name}


# ---------------- 功能 ----------------

def _feature_or_404(key: str):
    f = registry.get(key)
    if not f:
        raise HTTPException(404, f"未知功能：{key}")
    return f


@app.get("/api/features")
def features(user: db.User = Depends(web_auth.current_user)):
    out = []
    for f in registry.all():
        cred = db.load_credential(user.id, f.key)
        cfg = db.load_feature_config(user.id, f.key)
        saved = db.fdata_get(user.id, f.key, "__config_saved__", default=False)
        out.append({
            "key": f.key, "name": f.name, "description": f.description,
            "credential_kind": f.credential_kind.value,
            "state": cred.state, "last_error": cred.last_error,
            "enabled": cfg.enabled,
            "dry_run": cfg.dry_run if saved else f.dry_run_default,
            "display": cred.payload.get("userName") or cred.payload.get("username") or "",
            "jobs": [{"job_id": j.job_id, "name": j.name,
                      "next_run": scheduler.next_run(user.id, f.key, j.job_id)}
                     for j in f.default_jobs],
        })
    return {"features": out}


@app.get("/api/features/{key}")
def feature_detail(key: str, user: db.User = Depends(web_auth.current_user)):
    f = _feature_or_404(key)
    cred = db.load_credential(user.id, key)
    cfg = db.load_feature_config(user.id, key)
    saved = db.fdata_get(user.id, key, "__config_saved__", default=False)
    options_values = {}
    for spec in f.extra_options:
        if spec.source:
            options_values[spec.name] = db.fdata_get(user.id, key, spec.source.split(".")[-1], [])
    return {
        "key": f.key, "name": f.name, "description": f.description,
        "credential_kind": f.credential_kind.value,
        "credential_fields": [vars(x) for x in f.credential_fields],
        "state": cred.state, "last_error": cred.last_error,
        "last_ok_at": cred.last_ok_at,
        "display": cred.payload.get("userName") or cred.payload.get("username") or "",
        "config": {"enabled": cfg.enabled,
                   "dry_run": cfg.dry_run if saved else f.dry_run_default,
                   "schedule": cfg.schedule, "options": cfg.options},
        "jobs": [{**vars(j), "next_run": scheduler.next_run(user.id, key, j.job_id),
                  "failures": db.consecutive_failures(user.id, key, j.job_id)}
                 for j in f.default_jobs],
        "extra_options": [vars(x) for x in f.extra_options],
        "option_values": options_values,
        "views": [vars(x) for x in f.views],
    }


@app.post("/api/features/{key}/login")
def feature_login(key: str, body: dict,
                  user: db.User = Depends(web_auth.current_user)):
    _feature_or_404(key)
    try:
        info = runner.run_login(user.id, key, body)
    except FeatureLoginError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"{type(exc).__name__}: {exc}") from exc
    # 凭据刚变为有效：常开任务（保活）要从「无凭据不调度」变成开跑
    scheduler.reschedule_user(user.id)
    return {"ok": True, **info}


class ConfigBody(BaseModel):
    enabled: bool = False
    dry_run: bool = True
    schedule: dict = {}
    options: dict = {}


@app.post("/api/features/{key}/config")
def feature_config(key: str, body: ConfigBody,
                   user: db.User = Depends(web_auth.current_user)):
    f = _feature_or_404(key)
    cfg = db.FeatureConfig(key, body.enabled, body.dry_run, body.schedule, body.options)
    # editable=False 的任务不接受用户改时间
    editable_ids = {j.job_id for j in f.default_jobs if j.editable}
    cfg.schedule = {k: v for k, v in cfg.schedule.items() if k in editable_ids}
    db.save_feature_config(user.id, key, cfg)
    db.fdata_set(user.id, key, "__config_saved__", True)   # 标记已初始化，之后不再套用插件默认
    scheduler.reschedule_user(user.id)
    return {"ok": True, "config": {"enabled": cfg.enabled, "dry_run": cfg.dry_run,
                                   "schedule": cfg.schedule, "options": cfg.options}}


@app.post("/api/features/{key}/action")
def feature_action(key: str, action: str, body: dict,
                   user: db.User = Depends(web_auth.current_user)):
    """登录前辅助动作，如发送短信验证码。body 为当前表单内容。"""
    f = _feature_or_404(key)
    try:
        message = asyncio.run(f.send_action(user.id, action, body or {}))
    except FeatureLoginError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"{type(exc).__name__}: {exc}") from exc
    return {"ok": True, "message": message}


@app.post("/api/features/{key}/options/refresh")
def feature_refresh_options(key: str, user: db.User = Depends(web_auth.current_user)):
    f = _feature_or_404(key)
    try:
        items = asyncio.run(f.refresh_options(user.id))
    except FeatureLoginError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"{type(exc).__name__}: {exc}") from exc
    return {"ok": True, "options": items}


class RunBody(BaseModel):
    job_id: str


@app.post("/api/features/{key}/run")
def feature_run(key: str, body: RunBody,
                user: db.User = Depends(web_auth.current_user)):
    _feature_or_404(key)
    result = runner.execute(user.id, key, body.job_id)
    return {"ok": result.ok, "message": result.message, "detail": result.detail}


@app.get("/api/features/{key}/runs")
def feature_runs(key: str, limit: int = 20,
                 user: db.User = Depends(web_auth.current_user)):
    _feature_or_404(key)
    return {"runs": db.read_job_runs(user.id, key, limit)}


@app.get("/api/features/{key}/view/{view_id}")
def feature_view(key: str, view_id: str, date: str = "",
                 user: db.User = Depends(web_auth.current_user)):
    """只读查看视图（推荐 / 可订菜品 / 订餐状态）。不产生副作用。"""
    f = _feature_or_404(key)
    if view_id not in {v.view_id for v in f.views}:
        raise HTTPException(404, f"未知视图：{view_id}")
    try:
        data = asyncio.run(f.get_view(user.id, view_id, {"date": date}))
    except FeatureLoginError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"{type(exc).__name__}: {exc}") from exc
    return {"ok": True, "view_id": view_id, "data": data}