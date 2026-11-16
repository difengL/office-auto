from datetime import date

from app.features.timesheet.jobs import plan_week


def test_plan_fills_all_workdays_when_empty():
    days = [date(2026, 11, 16), date(2026, 11, 17)]
    plan = plan_week([], days, 8.0)
    assert plan["to_fill"] == ["2026-11-16", "2026-11-17"]
    assert plan["already"] == []
    assert plan["total_hours"] == 16.0


def test_plan_skips_already_filled():
    records = [{"content": [{"date": "2026-11-16", "task_time": 8.0}]}]
    days = [date(2026, 11, 16), date(2026, 11, 17)]
    plan = plan_week(records, days, 8.0)
    assert plan["already"] == ["2026-11-16"]
    assert plan["to_fill"] == ["2026-11-17"]


def test_plan_skips_weekend_days_not_in_input():
    # workdays_of_week 本身不含周末；这里验证只处理传入的天
    days = [date(2026, 11, 20)]
    plan = plan_week([], days, 8.0)
    assert plan["to_fill"] == ["2026-11-20"]


def test_plan_handles_partial_hours():
    records = [{"content": [{"date": "2026-11-16", "task_time": 4.0}]}]
    days = [date(2026, 11, 16)]
    plan = plan_week(records, days, 8.0)
    # 已有部分工时的日期仍算"已填"，不覆盖用户手填数据
    assert plan["already"] == ["2026-11-16"]
    assert plan["to_fill"] == []


def test_plan_all_workdays_five():
    days = [date(2026, 11, 16) + __import__("datetime").timedelta(days=i) for i in range(5)]
    plan = plan_week([], days, 8.0)
    assert len(plan["to_fill"]) == 5
    assert plan["total_hours"] == 40.0