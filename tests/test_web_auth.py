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