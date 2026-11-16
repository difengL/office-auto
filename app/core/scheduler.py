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

scheduler = AsyncIOScheduler(timezone="Asia/Shanghai",
                             job_defaults={"coalesce": True, "max_instances": 1,
                                           "misfire_grace_time": 900})


def job_id(feature_key: str, jid: str, user_id: int) -> str:
    return f"{feature_key}:{jid}:{user_id}"


def _cron_trigger(spec, override: dict) -> dict:
    merged = {**spec.default, **override}
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


def reschedule_user(user_id: int) -> None:
    """重建某用户的全部任务（先删后加）。"""
    prefix_suffix = f":{user_id}"
    for job in scheduler.get_jobs():
        if job.id.endswith(prefix_suffix):
            job.remove()

    for feature in registry.all():
        cfg = db.load_feature_config(user_id, feature.key)
        if not cfg.enabled:
            continue
        for spec in feature.default_jobs:
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
    for user_id, _feature_key in db.enabled_feature_pairs():
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