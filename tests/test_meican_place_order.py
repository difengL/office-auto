"""下单成功判断必须严格，不能把失败误报成成功。

回归背景：用户点「订餐」提示成功，实际没订上。真实响应是
HTTP 200 + {"message":"请选择企业地址后下单","status":"CORP_ADDRESS_NOT_FOUND"}，
但旧代码只检查 resultDescription（该响应里没有这个字段），漏判误报成功。
前端真实判断是 data.status == "SUCCESSFUL"。
"""
import pytest

from app.features.meican.client import MeicanClient, MeicanError


class FakeResp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def json(self):
        return self._payload


class FakeHTTP:
    def __init__(self, payload, status=200):
        self.payload = payload
        self.status = status
        self.calls = []

    def post(self, url, params=None, data=None, headers=None, json=None):
        self.calls.append({"url": url, "data": data})
        return FakeResp(self.payload, self.status)


def _client(payload, status=200):
    c = MeicanClient("AT")
    c._client = FakeHTTP(payload, status)
    return c


def test_corp_address_not_found_is_failure():
    # 真实失败响应：HTTP 200 但 status 标示错误
    c = _client({"message": "请选择企业地址后下单", "status": "CORP_ADDRESS_NOT_FOUND"})
    with pytest.raises(MeicanError, match="企业地址"):
        c.place_order("T", "2026-11-18 18:00", [{"dishId": 1}], "ADDR")


def test_generic_error_status_is_failure():
    c = _client({"message": "库存不足", "status": "SOLD_OUT"})
    with pytest.raises(MeicanError, match="库存不足"):
        c.place_order("T", "tt", [{"dishId": 1}], "ADDR")


def test_success_when_data_status_successful():
    c = _client({"data": {"status": "SUCCESSFUL", "order": {"uniqueId": "O1"}},
                 "resultDescription": "SUCCESS"})
    res = c.place_order("T", "tt", [{"dishId": 1}], "ADDR")
    assert res["data"]["order"]["uniqueId"] == "O1"


def test_success_when_top_level_status_successful():
    # 实测真实成功响应：status 在顶层，不在 data 里
    c = _client({"status": "SUCCESSFUL", "message": "",
                 "order": {"uniqueId": "O3"}})
    res = c.place_order("T", "tt", [{"dishId": 1}], "ADDR")
    assert res["order"]["uniqueId"] == "O3"


def test_success_when_result_description_success():
    # 兼容旧式成功响应
    c = _client({"resultDescription": "SUCCESS", "data": {"order": {"uniqueId": "O2"}}})
    res = c.place_order("T", "tt", [{"dishId": 1}], "ADDR")
    assert res["data"]["order"]["uniqueId"] == "O2"


def test_unknown_status_without_success_marker_is_failure():
    # 既无 SUCCESSFUL 也无 SUCCESS resultDescription，不能当成功
    c = _client({"data": {"status": "PENDING"}, "message": "处理中"})
    with pytest.raises(MeicanError):
        c.place_order("T", "tt", [{"dishId": 1}], "ADDR")
