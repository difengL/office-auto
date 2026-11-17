"""美餐长期凭据（remember cookie）。

这是「十几天不打开小程序也是登录态」的真正机制，与「token 一直续命」
无关。实测证据来自美餐前端 bundle（demeter-fe aa22780.js）：

  getTokenAndCookie()  → POST {V3_BASE}/oauth/token?remember=true
                         grant_type=password
                         meican_credential_type=mobile_verify_code
                         mobile / verifyCode / x=true
                       → 响应 Set-Cookie: remember=<长串>（约 1 年）

  getTokenUsingCookie() → POST {V3_BASE}/oauth/token
                          grant_type=password, meican_credential_type=cookie
                          Cookie: remember=<长串>
                        → 换出新的 access_token，不需要验证码

相比之下 accessToken 本身只活 1 小时（「最后一次使用后」计时），
所以长期免登录能力完全来自 remember cookie。
"""
import asyncio

import pytest

from app.core import db
from app.core.feature import CredentialKind, FeatureLoginError
from app.features.meican import feature as feat
from app.features.meican.client import MeicanClient, MeicanError, _extract_cookie
from app.features.meican.feature import MeicanFeature


class FakeResp:
    def __init__(self, payload, status=200, cookies=()):
        self._payload = payload
        self.status_code = status
        self.headers = _Headers(cookies)

    def json(self):
        return self._payload


class _Headers:
    def __init__(self, cookies):
        self._cookies = list(cookies)

    def get_list(self, name):
        return self._cookies if name.lower() == "set-cookie" else []


def test_extract_cookie_picks_the_right_one():
    r = FakeResp({}, cookies=[
        "machineId=abc; path=/",
        "remember=LONGLONGTOKEN; path=/; max-age=31536000",
        "guestId=xyz; path=/",
    ])
    assert _extract_cookie(r, "remember") == "LONGLONGTOKEN"


def test_extract_cookie_returns_empty_when_absent():
    r = FakeResp({}, cookies=["machineId=abc; path=/"])
    assert _extract_cookie(r, "remember") == ""


def test_extract_cookie_handles_no_cookies():
    r = FakeResp({}, cookies=[])
    assert _extract_cookie(r, "remember") == ""


class FakeHTTP:
    """模拟 oauth/token 的两种调用。"""

    def __init__(self, remember="", token_ok=True):
        self.remember = remember
        self.token_ok = token_ok
        self.calls: list[dict] = []

    def post(self, url, params=None, data=None, headers=None):
        self.calls.append({"url": url, "params": params or {},
                           "data": data or {}, "headers": headers or {}})
        if url.endswith("/accounts/sendVerifyCode"):
            return FakeResp({"resultCode": "OK", "resultDescription": "SUCCESS"}, 200)
        if not self.token_ok:
            return FakeResp({"error": "invalid_grant",
                             "error_description": "Refresh token is expired"}, 400,
                            cookies=["remember=%s; path=/" % self.remember])
        return FakeResp({"access_token": "NEW_AT", "refresh_token": "NEW_RT",
                         "token_type": "bearer"}, 200,
                        cookies=["remember=%s; path=/" % self.remember])


def _client(http):
    c = MeicanClient()
    c._client = http
    return c


def test_oauth_token_with_code_sends_remember_param():
    http = FakeHTTP(remember="RM1")
    c = _client(http)
    got = c.oauth_token_with_code("17600610776", "1234")

    call = http.calls[0]
    assert call["url"].endswith("/oauth/token")
    assert call["params"].get("remember") == "true"
    assert call["data"]["grant_type"] == "password"
    assert call["data"]["meican_credential_type"] == "mobile_verify_code"
    assert call["data"]["mobile"] == "17600610776"
    assert call["data"]["verifyCode"] == "1234"
    assert got["remember"] == "RM1"


def test_oauth_token_with_remember_sends_cookie_header():
    http = FakeHTTP()
    c = _client(http)
    data = c.oauth_token_with_remember("RM1")

    call = http.calls[0]
    assert call["data"] == {"grant_type": "password",
                            "meican_credential_type": "cookie"}
    assert "remember=RM1" in call["headers"]["Cookie"]
    assert data["access_token"] == "NEW_AT"


