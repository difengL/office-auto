"""HEM 工时填报业务逻辑。

默认 dry-run：只演算要填哪些天，不调用提交接口。
"""
from __future__ import annotations

from datetime import date

from app.core.feature import JobResult

from .client import HemClient, HemError, filled_dates, monday_of, workdays_of_week

DEFAULT_HOURS = 8.0


def plan_week(records: list[dict], days: list[date], default_hours: float) -> dict:
    """算出哪些天需要补填。已有工时的天一律跳过，不覆盖手填数据。"""
    filled = filled_dates(records)
    already = [d.isoformat() for d in days if d.isoformat() in filled]
    to_fill = [d.isoformat() for d in days if d.isoformat() not in filled]
    return {
        "already": already,
        "to_fill": to_fill,
        "hours_per_day": default_hours,
        "total_hours": round(len(to_fill) * default_hours, 2),
        "filled_detail": filled,
    }


def resolve_project(client: HemClient, wanted: str | None) -> dict:
    """项目选择：指定 → 含「日常工作」→ 第一个。"""
    projects = client.list_projects()
    if not projects:
        raise HemError("账号下没有可用项目")
    if wanted:
        for p in projects:
            if p["project_id"] == str(wanted):
                return p
    for p in projects:
        if "日常工作" in p["name"]:
            return p
    return projects[0]


def run_weekly_submit(username: str, password: str, project_id: str | None,
                      dry_run: bool, today: date,
                      default_hours: float = DEFAULT_HOURS) -> JobResult:
    """检查本周工时并提交周报。dry_run 时只返回计划。"""
    days = workdays_of_week(today)
    week_start = monday_of(today)
    with HemClient(username, password) as client:
        try:
            client.login()
            project = resolve_project(client, project_id)
            records = client.week_records(days[0], days[-1])
        except HemError as exc:
            return JobResult(False, f"工时系统访问失败：{exc}")

        plan = plan_week(records, days, default_hours)
        detail = {"week": week_start.isoformat(), "project": project, "plan": plan,
                  "dry_run": dry_run}

        if plan["to_fill"]:
            detail["warning"] = f"有 {len(plan['to_fill'])} 天未填工时：{', '.join(plan['to_fill'])}"
            if dry_run:
                return JobResult(True, f"[试运行] 本周待补填 {len(plan['to_fill'])} 天，"
                                       f"每天 {default_hours}h，项目「{project['name']}」", detail)
            # 真实填报接口尚未接入：先只提交周报，缺填天数如实告警
            try:
                res = client.submit(week_start)
            except HemError as exc:
                return JobResult(False, f"提交周报失败：{exc}", detail)
            detail["submit_response"] = res
            return JobResult(False, f"本周有 {len(plan['to_fill'])} 天未填工时，"
                                    f"已尝试提交周报但需人工补填", detail)

        if dry_run:
            return JobResult(True, f"[试运行] 本周工时已齐（5 天 × {default_hours}h），"
                                   f"将提交周报，项目「{project['name']}」", detail)
        try:
            res = client.submit(week_start)
        except HemError as exc:
            return JobResult(False, f"提交周报失败：{exc}", detail)
        detail["submit_response"] = res
        return JobResult(True, "本周工时已齐，周报提交成功", detail)