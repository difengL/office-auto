from datetime import date

from app.features.timesheet.client import monday_of, workdays_of_week


def test_monday_of_wednesday():
    assert monday_of(date(2026, 11, 18)) == date(2026, 11, 16)


def test_monday_of_monday():
    assert monday_of(date(2026, 11, 16)) == date(2026, 11, 16)


def test_monday_of_sunday():
    assert monday_of(date(2026, 11, 22)) == date(2026, 11, 16)


def test_workdays_are_five():
    days = workdays_of_week(date(2026, 11, 18))
    assert [d.isoformat() for d in days] == [
        "2026-11-16", "2026-11-17", "2026-11-18", "2026-11-19", "2026-11-20"]


def test_workdays_exclude_weekend():
    days = workdays_of_week(date(2026, 11, 22))
    assert all(d.weekday() < 5 for d in days)