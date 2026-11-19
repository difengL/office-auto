"""可订判定必须区分「已订餐导致入口关闭」与「餐厅真的关闭」。

回归背景：2026-11-19 12:18 用户订餐成功，12:30 页面把 9 家餐厅全显示成
「已关闭」，但份数都还有 37~86，当前 12:32 也仍在 09:00–16:00 窗口内。

实测与源码两端都确认了原因：
- 实测：未订餐 11:11 → 全部 open=true；已订餐 12:30 → 全部 open=false，
  但 availableDishCount 仍有 37~86、restaurantStatus 仍为 0。
- 源码 aa22780.js：
      return "AVAILABLE" !== o.status && (o.isAvailable = !1), o
  餐次一旦被订（状态变 ORDER），接口就把整列表 isAvailable 置 false，
  下发的 open 也就全变 false。

所以 open=false 有两种含义，必须靠「是否已订餐」+「是否在订餐窗口内」
+ restaurantStatus 区分，不能一律当餐厅打烊。
"""
from datetime import date, datetime, time

import pytest

from app.core import db
from app.features.meican import jobs


ITEM = {"userTab": {"uniqueId": "T1"}, "targetTime": 1794902400000,
        "openingTime": {"openTime": "09:00", "closeTime": "16:00"}}


class FakeClient:
    def __init__(self, rests, menus=None):
        self.rests = rests
        self.menus = menus or {}

    def restaurants(self, tab, tt):
        return self.rests

    def restaurant_menu(self, rid, tab, tt):
        return self.menus[rid]


MENU = {"dishList": [{"id": "D1", "name": "菜", "priceInCent": 2500}]}


def _rest(name="某店", open_=True, avail=50, status=0):
    return {"uniqueId": "R1", "name": name, "open": open_,
            "availableDishCount": avail, "restaurantStatus": status}


# ---------- 订餐窗口 ----------

def test_within_window_true_at_noon():
    assert jobs.within_order_window(ITEM, datetime(2026, 11, 19, 12, 30)) is True


def test_within_window_false_after_close():
    assert jobs.within_order_window(ITEM, datetime(2026, 11, 19, 20, 47)) is False


def test_within_window_false_before_open():
    assert jobs.within_order_window(ITEM, datetime(2026, 11, 19, 8, 30)) is False


def test_within_window_boundaries_inclusive():
    assert jobs.within_order_window(ITEM, datetime(2026, 11, 19, 9, 0)) is True
    assert jobs.within_order_window(ITEM, datetime(2026, 11, 19, 16, 0)) is True
    assert jobs.within_order_window(ITEM, datetime(2026, 11, 19, 16, 1)) is False


def test_within_window_without_opening_time_is_open():
    """没有 openingTime 字段时保守放行，交给后端报错。"""
    assert jobs.within_order_window({"userTab": {}}, datetime(2026, 11, 19, 3, 0)) is True


# ---------- 已订餐场景 ----------

def test_ordered_restaurant_stays_available_in_window():
    """订过餐后 open 全变 false，但窗口内仍可更换——不能显示已关闭。"""
    r = _rest(open_=False, avail=37, status=0)
    reason = jobs.restaurant_unavailable_reason(
        r, window_open=True, already_ordered=True)
    assert reason == ""


def test_not_ordered_open_false_is_closed():
    """没订过餐却 open=false，才是真的不能订。"""
    r = _rest(open_=False, avail=37, status=0)
    reason = jobs.restaurant_unavailable_reason(
        r, window_open=True, already_ordered=False)
    assert reason == "closed"


def test_ordered_but_window_closed_is_closed():
    """窗口外即便已订餐也不能更换，仍是关闭。"""
    r = _rest(open_=False, avail=37, status=0)
    reason = jobs.restaurant_unavailable_reason(
        r, window_open=False, already_ordered=True)
    assert reason == "closed"


def test_ordered_and_sold_out_still_sold_out():
    """已订餐不影响「份数为 0」的事实。"""
    r = _rest(open_=False, avail=0, status=0)
    assert jobs.restaurant_unavailable_reason(
        r, window_open=True, already_ordered=True) == "sold_out"


def test_ordered_restaurant_status_1_is_closed():
    """餐厅自己关闭点餐（status=1），即便已订餐也不能换到这家。"""
    r = _rest(open_=False, avail=37, status=1)
    assert jobs.restaurant_unavailable_reason(
        r, window_open=True, already_ordered=True) == "closed"


def test_ordered_restaurant_status_2_is_sold_out():
    r = _rest(open_=False, avail=37, status=2)
    assert jobs.restaurant_unavailable_reason(
        r, window_open=True, already_ordered=True) == "sold_out"


# ---------- 原有语义不能退化 ----------

def test_normal_in_window_available():
    assert jobs.restaurant_unavailable_reason(
        _rest(), window_open=True, already_ordered=False) == ""


def test_window_closed_all_closed():
    assert jobs.restaurant_unavailable_reason(
        _rest(), window_open=False, already_ordered=False) == "closed"


def test_missing_fields_treated_available():
    """字段全缺时保守放行，让后端给明确报错。"""
    assert jobs.restaurant_unavailable_reason(
        {"name": "x"}, window_open=True, already_ordered=False) == ""


# ---------- 共享缓存不能带用户态 ----------

def test_cache_is_user_agnostic(monkeypatch, tmp_path):
    """同一份缓存要给「已订」和「未订」两个用户算出不同 available。"""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr(db, "KEY_PATH", tmp_path / "t.key")
    db.init_db()

    def fake_calendar(payload, d):
        return {'targetTime': 1794902400000, 'title': '晚餐',
                'status': 'ORDER' if payload.get('ordered') else 'AVAILABLE',
                'openingTime': {'openTime': '09:00', 'closeTime': '16:00'},
                'userTab': {'uniqueId': 'T1',
                            'corp': {'namespace': 'NS'}},
                'corpOrderUser': {'uniqueId': 'O1'} if payload.get('ordered') else None}

    class C:
        def calendar_items(self, s, e, with_order=True):
            return [{"calendarItemList": [fake_calendar(self.p, s)]}]

        def restaurants(self, tab, tt):
            return [_rest(open_=False, avail=37, status=0)]

        def restaurant_menu(self, rid, tab, tt):
            return MENU

    def make_client(payload):
        c = C()
        c.p = payload
        return c

    monkeypatch.setattr(jobs, "_client", make_client)
    monkeypatch.setattr(jobs, "datetime", _FrozenDatetime)

    # 已订餐用户先拉，写进共享缓存。此时服务端返回的 open 全是 false，
    # 但这个值只反映「我下过单」，不能落库成餐厅状态。
    cands, _ = jobs.get_candidates_cached({"accessToken": "A", "ordered": True},
                                          date.today())
    assert cands[0].available is True

    # 未订餐用户随后读同一份缓存：open 未知，应保守放行而不是判「已关闭」
    cands2, _ = jobs.get_candidates_cached({"accessToken": "B", "ordered": False},
                                           date.today())
    assert cands2[0].available is True

    # 窗口外的已订餐用户，同一份缓存应判为关闭
    monkeypatch.setattr(jobs, "datetime", _LateDatetime)
    cands3, _ = jobs.get_candidates_cached({"accessToken": "B", "ordered": True},
                                           date.today())
    assert cands3[0].available is False
    assert cands3[0].unavailable_reason == "closed"


class _FrozenDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return datetime(2026, 11, 19, 12, 30)


class _LateDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return datetime(2026, 11, 19, 20, 47)
