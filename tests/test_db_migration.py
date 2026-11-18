"""启动自检：建库、表结构对账、初始化数据。

目标：新环境部署只需启动进程，不需要手工建表或跑迁移脚本；
老库缺字段时自动补，且不影响已有数据。
"""
import sqlite3

import pytest

from app.core import db


@pytest.fixture
def paths(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "platform.db")
    monkeypatch.setattr(db, "KEY_PATH", tmp_path / "secret.key")
    return tmp_path


def _columns(table):
    with sqlite3.connect(db.DB_PATH) as c:
        return [r[1] for r in c.execute(f"PRAGMA table_info({table})")]


def _tables():
    with sqlite3.connect(db.DB_PATH) as c:
        return {r[0] for r in c.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
            " AND name NOT LIKE 'sqlite_%'")}


# ---------- 全新环境 ----------

def test_creates_everything_from_scratch(paths):
    assert not db.DB_PATH.exists()
    report = db.init_db()

    assert report.db_created is True
    assert set(report.created_tables) == set(db.expected_schema())
    assert report.added_columns == []
    assert _tables() == set(db.expected_schema())
    # 所有表的字段都要和声明一致
    for table, cols in db.expected_schema().items():
        assert _columns(table) == list(cols)


def test_init_is_idempotent(paths):
    db.init_db()
    report = db.init_db()
    assert report.changed is False
    assert report.added_columns == []
    assert report.created_tables == []
    assert report.db_created is False


def test_user_version_written(paths):
    db.init_db()
    with sqlite3.connect(db.DB_PATH) as c:
        assert c.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION


# ---------- 老库自动补结构 ----------

def test_adds_missing_table(paths):
    db.init_db()
    with sqlite3.connect(db.DB_PATH) as c:
        c.execute("DROP TABLE job_runs")
    report = db.init_db()
    assert "job_runs" in report.created_tables
    assert "job_runs" in _tables()


def test_adds_missing_column_and_keeps_rows(paths):
    """老库缺字段：自动 ALTER 补齐，原有数据不受影响。"""
    db.init_db()
    u = db.create_user("alice", "HASH", "Alice")
    with sqlite3.connect(db.DB_PATH) as c:
        # 模拟旧版本：feature_data 没有 updated_at
        c.execute("ALTER TABLE feature_data RENAME TO fd_old")
        c.execute("""CREATE TABLE feature_data (
            user_id INTEGER NOT NULL, feature_key TEXT NOT NULL,
            key TEXT NOT NULL, value TEXT NOT NULL DEFAULT '',
            PRIMARY KEY (user_id, feature_key, key))""")
        c.execute("INSERT INTO feature_data(user_id,feature_key,key,value)"
                  " VALUES(?,?,?,?)", (u.id, "meican", "history", '{"a":1}'))
        c.execute("DROP TABLE fd_old")

    report = db.init_db()
    assert "feature_data.updated_at" in report.added_columns
    assert "updated_at" in _columns("feature_data")
    # 老行仍可读
    assert db.fdata_get(u.id, "meican", "history") == {"a": 1}


def test_missing_notnull_column_without_default_gets_sentinel(paths):
    """NOT NULL 且无默认值的补列：SQLite 会拒绝，用类型零值兜底。"""
    db.init_db()
    u = db.create_user("bob", "HASH", "Bob")
    with sqlite3.connect(db.DB_PATH) as c:
        c.execute("ALTER TABLE feature_credentials RENAME TO fc_old")
        c.execute("""CREATE TABLE feature_credentials (
            user_id INTEGER NOT NULL, feature_key TEXT NOT NULL,
            payload TEXT NOT NULL DEFAULT '',
            state TEXT NOT NULL DEFAULT 'not_configured',
            PRIMARY KEY (user_id, feature_key))""")
        c.execute("INSERT INTO feature_credentials VALUES(?,?,?,'active')",
                  (u.id, "meican", "TOKEN"))
        c.execute("DROP TABLE fc_old")

    report = db.init_db()
    assert "feature_credentials.last_ok_at" in report.added_columns
    cred = db.load_credential(u.id, "meican")
    assert cred.state == "active"
    assert cred.last_ok_at == 0            # 零值兜底，不炸


def test_reports_extra_column_without_dropping(paths):
    """库里有代码不认识的字段：只报告，绝不删（可能是代码回滚）。"""
    db.init_db()
    with sqlite3.connect(db.DB_PATH) as c:
        c.execute("ALTER TABLE users ADD COLUMN legacy_flag TEXT")
    report = db.init_db()
    assert "users.legacy_flag" in report.extra_columns
    assert "legacy_flag" in _columns("users")
    assert any("多出字段" in line for line in report.lines())


