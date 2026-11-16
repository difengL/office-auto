"""多功能平台数据层。

六张表：users / sessions / feature_credentials / feature_configs / feature_data / job_runs
功能数据一律按 (user_id, feature_key) 隔离；凭据用 Fernet 加密落盘。
"""
from __future__ import annotations

import json
import os
import secrets
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

BASE_DIR = Path(__file__).resolve().parents[2]
DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "platform.db"
KEY_PATH = DATA_DIR / "secret.key"

SESSION_DAYS = 30

# 凭据状态
STATE_NOT_CONFIGURED = "not_configured"
STATE_ACTIVE = "active"
STATE_EXPIRED = "expired"
STATE_ERROR = "error"


def _load_key() -> bytes:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if KEY_PATH.exists():
        return KEY_PATH.read_bytes().strip()
    key = Fernet.generate_key()
    KEY_PATH.write_bytes(key)
    os.chmod(KEY_PATH, 0o600)
    return key


def _fernet() -> Fernet:
    return Fernet(_load_key())


def encrypt_dict(payload: dict) -> str:
    return _fernet().encrypt(json.dumps(payload, ensure_ascii=False).encode()).decode()


def decrypt_dict(token: str) -> dict:
    if not token:
        return {}
    try:
        return json.loads(_fernet().decrypt(token.encode()).decode())
    except (InvalidToken, ValueError):
        return {}


@contextmanager
def conn():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB_PATH, timeout=10)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA foreign_keys=ON")
    try:
        yield c
        c.commit()
    finally:
        c.close()


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  username      TEXT UNIQUE NOT NULL,
  password_hash TEXT NOT NULL,
  display_name  TEXT NOT NULL DEFAULT '',
  created_at    INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
  token      TEXT PRIMARY KEY,
  user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  created_at INTEGER NOT NULL,
  exp        INTEGER NOT NULL,
  ua         TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);

CREATE TABLE IF NOT EXISTS feature_credentials (
  user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  feature_key TEXT NOT NULL,
  payload     TEXT NOT NULL DEFAULT '',
  state       TEXT NOT NULL DEFAULT 'not_configured',
  last_ok_at  INTEGER DEFAULT 0,
  last_error  TEXT DEFAULT '',
  updated_at  INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (user_id, feature_key)
);

CREATE TABLE IF NOT EXISTS feature_configs (
  user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  feature_key   TEXT NOT NULL,
  enabled       INTEGER NOT NULL DEFAULT 0,
  dry_run       INTEGER NOT NULL DEFAULT 1,
  schedule_json TEXT NOT NULL DEFAULT '{}',
  options_json  TEXT NOT NULL DEFAULT '{}',
  PRIMARY KEY (user_id, feature_key)
);

CREATE TABLE IF NOT EXISTS feature_data (
  user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  feature_key TEXT NOT NULL,
  key         TEXT NOT NULL,
  value       TEXT NOT NULL DEFAULT '',
  updated_at  INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (user_id, feature_key, key)
);

