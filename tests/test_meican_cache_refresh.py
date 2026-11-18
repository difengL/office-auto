"""美餐菜单缓存每 5 分钟自动刷新。

问题：当天可订菜品走 6 小时全局缓存，而剩余份数（availableDishCount）
会随别人下单实时变化。缓存久了，界面上的「可订/已订满」会与手机端不符。

方案：功能级共享任务，每 5 分钟强制重拉一次菜单与份数。该任务不挂在
任何用户名下——缓存是全企业共享的，绑在某个用户上会导致他登出后
所有人的状态都开始过期。
"""
import time
from datetime import date

import pytest

from app.core import db, registry, scheduler
from app.core.feature import CredentialKind, Feature, JobResult
from app.features.meican import jobs
from app.features.meican.client import MeicanError
from app.features.meican.feature import MeicanFeature


@pytest.fixture(autouse=True)
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr(db, "KEY_PATH", tmp_path / "t.key")
    db.init_db()
    scheduler.scheduler.remove_all_jobs()
    registry.clear()
    yield
    scheduler.scheduler.remove_all_jobs()
    registry.clear()


ITEM = {"userTab": {"uniqueId": "T1", "corp": {"namespace": "NS1"}},
        "targetTime": 1794902400000, "title": "晚餐"}


class FakeClient:
    """按调用次序返回不同的剩余份数，模拟别人在陆续下单。"""
    instances: list["FakeClient"] = []

    def __init__(self, availances=(56, 20, 0), fail=False):
        self.availances = list(availances)
        self.fail = fail
        self.menu_calls = 0
        FakeClient.instances.append(self)

    def calendar_items(self, start, end, with_order=True):
        return [{"date": start.isoformat(), "calendarItemList": [dict(ITEM)]}]

    def restaurants(self, tab, tt):
        if self.fail:
            raise MeicanError("token 失效", status=401)
        avail = self.availances[0] if len(self.availances) == 1 else self.availances.pop(0)
        return [{"uniqueId": "R1", "name": "金手勺", "open": True,
                 "availableDishCount": avail, "dishLimit": 60,
                 "restaurantStatus": 0 if avail else 2}]

    def restaurant_menu(self, rid, tab, tt):
        self.menu_calls += 1
        return {"dishList": [{"id": "D1", "name": "菜一", "priceInCent": 2500}]}


def _cred(user_id=None, feature="meican"):
    """建用户并保存有效凭据（凭据表有外键，用户必须先存在）。"""
    uid = user_id or db.create_user(f"u{db.count_users()}", "H", "U").id
    db.save_credential(uid, feature, {"accessToken": f"AT{uid}"}, "active")
    return uid


def _patch(fake):
    jobs._client = lambda p: fake


# ---------- 插件声明 ----------

def test_meican_declares_five_minute_refresh():
    assert MeicanFeature.shared_cache_interval == 300


def test_hem_has_no_shared_cache():
    from app.features.timesheet.feature import TimesheetFeature
    assert TimesheetFeature.shared_cache_interval == 0


# ---------- 强制刷新 ----------

def test_refresh_bypasses_ttl(monkeypatch):
    """缓存刚写过，refresh=True 仍必须打接口。"""
    fake = FakeClient(availances=(56, 8))
    monkeypatch.setattr(jobs, "_client", lambda p: fake)
    today = date.today()

    first, _ = jobs.get_candidates_cached({"accessToken": "AT"}, today)
    assert first[0].available_count == 56

    # 不刷新：命中缓存，份数不变
    again, _ = jobs.get_candidates_cached({"accessToken": "AT"}, today)
    assert again[0].available_count == 56

    # 强制刷新：拿到新份数
    fresh, _ = jobs.get_candidates_cached({"accessToken": "AT"}, today, refresh=True)
    assert fresh[0].available_count == 8


def test_refresh_updates_availability_flag(monkeypatch):
    """份数归零后必须变成不可订——这正是用户反馈的置灰不及时。"""
    fake = FakeClient(availances=(56, 0))
    monkeypatch.setattr(jobs, "_client", lambda p: fake)
    today = date.today()

    before, _ = jobs.get_candidates_cached({"accessToken": "AT"}, today)
    assert before[0].available is True

    after, _ = jobs.get_candidates_cached({"accessToken": "AT"}, today, refresh=True)
    assert after[0].available is False


