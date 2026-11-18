"""餐厅份数（总上限 / 剩余可用）传递到前端，显示「已订 X/Y」。

数据来自 restaurants 接口的 dishLimit（总份数上限）与
availableDishCount（剩余可订份数）。已订 = dishLimit - availableDishCount。
实测金手勺 dishLimit=80、availableDishCount=56 → 已订 24/80；
辣嘴鸭 60/0 → 已订 60/60（订满）。
"""
from app.features.meican import jobs
from app.features.meican.client import MeicanError


ITEM = {"userTab": {"uniqueId": "T1"}, "targetTime": 1794902400000}
MENU = {"dishList": [{"id": "D1", "name": "菜", "priceInCent": 2500}]}


class FakeClient:
    def __init__(self, rests):
        self.rests = rests

    def restaurants(self, tab, tt):
        return self.rests

    def restaurant_menu(self, rid, tab, tt):
        return MENU


def test_candidates_carry_quota_fields():
    r = {"uniqueId": "R1", "name": "金手勺", "open": True,
         "availableDishCount": 56, "restaurantStatus": 0, "dishLimit": 80}
    cands = jobs.build_candidates(FakeClient([r]), ITEM, max_price=0)
    assert cands[0].dish_limit == 80
    assert cands[0].available_count == 56


def test_full_restaurant_quota():
    r = {"uniqueId": "R2", "name": "辣嘴鸭", "open": False,
         "availableDishCount": 0, "restaurantStatus": 2, "dishLimit": 60}
    cands = jobs.build_candidates(FakeClient([r]), ITEM, max_price=0)
    assert cands[0].dish_limit == 60
    assert cands[0].available_count == 0
    assert cands[0].available is False


def test_missing_quota_fields_none():
    r = {"uniqueId": "R1", "name": "某店"}
    cands = jobs.build_candidates(FakeClient([r]), ITEM, max_price=0)
    assert cands[0].dish_limit is None
    assert cands[0].available_count is None
