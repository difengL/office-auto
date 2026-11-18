"""HEM 工时插件：账号密码登录（可自动重登），每周五提交周报。"""
from __future__ import annotations

from datetime import date, timedelta

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
        # 周五下班前提交：太早当天的工时还没填，太晚跨了周末。20:00 留足
        # 白天补填的余量，又赶在当天完成。
        JobSpec("weekly_submit", "提交周报", "cron",
                {"hour": 20, "minute": 0, "day_of_week": "fri"}, jitter=300,
                fixed_fields=("day_of_week",)),
        # 密码型凭据虽然能自动重登，但若密码被改/过期，只有到周五才发现
        # 就白丢一周工时。每天验一次，出问题当天就能看到。
        JobSpec("keepalive", "登录保活", "interval", {"hours": 24},
                jitter=600, editable=False, always_on=True),
    ]
    # 提交周报只在周五做才有意义，页面上不给「立即执行」入口，避免误触发
    manual_run = False
    extra_options = [
        OptionSpec("project_id", "归属项目", type="select",
                   source="timesheet.projects", refreshable=True),
    ]
    dry_run_default = False

    def migrate_config(self, cfg) -> bool:
        """把旧的 17:30 默认时刻迁到 20:00，并清掉用户存的星期。

        老用户库里存着 {"weekly_submit": {"hour": 17, "minute": 30}}，
        它会覆盖插件新的默认值，导致「默认 20:00」对老用户不生效。
        只迁移恰好等于旧默认值的配置——用户如果自己改过别的时刻，
        那是他的选择，不能动。

        星期现在由插件固定为周五，库里若残留 day_of_week 一律移除，
        避免用户配置与固定值不一致时调度器按旧值跑。
        """
        if not cfg.schedule:
            return False
        sub = cfg.schedule.get("weekly_submit")
        if not isinstance(sub, dict):
            return False
        changed = False
        if "day_of_week" in sub:
            sub.pop("day_of_week")
            changed = True
        if sub.get("hour") == 17 and sub.get("minute") == 30:
            sub.update({"hour": 20, "minute": 0})
            changed = True
        return changed

    # ---------- 每日工时（跨周归零） ----------

    HOURS_KEY = "week_hours"

    def week_hours(self, user_id: int, today: date | None = None
                   ) -> tuple[dict[str, float], bool]:
        """本周每日工时。跨周自动恢复默认 8 小时。

        返回 (hours, 是否发生了归零)，后者用于决定要不要回写。
        """
        raw = db.fdata_get(user_id, self.key, self.HOURS_KEY, default=None)
        hours, dirty = jobs.normalize_week_hours(raw, today or date.today())
        if dirty:
            self.save_week_hours(user_id, hours, today)
        return hours, dirty

    def save_week_hours(self, user_id: int, hours: dict[str, float],
                        today: date | None = None) -> dict[str, float]:
        """保存逐日工时，并绑定到「本周」。传入的是裸的 {周几: 小时}。"""
        monday = jobs.monday_of(today or date.today())
        clean, _ = jobs.normalize_week_hours(hours, today or date.today(), incoming=True)
        db.fdata_set(user_id, self.key, self.HOURS_KEY,
                     {"week": monday.isoformat(), "days": clean})
        return clean

    def get_settings(self, user_id: int) -> dict:
        """逐日工时（周一~周五），每项可改，跨周自动回到默认 8 小时。"""
        hours, _ = self.week_hours(user_id)
        monday = jobs.monday_of(date.today())
        return {
            "kind": "hours_by_day",
            "title": "每天填多少工时",
            "note": "默认为 8 小时，可逐天修改；每周一 00:00 自动恢复为默认值。",
            "default": jobs.DEFAULT_HOURS,
            "unit": "小时",
            "week": monday.isoformat(),
            "days": [{"key": k, "label": label, "hours": hours.get(k, jobs.DEFAULT_HOURS),
                      "date": (monday + timedelta(days=i)).isoformat()}
                     for i, (k, label) in enumerate(jobs.WEEKDAYS)],
        }

    def save_settings(self, user_id: int, body: dict) -> dict:
        days = body.get("days") if isinstance(body, dict) else None
        if not isinstance(days, dict):
            raise FeatureLoginError("每日工时格式不正确")
        self.save_week_hours(user_id, days)
        return self.get_settings(user_id)

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
        default = projects[0] if projects else None
        return {"display": info.get("name") or username,
                "projects": [{"value": jobs.project_key(p), "label": p["name"]}
                             for p in projects],
                "default_project": ({"value": jobs.project_key(default),
                                     "label": default["name"]} if default else None)}

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
        cred = db.load_credential(user_id, self.key)
        p = cred.payload
        if not p.get("username") or not p.get("password"):
            return JobResult(False, "凭据缺失，请重新登录")

        # 保活只验证账号密码仍可用，不做任何写操作
        if job_id == "keepalive":
            return self._run_keepalive(p)

        if job_id != "weekly_submit":
            return JobResult(False, f"未知任务：{job_id}")

        project_id = ctx.options.get("project_id") or None
        # 逐日工时来自界面设置；跨周已自动恢复默认
        hours, _ = self.week_hours(user_id)
        # 演练模式已移除：周报提交一律真实执行
        result = jobs.run_weekly_submit(
            p["username"], p["password"], project_id, False, date.today(),
            week_hours=hours)

        # 项目失效时回退默认并回写配置
        detail = result.detail or {}
        used = (detail.get("project") or {}).get("project_id")
        if used and str(used) != str(project_id):
            cfg = db.load_feature_config(user_id, self.key)
            cfg.options["project_id"] = used
            db.save_feature_config(user_id, self.key, cfg)
        return result

    @staticmethod
    def _run_keepalive(p: dict) -> JobResult:
        """只登录一次，确认账号密码还有效。"""
        with HemClient(p["username"], p["password"]) as client:
            try:
                client.login()
            except HemError as exc:
                return JobResult(False, f"登录失败，账号密码可能已失效：{exc}")
        return JobResult(True, "凭据正常，账号密码可用")

    async def refresh_options(self, user_id: int) -> list[dict]:
        cred = db.load_credential(user_id, self.key)
        p = cred.payload
        if not p.get("username") or not p.get("password"):
            raise FeatureLoginError("尚未保存 HEM 账号密码，请先在上方登录")
        with HemClient(p["username"], p["password"]) as client:
            try:
                client.login()          # 必须先登录，否则拿到的是登录页 HTML
                projects = client.list_projects()
            except HemError as exc:
                raise FeatureLoginError(f"拉取项目失败：{exc.message}") from exc
        db.fdata_set(user_id, self.key, "projects", projects)
        return [{"value": jobs.project_key(x), "label": x["name"]} for x in projects]