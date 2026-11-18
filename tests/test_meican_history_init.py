"""历史口味数据的自动初始化与缓存持久化。

回归背景：
1. 缓存键错位——fetch_history 把历史塞进 cache["history"]、读餐厅名用
   cache["restaurant_names"]，但调用方传的 cache 直接是餐厅名字典本身，
   导致历史污染餐厅名、餐厅名读不到。
2. get_view 拉完历史不回写，缓存永远建不起来，每次推荐都重拉。
3. 历史本应从美餐 API 拉，新用户首次登录 / 上生产部署要自动初始化，
   不该依赖旧库迁移。
"""
import asyncio
import time

import pytest

from app.core import db
from app.features.meican import feature as feat
from app.features.meican import jobs
from app.features.meican.feature import MeicanFeature


@pytest.fixture(autouse=True)
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr(db, "KEY_PATH", tmp_path / "t.key")
    db.init_db()
    yield


def _login(user_id):
    db.save_credential(user_id, "meican",
                       {"phone": "1", "accessToken": "AT"}, "active")


# ---------- 缓存键分离 ----------

def test_load_save_cache_roundtrip():
    u = db.create_user("u1", "H", "U1")
    cache = {"history": {"ts": 123, "records": [{"dish": "烤鸭"}]},
             "restaurant_names": {"R1": "金百万"}}
    jobs.save_cache(u.id, cache)

    got = jobs.load_cache(u.id)
    assert got["history"]["records"] == [{"dish": "烤鸭"}]
    assert got["restaurant_names"] == {"R1": "金百万"}
    # 两个独立键，互不污染
    assert db.fdata_get(u.id, "meican", "history")["records"] == [{"dish": "烤鸭"}]
    assert db.fdata_get(u.id, "meican", "restaurant_names") == {"R1": "金百万"}


def test_history_not_pollute_restaurant_names():
    u = db.create_user("u2", "H", "U2")
    jobs.save_cache(u.id, {"history": {"ts": 1, "records": []},
                           "restaurant_names": {"R1": "店A"}})
    rn = db.fdata_get(u.id, "meican", "restaurant_names")
    assert "history" not in rn          # 历史绝不能混进餐厅名键
    assert rn == {"R1": "店A"}


def test_load_cache_defaults_when_empty():
    u = db.create_user("u3", "H", "U3")
    got = jobs.load_cache(u.id)
    assert got["history"] is None
    assert got["restaurant_names"] == {}


# ---------- fetch_history 缓存与自动拉取 ----------

class FakeClient:
    def __init__(self, *a, **kw):
        self.calls = 0

    def calendar_items(self, start, end, with_order=True):
        self.calls += 1
        return [{"targetTime": "2026-11-10", "calendarItemList": [
            {"corpOrderUser": {"restaurantItemList": [
                {"restaurant": {"uniqueId": "R1", "name": "金百万"},
                 "dishItemList": [{"dish": {"name": "烤鸭", "priceInCent": 2500}}]}]}}]}]


def test_fetch_history_pulls_from_api_when_empty(monkeypatch):
    fake = FakeClient()
    monkeypatch.setattr(jobs, "_client", lambda p: fake)
    cache = {}
    records = jobs.fetch_history({"accessToken": "AT"}, cache)

    assert fake.calls > 0               # 真的打了 API
    assert records[0]["dish"] == "烤鸭"
    assert records[0]["restaurant"] == "金百万"
    assert cache["history"]["records"] == records


def test_fetch_history_uses_cache_when_fresh(monkeypatch):
    fake = FakeClient()
    monkeypatch.setattr(jobs, "_client", lambda p: fake)
    cache = {"history": {"ts": time.time(), "records": [{"dish": "缓存菜"}]}}

    records = jobs.fetch_history({"accessToken": "AT"}, cache)

    assert fake.calls == 0              # 新鲜缓存不打 API
    assert records == [{"dish": "缓存菜"}]


