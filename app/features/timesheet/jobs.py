"""HEM 工时填报业务逻辑。

默认 dry-run：只演算要填哪些天，不调用提交接口。
"""
from __future__ import annotations

from datetime import date

from app.core.feature import JobResult

from .client import HemClient, HemError, filled_dates, monday_of, workdays_of_week

DEFAULT_HOURS = 8.0

# 周一至周五，顺序即周内顺序。键用于存储，值用于界面。
WEEKDAYS = (("mon", "周一"), ("tue", "周二"), ("wed", "周三"),
            ("thu", "周四"), ("fri", "周五"))
WEEKDAY_KEYS = tuple(k for k, _ in WEEKDAYS)


def _coerce_hours(v) -> float | None:
    """把用户输入转成小时数。非法或缺省返回 None，由调用方回退默认值。"""
    try:
        h = float(v)
    except (TypeError, ValueError):
        return None
    if not (0 <= h <= 24):
        return None
    return round(h, 2)


def default_week_hours() -> dict[str, float]:
    return {k: DEFAULT_HOURS for k in WEEKDAY_KEYS}


def normalize_week_hours(raw, day: date, *, incoming: bool = False
                         ) -> tuple[dict[str, float], bool]:
    """把存下来的每日工时规整成「本周有效值」。

    存储形态 {"week": "2026-11-16", "days": {"mon": 8, ...}}。
    跨周即整体恢复默认——用户上周调过的工时不会延续到新一周。
    用读取时比对周次的方式归零（而不是等周一 0 点的定时任务）：
    服务在周一凌晨停机也不会漏掉，也不依赖「启用定时任务」开关。

    day 传「今天」即可，内部归一到本周一。
    incoming=True 表示 raw 来自用户刚保存的表单（裸的 {day: hours}），
    此时一律绑定到本周，不做跨周判断。
    """
    if incoming:
        days_raw = raw if isinstance(raw, dict) else {}
        out = default_week_hours()
        for k in WEEKDAY_KEYS:
            h = _coerce_hours(days_raw.get(k))
            if h is not None:
                out[k] = h
        return out, True

    week_start = monday_of(day)
    if not isinstance(raw, dict) or raw.get("week") != week_start.isoformat():
        return default_week_hours(), True        # 没有记录或跨周 → 归零

    stored = raw.get("days")
    if not isinstance(stored, dict):
        stored = {}
    out = default_week_hours()
    for k in WEEKDAY_KEYS:
        h = _coerce_hours(stored.get(k))
        if h is not None:
            out[k] = h
    # 同周不回写：缺失的天由默认值派生，不值得每次读都落一次库
    return out, False


def week_hours_for_days(hours: dict[str, float], days: list[date]) -> dict[str, float]:
    """把「周几 → 小时」映射到本周的具体日期上。"""
    out = {}
    for i, d in enumerate(days):
        if i < len(WEEKDAY_KEYS):
            out[d.isoformat()] = hours.get(WEEKDAY_KEYS[i], DEFAULT_HOURS)
    return out


def plan_week(records: list[dict], days: list[date],
              hours_by_day) -> dict:
    """算出哪些天需要补填。已有工时的天一律跳过，不覆盖手填数据。

    hours_by_day 可以是 {日期: 小时}，也可以是一个统一的标量小时数。
    """
    if not isinstance(hours_by_day, dict):
        uniform = _coerce_hours(hours_by_day)
        uniform = DEFAULT_HOURS if uniform is None else uniform
        hours_by_day = {d.isoformat(): uniform for d in days}
    filled = filled_dates(records)
    already = [d.isoformat() for d in days if d.isoformat() in filled]
    to_fill = [d.isoformat() for d in days if d.isoformat() not in filled]
    return {
        "already": already,
        "to_fill": to_fill,
        "hours_per_day": hours_by_day,
        "total_hours": round(sum(hours_by_day.get(d, DEFAULT_HOURS)
                                 for d in to_fill), 2),
        "filled_detail": filled,
    }


def project_key(p: dict) -> str:
    """项目的稳定标识。工时按 subProjectId 归属，同一 projectId 可能有多个子项目。"""
    return f"{p['project_id']}:{p.get('sub_project_id', '')}"


def resolve_project(client: HemClient, wanted: str | None) -> dict:
    """项目选择：用户已选且仍在列表里 → 用它；否则回退第一个（规格确认的默认规则）。"""
    projects = client.list_projects()
    if not projects:
        raise HemError("账号下没有可用项目")
    if wanted:
        for p in projects:
            if project_key(p) == str(wanted) or p["project_id"] == str(wanted):
                return p
    return projects[0]


def run_weekly_submit(username: str, password: str, project_id: str | None,
                      dry_run: bool, today: date,
                      week_hours: dict[str, float] | None = None) -> JobResult:
    """检查本周工时并提交周报。dry_run 时只返回计划。

    week_hours 是「周几 → 小时」，来自用户在界面上的逐日设置；
    缺省时按默认 8 小时。
    """
    days = workdays_of_week(today)
    week_start = monday_of(today)
    hours = {k: DEFAULT_HOURS for k in WEEKDAY_KEYS}
    hours.update({k: v for k, v in (week_hours or {}).items() if k in WEEKDAY_KEYS})
    hours_by_day = week_hours_for_days(hours, days)

    with HemClient(username, password) as client:
        try:
            client.login()
            project = resolve_project(client, project_id)
            records = client.week_records(days[0], days[-1])
        except HemError as exc:
            return JobResult(False, f"工时系统访问失败：{exc}")

        plan = plan_week(records, days, hours_by_day)
        detail = {"week": week_start.isoformat(), "project": project, "plan": plan,
                  "dry_run": dry_run, "week_hours": hours}

        if plan["to_fill"]:
            detail["warning"] = f"有 {len(plan['to_fill'])} 天未填工时：{', '.join(plan['to_fill'])}"
            if dry_run:
                return JobResult(True,
                                 f"[试运行] 本周待补填 {len(plan['to_fill'])} 天，"
                                 f"合计 {plan['total_hours']}h，项目「{project['name']}」",
                                 detail)
            # 真实填报接口尚未接入：先只提交周报，缺填天数如实告警
            try:
                res = client.submit(week_start)
            except HemError as exc:
                return JobResult(False, f"提交周报失败：{exc}", detail)
            detail["submit_response"] = res
            return JobResult(False, f"本周有 {len(plan['to_fill'])} 天未填工时，"
                                    f"已尝试提交周报但需人工补填", detail)

        total = round(sum(hours_by_day.values()), 2)
        if dry_run:
            return JobResult(True, f"[试运行] 本周工时已齐（合计 {total}h），"
                                   f"将提交周报，项目「{project['name']}」", detail)
        try:
            res = client.submit(week_start)
        except HemError as exc:
            return JobResult(False, f"提交周报失败：{exc}", detail)
        detail["submit_response"] = res
        return JobResult(True, "本周工时已齐，周报提交成功", detail)