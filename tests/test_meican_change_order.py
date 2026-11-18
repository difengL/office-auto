"""更换已订的餐：先取消旧订单再下新单。

需求：已订餐后菜单仍展示，按钮变「更换」。更换 = 退旧单 + 下新单。
取消失败则不动（避免退了旧单又下不了新单导致没饭吃）。
"""
from datetime import date

import pytest

from app.core import db
from app.features.meican import jobs
from app.features.meican.client import MeicanError


@pytest.fixture(autouse=True)
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr(db, "KEY_PATH", tmp_path / "t.key")
    db.init_db()
    yield


NEW_DISH = {"dish_id": "D2", "dish_name": "新菜", "restaurant_id": "R2",
            "restaurant_name": "切果NOW", "available": True}


def _status(ordered=True, old_id="OLD1", old_name="旧菜"):
    raw = {"userTab": {"uniqueId": "T1"}}
    if ordered:
        raw["corpOrderUser"] = {"uniqueId": old_id, "restaurantItemList": [
            {"dishItemList": [{"dish": {"name": old_name}}]}]}
    return {"has_item": True, "ordered": ordered, "raw_item": raw}


class FakeClient:
    def __init__(self, delete_ok=True, place_ok=True):
        self.delete_ok = delete_ok
        self.place_ok = place_ok
        self.deleted = []
        self.placed = []

    def delete_order(self, oid):
        self.deleted.append(oid)
        if not self.delete_ok:
            raise MeicanError("订单已锁定无法取消")
        return {"status": "SUCCESSFUL"}

    def place_order(self, **kw):
        self.placed.append(kw)
        if not self.place_ok:
            raise MeicanError("库存不足")
        return {"status": "SUCCESSFUL"}


def test_change_cancels_old_then_places_new(monkeypatch):
    # 退单前已订，退单后变未订（do_order 会重查状态）
    states = [_status(True), _status(False)]
    monkeypatch.setattr(jobs, "check_status", lambda p, d: states.pop(0))
    fake = FakeClient()
    monkeypatch.setattr(jobs, "_client", lambda p: fake)
    monkeypatch.setattr(jobs, "_resolve_address", lambda c, i: "ADDR")

    r = jobs.change_order({"accessToken": "AT"}, {}, date.today(), NEW_DISH)

    assert r.ok is True
    assert fake.deleted == ["OLD1"]          # 先退旧单
    assert len(fake.placed) == 1             # 再下新单
    assert "更换" in r.message
    assert "旧菜" in r.message and "新菜" in r.message


def test_change_cancel_failure_keeps_old(monkeypatch):
    monkeypatch.setattr(jobs, "check_status", lambda p, d: _status(True))
    fake = FakeClient(delete_ok=False)
    monkeypatch.setattr(jobs, "_client", lambda p: fake)

    r = jobs.change_order({"accessToken": "AT"}, {}, date.today(), NEW_DISH)

    assert r.ok is False
    assert "取消原订单失败" in r.message
    assert fake.placed == []                 # 取消失败就不下新单，旧单保留


def test_change_new_order_failure_warns(monkeypatch):
    states = [_status(True), _status(False)]
    monkeypatch.setattr(jobs, "check_status", lambda p, d: states.pop(0))
    fake = FakeClient(place_ok=False)
    monkeypatch.setattr(jobs, "_client", lambda p: fake)
    monkeypatch.setattr(jobs, "_resolve_address", lambda c, i: "ADDR")

    r = jobs.change_order({"accessToken": "AT"}, {}, date.today(), NEW_DISH)

    assert r.ok is False
    assert fake.deleted == ["OLD1"]          # 旧单已退
    assert "当前无订单" in r.message          # 明确告警


def test_change_when_not_ordered_just_places(monkeypatch):
    monkeypatch.setattr(jobs, "check_status", lambda p, d: _status(False))
    fake = FakeClient()
    monkeypatch.setattr(jobs, "_client", lambda p: fake)
    monkeypatch.setattr(jobs, "_resolve_address", lambda c, i: "ADDR")

    r = jobs.change_order({"accessToken": "AT"}, {}, date.today(), NEW_DISH)

    assert r.ok is True
    assert fake.deleted == []                # 没旧单可退
    assert len(fake.placed) == 1


def test_change_rejects_unavailable_dish(monkeypatch):
    dish = dict(NEW_DISH, available=False)
    r = jobs.change_order({"accessToken": "AT"}, {}, date.today(), dish)
    assert r.ok is False
    assert "订满" in r.message or "暂停营业" in r.message


def test_delete_order_checks_success():
    from app.features.meican.client import MeicanClient

    class Resp:
        status_code = 200
        def __init__(self, p): self.p = p
        def json(self): return self.p

    class HTTP:
        def __init__(self, p): self.p = p
        def post(self, *a, **kw): return Resp(self.p)

    c = MeicanClient("AT")
    c._client = HTTP({"status": "FAIL", "message": "订单已锁定"})
    with pytest.raises(MeicanError, match="订单已锁定"):
        c.delete_order("O1")
