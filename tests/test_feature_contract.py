import pytest

from app.core.feature import (CredentialKind, Feature, FeatureLoginError,
                              FieldSpec, JobContext, JobResult, JobSpec,
                              OptionSpec)


class DummyFeature(Feature):
    key = "dummy"
    name = "示例"
    description = "测试用"
    credential_kind = CredentialKind.PASSWORD
    credential_fields = [FieldSpec("username", "用户名")]
    default_jobs = [JobSpec("tick", "示例任务", "interval", {"minutes": 5})]

    async def login(self, user_id, payload):
        if payload.get("username") != "ok":
            raise FeatureLoginError("用户名不对")
        return {"display": "已登录"}

    async def check_alive(self, user_id):
        return True

    async def run_job(self, user_id, job_id, ctx):
        return JobResult(True, f"执行 {job_id}")


def test_feature_metadata():
    f = DummyFeature()
    assert f.key == "dummy"
    assert f.credential_kind is CredentialKind.PASSWORD
    assert f.dry_run_default is False
    assert f.extra_options == []


def test_cannot_instantiate_abstract():
    with pytest.raises(TypeError):
        Feature()


def test_jobspec_defaults():
    j = JobSpec("x", "任务", "cron", {"hour": 1})
    assert j.jitter == 0
    assert j.editable is True


def test_optionspec_defaults():
    o = OptionSpec("pid", "项目")
    assert o.type == "select"
    assert o.refreshable is False


def test_login_raises_typed_error():
    import asyncio
    f = DummyFeature()
    with pytest.raises(FeatureLoginError):
        asyncio.run(f.login(1, {"username": "bad"}))


def test_job_context_reads_and_writes(tmp_path, monkeypatch):
    from app.core import db
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr(db, "KEY_PATH", tmp_path / "t.key")
    db.init_db()
    u = db.create_user("ctxuser", "HASH", "U")      # feature_data 有外键，必须先建用户
    ctx = JobContext(u.id, "dummy", "tick", True, {"p": "1"})
    assert ctx.dry_run is True
    assert ctx.options["p"] == "1"
    assert ctx.get_data("k", default="d") == "d"
    ctx.set_data("k", {"v": 1})
    assert ctx.get_data("k")["v"] == 1