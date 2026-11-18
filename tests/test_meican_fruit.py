"""本周水果菜单：解析公告正文、按企业全局缓存、默认当天+展开整周。

数据来自美餐企业公告 corpNotice/list，title 含「水果」，content 按
周一~周五列每天水果。每周换一次，同企业用户共享。
"""
from datetime import date

import pytest

from app.core import db
from app.features.meican import jobs


@pytest.fixture(autouse=True)
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr(db, "KEY_PATH", tmp_path / "t.key")
    db.init_db()
    yield


CONTENT = ("水果及面包全部入餐柜\r\n周一\r\n【整果】云南青柚+鸡心果+香蕉+龙眼1000g\r\n"
           "周二\r\n【整果】苹果+砂糖桔+香梨+猕猴桃1000g\r\n"
           "周三\r\n【整果】红心火龙果+人参果+爱媛橙+青枣1000g\r\n"
           "周四\r\n【整果】秋月梨+耙耙柑+冬枣+小黄瓜1000g\r\n"
           "周五\r\n【整果】苹果+小番茄+脆柿+橙子1000g\r\n温馨提示：记得带走当天水果")


def test_parse_fruit_menu_extracts_each_day():
    menu = jobs.parse_fruit_menu(CONTENT)
    assert menu["周一"] == "【整果】云南青柚+鸡心果+香蕉+龙眼1000g"
    assert menu["周五"] == "【整果】苹果+小番茄+脆柿+橙子1000g"
    assert len(menu) == 5
    # 温馨提示不属于任何一天，不应混入
    assert "温馨提示" not in "".join(menu.values())


def test_parse_empty_content():
    assert jobs.parse_fruit_menu("") == {}
    assert jobs.parse_fruit_menu(None) == {}


class FakeClient:
    def __init__(self, notices=None, fail=False):
        self.notices = notices or []
        self.fail = fail
        self.notice_calls = 0

    def calendar_items(self, s, e, with_order=True):
        return [{"calendarItemList": [{"title": "晚餐",
                                       "userTab": {"corp": {"namespace": "102105"}}}]}]

    def corp_notices(self):
        self.notice_calls += 1
        if self.fail:
            raise jobs.MeicanError("接口挂了")
        return self.notices


NOTICE = [{"title": "本周水果菜单", "content": CONTENT}]


def test_get_fruit_menu_today_and_week(monkeypatch):
    monkeypatch.setattr(jobs, "_client", lambda p: FakeClient(NOTICE))
    # 2026-11-18 是周三
    data = jobs.get_fruit_menu({"accessToken": "AT"}, date(2026, 11, 18))
    assert data["weekday"] == "周三"
    assert "红心火龙果" in data["today"]
    assert len(data["week"]) == 5
    assert data["available"] is True


def test_fruit_menu_cached_globally(monkeypatch):
    fake = FakeClient(NOTICE)
    monkeypatch.setattr(jobs, "_client", lambda p: fake)
    jobs.get_fruit_menu({"accessToken": "A"}, date(2026, 11, 18))
    fake.notice_calls = 0
    # 同企业另一用户/另一天命中同一周缓存
    jobs.get_fruit_menu({"accessToken": "B"}, date(2026, 11, 19))
    assert fake.notice_calls == 0


def test_fruit_menu_unavailable_when_no_notice(monkeypatch):
    monkeypatch.setattr(jobs, "_client", lambda p: FakeClient([]))
    data = jobs.get_fruit_menu({"accessToken": "AT"}, date(2026, 11, 18))
    assert data["available"] is False
    assert data["today"] == ""


def test_fruit_menu_api_failure_graceful(monkeypatch):
    monkeypatch.setattr(jobs, "_client", lambda p: FakeClient(fail=True))
    data = jobs.get_fruit_menu({"accessToken": "AT"}, date(2026, 11, 18))
    assert data["available"] is False
