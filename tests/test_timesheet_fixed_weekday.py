"""HEM 定时：星期固定，用户只能改时刻。

需求原话：「每周五是个固定的，不用设置，只设置时间就行，时间默认 20:00」
"""
import pytest

from app.core import db, registry, scheduler
from app.features.timesheet.feature import TimesheetFeature


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr(db, "KEY_PATH", tmp_path / "t.key")
    db.init_db()
    scheduler.scheduler.remove_all_jobs()
    registry.clear()
    registry.register(TimesheetFeature())
    yield
    scheduler.scheduler.remove_all_jobs()
    registry.clear()


def test_weekly_job_marks_day_as_fixed():
    job = next(j for j in TimesheetFeature.default_jobs if j.job_id == "weekly_submit")
    assert "day_of_week" in job.fixed_fields
    assert job.default["day_of_week"] == "fri"
    assert job.default["hour"] == 20 and job.default["minute"] == 0


def test_user_cannot_override_weekday():
    """即使用户（或接口调用方）传了别的星期，排期仍是周五。"""
    u = db.create_user("u", "H", "U")
    cfg = db.FeatureConfig("timesheet", True, False,
                           {"weekly_submit": {"hour": 19, "minute": 30,
                                              "day_of_week": "wed"}}, {})
    db.save_feature_config(u.id, "timesheet", cfg)

    scheduler.reschedule_user(u.id)
    job = scheduler.scheduler.get_job(f"timesheet:weekly_submit:{u.id}")
    fields = {f.name: str(f) for f in job.trigger.fields}
    assert fields["day_of_week"] == "fri"
    assert fields["hour"] == "19"       # 时刻仍按用户设置
    assert fields["minute"] == "30"


def test_api_strips_fixed_field_from_saved_config():
    """接口保存时就把星期丢掉，库里不留脏数据。"""
    from fastapi.testclient import TestClient
    from app import main
    with TestClient(main.app) as c:
        scheduler.scheduler.remove_all_jobs()
        c.post("/api/register", json={"username": "h1", "password": "pw123456"})
        r = c.post("/api/features/timesheet/config", json={
            "enabled": True, "dry_run": False,
            "schedule": {"weekly_submit": {"hour": 21, "minute": 5,
                                           "day_of_week": "wed"}},
            "options": {}})
        assert r.status_code == 200
        assert r.json()["config"]["schedule"]["weekly_submit"] == {
            "hour": 21, "minute": 5}

        stored = db.load_feature_config(
            db.get_user_by_username("h1").id, "timesheet").schedule
        assert "day_of_week" not in stored["weekly_submit"]


def test_detail_reports_friday_even_with_dirty_stored_value():
    """库里若残留旧星期，界面仍按插件固定的周五展示。"""
    from fastapi.testclient import TestClient
    from app import main
    with TestClient(main.app) as c:
        scheduler.scheduler.remove_all_jobs()
        c.post("/api/register", json={"username": "h2", "password": "pw123456"})
        u = db.get_user_by_username("h2")
        db.save_feature_config(u.id, "timesheet", db.FeatureConfig(
            "timesheet", True, False, {"weekly_submit": {"hour": 20, "day_of_week": "wed"}}, {}))

        d = c.get("/api/features/timesheet").json()
        job = next(j for j in d["jobs"] if j["job_id"] == "weekly_submit")
        assert job["day_label"] == "每周五"


def test_legacy_day_of_week_cleaned_by_migration():
    """migrate_config 会移除旧的 day_of_week，后续保存不再累积。"""
    f = TimesheetFeature()
    cfg = db.FeatureConfig("timesheet", True, False,
                           {"weekly_submit": {"hour": 20, "day_of_week": "wed"}}, {})
    assert f.migrate_config(cfg) is True
    assert "day_of_week" not in cfg.schedule["weekly_submit"]