def test_reports_type_mismatch_without_altering(paths):
    db.init_db()
    with sqlite3.connect(db.DB_PATH) as c:
        c.execute("ALTER TABLE users RENAME TO u_old")
        c.execute("""CREATE TABLE users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            display_name REAL NOT NULL DEFAULT '',
            created_at INTEGER NOT NULL)""")
        c.execute("DROP TABLE u_old")

    report = db.init_db()
    assert any("users.display_name" in m for m in report.type_mismatches)


def test_equivalent_type_spellings_not_reported(paths):
    """INT vs INTEGER 是同一亲和性，不该误报。"""
    db.init_db()
    with sqlite3.connect(db.DB_PATH) as c:
        c.execute("ALTER TABLE job_runs RENAME TO jr_old")
        c.execute("""CREATE TABLE job_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INT NOT NULL, feature_key VARCHAR NOT NULL,
            job_id TEXT NOT NULL, ts INT NOT NULL, ok INT NOT NULL,
            message TEXT NOT NULL DEFAULT '', detail TEXT NOT NULL DEFAULT '')""")
        c.execute("DROP TABLE jr_old")

    report = db.init_db()
    assert report.type_mismatches == []


def test_report_lines_quiet_when_healthy(paths):
    db.init_db()
    lines = db.init_db().lines()
    assert len(lines) == 1 and "校验通过" in lines[0]


# ---------- 初始化数据 ----------

def test_seed_noop_without_env(paths, monkeypatch):
    monkeypatch.delenv("ADMIN_USERNAME", raising=False)
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    db.init_db()
    assert db.seed_initial_data() == []
    assert db.count_users() == 0


def test_seed_creates_admin(paths, monkeypatch):
    monkeypatch.setenv("ADMIN_USERNAME", "admin")
    monkeypatch.setenv("ADMIN_PASSWORD", "s3cret!")
    db.init_db()

    assert any("admin" in line for line in db.seed_initial_data())
    u = db.get_user_by_username("admin")
    assert u is not None
    from app.core import security
    assert security.verify_password("s3cret!", u.password_hash)


def test_seed_is_idempotent(paths, monkeypatch):
    monkeypatch.setenv("ADMIN_USERNAME", "admin")
    monkeypatch.setenv("ADMIN_PASSWORD", "s3cret!")
    db.init_db()
    db.seed_initial_data()
    assert db.seed_initial_data() == []       # 已有用户，不再创建
    assert db.count_users() == 1


def test_seed_skips_when_users_exist(paths, monkeypatch):
    db.init_db()
    db.create_user("someone", "HASH", "S")
    monkeypatch.setenv("ADMIN_USERNAME", "admin")
    monkeypatch.setenv("ADMIN_PASSWORD", "s3cret!")
    assert db.seed_initial_data() == []
    assert db.get_user_by_username("admin") is None


def test_seed_warns_when_locked_out(paths, monkeypatch):
    """注册关闭 + 无任何用户 + 没给管理员 → 必须明确告警，不能静默。"""
    monkeypatch.delenv("ADMIN_USERNAME", raising=False)
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    monkeypatch.setenv("ALLOW_REGISTER", "0")
    db.init_db()
    out = db.seed_initial_data()
    assert out and "ADMIN_USERNAME" in out[0]


def test_seed_rejects_weak_admin_credentials(paths, monkeypatch):
    monkeypatch.setenv("ADMIN_USERNAME", "a")
    monkeypatch.setenv("ADMIN_PASSWORD", "123")
    db.init_db()
    assert "至少" in db.seed_initial_data()[0]
    assert db.count_users() == 0


# ---------- 启动流程串联 ----------

def test_startup_wires_schema_and_seed(paths, monkeypatch):
    """新环境：只启动应用就该得到可用的库和数据。"""
    monkeypatch.setenv("ADMIN_USERNAME", "admin")
    monkeypatch.setenv("ADMIN_PASSWORD", "s3cret!")
    from fastapi.testclient import TestClient
    from app import main
    from app.core import registry, scheduler

    registry.clear()
    scheduler.scheduler.remove_all_jobs()
    with TestClient(main.app) as c:
        assert c.post("/api/login",
                      json={"username": "admin", "password": "s3cret!"}
                      ).status_code == 200
        assert c.get("/api/me").json()["username"] == "admin"
        assert c.get("/api/features").status_code == 200
