import asyncio

import pytest

from app.core import db, registry, runner
from app.core.feature import (CredentialKind, Feature, FeatureLoginError,
                              JobContext, JobResult, JobSpec)


class FakeFeature(Feature):
    key = "fake"
    name = "假功能"
    credential_kind = CredentialKind.PASSWORD
    default_jobs = [JobSpec("j", "任务", "interval", {"minutes": 5})]

    def __init__(self):
        self.alive = True
        self.relogin_ok = True
        self.executed = 0

    async def login(self, user_id, payload):
        if payload.get("bad"):
            raise FeatureLoginError("凭据错误")
        return {"display": "ok"}

    async def check_alive(self, user_id):
        return self.alive

    async def run_job(self, user_id, job_id, ctx: JobContext):
        self.executed += 1
        return JobResult(True, f"done dry_run={ctx.dry_run}")

    async def relogin(self, user_id):
        return self.relogin_ok


class InteractiveFeature(FakeFeature):
    key = "inter"
    credential_kind = CredentialKind.INTERACTIVE


@pytest.fixture(autouse=True)
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr(db, "KEY_PATH", tmp_path / "t.key")
    db.init_db()
    registry.clear()
    yield
    registry.clear()


def _user():
    return db.create_user("u", "HASH", "U")


def test_register_and_get():
    f = FakeFeature()
    registry.register(f)
    assert registry.get("fake") is f
    assert registry.get("nope") is None
    assert len(registry.all()) == 1


def test_run_login_persists_credential():
    u = _user()
    registry.register(FakeFeature())
    result = runner.run_login(u.id, "fake", {"username": "ok"})
    assert result["display"] == "ok"
    assert db.load_credential(u.id, "fake").state == "active"


def test_run_login_failure_does_not_persist_active():
    u = _user()
    registry.register(FakeFeature())
    with pytest.raises(FeatureLoginError):
        runner.run_login(u.id, "fake", {"bad": True})
    assert db.load_credential(u.id, "fake").state == "not_configured"


def test_execute_skips_when_not_configured():
    u = _user()
    f = FakeFeature()
    registry.register(f)
    r = runner.execute(u.id, "fake", "j")
    assert r.ok is False
    assert "未配置" in r.message
    assert f.executed == 0


def test_execute_runs_when_active():
    u = _user()
    f = FakeFeature()
    registry.register(f)
    runner.run_login(u.id, "fake", {"username": "ok"})
    r = runner.execute(u.id, "fake", "j")
    assert r.ok is True
    assert f.executed == 1


def test_execute_passes_dry_run_from_config():
    u = _user()
    f = FakeFeature()
    registry.register(f)
    runner.run_login(u.id, "fake", {"username": "ok"})
    db.save_feature_config(u.id, "fake", db.FeatureConfig("fake", True, False, {}, {}))
    r = runner.execute(u.id, "fake", "j")
    assert "dry_run=False" in r.message


def test_execute_autorelogin_for_password_kind():
    u = _user()
    f = FakeFeature()
    registry.register(f)
    runner.run_login(u.id, "fake", {"username": "ok"})
    f.alive = False
    f.relogin_ok = True
    r = runner.execute(u.id, "fake", "j")
    assert r.ok is True
    assert f.executed == 1


def test_execute_marks_expired_when_relogin_fails():
    u = _user()
    f = FakeFeature()
    registry.register(f)
    runner.run_login(u.id, "fake", {"username": "ok"})
    f.alive = False
    f.relogin_ok = False
    r = runner.execute(u.id, "fake", "j")
    assert r.ok is False
    assert db.load_credential(u.id, "fake").state == "expired"
    assert f.executed == 0


def test_execute_interactive_kind_never_relogins():
    u = _user()
    f = InteractiveFeature()
    registry.register(f)
    runner.run_login(u.id, "inter", {"username": "ok"})
    f.alive = False
    r = runner.execute(u.id, "inter", "j")
    assert r.ok is False
    assert db.load_credential(u.id, "inter").state == "expired"


def test_execute_records_job_run():
    u = _user()
    f = FakeFeature()
    registry.register(f)
    runner.run_login(u.id, "fake", {"username": "ok"})
    runner.execute(u.id, "fake", "j")
    runs = db.read_job_runs(u.id, "fake")
    assert len(runs) == 1
    assert runs[0]["ok"] is True