"""查询视图遇到 token 失效应自动用长期凭据续期重试，而非直接报错。

回归背景：用户打开今日订餐报「查询失败：token 已失效」。保活任务能
用 remember 续期，但查询视图（status/recommend/candidates/fruit）此前
直接抛错。应在 401 时自动续期并重试一次。
"""
import asyncio
from datetime import date

import pytest

from app.core import db
from app.core.feature import FeatureLoginError
from app.features.meican import feature as feat
from app.features.meican import jobs
from app.features.meican.client import MeicanError
from app.features.meican.feature import MeicanFeature


@pytest.fixture(autouse=True)
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr(db, "KEY_PATH", tmp_path / "t.key")
    db.init_db()
    yield


def _login(user_id, remember="RM"):
    db.save_credential(user_id, "meican",
                       {"phone": "1", "accessToken": "OLD", "remember": remember}, "active")


def test_view_relogins_on_401_and_retries(monkeypatch):
    u = db.create_user("u1", "H", "U1")
    _login(u.id)
    calls = {"n": 0}

    def fake_status(payload, target):
        calls["n"] += 1
        if payload["accessToken"] == "OLD":
            raise MeicanError("token 已失效", 401)
        return {"date": target.isoformat(), "has_item": True, "ordered": False}

    monkeypatch.setattr(jobs, "check_status", fake_status)

    class RenewClient:
        def __init__(self, *a, **kw):
            pass

        def oauth_token_with_remember(self, remember):
            return {"access_token": "NEW", "refresh_token": "NRT"}

    monkeypatch.setattr(feat, "MeicanClient", RenewClient)

    data = asyncio.run(MeicanFeature().get_view(u.id, "status", {}))
    assert data["has_item"] is True
    assert calls["n"] == 2                       # 失败一次 + 续期后重试一次
    # 新 token 已落库
    assert db.load_credential(u.id, "meican").payload["accessToken"] == "NEW"


def test_view_no_remember_reports_error(monkeypatch):
    u = db.create_user("u2", "H", "U2")
    _login(u.id, remember="")                    # 无长期凭据
    monkeypatch.setattr(jobs, "check_status",
                        lambda p, t: (_ for _ in ()).throw(MeicanError("token 已失效", 401)))
    with pytest.raises(FeatureLoginError, match="登录"):
        asyncio.run(MeicanFeature().get_view(u.id, "status", {}))


def test_view_relogin_failure_reports_error(monkeypatch):
    u = db.create_user("u3", "H", "U3")
    _login(u.id)
    monkeypatch.setattr(jobs, "check_status",
                        lambda p, t: (_ for _ in ()).throw(MeicanError("token 已失效", 401)))

    class DeadClient:
        def __init__(self, *a, **kw):
            pass

        def oauth_token_with_remember(self, remember):
            raise MeicanError("长期凭据已失效")

    monkeypatch.setattr(feat, "MeicanClient", DeadClient)
    with pytest.raises(FeatureLoginError):
        asyncio.run(MeicanFeature().get_view(u.id, "status", {}))


def test_view_non_401_error_not_retried(monkeypatch):
    u = db.create_user("u4", "H", "U4")
    _login(u.id)
    calls = {"n": 0}

    def fake_status(payload, target):
        calls["n"] += 1
        raise MeicanError("服务器内部错误", 500)

    monkeypatch.setattr(jobs, "check_status", fake_status)
    with pytest.raises(FeatureLoginError, match="查询失败"):
        asyncio.run(MeicanFeature().get_view(u.id, "status", {}))
    assert calls["n"] == 1                       # 非 401 不续期重试


def test_view_success_no_relogin(monkeypatch):
    u = db.create_user("u5", "H", "U5")
    _login(u.id)
    monkeypatch.setattr(jobs, "check_status",
                        lambda p, t: {"has_item": True, "ordered": False})
    renewed = {"n": 0}

    class NoRenew:
        def __init__(self, *a, **kw):
            pass

        def oauth_token_with_remember(self, remember):
            renewed["n"] += 1
            return {"access_token": "X"}

    monkeypatch.setattr(feat, "MeicanClient", NoRenew)
    asyncio.run(MeicanFeature().get_view(u.id, "status", {}))
    assert renewed["n"] == 0                     # 成功就不续期