CREATE TABLE IF NOT EXISTS job_runs (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id     INTEGER NOT NULL,
  feature_key TEXT NOT NULL,
  job_id      TEXT NOT NULL,
  ts          INTEGER NOT NULL,
  ok          INTEGER NOT NULL,
  message     TEXT NOT NULL DEFAULT '',
  detail      TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_job_runs ON job_runs(user_id, feature_key, id DESC);
"""


def init_db() -> None:
    with conn() as c:
        c.executescript(SCHEMA)


# ---------------- 用户 ----------------

@dataclass
class User:
    id: int
    username: str
    password_hash: str
    display_name: str
    created_at: int


def _row_to_user(row) -> User:
    return User(row["id"], row["username"], row["password_hash"],
                row["display_name"], row["created_at"])


def create_user(username: str, password_hash: str, display_name: str = "") -> User:
    now = int(time.time())
    with conn() as c:
        cur = c.execute(
            "INSERT INTO users(username, password_hash, display_name, created_at)"
            " VALUES(?,?,?,?)",
            (username, password_hash, display_name or username, now),
        )
        uid = cur.lastrowid
        row = c.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    return _row_to_user(row)


def get_user(user_id: int) -> User | None:
    with conn() as c:
        row = c.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    return _row_to_user(row) if row else None


def get_user_by_username(username: str) -> User | None:
    with conn() as c:
        row = c.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
    return _row_to_user(row) if row else None


def list_users() -> list[User]:
    with conn() as c:
        rows = c.execute("SELECT * FROM users ORDER BY id").fetchall()
    return [_row_to_user(r) for r in rows]


def count_users() -> int:
    with conn() as c:
        return c.execute("SELECT COUNT(*) n FROM users").fetchone()["n"]


def update_password(user_id: int, password_hash: str) -> None:
    with conn() as c:
        c.execute("UPDATE users SET password_hash=? WHERE id=?", (password_hash, user_id))


# ---------------- 会话 ----------------

def issue_session(user_id: int, ua: str = "") -> tuple[str, int]:
    token = secrets.token_hex(32)
    now = int(time.time())
    exp = now + SESSION_DAYS * 86400
    with conn() as c:
        c.execute("INSERT INTO sessions(token,user_id,created_at,exp,ua) VALUES(?,?,?,?,?)",
                  (token, user_id, now, exp, ua[:200]))
    return token, exp


def user_by_session(token: str) -> User | None:
    if not token:
        return None
    with conn() as c:
        row = c.execute(
            "SELECT u.* FROM sessions s JOIN users u ON u.id=s.user_id"
            " WHERE s.token=? AND s.exp>?", (token, int(time.time()))).fetchone()
    return _row_to_user(row) if row else None


def revoke_session(token: str) -> None:
    with conn() as c:
        c.execute("DELETE FROM sessions WHERE token=?", (token,))


def revoke_all_sessions(user_id: int) -> None:
    with conn() as c:
        c.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))


def purge_expired_sessions() -> None:
    with conn() as c:
        c.execute("DELETE FROM sessions WHERE exp<=?", (int(time.time()),))


# ---------------- 功能凭据 ----------------

@dataclass
class Credential:
    feature_key: str
    payload: dict = field(default_factory=dict)
    state: str = STATE_NOT_CONFIGURED
    last_ok_at: int = 0
    last_error: str = ""


def load_credential(user_id: int, feature_key: str) -> Credential:
    with conn() as c:
        row = c.execute(
            "SELECT * FROM feature_credentials WHERE user_id=? AND feature_key=?",
            (user_id, feature_key)).fetchone()
    if not row:
        return Credential(feature_key)
    return Credential(feature_key, decrypt_dict(row["payload"]), row["state"],
                      row["last_ok_at"], row["last_error"])


def save_credential(user_id: int, feature_key: str, payload: dict,
                    state: str = STATE_ACTIVE) -> None:
    now = int(time.time())
    with conn() as c:
        c.execute(
            "INSERT INTO feature_credentials(user_id,feature_key,payload,state,last_ok_at,"
            "last_error,updated_at) VALUES(?,?,?,?,?,?,?)"
            " ON CONFLICT(user_id,feature_key) DO UPDATE SET"
            " payload=excluded.payload, state=excluded.state,"
            " last_ok_at=excluded.last_ok_at, last_error='', updated_at=excluded.updated_at",
            (user_id, feature_key, encrypt_dict(payload), state,
             now if state == STATE_ACTIVE else 0, "", now))


def set_credential_state(user_id: int, feature_key: str, state: str,
                         error: str = "") -> None:
    with conn() as c:
        c.execute(
            "UPDATE feature_credentials SET state=?, last_error=?, updated_at=?"
            " WHERE user_id=? AND feature_key=?",
            (state, error, int(time.time()), user_id, feature_key))


# ---------------- 功能配置 ----------------

@dataclass
class FeatureConfig:
    feature_key: str
    enabled: bool = False
    dry_run: bool = True
    schedule: dict = field(default_factory=dict)
    options: dict = field(default_factory=dict)


def load_feature_config(user_id: int, feature_key: str) -> FeatureConfig:
    with conn() as c:
        row = c.execute(
            "SELECT * FROM feature_configs WHERE user_id=? AND feature_key=?",
            (user_id, feature_key)).fetchone()
    if not row:
        return FeatureConfig(feature_key)
    return FeatureConfig(feature_key, bool(row["enabled"]), bool(row["dry_run"]),
                         json.loads(row["schedule_json"]), json.loads(row["options_json"]))


def save_feature_config(user_id: int, feature_key: str, cfg: FeatureConfig) -> None:
    with conn() as c:
        c.execute(
            "INSERT INTO feature_configs(user_id,feature_key,enabled,dry_run,"
            "schedule_json,options_json) VALUES(?,?,?,?,?,?)"
            " ON CONFLICT(user_id,feature_key) DO UPDATE SET"
            " enabled=excluded.enabled, dry_run=excluded.dry_run,"
            " schedule_json=excluded.schedule_json, options_json=excluded.options_json",
            (user_id, feature_key, int(cfg.enabled), int(cfg.dry_run),
             json.dumps(cfg.schedule, ensure_ascii=False),
             json.dumps(cfg.options, ensure_ascii=False)))


def enabled_feature_pairs() -> list[tuple[int, str]]:
    with conn() as c:
        rows = c.execute(
            "SELECT user_id, feature_key FROM feature_configs WHERE enabled=1").fetchall()
    return [(r["user_id"], r["feature_key"]) for r in rows]


# ---------------- 功能 KV ----------------

def fdata_get(user_id: int, feature_key: str, key: str, default=None):
    with conn() as c:
        row = c.execute(
            "SELECT value FROM feature_data WHERE user_id=? AND feature_key=? AND key=?",
            (user_id, feature_key, key)).fetchone()
    if not row:
        return default
    try:
        return json.loads(row["value"])
    except ValueError:
        return default


def fdata_set(user_id: int, feature_key: str, key: str, value) -> None:
    with conn() as c:
        c.execute(
            "INSERT INTO feature_data(user_id,feature_key,key,value,updated_at)"
            " VALUES(?,?,?,?,?)"
            " ON CONFLICT(user_id,feature_key,key) DO UPDATE SET"
            " value=excluded.value, updated_at=excluded.updated_at",
            (user_id, feature_key, key, json.dumps(value, ensure_ascii=False),
             int(time.time())))


# ---------------- 运行记录 ----------------

def record_job_run(user_id: int, feature_key: str, job_id: str, ok: bool,
                   message: str = "", detail: dict | None = None) -> None:
    with conn() as c:
        c.execute(
            "INSERT INTO job_runs(user_id,feature_key,job_id,ts,ok,message,detail)"
            " VALUES(?,?,?,?,?,?,?)",
            (user_id, feature_key, job_id, int(time.time()), int(ok), message,
             json.dumps(detail or {}, ensure_ascii=False)))


def read_job_runs(user_id: int, feature_key: str, limit: int = 20) -> list[dict]:
    with conn() as c:
        rows = c.execute(
            "SELECT * FROM job_runs WHERE user_id=? AND feature_key=?"
            " ORDER BY id DESC LIMIT ?", (user_id, feature_key, limit)).fetchall()
    return [{
        "job_id": r["job_id"],
        "time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(r["ts"])),
        "ok": bool(r["ok"]),
        "message": r["message"],
        "detail": json.loads(r["detail"]) if r["detail"] else {},
    } for r in rows]


def consecutive_failures(user_id: int, feature_key: str, job_id: str) -> int:
    """最近连续失败次数，用于界面告警。"""
    with conn() as c:
        rows = c.execute(
            "SELECT ok FROM job_runs WHERE user_id=? AND feature_key=? AND job_id=?"
            " ORDER BY id DESC LIMIT 10", (user_id, feature_key, job_id)).fetchall()
    n = 0
    for r in rows:
        if r["ok"]:
            break
        n += 1
    return n