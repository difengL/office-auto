import pytest

from app.core import db
from app.core.feature import CredentialKind, FeatureLoginError, JobContext
from app.features.meican.feature import MeicanFeature


@pytest.fixture(autouse=True)
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr(db, "KEY_PATH", tmp_path / "t.key")
    db.init_db()
    yield


def test_metadata():
    f = MeicanFeature()
    assert f.key == "meican"
    assert f.credential_kind is CredentialKind.INTERACTIVE
    assert {x.name for x in f.credential_fields} == {"phone", "code"}
    ids = {j.job_id for j in f.default_jobs}
    assert ids == {"order", "keepalive"}
    keepalive = next(j for j in f.default_jobs if j.job_id == "keepalive")
    assert keepalive.editable is False


def test_login_requires_phone_and_code(monkeypatch):
    import asyncio
    f = MeicanFeature()
    with pytest.raises(FeatureLoginError):
        asyncio.run(f.login(1, {"phone": ""}))
    with pytest.raises(FeatureLoginError):
        asyncio.run(f.login(1, {"phone": "176", "code": ""}))


def test_login_strips_code_and_keeps_token(monkeypatch):
    import asyncio

    from app.features.meican import feature as feat

    class FakeClient:
        def __init__(self, access_token=None, timeout=25.0):
            self.access_token = access_token

        def account(self):
            return {"snowflakeId": "SF1", "name": "张三"}

        def login_with_sms(self, phone, code):
            return {"ticket": "T", "signature": "S",
                    "userList": [{"accountVersion": "AccountThreeDotZero",
                                  "snowflakeId": "SF1", "name": "张三"}]}

        def choose_account(self, ticket, signature, snowflake_id=None):
            return {"accessToken": "AT", "refreshToken": "RT", "tokenType": "bearer"}

    monkeypatch.setattr(feat, "MeicanClient", FakeClient)
    u = db.create_user("mc", "HASH", "MC")
    f = MeicanFeature()
    payload = {"phone": "17600000000", "code": "123456"}
    info = asyncio.run(f.login(u.id, payload))
    assert info["display"] == "张三"
    assert payload["accessToken"] == "AT"
    assert "code" not in payload
    db.save_credential(u.id, "meican", payload, "active")
    assert db.load_credential(u.id, "meican").payload.get("code") is None


def test_run_job_keepalive_uses_jobs(monkeypatch):
    import asyncio

    from app.features.meican import feature as feat
    u = db.create_user("mc2", "HASH", "MC")
    db.save_credential(u.id, "meican", {"accessToken": "AT"}, "active")
    calls = []
    monkeypatch.setattr(feat.jobs, "keepalive", lambda payload: calls.append(payload) or True)
    f = MeicanFeature()
    ctx = JobContext(u.id, "meican", "keepalive", True, {})
    r = asyncio.run(f.run_job(u.id, "keepalive", ctx))
    assert r.ok is True
    assert calls[0]["accessToken"] == "AT"