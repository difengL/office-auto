"""统一执行器：校验态 → 按凭据类型决定是否自动重登 → 执行 → 记录。

所有功能共用这一套策略，插件只负责业务本身。
"""
from __future__ import annotations

import asyncio

from . import db, registry
from .feature import CredentialKind, FeatureLoginError, FeatureState, JobContext, JobResult


def run_login(user_id: int, feature_key: str, payload: dict) -> dict:
    """校验并保存凭据。失败抛 FeatureLoginError。"""
    feature = registry.get(feature_key)
    if feature is None:
        raise FeatureLoginError(f"未知功能：{feature_key}")
    info = asyncio.run(feature.login(user_id, payload))
    db.save_credential(user_id, feature_key, payload, FeatureState.ACTIVE.value)
    return info


def execute(user_id: int, feature_key: str, job_id: str) -> JobResult:
    """执行一次任务。同步接口，内部用 asyncio.run 驱动插件协程。

    只能从同步上下文调用：FastAPI 路由用 def，APScheduler 任务经 asyncio.to_thread。
    """
    feature = registry.get(feature_key)
    if feature is None:
        return JobResult(False, f"未知功能：{feature_key}")

    cred = db.load_credential(user_id, feature_key)
    if cred.state == FeatureState.NOT_CONFIGURED.value:
        return JobResult(False, "尚未配置凭据，请先在功能页面登录")
    if cred.state == FeatureState.EXPIRED.value:
        return JobResult(False, "凭据已失效，请在功能页面重新登录")

    async def _run() -> JobResult:
        if not await feature.check_alive(user_id):
            if feature.credential_kind == CredentialKind.PASSWORD:
                if not await feature.relogin(user_id):
                    db.set_credential_state(user_id, feature_key,
                                            FeatureState.EXPIRED.value, "自动重登失败")
                    return JobResult(
                        False, "登录态失效且自动重登失败，账号或密码可能已变更，请重新登录")
            else:
                db.set_credential_state(user_id, feature_key,
                                        FeatureState.EXPIRED.value, "登录态失效")
                return JobResult(False, "登录态失效，请重新登录该功能")

        cfg = db.load_feature_config(user_id, feature_key)
        # 从未保存过配置时，dry_run 用插件声明的默认值
        if not db.fdata_get(user_id, feature_key, "__config_saved__", default=False):
            cfg.dry_run = feature.dry_run_default
        ctx = JobContext(user_id, feature_key, job_id, cfg.dry_run, cfg.options)
        try:
            return await feature.run_job(user_id, job_id, ctx)
        except Exception as exc:  # noqa: BLE001
            return JobResult(False, f"{type(exc).__name__}: {exc}")

    result = asyncio.run(_run())
    db.record_job_run(user_id, feature_key, job_id, result.ok,
                      result.message, result.detail)
    return result