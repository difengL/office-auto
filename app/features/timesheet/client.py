"""HEM 工时系统客户端。

登录态由 JSESSIONID + rememberMe cookie 维持，每次调用重新登录（成本低、无状态）。
凭据只在内存中流转，不落日志。
"""
from __future__ import annotations

from datetime import date, timedelta

import httpx

BASE = "https://hem.100credit.cn"
LOGIN_PAGE = "/login"
LOGIN_API = "/api/login"
REMEMBER_ME_DAYS = 7


class HemError(Exception):
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


def monday_of(any_day: date) -> date:
    return any_day - timedelta(days=any_day.weekday())


def workdays_of_week(any_day: date) -> list[date]:
    m = monday_of(any_day)
    return [m + timedelta(days=i) for i in range(5)]


class HemClient:
    def __init__(self, username: str, password: str, timeout: float = 25.0):
        self.username = username
        self.password = password
        self._client = httpx.Client(base_url=BASE, timeout=timeout,
                                    follow_redirects=True,
                                    headers={"User-Agent": "Mozilla/5.0"})
        self.uid: str = ""
        self.name: str = ""

    def close(self) -> None:
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def login(self) -> dict:
        # 先取登录页拿初始 cookie
        self._client.get(LOGIN_PAGE)
        r = self._client.post(LOGIN_API, data={
            "username": self.username,
            "password": self.password,
            "rememberMe": "true",
        }, headers={"X-Requested-With": "XMLHttpRequest"})
        try:
            payload = r.json()
        except ValueError as exc:
            raise HemError("登录响应不是 JSON，可能被重定向到登录页") from exc
        if payload.get("code") not in (0, 200, "0", "200") and not payload.get("success"):
            raise HemError(payload.get("msg") or payload.get("message") or "登录失败")
        data = payload.get("data") or {}
        self.uid = str(data.get("uid") or data.get("userId") or "")
        self.name = data.get("name") or data.get("realname") or self.username
        # 规格实测：HEM 下发 session_id + token（HttpOnly，有效期 10 天）
        if not any(k in self._client.cookies for k in ("session_id", "token")):
            raise HemError("登录后未获得会话 cookie")
        return {"uid": self.uid, "username": self.username, "name": self.name}

    def _get(self, path: str, params: dict) -> dict:
        r = self._client.get(path, params=params,
                             headers={"X-Requested-With": "XMLHttpRequest"})
        if r.status_code != 200:
            raise HemError(f"{path} HTTP {r.status_code}")
        try:
            payload = r.json()
        except ValueError as exc:
            raise HemError(f"{path} 返回非 JSON（登录态可能失效）") from exc
        if payload.get("code") not in (0, 200, "0", "200", None) and not payload.get("success"):
            raise HemError(payload.get("msg") or f"{path} 业务失败")
        return payload

    def list_projects(self) -> list[dict]:
        payload = self._get("/api/localproject/list", {"page_size": 10000})
        data = payload.get("data") or {}
        items = data.get("list") if isinstance(data, dict) else data
        out = []
        for p in items or []:
            pid = p.get("project_id") or p.get("projectId")
            name = p.get("name") or ""
            if pid and name:
                out.append({"project_id": str(pid), "name": name})
        return out

    def week_records(self, start: date, end: date) -> list[dict]:
        payload = self._get("/api/daily/week", {
            "start_time": start.isoformat(),
            "end_time": end.isoformat(),
            "new_menu": 0,
        })
        data = payload.get("data") or {}
        return data.get("list") or []

    def submit(self, week_date: date, weekly_type: int = 1) -> dict:
        payload = self._get("/api/hem/weekly/user-submit", {
            "username": self.username,
            "dateId": week_date.isoformat(),
            "type": weekly_type,
        })
        return payload


def filled_dates(records: list[dict]) -> dict[str, float]:
    """从 week_records 提取「日期 → 已填小时数」。"""
    out: dict[str, float] = {}
    for rec in records:
        for item in rec.get("content") or []:
            d = item.get("date")
            hours = float(item.get("task_time") or 0)
            if d and hours > 0:
                out[d] = out.get(d, 0.0) + hours
    return out