import pytest

from app.core import db


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(db, "KEY_PATH", tmp_path / "test.key")
    db.init_db()
    yield


def test_create_and_get_user():
    u = db.create_user("alice", "hash123", "Alice")
    assert u.id > 0
    assert db.get_user_by_username("alice").display_name == "Alice"
    assert db.get_user(u.id).username == "alice"
    assert db.get_user_by_username("nobody") is None


def test_username_unique():
    db.create_user("bob", "h")
    with pytest.raises(Exception):
        db.create_user("bob", "h")


def test_session_roundtrip():
    u = db.create_user("carol", "h")
    token, exp = db.issue_session(u.id, ua="pytest")
    assert len(token) == 64
    got = db.user_by_session(token)
    assert got is not None and got.id == u.id
    db.revoke_session(token)
    assert db.user_by_session(token) is None


def test_session_expiry():
    u = db.create_user("dave", "h")
    token, _ = db.issue_session(u.id)
    with db.conn() as c:
        c.execute("UPDATE sessions SET exp=1 WHERE token=?", (token,))
    assert db.user_by_session(token) is None


def test_credential_encrypted_at_rest():
    u = db.create_user("erin", "h")
    db.save_credential(u.id, "meican", {"username": "e@x.com", "password": "p@ss"}, "active")
    cred = db.load_credential(u.id, "meican")
    assert cred.payload["password"] == "p@ss"
    assert cred.state == "active"
    assert cred.last_ok_at > 0
    # 数据库里必须是密文
    with db.conn() as c:
        raw = c.execute("SELECT payload FROM feature_credentials WHERE user_id=?",
                        (u.id,)).fetchone()["payload"]
    assert "p@ss" not in raw
    assert raw.startswith("gAAAA")   # Fernet token 前缀


def test_credential_state_update_keeps_payload():
    u = db.create_user("frank", "h")
    db.save_credential(u.id, "meican", {"password": "x"}, "active")
    db.set_credential_state(u.id, "meican", "expired", "登录失效")
    cred = db.load_credential(u.id, "meican")
    assert cred.state == "expired"
    assert cred.payload["password"] == "x"
    assert cred.last_error == "登录失效"


def test_credential_isolated_by_user_and_feature():
    a = db.create_user("ga", "h")
    b = db.create_user("gb", "h")
    db.save_credential(a.id, "meican", {"password": "A"}, "active")
    db.save_credential(b.id, "meican", {"password": "B"}, "active")
    db.save_credential(a.id, "timesheet", {"password": "T"}, "active")
    assert db.load_credential(a.id, "meican").payload["password"] == "A"
    assert db.load_credential(b.id, "meican").payload["password"] == "B"
    assert db.load_credential(a.id, "timesheet").payload["password"] == "T"
    assert db.load_credential(b.id, "timesheet").state == "not_configured"


def test_feature_config_defaults_and_roundtrip():
    u = db.create_user("hank", "h")
    cfg = db.load_feature_config(u.id, "meican")
    assert cfg.enabled is False
    assert cfg.dry_run is True      # 默认 dry-run
    cfg.enabled = True
    cfg.schedule = {"order": {"hour": 10, "minute": 5}}
    cfg.options = {"prefer": "低脂"}
    db.save_feature_config(u.id, "meican", cfg)
    back = db.load_feature_config(u.id, "meican")
    assert back.enabled is True
    assert back.schedule["order"]["hour"] == 10
    assert back.options["prefer"] == "低脂"


def test_enabled_feature_pairs():
    a = db.create_user("ia", "h")
    b = db.create_user("ib", "h")
    db.save_feature_config(a.id, "meican", db.FeatureConfig("meican", enabled=True))
    db.save_feature_config(b.id, "timesheet", db.FeatureConfig("timesheet", enabled=True))
    db.save_feature_config(a.id, "timesheet", db.FeatureConfig("timesheet", enabled=False))
    pairs = set(db.enabled_feature_pairs())
    assert (a.id, "meican") in pairs
    assert (b.id, "timesheet") in pairs
    assert (a.id, "timesheet") not in pairs


def test_fdata_scoped_by_feature():
    u = db.create_user("jack", "h")
    db.fdata_set(u.id, "meican", "order_cache", {"2026-11-17": 123})
    db.fdata_set(u.id, "timesheet", "order_cache", {"other": 1})
    assert db.fdata_get(u.id, "meican", "order_cache")["2026-11-17"] == 123
    assert db.fdata_get(u.id, "timesheet", "order_cache") == {"other": 1}
    assert db.fdata_get(u.id, "meican", "missing", "默认") == "默认"


def test_job_runs_and_consecutive_failures():
    u = db.create_user("kate", "h")
    db.record_job_run(u.id, "meican", "order", False, "失败1")
    db.record_job_run(u.id, "meican", "order", False, "失败2")
    runs = db.read_job_runs(u.id, "meican")
    assert len(runs) == 2
    assert runs[0]["message"] == "失败2"   # 最新在前
    assert db.consecutive_failures(u.id, "meican", "order") == 2
    db.record_job_run(u.id, "meican", "order", True, "成功")
    assert db.consecutive_failures(u.id, "meican", "order") == 0
    # 别的用户看不到
    other = db.create_user("leo", "h")
    assert db.read_job_runs(other.id, "meican") == []