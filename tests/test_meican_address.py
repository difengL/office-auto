"""下单企业地址解析。

回归背景：下单报「请选择企业地址后下单」(CORP_ADDRESS_NOT_FOUND)，
因为餐次 corpAddress 为 null，旧代码传空地址。需从企业地址接口取
最近用过的地址（recentList）或地址列表首个（addressList.finalValue）。
"""
from app.features.meican import jobs
from app.features.meican.client import MeicanError


class FakeClient:
    def __init__(self, data=None, fail=False):
        self.data = data or {}
        self.fail = fail
        self.asked_ns = None

    def corp_addresses(self, ns):
        self.asked_ns = ns
        if self.fail:
            raise MeicanError("接口挂了")
        return self.data


ITEM = {"userTab": {"uniqueId": "T1", "corp": {"namespace": "102105"}}}


def test_uses_item_corp_address_first():
    item = dict(ITEM, corpAddress={"uniqueId": "FROM_ITEM"})
    assert jobs._resolve_address(FakeClient(), item) == "FROM_ITEM"


def test_falls_back_to_recent_list():
    c = FakeClient({"recentList": [{"uniqueId": "RECENT"}],
                    "addressList": [{"finalValue": {"uniqueId": "FIRST"}}]})
    assert jobs._resolve_address(c, ITEM) == "RECENT"
    assert c.asked_ns == "102105"


def test_falls_back_to_address_list():
    c = FakeClient({"recentList": [],
                    "addressList": [{"finalValue": {"uniqueId": "FIRST"}}]})
    assert jobs._resolve_address(c, ITEM) == "FIRST"


def test_empty_when_no_address():
    c = FakeClient({"recentList": [], "addressList": []})
    assert jobs._resolve_address(c, ITEM) == ""


def test_empty_when_api_fails():
    assert jobs._resolve_address(FakeClient(fail=True), ITEM) == ""


def test_empty_when_no_namespace():
    item = {"userTab": {"uniqueId": "T1", "corp": {}}}
    assert jobs._resolve_address(FakeClient(), item) == ""
