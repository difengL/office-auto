"""美餐插件：手机号+短信验证码登录（交互型），订餐 + 保活两个任务。"""
from __future__ import annotations

import time
from datetime import date, timedelta

from app.core import db
from app.core.feature import (CredentialKind, Feature, FeatureLoginError,
                              FeatureState, FieldSpec, JobContext, JobResult,
                              JobSpec, ViewSpec)

from . import jobs
from .client import MeicanClient, MeicanError


class MeicanFeature(Feature):
    key = "meican"
    name = "美餐自动订餐"
    description = "学习历史口味，每天自动挑菜下单；短信登录，登录态自动保活。"
    credential_kind = CredentialKind.INTERACTIVE
    # 登录时会尝试拿 remember cookie（约 1 年），有它就能自动续期
    can_relogin = True
    credential_fields = [
        FieldSpec("phone", "手机号", type="text"),
        FieldSpec("code", "短信验证码", type="text",
                  action="send_sms", action_label="发送验证码"),
    ]
    default_jobs = [
        JobSpec("order", "自动订餐", "cron",
                {"hour": 10, "minute": 5, "day_of_week": "mon-fri"}, jitter=180),
        # accessToken 的 TTL 是「最后一次使用后 1 小时」，保活间隔必须明显
        # 小于它，否则中间会出现 token 已死的窗口，恰好撞上订餐就会失败。
        # 30 分钟一次，留足重试余量。
        JobSpec("keepalive", "登录保活", "interval", {"minutes": 30}, jitter=300,
                editable=False, always_on=True),
    ]
    views = [
        ViewSpec("status", "今日订餐状态", kind="status"),
        ViewSpec("recommend", "按口味推荐", kind="recommend"),
        ViewSpec("candidates", "当天可订菜品", kind="candidates"),
        ViewSpec("fruit", "本周水果", kind="fruit"),
    ]

    async def login(self, user_id: int, payload: dict) -> dict:
        phone = (payload.get("phone") or "").strip()
        code = (payload.get("code") or "").strip()
        if not phone or not code:
            raise FeatureLoginError("请填写手机号和短信验证码")

        # 首选 v3 OAuth 通道：它是唯一能下发长期 remember cookie 的路径，
        # 拿到后一年内无需再收短信。发码也必须走 v3（send_sms_code_v3），
        # 两个通道的验证码互不通用。
        remember = ""
        access_token = ""
        refresh_token = ""
        try:
            got = MeicanClient().oauth_token_with_code(phone, code)
            access_token = got["token"]["access_token"]
            refresh_token = got["token"].get("refresh_token", "")
            remember = got.get("remember") or ""
        except MeicanError:
            access_token = ""

        # v3 失败则回退 GraphQL 通道（拿不到 remember，但能登录）
        if not access_token:
            client = MeicanClient()
            try:
                ticket_res = client.login_with_sms(phone, code)
            except MeicanError as exc:
                raise FeatureLoginError(f"登录失败：{exc}") from exc
            accounts = ticket_res.get("userList") or []
            target = next((a for a in accounts
                           if a.get("accountVersion") == "AccountThreeDotZero"), None)
            if target is None and accounts:
                target = accounts[0]
            if target is None:
                raise FeatureLoginError("该手机号下没有可用账号")
            try:
                token = client.choose_account(
                    ticket_res["ticket"], ticket_res["signature"],
                    snowflake_id=target.get("snowflakeId"))
            except MeicanError as exc:
                raise FeatureLoginError(f"选择账号失败：{exc}") from exc
            access_token = token["accessToken"]
            refresh_token = token.get("refreshToken", "")

        acct = MeicanClient(access_token).account()
        payload.update({
            "phone": phone,
            "accessToken": access_token,
            "refreshToken": refresh_token,
            "tokenType": "bearer",
            "obtainedAt": int(time.time()),
            "snowflakeId": acct.get("snowflakeId", ""),
            "userName": acct.get("name", ""),
            "remember": remember,      # 长期凭据，实测约 2 个月有效
        })
        payload.pop("code", None)          # 验证码是一次性的，不落库

        # 首次登录自动初始化历史口味：从美餐 API 拉近 90 天订单建偏好模型。
        # 新用户、上生产部署都走这里，不依赖旧库迁移。失败不阻断登录。
        history_ready = False
        try:
            cache = jobs.load_cache(user_id)
            if not (cache.get("history") or {}).get("records"):
                jobs.fetch_history(payload, cache)
                jobs.save_cache(user_id, cache)
            history_ready = bool((cache.get("history") or {}).get("records"))
        except Exception:  # noqa: BLE001
            history_ready = False

        msg = acct.get("name") or phone
        return {"display": msg, "long_term": bool(remember),
                "history_ready": history_ready}

    async def send_action(self, user_id: int, action: str, payload: dict) -> str:
        if action != "send_sms":
            raise FeatureLoginError(f"不支持的操作：{action}")
        phone = (payload.get("phone") or "").strip()
        if not phone:
            raise FeatureLoginError("请先填写手机号")
        client = MeicanClient()
        try:
            # 必须走 v3 通道：login 首选 v3 OAuth，两个通道的验证码互不通用
            client.send_sms_code_v3(phone)
        except MeicanError as exc:
            raise FeatureLoginError(f"发送验证码失败：{exc}") from exc
        # 美餐发码是静默限流的：返回成功不代表短信一定到了
        return "验证码已发送，请注意查收（4 位数字）。若长时间未收到，可能是当日发送次数过多被限流。"

    async def check_alive(self, user_id: int) -> bool:
        cred = db.load_credential(user_id, self.key)
        return jobs.keepalive(cred.payload)

    async def relogin(self, user_id: int) -> bool:
        """用长期凭据（remember cookie）换新 token，无需短信验证码。

        这是「十几天不打开也保持登录」的真正机制。
        """
        payload = db.load_credential(user_id, self.key).payload
        remember = payload.get("remember") or ""
        if not remember:
            return False
        try:
            data = MeicanClient().oauth_token_with_remember(remember)
        except MeicanError:
            return False
        payload["accessToken"] = data["access_token"]
        if data.get("refresh_token"):
            payload["refreshToken"] = data["refresh_token"]
        payload["obtainedAt"] = int(time.time())
        db.save_credential(user_id, self.key, payload, FeatureState.ACTIVE.value)
        return True

    async def run_job(self, user_id: int, job_id: str, ctx: JobContext) -> JobResult:
        cred = db.load_credential(user_id, self.key)
        payload = cred.payload

        if job_id == "keepalive":
            if jobs.keepalive(payload):
                return JobResult(True, "保活成功，登录态正常")
            # token 已死：有长期凭据就当场续期，不必等用户收短信
            if await self.relogin(user_id):
                return JobResult(True, "token 已过期，已用长期凭据自动续期")
            if payload.get("remember"):
                return JobResult(False, "长期凭据已失效，请重新短信登录")
            return JobResult(False, "登录态失效，请重新短信登录")

        if job_id == "order":
            cache = jobs.load_cache(user_id)
            days = int(ctx.options.get("target_offset_days", 1))
            target = date.today() + timedelta(days=days)
            # 演练模式已移除：定时订餐一律真实下单
            result = jobs.do_order(payload, cache, target, None, dry_run=False)
            jobs.save_cache(user_id, cache)
            return result

        return JobResult(False, f"未知任务：{job_id}")

    async def do_action(self, user_id: int, action: str, params: dict) -> JobResult:
        if action != "order_dish":
            raise FeatureLoginError(f"不支持的操作：{action}")
        cred = db.load_credential(user_id, self.key)
        payload = cred.payload
        if not payload.get("accessToken"):
            raise FeatureLoginError("尚未登录，请先在上方登录")

        dish = (params or {}).get("dish") or {}
        if not dish.get("dish_id"):
            raise FeatureLoginError("缺少菜品信息")
        day_str = (params or {}).get("date") or date.today().isoformat()
        try:
            target = date.fromisoformat(day_str)
        except ValueError:
            raise FeatureLoginError(f"日期格式不正确：{day_str}")

        # 演练模式已移除：用户主动订餐一律真实下单
        cache = jobs.load_cache(user_id)
        result = jobs.do_order(payload, cache, target, dish, dry_run=False)
        # token 失效导致下单失败：有长期凭据就续期重试一次
        if not result.ok and "token 已失效" in result.message:
            if await self.relogin(user_id):
                fresh = db.load_credential(user_id, self.key).payload
                cache = jobs.load_cache(user_id)
                result = jobs.do_order(fresh, cache, target, dish, dry_run=False)
        jobs.save_cache(user_id, cache)
        # 用户主动订餐记一条运行记录，便于追溯
        db.record_job_run(user_id, self.key, "order", result.ok,
                          result.message, result.detail)
        return result

    async def get_view(self, user_id: int, view_id: str, params: dict) -> dict:
        cred = db.load_credential(user_id, self.key)
        payload = cred.payload
        if not payload.get("accessToken"):
            raise FeatureLoginError("尚未登录，请先在上方登录")

        # 日期参数：默认今天；订餐窗口内当天可订，也允许看明天
        day_str = (params or {}).get("date") or date.today().isoformat()
        try:
            target = date.fromisoformat(day_str)
        except ValueError:
            raise FeatureLoginError(f"日期格式不正确：{day_str}")

        if view_id not in {v.view_id for v in self.views}:
            raise FeatureLoginError(f"未知视图：{view_id}")

        def run_query(p: dict) -> dict:
            if view_id == "status":
                data = jobs.check_status(p, target)
                data.pop("raw_item", None)      # 原始报文太大，前端用不到
                return data
            if view_id == "recommend":
                cache = jobs.load_cache(user_id)
                data = jobs.recommend(p, cache, target)
                jobs.save_cache(user_id, cache)   # 首次拉取的历史要落库
                return data
            if view_id == "candidates":
                return self._candidates_view(p, target)
            return jobs.get_fruit_menu(p, target)   # fruit

        try:
            return run_query(payload)
        except MeicanError as exc:
            # token 失效（401）：有长期凭据就自动续期重试一次，
            # 不必让用户手动重新登录。其他错误直接抛出。
            if exc.status != 401:
                raise FeatureLoginError(f"查询失败：{exc}") from exc
            if not await self.relogin(user_id):
                raise FeatureLoginError(
                    "登录态已失效且无法自动续期，请重新短信登录") from exc
            fresh = db.load_credential(user_id, self.key).payload
            try:
                return run_query(fresh)
            except MeicanError as exc2:
                raise FeatureLoginError(f"查询失败：{exc2}") from exc2

    @staticmethod
    def _candidates_view(payload: dict, target: date) -> dict:
        """当天可订的全部菜品（不排序、不限量），供用户浏览。走全局共享缓存。"""
        cands, item = jobs.get_candidates_cached(payload, target, max_price=0)
        if not item:
            return {"date": target.isoformat(), "error": "该日期没有可订餐次"}
        return {
            "date": target.isoformat(),
            "title": item.get("title"),
            "already_ordered": bool(item.get("corpOrderUser")),
            "total_candidates": len(cands),
            "candidates": [{
                "restaurant_name": c.restaurant_name, "restaurant_id": c.restaurant_id,
                "dish_id": c.dish_id, "dish_name": c.dish_name, "price": c.price,
                "available": c.available,
                "dish_limit": c.dish_limit, "available_count": c.available_count,
            } for c in cands],
        }