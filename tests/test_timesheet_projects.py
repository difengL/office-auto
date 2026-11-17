"""list_projects 必须用工时填报自身的项目接口。

历史问题：曾用 /api/localproject/list（CRM 项目列表），
  1. 内容是 CRM 项目，混入「护理假 / 哺乳假」等假勤伪项目，
     且缺少工时实际归属的 subProjectId；
  2. 实测该接口要 7~8 秒，而 /api/hem/workhour/projects 只要 0.1~0.3 秒。
"""
from app.features.timesheet.client import PROJECT_KINDS, HemClient


class FakeResp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def json(self):
        return self._payload


class FakeHTTP:
    """按 path+params 返回预置响应，并记录所有请求。"""

    def __init__(self, table):
        self.table = table
        self.calls: list[tuple[str, dict]] = []

    def get(self, path, params=None, headers=None):
        self.calls.append((path, dict(params or {})))
        return FakeResp(self.table.get((path, (params or {}).get("type"))))

    def close(self):
        pass


def _client_with(table) -> HemClient:
    c = HemClient("u", "p")
    c._client = FakeHTTP(table)
    return c


def test_uses_workhour_projects_endpoint():
    table = {("/api/hem/workhour/projects", k): {"code": 0, "data": []}
             for k in PROJECT_KINDS}
    c = _client_with(table)

    c.list_projects()

    paths = {p for p, _ in c._client.calls}
    assert paths == {"/api/hem/workhour/projects"}
    # 绝不能再去打慢且内容错的 CRM 项目接口
    assert "/api/localproject/list" not in paths


def test_queries_all_three_kinds():
    table = {("/api/hem/workhour/projects", k): {"code": 0, "data": []}
             for k in PROJECT_KINDS}
    c = _client_with(table)

    c.list_projects()

    types = [params.get("type") for _, params in c._client.calls]
    assert types == list(PROJECT_KINDS)


def test_merges_kinds_and_keeps_sub_project_id():
    table = {
        ("/api/hem/workhour/projects", "INTERNAL_WORK"): {"code": 0, "data": [
            {"projectId": "2959", "subProjectId": 4425, "projectName": "部门周会",
             "category": "INTERNAL_WORK", "projectClassName": "内部事务类工作"}]},
        ("/api/hem/workhour/projects", "INTERNAL_PROJECT"): {"code": 0, "data": [
            {"projectId": "6001", "subProjectId": 17560, "projectName": "百察智能体项目",
             "category": "INTERNAL_PROJECT", "projectClassName": ""}]},
        ("/api/hem/workhour/projects", "CUSTOMER_PROJECT"): {"code": 0, "data": []},
    }
    c = _client_with(table)

    items = c.list_projects()

    assert [x["project_id"] for x in items] == ["2959", "6001"]
    assert items[0]["sub_project_id"] == "4425"
    assert items[1]["sub_project_id"] == "17560"
    assert items[1]["name"] == "百察智能体项目"
    assert items[1]["category"] == "INTERNAL_PROJECT"


def test_deduplicates_same_project_and_sub_project():
    dup = {"code": 0, "data": [
        {"projectId": "6001", "subProjectId": 17560, "projectName": "百察智能体项目"}]}
    table = {("/api/hem/workhour/projects", k): dup for k in PROJECT_KINDS}
    c = _client_with(table)

    items = c.list_projects()

    assert len(items) == 1


def test_skips_entries_without_id_or_name():
    table = {("/api/hem/workhour/projects", k): {"code": 0, "data": []}
             for k in PROJECT_KINDS}
    table[("/api/hem/workhour/projects", "INTERNAL_WORK")] = {"code": 0, "data": [
        {"projectId": "", "subProjectId": 1, "projectName": "无ID"},
        {"projectId": "1", "subProjectId": 2, "projectName": ""},
        {"projectId": "2", "subProjectId": 3, "projectName": "正常"}]}
    c = _client_with(table)

    items = c.list_projects()

    assert [x["name"] for x in items] == ["正常"]
