"""HEM 登录保活。

HEM 是密码型凭据，掉线能自动重登，所以保活不是为了「省去重新登录」，
而是为了提前发现凭据失效——每周五才跑一次的周报任务如果那天才发现
密码错了，这一周的工时就已经漏了。

保活必须常开（always_on），且不产生任何提交动作。
"""
import asyncio
from datetime import date

import pytest

from app.core import db
from app.core.feature import JobContext
from app.features.timesheet import feature as feat
from app.features.timesheet.feature import TimesheetFeature


@pytest.fixture(autouse=True)
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr(db, "KEY_PATH", tmp_path / "t.key")
    db.init_db()
    yield


def test_keepalive_job_is_always_on():
    jobs = {j.job_id: j for j in TimesheetFeature.default_jobs}
    keep = jobs["keepalive"]
    assert keep.always_on is True
    assert keep.editable is False
    assert keep.trigger == "interval"


def test_keepalive_interval_is_daily():
    keep = next(j for j in TimesheetFeature.default_jobs if j.job_id == "keepalive")
    # 每天一次足够提前发现密码问题，又不会给 HEM 造成压力
    total = keep.default.get("hours", 0) + keep.default.get("minutes", 0) / 60
    assert total == pytest.approx(24, abs=0.01)


class FakeClient:
    instances: list["FakeClient"] = []
    fail = False

    def __init__(self, username, password, timeout=25.0):
        self.username = username
        self.password = password
        FakeClient.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def login(self):
        if FakeClient.fail:
            raise feat.HemError("账号或密码错误")
        return {"uid": "U1", "username": self.username, "name": "测试"}

    def list_projects(self):
        return [{"project_id": "6001", "sub_project_id": "17560", "name": "项目A"}]

    def week_records(self, start, end):
        return []

    def submit(self, week, weekly_type=1):
        raise AssertionError("保活绝不能触发提交")


@pytest.fixture
def fake(monkeypatch):
    FakeClient.instances = []
    FakeClient.fail = False
    monkeypatch.setattr(feat, "HemClient", FakeClient)
    return FakeClient


def _cred(user_id):
    db.save_credential(user_id, "timesheet",
                       {"username": "u", "password": "p"}, "active")


def test_run_keepalive_succeeds(fake):
    u = db.create_user("u1", "H", "U1")
    _cred(u.id)
    ctx = JobContext(u.id, "timesheet", "keepalive", True, {})

    result = asyncio.run(TimesheetFeature().run_job(u.id, "keepalive", ctx))

    assert result.ok is True
    assert "正常" in result.message or "成功" in result.message
    assert fake.instances, "应该实际登录了一次"


def test_run_keepalive_reports_bad_password(fake):
    u = db.create_user("u2", "H", "U2")
    _cred(u.id)
    fake.fail = True
    ctx = JobContext(u.id, "timesheet", "keepalive", True, {})

    result = asyncio.run(TimesheetFeature().run_job(u.id, "keepalive", ctx))

    assert result.ok is False
    assert "密码" in result.message or "登录" in result.message


def test_keepalive_never_submits(fake):
    """保活不允许有任何写操作，FakeClient.submit 会直接抛断言。"""
    u = db.create_user("u3", "H", "U3")
    _cred(u.id)
    ctx = JobContext(u.id, "timesheet", "keepalive", False, {})   # 即使非 dry-run

    result = asyncio.run(TimesheetFeature().run_job(u.id, "keepalive", ctx))

    assert result.ok is True


def test_keepalive_without_credential(fake):
    u = db.create_user("u4", "H", "U4")
    ctx = JobContext(u.id, "timesheet", "keepalive", True, {})

    result = asyncio.run(TimesheetFeature().run_job(u.id, "keepalive", ctx))

    assert result.ok is False
    assert "凭据" in result.message
