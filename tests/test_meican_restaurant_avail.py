"""不可订餐厅（订满/打烊）置灰展示但不可订，与手机端一致。

回归背景：用户手机端看到「辣嘴鸭(三里屯店)」灰色（订餐达上限）。
平台不应过滤掉它，而应像手机端一样置灰显示、禁用订餐按钮。
真实数据：不可订餐厅 open=false、availableDishCount=0、restaurantStatus=2；
正常餐厅 open=true、availableDishCount>0、restaurantStatus=0。
"""
from datetime import date

import pytest

from app.core import db
from app.features.meican import jobs
from app.features.meican.client import MeicanError


ITEM = {"userTab": {"uniqueId": "T1"}, "targetTime": 1794902400000}


class FakeClient:
    def __init__(self, rests, menus=None):
        self.rests = rests
        self.menus = menus or {}

    def restaurants(self, tab, tt):
        return self.rests

    def restaurant_menu(self, rid, tab, tt):
        if rid not in self.menus:
            raise MeicanError("无菜单")
        return self.menus[rid]


def _rest(uid, name, open_=True, avail=10, status=0):
    return {"uniqueId": uid, "name": name, "open": open_,
            "availableDishCount": avail, "restaurantStatus": status}


MENU = {"dishList": [{"id": "D1", "name": "菜", "priceInCent": 2500}]}


def test_closed_restaurant_marked_unavailable_not_filtered():
    c = FakeClient([_rest("R1", "辣嘴鸭", open_=False, avail=0, status=2)],
                   {"R1": MENU})
    cands = jobs.build_candidates(c, ITEM, max_price=0)
    assert len(cands) == 1                 # 仍展示，不过滤
    assert cands[0].available is False     # 但标记不可订


def test_normal_restaurant_available():
    c = FakeClient([_rest("R1", "金手勺", open_=True, avail=56, status=0)],
                   {"R1": MENU})
    cands = jobs.build_candidates(c, ITEM, max_price=0)
    assert cands[0].available is True


def test_mixed_both_listed_with_flags():
    c = FakeClient([
        _rest("R1", "金手勺", open_=True, avail=56, status=0),
        _rest("R2", "辣嘴鸭", open_=False, avail=0, status=2),
    ], {"R1": MENU, "R2": MENU})
    cands = jobs.build_candidates(c, ITEM, max_price=0)
    flags = {x.restaurant_name: x.available for x in cands}
    assert flags == {"金手勺": True, "辣嘴鸭": False}


def test_missing_fields_treated_available():
    r = {"uniqueId": "R1", "name": "某店"}
    c = FakeClient([r], {"R1": MENU})
    assert jobs.build_candidates(c, ITEM, max_price=0)[0].available is True


def test_restaurant_available_helper():
    assert jobs._restaurant_available(_rest("R", "x", True, 10, 0)) is True
    assert jobs._restaurant_available(_rest("R", "x", False, 0, 2)) is False
    assert jobs._restaurant_available(_rest("R", "x", True, 0, 0)) is False
    assert jobs._restaurant_available({"name": "x"}) is True


def test_closed_is_unavailable_even_with_quota():
    """过了供餐时段 open=false，即便还有份数也不能下单。

    依据美餐前端 generateRestaurantSections：isDisabled = !t.open，
    与份数判据并列。两处触发不同文案，但都不可订。
    """
    assert jobs._restaurant_available(_rest("R", "金手勺", False, 56, 0)) is False
    assert jobs.restaurant_unavailable_reason(_rest("R", "金手勺", False, 56, 0)) == "closed"


def test_sold_out_takes_precedence_over_closed():
    """份数与关闭同时成立时，报「超限」——与前端先判 count 的顺序一致。"""
    r = _rest("R", "辣嘴鸭", False, 0, 2)
    assert jobs.restaurant_unavailable_reason(r) == "sold_out"


