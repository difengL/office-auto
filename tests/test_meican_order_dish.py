"""美餐「订这道菜」主动动作。

用户在推荐/可订列表里点某道菜的「订餐」按钮，即订选中的那道菜，
区别于定时任务的自动挑菜。尊重 dry_run 配置，并记运行记录。
"""
import asyncio
from datetime import date

import pytest

from app.core import db
from app.core.feature import FeatureLoginError
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


DISH = {"dish_id": "D1", "dish_name": "烤鸭", "restaurant_id": "R1",
        "restaurant_name": "金百万", "price": 25}


def test_order_dish_calls_do_order_with_selected(monkeypatch):
    u = db.create_user("u1", "H", "U1")
    _login(u.id)
    seen = {}

    def fake_do_order(payload, cache, target, dish, dry_run):
        seen["dish"] = dish
        seen["dry_run"] = dry_run
        return jobs.JobResult(True, "下单成功", {})

    monkeypatch.setattr(jobs, "do_order", fake_do_order)
    r = asyncio.run(MeicanFeature().do_action(
        u.id, "order_dish", {"dish": DISH, "date": date.today().isoformat()}))

    assert r.ok is True
    assert seen["dish"]["dish_id"] == "D1"     # 订的是选中的菜，不是自动挑
    # 未保存配置时用插件默认 dry_run（美餐默认 False=真下单）
    assert seen["dry_run"] is MeicanFeature.dry_run_default


def test_order_dish_always_real_even_if_config_dry_run(monkeypatch):
    """演练模式已移除：即便旧配置里 dry_run=True，订餐也真实执行。"""
    u = db.create_user("u2", "H", "U2")
    _login(u.id)
    db.save_feature_config(u.id, "meican",
                           db.FeatureConfig("meican", False, True, {}, {}))
    db.fdata_set(u.id, "meican", "__config_saved__", True)
    seen = {}

    def fake_do_order(p, c, t, d, dry_run):
        seen["dr"] = dry_run
        return jobs.JobResult(True, "ok")

    monkeypatch.setattr(jobs, "do_order", fake_do_order)
    asyncio.run(MeicanFeature().do_action(u.id, "order_dish", {"dish": DISH}))
    assert seen["dr"] is False


def test_order_dish_records_run(monkeypatch):
    u = db.create_user("u3", "H", "U3")
    _login(u.id)

    def fake_do_order(p, c, t, d, dry_run):
        return jobs.JobResult(True, "下单成功：烤鸭")

    monkeypatch.setattr(jobs, "do_order", fake_do_order)
    asyncio.run(MeicanFeature().do_action(u.id, "order_dish", {"dish": DISH}))
    runs = db.read_job_runs(u.id, "meican")
    assert any(r["job_id"] == "order" and "烤鸭" in r["message"] for r in runs)


def test_order_dish_requires_dish_id():
    u = db.create_user("u4", "H", "U4")
    _login(u.id)
    with pytest.raises(FeatureLoginError, match="缺少菜品"):
        asyncio.run(MeicanFeature().do_action(u.id, "order_dish", {"dish": {}}))


def test_order_dish_requires_login():
    u = db.create_user("u5", "H", "U5")
    with pytest.raises(FeatureLoginError, match="尚未登录"):
        asyncio.run(MeicanFeature().do_action(u.id, "order_dish", {"dish": DISH}))


def test_order_dish_rejects_bad_date():
    u = db.create_user("u6", "H", "U6")
    _login(u.id)
    with pytest.raises(FeatureLoginError, match="日期格式"):
        asyncio.run(MeicanFeature().do_action(
            u.id, "order_dish", {"dish": DISH, "date": "bad"}))


def test_unknown_action_rejected():
    u = db.create_user("u7", "H", "U7")
    _login(u.id)
    with pytest.raises(FeatureLoginError, match="不支持的操作"):
        asyncio.run(MeicanFeature().do_action(u.id, "nope", {}))
