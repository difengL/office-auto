"""refresh_options 必须先用已存凭据登录，否则拿到的是登录页 HTML。

回归背景：功能页「刷新」按钮曾报
「拉取项目失败：/api/localproject/list 返回非 JSON（登录态可能失效）」，
根因是 refresh_options 直接调 list_projects() 而跳过了 login()。
"""
import asyncio

import pytest

from app.core import db
from app.features.timesheet import feature as feat
from app.features.timesheet.feature import TimesheetFeature


@pytest.fixture(autouse=True)
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr(db, "KEY_PATH", tmp_path / "t.key")
    db.init_db()
    yield


class FakeClient:
    """记录调用顺序，模拟 HEM：未登录时拉项目会拿到非 JSON。"""

    instances: list["FakeClient"] = []

    def __init__(self, username, password, timeout=25.0):
        self.username = username
        self.password = password
        self.logged_in = False
        self.closed = False
        self.calls: list[str] = []
        FakeClient.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self):
        self.closed = True

    def login(self):
        self.calls.append("login")
        self.logged_in = True
        return {"uid": "U1", "username": self.username, "name": "测试用户"}

    def list_projects(self):
        self.calls.append("list_projects")
        if not self.logged_in:
            raise feat.HemError("/api/localproject/list 返回非 JSON（登录态可能失效）")
        return [{"project_id": "6001", "name": "百察智能体项目"},
                {"project_id": "7051", "name": "百灵-百回催收系统"}]


@pytest.fixture
def fake(monkeypatch):
    FakeClient.instances = []
    monkeypatch.setattr(feat, "HemClient", FakeClient)
    return FakeClient


def _save_cred(user_id: int) -> None:
    db.save_credential(user_id, "timesheet",
                       {"username": "yao.liu", "password": "pw"}, "active")


def test_refresh_logs_in_before_listing_projects(fake):
    u = db.create_user("u1", "HASH", "U1")
    _save_cred(u.id)

    items = asyncio.run(TimesheetFeature().refresh_options(u.id))

    assert len(fake.instances) == 1
    client = fake.instances[0]
    # 顺序必须先是 login，再是 list_projects
    assert client.calls == ["login", "list_projects"]
    assert [x["value"] for x in items] == ["6001", "7051"]


def test_refresh_returns_value_label_pairs(fake):
    u = db.create_user("u2", "HASH", "U2")
    _save_cred(u.id)

    items = asyncio.run(TimesheetFeature().refresh_options(u.id))

    assert items[0] == {"value": "6001", "label": "百察智能体项目"}


def test_refresh_closes_client(fake):
    u = db.create_user("u3", "HASH", "U3")
    _save_cred(u.id)

    asyncio.run(TimesheetFeature().refresh_options(u.id))

    assert fake.instances[0].closed is True


def test_refresh_caches_projects(fake):
    u = db.create_user("u4", "HASH", "U4")
    _save_cred(u.id)

    asyncio.run(TimesheetFeature().refresh_options(u.id))

    cached = db.fdata_get(u.id, "timesheet", "projects", [])
    assert [x["project_id"] for x in cached] == ["6001", "7051"]


def test_refresh_raises_login_error_on_failure(fake, monkeypatch):
    u = db.create_user("u5", "HASH", "U5")
    _save_cred(u.id)

    def boom(self):
        raise feat.HemError("账号或密码错误")

    monkeypatch.setattr(FakeClient, "login", boom)

    from app.core.feature import FeatureLoginError
    with pytest.raises(FeatureLoginError) as ei:
        asyncio.run(TimesheetFeature().refresh_options(u.id))
    assert "拉取项目失败" in str(ei.value)


def test_refresh_without_credential_raises(fake):
    u = db.create_user("u6", "HASH", "U6")

    from app.core.feature import FeatureLoginError
    with pytest.raises(FeatureLoginError):
        asyncio.run(TimesheetFeature().refresh_options(u.id))
