"""发短信验证码动作。

回归背景：功能页只给了「短信验证码」输入框，却没有发码入口，
用户根本拿不到验证码，无法完成美餐登录。
"""
import asyncio

import pytest

from app.core import db
from app.core.feature import FeatureLoginError
from app.features.meican import feature as feat
from app.features.meican.feature import MeicanFeature


@pytest.fixture(autouse=True)
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr(db, "KEY_PATH", tmp_path / "t.key")
    db.init_db()
    yield


class FakeClient:
    instances: list["FakeClient"] = []
    fail = False

    def __init__(self, access_token=None, timeout=25.0):
        self.sent_to: list[str] = []
        FakeClient.instances.append(self)

    def send_sms_code(self, phone):
        if FakeClient.fail:
            raise feat.MeicanError("接口拒绝")
        self.sent_to.append(phone)


@pytest.fixture
def fake(monkeypatch):
    FakeClient.instances = []
    FakeClient.fail = False
    monkeypatch.setattr(feat, "MeicanClient", FakeClient)
    return FakeClient


def test_code_field_declares_send_action():
    f = MeicanFeature()
    code = next(x for x in f.credential_fields if x.name == "code")
    assert code.action == "send_sms"
    assert code.action_label == "发送验证码"


def test_phone_field_has_no_action():
    f = MeicanFeature()
    phone = next(x for x in f.credential_fields if x.name == "phone")
    assert phone.action == ""


def test_send_sms_calls_client_with_phone(fake):
    u = db.create_user("u1", "HASH", "U1")
    msg = asyncio.run(MeicanFeature().send_action(u.id, "send_sms", {"phone": "17600610776"}))

    assert fake.instances[0].sent_to == ["17600610776"]
    assert "验证码已发送" in msg


def test_send_sms_requires_phone(fake):
    u = db.create_user("u2", "HASH", "U2")
    with pytest.raises(FeatureLoginError, match="请先填写手机号"):
        asyncio.run(MeicanFeature().send_action(u.id, "send_sms", {"phone": "  "}))


def test_send_sms_wraps_client_error(fake):
    u = db.create_user("u3", "HASH", "U3")
    fake.fail = True
    with pytest.raises(FeatureLoginError, match="发送验证码失败"):
        asyncio.run(MeicanFeature().send_action(u.id, "send_sms", {"phone": "17600610776"}))


def test_send_sms_warns_about_rate_limit(fake):
    u = db.create_user("u4", "HASH", "U4")
    msg = asyncio.run(MeicanFeature().send_action(u.id, "send_sms", {"phone": "17600610776"}))
    # 美餐发码静默限流，必须提示用户，否则会以为程序坏了
    assert "限流" in msg


def test_unknown_action_rejected(fake):
    u = db.create_user("u5", "HASH", "U5")
    with pytest.raises(FeatureLoginError, match="不支持的操作"):
        asyncio.run(MeicanFeature().send_action(u.id, "reboot", {}))


def test_timesheet_does_not_support_action(tmp_path, monkeypatch):
    """密码型功能没有发码动作，应明确拒绝而不是静默成功。"""
    from app.features.timesheet.feature import TimesheetFeature
    u = db.create_user("u6", "HASH", "U6")
    with pytest.raises(FeatureLoginError):
        asyncio.run(TimesheetFeature().send_action(u.id, "send_sms", {}))