def test_oauth_token_with_remember_raises_when_dead():
    http = FakeHTTP(token_ok=False)
    c = _client(http)
    with pytest.raises(MeicanError, match="expired"):
        c.oauth_token_with_remember("OLD")


def test_meican_declares_can_relogin():
    assert MeicanFeature.can_relogin is True
    assert MeicanFeature.credential_kind is CredentialKind.INTERACTIVE


# ---------- 插件层：relogin 与保活 ----------

@pytest.fixture(autouse=True)
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr(db, "KEY_PATH", tmp_path / "t.key")
    db.init_db()
    yield


class StubClient:
    def __init__(self, *a, **kw):
        pass

    def oauth_token_with_remember(self, remember):
        if remember != "GOOD":
            raise MeicanError("长期凭据已失效")
        return {"access_token": "FRESH_AT", "refresh_token": "FRESH_RT"}


def test_relogin_uses_remember_and_updates_token(monkeypatch):
    monkeypatch.setattr(feat, "MeicanClient", StubClient)
    u = db.create_user("u1", "H", "U1")
    db.save_credential(u.id, "meican",
                       {"phone": "1", "accessToken": "OLD", "remember": "GOOD"}, "active")

    ok = asyncio.run(MeicanFeature().relogin(u.id))

    assert ok is True
    p = db.load_credential(u.id, "meican").payload
    assert p["accessToken"] == "FRESH_AT"
    assert db.load_credential(u.id, "meican").state == "active"


def test_relogin_without_remember_returns_false(monkeypatch):
    monkeypatch.setattr(feat, "MeicanClient", StubClient)
    u = db.create_user("u2", "H", "U2")
    db.save_credential(u.id, "meican", {"phone": "1", "accessToken": "OLD"}, "active")

    assert asyncio.run(MeicanFeature().relogin(u.id)) is False


def test_relogin_reports_dead_remember(monkeypatch):
    monkeypatch.setattr(feat, "MeicanClient", StubClient)
    u = db.create_user("u3", "H", "U3")
    db.save_credential(u.id, "meican",
                       {"phone": "1", "accessToken": "OLD", "remember": "BAD"}, "active")

    assert asyncio.run(MeicanFeature().relogin(u.id)) is False


# ---------- login 双通道与发码 ----------

def test_login_falls_back_to_graphql_when_v3_fails(monkeypatch):
    """v3 拿不到 remember 时，仍要能登录（走 GraphQL），只是没有长期凭据。"""
    class FallbackClient:
        def __init__(self, *a, **kw):
            pass

        def oauth_token_with_code(self, phone, code):
            raise MeicanError("验证码无效")

        def login_with_sms(self, phone, code):
            return {"ticket": "T", "signature": "S",
                    "userList": [{"accountVersion": "AccountThreeDotZero",
                                  "snowflakeId": "SF", "name": "李四"}]}

        def choose_account(self, ticket, signature, snowflake_id=None):
            return {"accessToken": "GQL_AT", "refreshToken": "GQL_RT"}

        def account(self):
            return {"snowflakeId": "SF", "name": "李四"}

    monkeypatch.setattr(feat, "MeicanClient", FallbackClient)
    u = db.create_user("u4", "H", "U4")
    payload = {"phone": "176", "code": "1234"}
    info = asyncio.run(MeicanFeature().login(u.id, payload))

    assert info["display"] == "李四"
    assert info["long_term"] is False
    assert payload["accessToken"] == "GQL_AT"
    assert payload["remember"] == ""
    assert "code" not in payload


def test_send_sms_code_v3_uses_mobileNumber_field():
    http = FakeHTTP()
    c = _client(http)
    c.send_sms_code_v3("17600610776")
    call = http.calls[0]
    assert call["url"].endswith("/accounts/sendVerifyCode")
    assert call["data"] == {"mobileNumber": "17600610776"}


def test_send_sms_code_v3_reports_throttle():
    class ThrottleHTTP(FakeHTTP):
        def post(self, url, params=None, data=None, headers=None):
            return FakeResp({"resultCode": "FAIL", "data": {"left": 44}}, 200)

    c = _client(ThrottleHTTP())
    with pytest.raises(MeicanError, match="44 秒"):
        c.send_sms_code_v3("17600610776")
