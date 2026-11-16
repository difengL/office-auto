"""HEM 工时插件：账号密码登录（可自动重登），每周五提交周报。"""
from __future__ import annotations

from datetime import date

from app.core import db
from app.core.feature import (CredentialKind, Feature, FeatureLoginError,
                              FieldSpec, JobContext, JobResult, JobSpec,
                              OptionSpec)

from . import jobs
from .client import HemClient, HemError


class TimesheetFeature(Feature):
    key = "timesheet"
    name = "HEM 工时填报"
    description = "每周五检查本周工时是否填齐并提交周报；默认试运行，不实际提交。"
    credential_kind = CredentialKind.PASSWORD
    credential_fields = [
        FieldSpec("username", "HEM 账号", type="text"),
        FieldSpec("password", "HEM 密码", type="password", secret=True),
    ]
    default_jobs = [
        JobSpec("weekly_submit", "提交周报", "cron",
                {"hour": 17, "minute": 30, "day_of_week": "fri"}, jitter=300),
    ]
    extra_options = [
        OptionSpec("project_id", "归属项目", type="select",
                   source="timesheet.projects", refreshable=True),
    ]
    dry_run_default = True

    async def login(self, user_id: int, payload: dict) -> dict:
        username = (payload.get("username") or "").strip()
        password = payload.get("password") or ""
        if not username or not password:
            raise FeatureLoginError("请填写 HEM 账号和密码")
        with HemClient(username, password) as client:
            try:
                info = client.login()
                projects = client.list_projects()
            except HemError as exc:
                raise FeatureLoginError(f"HEM 登录失败：{exc}") from exc
        payload.update({"username": username, "password": password})
        db.fdata_set(user_id, self.key, "projects", projects)
        default = next((p for p in projects if "日常工作" in p["name"]),
                       projects[0] if projects else None)
        return {"display": info.get("name") or username,
                "projects": projects,
                "default_project": default}

    async def check_alive(self, user_id: int) -> bool:
        return await self.relogin(user_id)

    async def relogin(self, user_id: int) -> bool:
        cred = db.load_credential(user_id, self.key)
        p = cred.payload
        if not p.get("username") or not p.get("password"):
            return False
        with HemClient(p["username"], p["password"]) as client:
            try:
                client.login()
                return True
            except HemError:
                return False

    async def run_job(self, user_id: int, job_id: str, ctx: JobContext) -> JobResult:
        if job_id != "weekly_submit":
            return JobResult(False, f"未知任务：{job_id}")

        cred = db.load_credential(user_id, self.key)
        p = cred.payload
        if not p.get("username") or not p.get("password"):
            return JobResult(False, "凭据缺失，请重新登录")

        project_id = ctx.options.get("project_id") or None
        result = jobs.run_weekly_submit(
            p["username"], p["password"], project_id, ctx.dry_run, date.today())

        # 项目失效时回退默认并回写配置
        detail = result.detail or {}
        used = (detail.get("project") or {}).get("project_id")
        if used and str(used) != str(project_id):
            cfg = db.load_feature_config(user_id, self.key)
            cfg.options["project_id"] = used
            db.save_feature_config(user_id, self.key, cfg)
        return result

    async def refresh_options(self, user_id: int) -> list[dict]:
        cred = db.load_credential(user_id, self.key)
        p = cred.payload
        client = HemClient(p.get("username", ""), p.get("password", ""))
        try:
            projects = client.list_projects()
        except HemError as exc:
            raise FeatureLoginError(f"拉取项目失败：{exc.message}") from exc
        db.fdata_set(user_id, self.key, "projects", projects)
        return [{"value": x["project_id"], "label": x["name"]} for x in projects]