def test_fetch_history_refetches_when_stale(monkeypatch):
    fake = FakeClient()
    monkeypatch.setattr(jobs, "_client", lambda p: fake)
    cache = {"history": {"ts": time.time() - 13 * 3600, "records": [{"dish": "旧"}]}}

    jobs.fetch_history({"accessToken": "AT"}, cache)

    assert fake.calls > 0               # 过期要重拉


def test_fetch_history_skips_failed_chunk_not_abort(monkeypatch):
    """美餐日历只能查约 75 天内，超范围段报错；不能因此丢掉后面的历史。"""
    from datetime import date, timedelta

    class RangeClient:
        def __init__(self):
            self.ok_calls = 0

        def calendar_items(self, start, end, with_order=True):
            # 早于 70 天前的段报 out of range，其余正常返回一个订单
            if start < date.today() - timedelta(days=70):
                raise feat.MeicanError("Period is out of range")
            self.ok_calls += 1
            return [{"targetTime": start.isoformat(), "calendarItemList": [
                {"corpOrderUser": {"restaurantItemList": [
                    {"restaurant": {"uniqueId": "R1", "name": "店"},
                     "dishItemList": [{"dish": {"name": "菜", "priceInCent": 2000}}]}]}}]}]

    rc = RangeClient()
    monkeypatch.setattr(jobs, "_client", lambda p: rc)
    records = jobs.fetch_history({"accessToken": "AT"}, {}, days=90)

    # 早期段失败被跳过，近期段仍拉到订单（而不是 0 条）
    assert rc.ok_calls > 0
    assert len(records) > 0


def test_history_days_within_api_range():
    # 默认天数必须落在美餐可查范围内（实测约 75 天上限）
    assert jobs.HISTORY_DAYS <= 75


# ---------- get_view 回写缓存 ----------

def test_recommend_view_persists_history(monkeypatch):
    u = db.create_user("u4", "H", "U4")
    _login(u.id)
    fake = FakeClient()
    monkeypatch.setattr(jobs, "_client", lambda p: fake)
    # recommend 内部还会调 restaurants/restaurant_menu，补上
    fake.restaurants = lambda tab, tt: [{"name": "金百万", "uniqueId": "R1"}]
    fake.restaurant_menu = lambda rid, tab, tt: {"dishList": [
        {"id": "D1", "name": "烤鸭", "priceInCent": 2500}]}

    asyncio.run(MeicanFeature().get_view(u.id, "recommend", {}))

    # 首次拉取的历史必须落库，下次不再重拉
    hist = db.fdata_get(u.id, "meican", "history")
    assert hist and hist["records"]


# ---------- 登录后自动初始化 ----------

def test_login_initializes_history(monkeypatch):
    u = db.create_user("u5", "H", "U5")

    class LoginClient:
        def __init__(self, *a, **kw):
            pass

        def oauth_token_with_code(self, phone, code):
            return {"token": {"access_token": "AT", "refresh_token": "RT"},
                    "remember": "RM"}

        def account(self):
            return {"name": "张三", "snowflakeId": "SF"}

    monkeypatch.setattr(feat, "MeicanClient", LoginClient)
    fake = FakeClient()
    monkeypatch.setattr(jobs, "_client", lambda p: fake)

    info = asyncio.run(MeicanFeature().login(u.id, {"phone": "1", "code": "1234"}))

    assert info["history_ready"] is True
    assert db.fdata_get(u.id, "meican", "history")["records"]


def test_login_history_failure_does_not_block(monkeypatch):
    u = db.create_user("u6", "H", "U6")

    class LoginClient:
        def __init__(self, *a, **kw):
            pass

        def oauth_token_with_code(self, phone, code):
            return {"token": {"access_token": "AT", "refresh_token": "RT"},
                    "remember": ""}

        def account(self):
            return {"name": "李四", "snowflakeId": "SF"}

    monkeypatch.setattr(feat, "MeicanClient", LoginClient)

    def boom(p, c, **kw):
        raise feat.MeicanError("网络挂了")

    monkeypatch.setattr(jobs, "fetch_history", boom)
    info = asyncio.run(MeicanFeature().login(u.id, {"phone": "1", "code": "1234"}))

    # 历史拉取失败不应让登录失败
    assert info["display"] == "李四"
    assert info["history_ready"] is False
