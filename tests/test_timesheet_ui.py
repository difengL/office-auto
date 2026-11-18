"""HEM 页面的两个界面调整：

1. 去掉「立即执行」——提交周报只在周五做才有意义，给个按钮容易误触发。
2. 定时显示「每周五 HH:MM」，默认 20:00；老库里存的 17:30 要自动迁过来。
"""
import pytest

from app.core import db, registry, scheduler
from app.core.feature import CredentialKind, Feature, JobResult, JobSpec


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr(db, "KEY_PATH", tmp_path / "t.key")
    db.init_db()
    scheduler.scheduler.remove_all_jobs()
    yield
    scheduler.scheduler.remove_all_jobs()


# ---------- 星期文案 ----------

def test_day_label_single_day():
    assert scheduler.day_label("fri") == "每周五"
    assert scheduler.day_label("mon") == "每周一"


def test_day_label_common_forms():
    assert scheduler.day_label("mon-fri") == "工作日"
    assert scheduler.day_label("*") == "每天"
    assert scheduler.day_label("mon,wed,fri") == "每周一、周三、周五"
    assert scheduler.day_label("sat-sun") == "每周六至周日"


def test_day_label_unknown_passthrough():
    """认不出的写法原样返回，不能猜。"""
    assert scheduler.day_label("2") == "每 2"
    assert scheduler.day_label(None) == ""


# ---------- 插件声明 ----------

def _ts():
    from app.features.timesheet.feature import TimesheetFeature
    return TimesheetFeature()


def test_timesheet_disables_manual_run():
    assert _ts().manual_run is False


def test_timesheet_default_is_friday_2000():
    spec = next(j for j in _ts().default_jobs if j.job_id == "weekly_submit")
    assert spec.default["day_of_week"] == "fri"
    assert spec.default["hour"] == 20
    assert spec.default["minute"] == 0


def test_meican_still_allows_manual_run():
    from app.features.meican.feature import MeicanFeature
    assert MeicanFeature().manual_run is True


# ---------- 旧配置迁移 ----------

def test_migrates_legacy_1730_to_2000():
    f = _ts()
    user = db.create_user("u", "H", "U")
    db.save_feature_config(user.id, "timesheet", db.FeatureConfig(
        "timesheet", True, False, {"weekly_submit": {"hour": 17, "minute": 30}}, {}))

    cfg = __import__("app.core.runner", fromlist=["runner"]).load_config(user.id, f)
    assert cfg.schedule["weekly_submit"] == {"hour": 20, "minute": 0}
    # 迁移要落库，不能每次读都重算
    assert db.load_feature_config(user.id, "timesheet").schedule["weekly_submit"]["hour"] == 20


def test_keeps_user_custom_time():
    """用户自己改过的时刻不能动。"""
    f = _ts()
    user = db.create_user("u", "H", "U")
    db.save_feature_config(user.id, "timesheet", db.FeatureConfig(
        "timesheet", True, False, {"weekly_submit": {"hour": 21, "minute": 15}}, {}))

    from app.core import runner
    cfg = runner.load_config(user.id, f)
    assert cfg.schedule["weekly_submit"] == {"hour": 21, "minute": 15}


def test_no_schedule_is_untouched():
    f = _ts()
    user = db.create_user("u", "H", "U")
    from app.core import runner
    cfg = runner.load_config(user.id, f)
    assert cfg.schedule.get("weekly_submit") is None
    # 未配置时调度器用 default → 周五 20:00
    assert f.default_jobs[0].default["hour"] == 20


def test_migration_makes_scheduler_use_new_time():
    """迁移后实际排期必须是周五 20:00 之后。"""
    f = _ts()
    registry.clear()
    registry.register(f)
    user = db.create_user("u", "H", "U")
    db.save_feature_config(user.id, "timesheet", db.FeatureConfig(
        "timesheet", True, False, {"weekly_submit": {"hour": 17, "minute": 30}}, {}))
    scheduler.reschedule_user(user.id)

    job = scheduler.scheduler.get_job(f"timesheet:weekly_submit:{user.id}")
    assert job is not None
    fields = {fld.name: str(fld) for fld in job.trigger.fields}
    assert fields["day_of_week"] == "fri"
    assert fields["hour"] == "20"
    assert fields["minute"] == "0"
    registry.clear()


# ---------- 手动执行入口 ----------

@pytest.fixture
def client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr(db, "KEY_PATH", tmp_path / "t.key")
    db.init_db()
    registry.clear()
    registry.register(_ts())
    from app import main
    with TestClient(main.app) as c:
        scheduler.scheduler.remove_all_jobs()
        yield c
    registry.clear()


def test_detail_exposes_manual_run_flag(client):
    client.post("/api/register", json={"username": "h1", "password": "pw123456"})
    d = client.get("/api/features/timesheet").json()
    assert d["manual_run"] is False
    job = next(j for j in d["jobs"] if j["job_id"] == "weekly_submit")
    assert job["day_label"] == "每周五"


def test_run_endpoint_blocked_when_manual_run_disabled(client):
    """界面去掉了按钮，接口也必须拒绝，否则只是视觉上的隐藏。"""
    client.post("/api/register", json={"username": "h2", "password": "pw123456"})
    r = client.post("/api/features/timesheet/run", json={"job_id": "weekly_submit"})
    assert r.status_code == 403
    assert "手动执行" in r.json()["detail"]


def test_list_endpoint_day_label_uses_merged_schedule(client):
    """用户改过时刻后，星期仍来自 default（前端不该被覆盖成空）。"""
    client.post("/api/register", json={"username": "h3", "password": "pw123456"})
    client.post("/api/features/timesheet/config", json={
        "enabled": True, "dry_run": False,
        "schedule": {"weekly_submit": {"hour": 19, "minute": 45}}, "options": {}})
    d = client.get("/api/features/timesheet").json()
    job = next(j for j in d["jobs"] if j["job_id"] == "weekly_submit")
    assert job["day_label"] == "每周五"
    assert job["next_run"] is not None
