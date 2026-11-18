"""当天可订菜品的全局共享缓存。

当天菜单对同企业用户是固定的，拉一次全局缓存即可，不必每个用户、
每次推荐都重打几十次餐厅菜单接口。推荐也基于这份缓存做。
缓存键含日期+企业namespace+餐次时间：同企业共享，不同企业互不干扰。
"""
import time
from datetime import date

import pytest

from app.core import db
from app.features.meican import jobs
from app.features.meican.preference import DishCandidate


@pytest.fixture(autouse=True)
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr(db, "KEY_PATH", tmp_path / "t.key")
    db.init_db()
    yield


ITEM = {"title": "晚餐", "targetTime": 1794902400000,
        "userTab": {"uniqueId": "T1", "corp": {"namespace": "102105"}}}


class FakeClient:
    def __init__(self):
        self.menu_calls = 0

    def calendar_items(self, s, e, with_order=True):
        return [{"calendarItemList": [ITEM]}]

    def restaurants(self, tab, tt):
        return [{"name": "金百万", "uniqueId": "R1"}]

    def restaurant_menu(self, rid, tab, tt):
        self.menu_calls += 1
        return {"dishList": [
            {"id": "D1", "name": "烤鸭", "priceInCent": 2500},
            {"id": "D2", "name": "贵的菜", "priceInCent": 5000}]}


def test_first_call_pulls_and_caches(monkeypatch):
    fake = FakeClient()
    monkeypatch.setattr(jobs, "_client", lambda p: fake)
    cands, item = jobs.get_candidates_cached({"accessToken": "AT"}, date.today())

    assert fake.menu_calls == 1
    assert len(cands) == 2
    assert item["title"] == "晚餐"
    # 全局缓存已写入
    key = jobs._menu_cache_key(date.today(), ITEM)
    assert db.gdata_get(key)["candidates"]


def test_second_call_uses_cache_no_api(monkeypatch):
    fake = FakeClient()
    monkeypatch.setattr(jobs, "_client", lambda p: fake)
    jobs.get_candidates_cached({"accessToken": "AT"}, date.today())
    fake.menu_calls = 0

    cands, _ = jobs.get_candidates_cached({"accessToken": "AT"}, date.today())
    assert fake.menu_calls == 0          # 命中缓存，不再打菜单接口
    assert len(cands) == 2


def test_cache_shared_across_users(monkeypatch):
    fake = FakeClient()
    monkeypatch.setattr(jobs, "_client", lambda p: fake)
    # 用户A拉一次
    jobs.get_candidates_cached({"accessToken": "A"}, date.today())
    # 用户B直接命中同一份缓存（同企业）
    fake.menu_calls = 0
    cands, _ = jobs.get_candidates_cached({"accessToken": "B"}, date.today())
    assert fake.menu_calls == 0
    assert len(cands) == 2


def test_different_namespace_not_shared(monkeypatch):
    fake = FakeClient()
    monkeypatch.setattr(jobs, "_client", lambda p: fake)
    jobs.get_candidates_cached({"accessToken": "A"}, date.today())

    # 换个企业namespace，缓存键不同，要重新拉
    other = dict(ITEM, userTab={"uniqueId": "T2", "corp": {"namespace": "999999"}})
    fake.calendar_items = lambda s, e, with_order=True: [{"calendarItemList": [other]}]
    fake.menu_calls = 0
    jobs.get_candidates_cached({"accessToken": "A"}, date.today())
    assert fake.menu_calls == 1


def test_max_price_filters_on_top_of_cache(monkeypatch):
    fake = FakeClient()
    monkeypatch.setattr(jobs, "_client", lambda p: fake)
    # 缓存存全量（含50元的菜）
    jobs.get_candidates_cached({"accessToken": "AT"}, date.today(), max_price=0)
    # 限价25过滤，但不应重新拉接口
    fake.menu_calls = 0
    cands, _ = jobs.get_candidates_cached({"accessToken": "AT"}, date.today(), max_price=25)
    assert fake.menu_calls == 0
    assert [c.dish_name for c in cands] == ["烤鸭"]   # 贵的菜被滤掉


def test_stale_cache_refetches(monkeypatch):
    fake = FakeClient()
    monkeypatch.setattr(jobs, "_client", lambda p: fake)
    jobs.get_candidates_cached({"accessToken": "AT"}, date.today())
    # 把缓存时间戳改成过期
    key = jobs._menu_cache_key(date.today(), ITEM)
    cached = db.gdata_get(key)
    cached["ts"] = time.time() - (jobs.MENU_TTL + 100)
    db.gdata_set(key, cached)
    fake.menu_calls = 0

    jobs.get_candidates_cached({"accessToken": "AT"}, date.today())
    assert fake.menu_calls == 1          # 过期重拉


def test_recommend_uses_cached_menu(monkeypatch):
    fake = FakeClient()
    monkeypatch.setattr(jobs, "_client", lambda p: fake)
    monkeypatch.setattr(jobs, "fetch_history", lambda p, c: [])
    # 先建立缓存
    jobs.get_candidates_cached({"accessToken": "AT"}, date.today())
    fake.menu_calls = 0

    rec = jobs.recommend({"accessToken": "AT"}, {}, date.today(), max_price=25)
    assert fake.menu_calls == 0          # 推荐复用缓存，不重拉菜单
    assert rec["total_candidates"] == 1  # 限价25后只剩烤鸭
    assert rec["candidates"][0]["dish_name"] == "烤鸭"
