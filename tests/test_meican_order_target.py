"""定时订餐必须订「当天」，且餐次未开放时不能报「餐厅已订满」。

回归背景：2026-11-19 10:02 定时任务失败，提示「没有可订的候选菜品
（餐厅可能均已订满）」，但当天 9 家餐厅全部可订。真实原因是
`run_job` 里 `target_offset_days` 默认 1，任务订的是**明天**；
明天的餐次当时状态为 NOT_YET「超出预定支持的时间范围」，所以每家
餐厅的 open 都是 false，被统一判为 closed，最后报出一条与事实
完全不符的错误信息。
"""
import asyncio
from datetime import date, timedelta

import pytest

from app.core import db
from app.core.feature import JobContext
from app.features.meican import feature as feat
from app.features.meican import jobs
from app.features.meican.feature import MeicanFeature


@pytest.fixture(autouse=True)
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr(db, "KEY_PATH", tmp_path / "t.key")
    db.init_db()
    yield


def _user_with_cred(monkeypatch, name="mc"):
    u = db.create_user(name, "HASH", name.upper())
    db.save_credential(u.id, "meican", {"accessToken": "AT"}, "active")
    monkeypatch.setattr(feat.jobs, "load_cache", lambda uid: {})
    monkeypatch.setattr(feat.jobs, "save_cache", lambda uid, c: None)
    return u


def _run_order(monkeypatch, u, options=None):
    seen = {}

    def fake_do_order(payload, cache, target, dish, dry_run):
        seen["target"] = target
        return jobs.JobResult(True, "ok", {})

    monkeypatch.setattr(feat.jobs, "do_order", fake_do_order)
    ctx = JobContext(u.id, "meican", "order", False, options or {})
    r = asyncio.run(MeicanFeature().run_job(u.id, "order", ctx))
    return r, seen


def test_order_job_targets_today_by_default(monkeypatch):
    """默认订当天——订明天时餐次还没开放，等于每跑必败。"""
    u = _user_with_cred(monkeypatch)
    r, seen = _run_order(monkeypatch, u)
    assert r.ok is True
    assert seen["target"] == date.today()


def test_order_job_respects_explicit_offset(monkeypatch):
    u = _user_with_cred(monkeypatch, "mc2")
    _, seen = _run_order(monkeypatch, u, {"target_offset_days": 1})
    assert seen["target"] == date.today() + timedelta(days=1)


# ---------- 餐次状态：不能把「未开放」说成「餐厅订满」 ----------

def test_slot_not_yet_message_says_not_open():
    msg = jobs.slot_unavailable_message(
        date(2026, 11, 20), "NOT_YET", "超出预定支持的时间范围，请重新选择时间")
    assert "尚未开放" in msg
    assert "超出预定支持的时间范围" in msg
    assert "订满" not in msg


def test_slot_closed_message():
    msg = jobs.slot_unavailable_message(date(2026, 11, 21), "CLOSED", "抱歉，目前没有供餐的餐厅")
    assert "已关闭" in msg
    assert "订满" not in msg


def test_slot_available_returns_empty():
    assert jobs.slot_unavailable_message(date.today(), "AVAILABLE", "") == ""
    assert jobs.slot_unavailable_message(date.today(), "", "") == ""


def test_slot_all_restaurant_exceed():
    msg = jobs.slot_unavailable_message(date.today(), "ALL_RESTAURANT_EXCEED", "")
    assert "订满" in msg


def test_do_order_rejects_before_slot_opens(monkeypatch):
    """餐次未开放时给明确提示，且不能走「候选为空」那条误导分支。"""
    monkeypatch.setattr(jobs, "check_status", lambda p, d: {
        "date": d.isoformat(), "has_item": True, "ordered": False,
        "status": "NOT_YET", "reason": "超出预定支持的时间范围，请重新选择时间",
        "raw_item": {}})
    called = {"recommend": False}
    monkeypatch.setattr(jobs, "recommend",
                        lambda *a, **k: called.__setitem__("recommend", True) or {})
    r = jobs.do_order({"accessToken": "AT"}, {}, date.today(), None, dry_run=False)
    assert r.ok is False
    assert "尚未开放" in r.message
    assert "订满" not in r.message
    assert called["recommend"] is False        # 不该再去拉候选


def test_do_order_no_candidates_message_names_date(monkeypatch):
    """确实全部不可订时，消息里要有日期和原因统计，便于排查。"""
    monkeypatch.setattr(jobs, "check_status", lambda p, d: {
        "date": d.isoformat(), "has_item": True, "ordered": False,
        "status": "AVAILABLE", "reason": "", "raw_item": {}})
    monkeypatch.setattr(jobs, "recommend", lambda *a, **k: {
        "candidates": [
            {"dish_id": "D1", "available": False, "unavailable_reason": "closed"},
            {"dish_id": "D2", "available": False, "unavailable_reason": "sold_out"},
        ]})
    day = date(2026, 11, 20)
    r = jobs.do_order({"accessToken": "AT"}, {}, day, None, dry_run=False)
    assert r.ok is False
    assert day.isoformat() in r.message
    assert "1" in r.message                    # 关闭 1 家
    assert "订满" in r.message or "售罄" in r.message
