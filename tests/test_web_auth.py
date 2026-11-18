import pytest
from fastapi.testclient import TestClient

from app.core import db, registry, security
from app.core.feature import CredentialKind, Feature, JobResult, JobSpec


class TestFeature(Feature):
    key = "test"
    name = "测试功能"
    credential_kind = CredentialKind.PASSWORD
    default_jobs = [JobSpec("j", "任务", "interval", {"minutes": 5})]

    async def login(self, user_id, payload):
        return {"display": "ok"}

    async def check_alive(self, user_id):
        return True

    async def run_job(self, user_id, job_id, ctx):
        return JobResult(True, "ran")


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr(db, "KEY_PATH", tmp_path / "t.key")
    db.init_db()
    registry.clear()
    registry.register(TestFeature())
    from app import main
    with TestClient(main.app) as c:
        yield c
    registry.clear()


def test_register_and_login(client):
    r = client.post("/api/register", json={"username": "alice", "password": "pw123456"})
    assert r.status_code == 200
    assert "mc_session" in r.cookies
    r = client.get("/api/me")
    assert r.json()["username"] == "alice"


def test_login_wrong_password(client):
    client.post("/api/register", json={"username": "bob", "password": "pw123456"})
    client.post("/api/logout")
    r = client.post("/api/login", json={"username": "bob", "password": "wrong"})
    assert r.status_code == 401


def test_unauthenticated_feature_access(client):
    assert client.get("/api/features").status_code == 401


def test_features_listed_from_registry(client):
    client.post("/api/register", json={"username": "carol", "password": "pw123456"})
    r = client.get("/api/features")
    keys = [f["key"] for f in r.json()["features"]]
    assert "test" in keys


def test_feature_config_and_run(client):
    client.post("/api/register", json={"username": "dave", "password": "pw123456"})
    client.post("/api/features/test/login", json={"username": "x"})
    r = client.post("/api/features/test/config",
                    json={"enabled": True, "dry_run": True, "schedule": {}, "options": {}})
    assert r.status_code == 200
    r = client.post("/api/features/test/run", json={"job_id": "j"})
    assert r.json()["ok"] is True
    runs = client.get("/api/features/test/runs").json()["runs"]
    assert len(runs) == 1


def test_duplicate_username_rejected(client):
    client.post("/api/register", json={"username": "erin", "password": "pw123456"})
    client.post("/api/logout")
    r = client.post("/api/register", json={"username": "erin", "password": "pw123456"})
    assert r.status_code == 400


def test_users_isolated(client):
    client.post("/api/register", json={"username": "u1", "password": "pw123456"})
    client.post("/api/features/test/login", json={"username": "a"})
    client.post("/api/features/test/run", json={"job_id": "j"})
    client.post("/api/logout")
    client.post("/api/register", json={"username": "u2", "password": "pw123456"})
    assert client.get("/api/features/test/runs").json()["runs"] == []


class KeepaliveFeature(Feature):
    """带常开保活任务的功能，用于验证运行记录过滤与手机号显示。"""
    key = "ka"
    name = "保活测试"
    credential_kind = CredentialKind.INTERACTIVE
    default_jobs = [
        JobSpec("order", "订餐", "cron", {"hour": 10, "minute": 5}),
        JobSpec("keepalive", "保活", "interval", {"minutes": 30},
                editable=False, always_on=True),
    ]

    async def login(self, user_id, payload):
        return {"display": payload.get("phone", "")}

    async def check_alive(self, user_id):
        return True

    async def run_job(self, user_id, job_id, ctx):
        return JobResult(True, f"ran {job_id}")


@pytest.fixture
def ka_client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr(db, "KEY_PATH", tmp_path / "t.key")
    db.init_db()
    registry.clear()
    registry.register(KeepaliveFeature())
    from app import main
    from app.core import scheduler as sched_mod
    with TestClient(main.app) as c:
        # scheduler 是模块级单例，上一个测试的用户作业（user_id 相同）
        # 会残留，污染 next_run 断言。每个测试从干净调度表开始。
        sched_mod.scheduler.remove_all_jobs()
        yield c
    registry.clear()


def test_runs_hide_always_on_jobs(ka_client):
    ka_client.post("/api/register", json={"username": "k1", "password": "pw123456"})
    ka_client.post("/api/features/ka/login", json={"phone": "17600610776"})
    # 直接写运行记录：保活 + 业务各若干
    u = db.get_user_by_username("k1")
    for _ in range(5):
        db.record_job_run(u.id, "ka", "keepalive", True, "保活成功")
    db.record_job_run(u.id, "ka", "order", True, "下单成功")

    runs = ka_client.get("/api/features/ka/runs").json()["runs"]
    # 保活记录不应出现，业务记录要在
    assert all(r["job_id"] != "keepalive" for r in runs)
    assert any(r["job_id"] == "order" for r in runs)


def test_display_falls_back_to_phone(ka_client):
    ka_client.post("/api/register", json={"username": "k2", "password": "pw123456"})
    ka_client.post("/api/features/ka/login", json={"phone": "17600610776"})
    u = db.get_user_by_username("k2")
    # 模拟美餐 v3：userName 为空，只有 phone
    db.save_credential(u.id, "ka", {"phone": "17600610776", "userName": ""}, "active")

    detail = ka_client.get("/api/features/ka").json()
    assert detail["display"] == "17600610776"


# ---------- 主页面「下次执行」不得暴露保活 ----------

def test_index_next_run_hidden_when_schedule_disabled(ka_client):
    """定时关闭时，主页面「下次执行」必须为空。

    回归背景：保活是 always_on，即便定时关闭也在后台排期，旧实现
    直接把 jobs 的 next_run 拿来展示，导致「定时关闭」却显示下次执行时间。
    """
    ka_client.post("/api/register", json={"username": "k3", "password": "pw123456"})
    ka_client.post("/api/features/ka/login", json={"phone": "17600610776"})

    f = next(x for x in ka_client.get("/api/features").json()["features"]
             if x["key"] == "ka")
    assert f["enabled"] is False
    # 保活确实在跑，但业务任务没排期
    ka_jobs = {j["job_id"]: j for j in f["jobs"]}
    assert ka_jobs["keepalive"]["next_run"] is not None
    assert ka_jobs["keepalive"]["always_on"] is True
    assert ka_jobs["order"]["next_run"] is None
    # 汇总字段过滤掉保活 → 空
    assert f["next_run"] is None


def test_index_next_run_shows_business_job_when_enabled(ka_client):
    ka_client.post("/api/register", json={"username": "k4", "password": "pw123456"})
    ka_client.post("/api/features/ka/login", json={"phone": "17600610776"})
    ka_client.post("/api/features/ka/config",
                   json={"enabled": True, "dry_run": False, "schedule": {}, "options": {}})

    f = next(x for x in ka_client.get("/api/features").json()["features"]
             if x["key"] == "ka")
    assert f["next_run"] is not None
    order = next(j for j in f["jobs"] if j["job_id"] == "order")
    assert f["next_run"] == order["next_run"]


def test_index_next_run_none_without_credential(ka_client):
    """未登录（无凭据）时保活不排期，下次执行同样为空。"""
    ka_client.post("/api/register", json={"username": "k5", "password": "pw123456"})
    f = next(x for x in ka_client.get("/api/features").json()["features"]
             if x["key"] == "ka")
    assert f["next_run"] is None