def test_reason_distinguishes_two_causes():
    """两个原因必须分开——前端 i18n 是两套文案。"""
    assert jobs.restaurant_unavailable_reason(_rest("R", "x", True, 0, 0)) == "sold_out"
    assert jobs.restaurant_unavailable_reason(_rest("R", "x", False, 10, 1)) == "closed"
    assert jobs.restaurant_unavailable_reason(_rest("R", "x", True, 10, 0)) == ""


def test_status_alone_can_block_when_fields_missing():
    """open/份数缺失但总状态明示不可订时，不能放行。"""
    assert jobs._restaurant_available({"name": "x", "restaurantStatus": 2}) is False
    assert jobs._restaurant_available({"name": "x", "restaurantStatus": 1}) is False
    assert jobs._restaurant_available({"name": "x", "restaurantStatus": 0}) is True


def test_missing_all_status_fields_treated_available():
    """所有状态字段都没返回时保守视为可订，让后端给明确报错。"""
    assert jobs._restaurant_available({"name": "x"}) is True


def test_build_candidates_carries_reason():
    c = FakeClient([_rest("R1", "辣嘴鸭", open_=False, avail=0, status=2),
                    _rest("R2", "某店", open_=False, avail=30, status=1),
                    _rest("R3", "金手勺", open_=True, avail=40, status=0)],
                   {"R1": MENU, "R2": MENU, "R3": MENU})
    got = {x.restaurant_name: x.unavailable_reason
           for x in jobs.build_candidates(c, ITEM, max_price=0)}
    assert got == {"辣嘴鸭": "sold_out", "某店": "closed", "金手勺": ""}


def test_unavailable_message_matches_mealican_wording():
    assert "限额" in jobs.unavailable_message("辣嘴鸭", "sold_out")
    assert "关闭点餐" in jobs.unavailable_message("某店", "closed")
    assert "无法下单" in jobs.unavailable_message("某店", "")


# ---------- 下单拦截不可订 ----------

@pytest.fixture(autouse=True)
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr(db, "KEY_PATH", tmp_path / "t.key")
    db.init_db()
    yield


def test_do_order_rejects_unavailable_dish(monkeypatch):
    monkeypatch.setattr(jobs, "check_status", lambda p, d: {
        "has_item": True, "ordered": False, "raw_item": ITEM})
    dish = {"dish_id": "D1", "dish_name": "菜", "restaurant_name": "辣嘴鸭",
            "restaurant_id": "R2", "available": False}
    r = jobs.do_order({"accessToken": "AT"}, {}, date.today(), dish, dry_run=False)
    assert r.ok is False
    assert "订满" in r.message or "暂停营业" in r.message


def test_do_order_auto_picks_available(monkeypatch):
    monkeypatch.setattr(jobs, "check_status", lambda p, d: {
        "has_item": True, "ordered": False, "raw_item": ITEM})
    # recommend 返回不可订在前、可订在后（模拟排序前的原始顺序）
    monkeypatch.setattr(jobs, "recommend", lambda p, c, d, limit, max_price: {
        "candidates": [
            {"dish_id": "D2", "dish_name": "满菜", "restaurant_name": "辣嘴鸭",
             "restaurant_id": "R2", "available": False},
            {"dish_id": "D1", "dish_name": "可订菜", "restaurant_name": "金手勺",
             "restaurant_id": "R1", "available": True}]})
    seen = {}
    monkeypatch.setattr(jobs, "_resolve_address", lambda c, i: "ADDR")

    class C:
        def place_order(self, **kw):
            seen["dish"] = kw["dishes"][0]["dishId"]
            return {"status": "SUCCESSFUL"}

    monkeypatch.setattr(jobs, "_client", lambda p: C())
    r = jobs.do_order({"accessToken": "AT"}, {}, date.today(), None, dry_run=False)
    assert r.ok is True
    assert seen["dish"] == "D1"      # 自动挑了可订的，跳过订满的
