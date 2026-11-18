"""按 用户 × 功能 × 任务 三维调度。

任务 ID 形如 "meican:order:3"（功能:任务:用户）。
用户改配置 → reschedule_user(user_id) 只重建该用户的任务。
"""
from __future__ import annotations

import random

from apscheduler.schedulers.base import STATE_STOPPED
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from . import db, registry, runner

_WEEKDAY_CN = {"mon": "周一", "tue": "周二", "wed": "周三", "thu": "周四",
               "fri": "周五", "sat": "周六", "sun": "周日"}


def day_label(day_of_week: str | None) -> str:
    """把 cron 的 day_of_week 译成中文，供界面显示「每周五」这类文案。

    只处理常见写法：单日、逗号列表、mon-fri、区间。认不出的原样返回，
    宁可显示得生硬，也不要猜错用户配置。
    """
    if not day_of_week:
        return ""
    raw = str(day_of_week).strip().lower()
    if raw in ("*", "?", "every"):
        return "每天"
    if raw == "mon-fri":
        return "工作日"
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    if parts and all(p in _WEEKDAY_CN for p in parts):
        if len(parts) == 1:
            return "每" + _WEEKDAY_CN[parts[0]]
        return "每" + "、".join(_WEEKDAY_CN[p] for p in parts)
    if len(parts) == 1 and "-" in parts[0]:
        a, b = (x.strip() for x in parts[0].split("-", 1))
        if a in _WEEKDAY_CN and b in _WEEKDAY_CN:
            return f"每{_WEEKDAY_CN[a]}至{_WEEKDAY_CN[b]}"
    return f"每 {day_of_week}"

scheduler = AsyncIOScheduler(timezone="Asia/Shanghai",
                             job_defaults={"coalesce": True, "max_instances": 1,
                                           "misfire_grace_time": 900})


def job_id(feature_key: str, jid: str, user_id: int) -> str:
    return f"{feature_key}:{jid}:{user_id}"


def _cron_trigger(spec, override: dict) -> dict:
    # fixed_fields 里的键（如 day_of_week）不允许用户覆盖，永远取默认值
    merged = {k: v for k, v in (override or {}).items()
              if k not in spec.fixed_fields}
    merged = {**spec.default, **merged}
    kw = {k: v for k, v in merged.items()
          if k in ("hour", "minute", "second", "day_of_week", "day", "month")}
    if spec.jitter:
        kw["jitter"] = spec.jitter          # JobSpec.jitter 单位是秒
    return kw


def _interval_trigger(spec, override: dict) -> dict:
    merged = {**spec.default, **override}
    kw = {k: v for k, v in merged.items() if k in ("minutes", "hours", "seconds", "weeks")}
    if spec.jitter:
        kw["jitter"] = spec.jitter          # JobSpec.jitter 单位是秒
    return kw


def _keepalive_eligible(user_id: int, feature_key: str) -> bool:
    """常开任务的前提：凭据存在且有效。

    失效态不调度——交互型凭据无法自动重登，空转只会刷一堆失败记录；
    等用户重新登录后，登录接口会再次触发 reschedule。
    """
    cred = db.load_credential(user_id, feature_key)
    return cred.state == "active"


def reschedule_user(user_id: int) -> None:
    """重建某用户的全部任务（先删后加）。"""
    prefix_suffix = f":{user_id}"
    for job in scheduler.get_jobs():
        if job.id.endswith(prefix_suffix):
            job.remove()

    for feature in registry.all():
        cfg = runner.load_config(user_id, feature)
        for spec in feature.default_jobs:
            # always_on 的任务不受总开关约束，其余必须 cfg.enabled
            if not (cfg.enabled or spec.always_on):
                continue
            if spec.always_on and not _keepalive_eligible(user_id, feature.key):
                continue
            jid = job_id(feature.key, spec.job_id, user_id)
            override = cfg.schedule.get(spec.job_id, {}) if spec.editable else {}
            if spec.trigger == "interval":
                kw = _interval_trigger(spec, override)
                ttype = "interval"
            else:
                kw = _cron_trigger(spec, override)
                ttype = "cron"
            scheduler.add_job(
                _run_job, ttype, args=[user_id, feature.key, spec.job_id],
                id=jid, replace_existing=True, **kw,
            )


def reschedule_all() -> None:
    """重建所有用户的任务。

    不能只看 enabled_feature_pairs：常开任务（保活）在开关关闭、
    用户从未保存过配置时也要跑，所以所有有凭据的用户都要覆盖到。
    """
    pairs = set(db.enabled_feature_pairs()) | set(db.credential_pairs())
    for user_id in {u for u, _k in pairs}:
        reschedule_user(user_id)


def next_run(user_id: int, feature_key: str, jid: str) -> str | None:
    job = scheduler.get_job(job_id(feature_key, jid, user_id))
    if not job:
        return None
    # scheduler 未启动时 next_run_time 属性不存在
    nxt = getattr(job, "next_run_time", None)
    return str(nxt) if nxt else None


async def _run_job(user_id: int, feature_key: str, jid: str) -> None:
    import asyncio
    try:
        await asyncio.to_thread(runner.execute, user_id, feature_key, jid)
    except Exception as exc:  # noqa: BLE001
        db.record_job_run(user_id, feature_key, jid, False,
                          f"调度异常：{type(exc).__name__}: {exc}", {})


def start() -> None:
    """启动调度器。若上次绑定的事件循环已关闭（进程内重启 / 测试多次 lifespan），
    先复位状态再重新绑定当前循环，否则 add_job 会往死循环上投递唤醒。"""
    loop = getattr(scheduler, "_eventloop", None)
    if scheduler.running and loop is not None and loop.is_closed():
        scheduler._stop_timer()
        scheduler._eventloop = None
        scheduler.state = STATE_STOPPED
    if not scheduler.running:
        scheduler.start()


def stop() -> None:
    """停机时释放调度器。

    AsyncIOScheduler.shutdown 内部是 call_soon_threadsafe 异步完成的，
    状态切换发生在那次回调里。这里绝不能抢先改 state——否则回调稍后
    执行时看到已是 STOPPED，会抛 SchedulerNotRunningError。
    若事件循环先关闭导致回调没机会跑，下次 start() 会检测到死循环并复位。
    """
    if not scheduler.running:
        return
    try:
        scheduler.shutdown(wait=False)
    except Exception:  # noqa: BLE001 - 停机路径不应再抛
        pass