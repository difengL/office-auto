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
    with TestClient(main.app) as c:
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