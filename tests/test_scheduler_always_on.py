"""常开任务（always_on）不受「启用定时任务」开关影响。

背景：登录保活是基本功能——不保活，token 一小时就失效，用户就得反复
重新短信登录。它不应该是一个需要用户理解的开关。

订餐类任务仍需用户显式开启，所以不能直接把整个 enabled 开关删掉。
"""
import pytest

from app.core import db, registry, scheduler
from app.core.feature import CredentialKind, Feature, JobResult, JobSpec


class AlwaysOnFeature(Feature):
    key = "ao"
    name = "常开测试"
    credential_kind = CredentialKind.INTERACTIVE
    default_jobs = [
        JobSpec("work", "业务任务", "cron", {"hour": 10, "minute": 5}),
        JobSpec("keep", "保活", "interval", {"minutes": 30}, editable=False,
                always_on=True),
    ]

    async def login(self, user_id, payload):
        return {}

    async def check_alive(self, user_id):
        return True

    async def run_job(self, user_id, job_id, ctx):
        return JobResult(True, "ok")


@pytest.fixture(autouse=True)
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr(db, "KEY_PATH", tmp_path / "t.key")
    db.init_db()
    registry.clear()
    registry.register(AlwaysOnFeature())
    yield
    for job in scheduler.scheduler.get_jobs():
        job.remove()
    registry.clear()


def _enable(user_id, enabled, dry_run=True):
    db.save_feature_config(user_id, "ao",
                           db.FeatureConfig("ao", enabled, dry_run, {}, {}))


def test_keepalive_scheduled_even_when_feature_disabled():
    u = db.create_user("u", "H", "U")
    db.save_credential(u.id, "ao", {"phone": "1"}, "active")
    _enable(u.id, False)

    scheduler.reschedule_user(u.id)

    ids = {j.id for j in scheduler.scheduler.get_jobs()}
    assert f"ao:keep:{u.id}" in ids
    # 业务任务仍受开关控制，不应出现
    assert f"ao:work:{u.id}" not in ids


def test_both_scheduled_when_enabled():
    u = db.create_user("u", "H", "U")
    db.save_credential(u.id, "ao", {"phone": "1"}, "active")
    _enable(u.id, True)

    scheduler.reschedule_user(u.id)

    ids = {j.id for j in scheduler.scheduler.get_jobs()}
    assert f"ao:keep:{u.id}" in ids
    assert f"ao:work:{u.id}" in ids


def test_no_keepalive_without_credential():
    """没登录过就没必要保活，避免空跑产生一堆失败记录。"""
    u = db.create_user("u", "H", "U")
    _enable(u.id, False)

    scheduler.reschedule_user(u.id)

    ids = {j.id for j in scheduler.scheduler.get_jobs()}
    assert f"ao:keep:{u.id}" not in ids


def test_no_keepalive_when_credential_expired():
    """凭据已失效时保活无意义（交互型无法自动重登），不该空转。"""
    u = db.create_user("u", "H", "U")
    db.save_credential(u.id, "ao", {"phone": "1"}, "expired")
    _enable(u.id, False)

    scheduler.reschedule_user(u.id)

    ids = {j.id for j in scheduler.scheduler.get_jobs()}
    assert f"ao:keep:{u.id}" not in ids


def test_keepalive_created_for_user_without_any_config():
    """从没保存过设置的用户，只要登录了也该享受保活。"""
    u = db.create_user("u", "H", "U")
    db.save_credential(u.id, "ao", {"phone": "1"}, "active")

    scheduler.reschedule_all()

    ids = {j.id for j in scheduler.scheduler.get_jobs()}
    assert f"ao:keep:{u.id}" in ids