def test_refresh_writes_cache(monkeypatch):
    fake = FakeClient(availances=(10,))
    monkeypatch.setattr(jobs, "_client", lambda p: fake)
    today = date.today()
    jobs.get_candidates_cached({"accessToken": "AT"}, today, refresh=True)

    cached = db.gdata_get(jobs._menu_cache_key(today, ITEM))
    assert cached["candidates"][0]["available_count"] == 10
    assert time.time() - cached["ts"] < 60


# ---------- 共享刷新的企业去重 ----------

def test_refresh_shared_cache_dedupes_same_corp(monkeypatch):
    """同企业多个用户只该拉一次菜单。"""
    fake = FakeClient(availances=(56,))
    monkeypatch.setattr("app.features.meican.feature.jobs._client", lambda p: fake)

    msg = MeicanFeature().refresh_shared_cache([{"accessToken": "A"},
                                               {"accessToken": "B"}])
    assert fake.menu_calls == 1
    assert "1 个企业" in msg


def test_refresh_shared_cache_without_payloads():
    assert "跳过" in MeicanFeature().refresh_shared_cache([])


def test_refresh_shared_cache_reports_failure(monkeypatch):
    fake = FakeClient(fail=True)
    monkeypatch.setattr("app.features.meican.feature.jobs._client", lambda p: fake)
    msg = MeicanFeature().refresh_shared_cache([{"accessToken": "A"}])
    assert "失败" in msg


def test_refresh_shared_cache_no_meal_today(monkeypatch):
    class NoMeal(FakeClient):
        def calendar_items(self, start, end, with_order=True):
            return [{"date": start.isoformat(), "calendarItemList": []}]

    monkeypatch.setattr("app.features.meican.feature.jobs._client",
                        lambda p: NoMeal())
    assert "没有可订餐次" in MeicanFeature().refresh_shared_cache([{"accessToken": "A"}])


# ---------- 调度器挂载 ----------

def test_shared_job_is_not_bound_to_any_user():
    registry.register(MeicanFeature())
    scheduler.reschedule_all()

    shared = [j for j in scheduler.scheduler.get_jobs() if j.id.startswith("shared:")]
    assert any(j.id == "shared:meican" for j in shared)
    job = scheduler.scheduler.get_job("shared:meican")
    assert job.trigger.interval.total_seconds() == 300
    # 不能带用户后缀，否则会被 reschedule_user 的删除逻辑误伤
    assert not job.id.endswith(":1")


def test_shared_job_survives_user_reschedule():
    registry.register(MeicanFeature())
    uid = _cred()
    scheduler.reschedule_all()
    scheduler.reschedule_user(uid)
    assert scheduler.scheduler.get_job("shared:meican") is not None


def test_no_shared_job_for_feature_without_cache():
    from app.features.timesheet.feature import TimesheetFeature
    registry.register(TimesheetFeature())
    scheduler.reschedule_all()
    assert scheduler.scheduler.get_job("shared:timesheet") is None


def test_shared_job_uses_active_credentials(monkeypatch):
    """刷新用所有有效凭据；失效的不参与（打不通接口）。"""
    seen = {}

    def fake_refresh(payloads):
        seen["payloads"] = payloads
        return "ok"

    class F(MeicanFeature):
        def refresh_shared_cache(self, payloads):
            return fake_refresh(payloads)

    F.shared_cache_interval = 300
    registry.register(F())
    uid = _cred()
    dead = db.create_user("dead", "H", "D").id
    db.save_credential(dead, "meican", {"accessToken": "DEAD"}, "expired")

    import asyncio
    asyncio.run(scheduler._run_shared_cache("meican"))

    assert len(seen["payloads"]) == 1
    assert seen["payloads"][0]["accessToken"] == f"AT{uid}"


def test_shared_job_silent_without_credentials():
    registry.register(MeicanFeature())
    import asyncio
    asyncio.run(scheduler._run_shared_cache("meican"))   # 不应抛异常


def test_shared_job_swallows_exceptions(monkeypatch):
    class Boom(MeicanFeature):
        def refresh_shared_cache(self, payloads):
            raise RuntimeError("炸了")

    registry.register(Boom())
    _cred()
    import asyncio
    asyncio.run(scheduler._run_shared_cache("meican"))   # 后台任务不能把调度器打挂


def test_shared_job_does_not_write_run_records(monkeypatch):
    """基础设施任务不该出现在用户的运行记录里。"""
    registry.register(MeicanFeature())
    uid = _cred()
    import asyncio
    asyncio.run(scheduler._run_shared_cache("meican"))
    assert db.read_job_runs(uid, "meican") == []
