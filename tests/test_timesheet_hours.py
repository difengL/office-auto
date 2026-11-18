"""HEM 每日工时设置。

需求：每天工时填多少展示在界面上，默认 8，用户可改；
改过的值在下周一 00:00 自动恢复为 8。
"""
from datetime import date

import pytest

from app.core import db
from app.features.timesheet import jobs


@pytest.fixture(autouse=True)
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr(db, "KEY_PATH", tmp_path / "t.key")
    db.init_db()
    yield


def _ts():
    from app.features.timesheet.feature import TimesheetFeature
    return TimesheetFeature()


# ---------- 归一化规则 ----------

def test_default_is_eight_for_every_weekday():
    hours, _ = jobs.normalize_week_hours(None, date(2026, 11, 16))
    assert hours == {"mon": 8.0, "tue": 8.0, "wed": 8.0, "thu": 8.0, "fri": 8.0}


def test_same_week_keeps_user_values():
    raw = {"week": "2026-11-16", "days": {"mon": 6, "fri": 6.5}}
    hours, dirty = jobs.normalize_week_hours(raw, date(2026, 11, 18))
    assert hours["mon"] == 6.0 and hours["fri"] == 6.5
    assert hours["tue"] == 8.0          # 没改的天仍是默认
    assert dirty is False


def test_new_week_resets_to_default():
    """上周改过的工时，跨周后必须回到 8。"""
    raw = {"week": "2026-11-09", "days": {"mon": 4, "tue": 4}}
    hours, dirty = jobs.normalize_week_hours(raw, date(2026, 11, 16))
    assert hours == {"mon": 8.0, "tue": 8.0, "wed": 8.0, "thu": 8.0, "fri": 8.0}
    assert dirty is True


def test_sunday_evening_still_current_week():
    """周日仍属于上周（周一起算），不能提前归零。"""
    raw = {"week": "2026-11-09", "days": {"mon": 3}}
    hours, dirty = jobs.normalize_week_hours(raw, date(2026, 11, 15))
    assert hours["mon"] == 3.0 and dirty is False


def test_monday_is_new_week():
    raw = {"week": "2026-11-09", "days": {"mon": 3}}
    hours, dirty = jobs.normalize_week_hours(raw, date(2026, 11, 16))
    assert hours["mon"] == 8.0 and dirty is True


@pytest.mark.parametrize("bad", [-1, 25, "abc", None, 99])
def test_invalid_hours_fall_back_to_default(bad):
    raw = {"week": "2026-11-16", "days": {"mon": bad}}
    hours, _ = jobs.normalize_week_hours(raw, date(2026, 11, 16))
    assert hours["mon"] == 8.0


def test_incoming_ignores_week_and_binds_now():
    """表单提交的是裸的 {周几: 小时}，直接绑定本周。"""
    hours, dirty = jobs.normalize_week_hours(
        {"mon": 7.5, "fri": 6}, date(2026, 11, 16), incoming=True)
    assert hours["mon"] == 7.5 and hours["fri"] == 6.0
    assert dirty is True


def test_week_hours_map_to_dates():
    days = [date(2026, 11, 16), date(2026, 11, 17)]
    got = jobs.week_hours_for_days({"mon": 6, "tue": 7}, days)
    assert got == {"2026-11-16": 6, "2026-11-17": 7}


# ---------- plan_week 用逐日工时 ----------

def test_plan_week_uses_per_day_hours():
    days = [date(2026, 11, 16), date(2026, 11, 17)]
    hours_by_day = {"2026-11-16": 6.0, "2026-11-17": 4.0}
    plan = jobs.plan_week([], days, hours_by_day)
    assert plan["total_hours"] == 10.0
    assert plan["hours_per_day"]["2026-11-17"] == 4.0


def test_plan_week_skips_filled_days_in_total():
    records = [{"content": [{"date": "2026-11-16", "task_time": 8.0}]}]
    days = [date(2026, 11, 16), date(2026, 11, 17)]
    plan = jobs.plan_week(records, days, {"2026-11-16": 8.0, "2026-11-17": 5.0})
    assert plan["total_hours"] == 5.0     # 已填的天不计入待填总量


# ---------- 插件读写 ----------

def test_get_settings_shape():
    u = db.create_user("u", "H", "U")
    s = _ts().get_settings(u.id)
    assert s["kind"] == "hours_by_day"
    assert s["default"] == 8.0
    assert [d["key"] for d in s["days"]] == ["mon", "tue", "wed", "thu", "fri"]
    assert all(d["hours"] == 8.0 for d in s["days"])
    assert len({d["date"] for d in s["days"]}) == 5


def test_save_settings_persists():
    u = db.create_user("u", "H", "U")
    f = _ts()
    saved = f.save_settings(u.id, {"days": {"mon": 6, "fri": 6.5}})
    assert {d["key"]: d["hours"] for d in saved["days"]}["mon"] == 6.0
    # 再读一次仍在
    again = f.get_settings(u.id)
    assert {d["key"]: d["hours"] for d in again["days"]}["fri"] == 6.5


def test_get_settings_resets_after_week_change(monkeypatch):
    """存储的是上周 → 读取时自动回到 8，并落库。"""
    u = db.create_user("u", "H", "U")
    f = _ts()
    f.save_week_hours(u.id, {"mon": 5}, date(2026, 11, 9))

    class FakeDate(date):
        @classmethod
        def today(cls):
            return date(2026, 11, 16)

    import app.features.timesheet.feature as feat
    monkeypatch.setattr(feat, "date", FakeDate)
    s = f.get_settings(u.id)
    assert all(d["hours"] == 8.0 for d in s["days"])

    stored = db.fdata_get(u.id, "timesheet", "week_hours")
    assert stored["week"] == "2026-11-16"
    assert stored["days"]["mon"] == 8.0


def test_save_settings_rejects_bad_payload():
    from app.core.feature import FeatureLoginError
    u = db.create_user("u", "H", "U")
    with pytest.raises(FeatureLoginError):
        _ts().save_settings(u.id, {"days": "oops"})


def test_run_weekly_submit_passes_user_hours(monkeypatch):
    """任务执行必须用界面设置的值，而不是写死的 8。"""
    from app.core.feature import JobContext
    from app.features.timesheet.feature import TimesheetFeature
    import asyncio

    seen = {}

    def fake_run(username, password, project_id, dry_run, today, week_hours=None):
        seen["hours"] = week_hours
        from app.core.feature import JobResult
        return JobResult(True, "ok")

    import app.features.timesheet.feature as feat
    monkeypatch.setattr(feat.jobs, "run_weekly_submit", fake_run)

    u = db.create_user("u", "H", "U")
    db.save_credential(u.id, "timesheet", {"username": "a", "password": "b"}, "active")
    f = TimesheetFeature()
    f.save_settings(u.id, {"days": {"mon": 7}})
    asyncio.run(f.run_job(u.id, "weekly_submit",
                          JobContext(u.id, "timesheet", "weekly_submit", False, {})))

    assert seen["hours"]["mon"] == 7.0
    assert seen["hours"]["tue"] == 8.0
