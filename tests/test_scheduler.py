import pytest

from app.core import db, registry, scheduler
from app.core.feature import CredentialKind, Feature, JobResult, JobSpec


class SchedFeature(Feature):
    key = "sched"
    name = "调度测试"
    credential_kind = CredentialKind.PASSWORD
    default_jobs = [
        JobSpec("daily", "每日", "cron", {"hour": 9, "minute": 15}),
        JobSpec("tick", "间隔", "interval", {"minutes": 30}, jitter=3, editable=False),
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
    registry.register(SchedFeature())
    yield
    for job in scheduler.scheduler.get_jobs():
        job.remove()
    registry.clear()


def test_build_job_id():
    assert scheduler.job_id("sched", "daily", 7) == "sched:daily:7"


def test_reschedule_creates_jobs():
    u = db.create_user("u", "H", "U")
    db.save_feature_config(u.id, "sched",
                           db.FeatureConfig("sched", True, True, {}, {}))
    db.issue_session(u.id)
    scheduler.reschedule_user(u.id)
    ids = {j.id for j in scheduler.scheduler.get_jobs()}
    assert "sched:daily:1" in ids
    assert "sched:tick:1" in ids


def test_disabled_feature_has_no_jobs():
    u = db.create_user("u", "H", "U")
    db.save_feature_config(u.id, "sched",
                           db.FeatureConfig("sched", False, True, {}, {}))
    scheduler.reschedule_user(u.id)
    assert scheduler.scheduler.get_jobs() == []


def test_user_schedule_override_applied():
    u = db.create_user("u", "H", "U")
    cfg = db.FeatureConfig("sched", True, True, {"daily": {"hour": 7, "minute": 5}}, {})
    db.save_feature_config(u.id, "sched", cfg)
    scheduler.reschedule_user(u.id)
    job = scheduler.scheduler.get_job(f"sched:daily:{u.id}")
    assert job is not None
    trigger = str(job.trigger)
    assert "hour='7'" in trigger
    assert "minute='5'" in trigger


def test_interval_job_created_with_jitter():
    from datetime import timedelta
    u = db.create_user("u", "H", "U")
    db.save_feature_config(u.id, "sched",
                           db.FeatureConfig("sched", True, True, {}, {}))
    scheduler.reschedule_user(u.id)
    job = scheduler.scheduler.get_job(f"sched:tick:{u.id}")
    assert job is not None
    # IntervalTrigger 的 interval 就是 30 分钟基准
    assert job.trigger.interval == timedelta(minutes=30)
    # jitter 交给 APScheduler 每轮执行时随机，不在建任务时固定；单位是秒
    assert job.trigger.jitter == 3