"""美餐只读查看视图：订餐状态 / 口味推荐 / 当天可订菜品。

回归背景：重构为插件平台后，只接了「登录/配置/执行/记录」通用框架，
旧版的「看推荐、看可订、看已订」三个查看功能丢了——后端 jobs 里逻辑还在，
但没有 API 与界面。这里锁定 get_view 的行为。
"""
import asyncio
from datetime import date

import pytest

from app.core import db
from app.core.feature import FeatureLoginError, ViewSpec
from app.features.meican import feature as feat
from app.features.meican.feature import MeicanFeature


@pytest.fixture(autouse=True)
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr(db, "KEY_PATH", tmp_path / "t.key")
    db.init_db()
    yield


def test_views_declared():
    ids = {v.view_id for v in MeicanFeature.views}
    assert ids == {"status", "recommend", "candidates", "fruit"}
    assert all(isinstance(v, ViewSpec) for v in MeicanFeature.views)


def _login(user_id):
    db.save_credential(user_id, "meican",
                       {"phone": "1", "accessToken": "AT"}, "active")


def test_status_view(monkeypatch):
    u = db.create_user("u1", "H", "U1")
    _login(u.id)
    monkeypatch.setattr(feat.jobs, "check_status", lambda p, d: {
        "date": d.isoformat(), "has_item": True, "ordered": True,
        "title": "晚餐", "raw_item": {"huge": "blob"},
        "order_detail": {"dishes": [{"name": "烤鸭", "price": 25}]}})

    data = asyncio.run(MeicanFeature().get_view(u.id, "status", {}))

    assert data["ordered"] is True
    assert data["order_detail"]["dishes"][0]["name"] == "烤鸭"
    # 原始报文不应下发前端
    assert "raw_item" not in data


def test_recommend_view(monkeypatch):
    u = db.create_user("u2", "H", "U2")
    _login(u.id)
    monkeypatch.setattr(feat.jobs, "recommend", lambda p, c, d: {
        "date": d.isoformat(), "title": "晚餐", "already_ordered": False,
        "candidates": [{"dish_name": "青椒肉丝", "score": 8.5,
                        "restaurant_name": "某店", "price": 22}],
        "total_candidates": 12})

    data = asyncio.run(MeicanFeature().get_view(u.id, "recommend", {}))

    assert data["candidates"][0]["dish_name"] == "青椒肉丝"
    assert data["total_candidates"] == 12


def test_candidates_view(monkeypatch):
    u = db.create_user("u3", "H", "U3")
    _login(u.id)

    class FakeClient:
        def __init__(self, *a, **kw):
            pass

        def calendar_items(self, s, e, with_order=True):
            return [{"calendarItemList": [{"title": "晚餐",
                                           "userTab": {"uniqueId": "T"}}]}]

        def restaurants(self, tab, tt):
            return [{"name": "店A", "uniqueId": "R1"}]

        def restaurant_menu(self, rid, tab, tt):
            return {"dishList": [
                {"id": "D1", "name": "菜1", "priceInCent": 2000},
                {"id": "D2", "name": "菜2", "priceInCent": 3000},
                {"id": "S", "name": "分隔", "isSection": True}]}

    monkeypatch.setattr(feat.jobs, "_client", lambda p: FakeClient())
    data = asyncio.run(MeicanFeature().get_view(u.id, "candidates", {}))

    assert data["total_candidates"] == 2      # 分隔项被过滤
    assert [c["dish_name"] for c in data["candidates"]] == ["菜1", "菜2"]
    assert data["candidates"][0]["price"] == 20.0


def test_view_requires_login():
    u = db.create_user("u4", "H", "U4")
    with pytest.raises(FeatureLoginError, match="尚未登录"):
        asyncio.run(MeicanFeature().get_view(u.id, "status", {}))


def test_view_rejects_bad_date():
    u = db.create_user("u5", "H", "U5")
    _login(u.id)
    with pytest.raises(FeatureLoginError, match="日期格式"):
        asyncio.run(MeicanFeature().get_view(u.id, "status", {"date": "not-a-date"}))


def test_view_unknown_id():
    u = db.create_user("u6", "H", "U6")
    _login(u.id)
    with pytest.raises(FeatureLoginError, match="未知视图"):
        asyncio.run(MeicanFeature().get_view(u.id, "nope", {}))


def test_view_wraps_meican_error(monkeypatch):
    u = db.create_user("u7", "H", "U7")
    _login(u.id)

    def boom(p, d):
        raise feat.MeicanError("服务器内部错误", 500)   # 非 401，直接包装报错

    monkeypatch.setattr(feat.jobs, "check_status", boom)
    with pytest.raises(FeatureLoginError, match="查询失败"):
        asyncio.run(MeicanFeature().get_view(u.id, "status", {}))


def test_timesheet_has_no_views():
    from app.features.timesheet.feature import TimesheetFeature
    assert TimesheetFeature.views == []
    u = db.create_user("u8", "H", "U8")
    with pytest.raises(FeatureLoginError):
        asyncio.run(TimesheetFeature().get_view(u.id, "status", {}))
