# 多功能平台架构 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把单一功能的「美餐自动订餐」重构为支持多功能的平台，统一系统登录，功能插件化，并新增 HEM 工时自动填报。

**Architecture:** 分 `core/`（框架，零业务）与 `features/`（插件，各自独立）。插件实现 `Feature` 抽象基类声明凭据类型与任务；`runner.py` 按凭据类型统一分派"校验态 → 重登 → 执行"策略。数据按 `(user_id, feature_key)` 双维度隔离。界面为通用模板，按插件声明渲染，新增功能无需改前端。

**Tech Stack:** Python 3.13 / FastAPI / SQLite (WAL) / APScheduler / httpx / cryptography (Fernet) / pytest

## Global Constraints

- 工作目录：`/Users/admin/meican-auto`（Task 14 改名为 `office-auto`）
- Python 解释器：`./.venv/bin/python`，所有命令在项目根目录执行
- `core/` 目录**禁止** import `features/` 下任何模块
- 密码哈希用标准库 `hashlib.pbkdf2_hmac`，不得引入第三方密码库
- 功能凭据用 Fernet 加密，密钥沿用 `data/secret.key`（600 权限）
- 会话 cookie 名固定为 `mc_session`，HttpOnly，SameSite=lax，30 天
- 自助注册默认开放，环境变量 `ALLOW_REGISTER=0` 时关闭
- 工时提交类功能默认 dry-run，不调用提交接口
- HEM 账号密码不得写入任何代码、测试、文档或日志
- 所有中文注释与界面文案使用简体中文
- 每完成一个 Task 提交一次 git commit
- **事件循环约束**：`core/runner.execute` 与 `runner.run_login` 内部使用 `asyncio.run`，只能从**同步上下文**调用。FastAPI 路由必须用 `def`（不是 `async def`），APScheduler 的任务必须经 `asyncio.to_thread` 包一层。违反此约束会抛 `RuntimeError: asyncio.run() cannot be called from a running event loop`。

---

## File Structure

| 文件 | 职责 |
|---|---|
| `app/core/security.py` | 密码哈希 / 校验 |
| `app/core/db.py` | SQLite：用户、会话、功能凭据、配置、KV、运行记录 |
| `app/core/feature.py` | `Feature` 抽象基类与配套 dataclass |
| `app/core/registry.py` | 插件注册表 |
| `app/core/runner.py` | 统一执行流程 |
| `app/core/scheduler.py` | 按 用户×功能×任务 调度 |
| `app/features/meican/client.py` | 美餐 API（原 `app/meican.py`） |
| `app/features/meican/preference.py` | 偏好模型（原 `app/preference.py`） |
| `app/features/meican/jobs.py` | 订餐 / 保活逻辑（原 `app/service.py` + `keepalive.py`） |
| `app/features/meican/feature.py` | `MeicanFeature` |
| `app/features/timesheet/client.py` | HEM API |
| `app/features/timesheet/jobs.py` | 周填报逻辑 |
| `app/features/timesheet/feature.py` | `TimesheetFeature` |
| `app/main.py` | 路由 + 启动装配 |
| `app/templates/*.html` | 登录 / 首页 / 功能面板 |
| `tests/` | pytest 测试 |

---

## Task 1: 项目骨架与密码哈希

**Files:**
- Create: `app/core/__init__.py`, `app/features/__init__.py`, `tests/__init__.py`
- Create: `app/core/security.py`
- Create: `tests/test_security.py`
- Modify: `.venv` 安装 pytest

**Interfaces:**
- Produces: `security.hash_password(password: str) -> str`、`security.verify_password(password: str, stored: str) -> bool`

- [ ] **Step 1: 安装 pytest 并建目录**

```bash
cd /Users/admin/meican-auto
./.venv/bin/pip install -q pytest
mkdir -p app/core app/features tests
touch app/core/__init__.py app/features/__init__.py tests/__init__.py
```

- [ ] **Step 2: 写失败的测试**

创建 `tests/test_security.py`：

```python
from app.core import security


def test_hash_is_not_plaintext():
    h = security.hash_password("secret123")
    assert h != "secret123"
    assert h.startswith("pbkdf2_sha256$")


def test_verify_accepts_correct_password():
    h = security.hash_password("secret123")
    assert security.verify_password("secret123", h) is True


def test_verify_rejects_wrong_password():
    h = security.hash_password("secret123")
    assert security.verify_password("wrong", h) is False


def test_same_password_gets_different_hashes():
    a = security.hash_password("secret123")
    b = security.hash_password("secret123")
    assert a != b          # salt 不同
    assert security.verify_password("secret123", a)
    assert security.verify_password("secret123", b)


def test_verify_handles_malformed_hash():
    assert security.verify_password("x", "garbage") is False
    assert security.verify_password("x", "") is False
```

- [ ] **Step 3: 运行测试确认失败**

Run: `./.venv/bin/python -m pytest tests/test_security.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.core.security'`

- [ ] **Step 4: 实现 security.py**

创建 `app/core/security.py`：

```python
"""密码哈希与校验。

用标准库 PBKDF2-HMAC-SHA256，不引入第三方依赖。
存储格式：pbkdf2_sha256$<迭代次数>$<salt hex>$<hash hex>
"""
from __future__ import annotations

import hashlib
import hmac
import secrets

ALGORITHM = "pbkdf2_sha256"
ITERATIONS = 260_000          # OWASP 2023 建议下限附近，单次约 100ms
SALT_BYTES = 16


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(SALT_BYTES)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, ITERATIONS)
    return f"{ALGORITHM}${ITERATIONS}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    if not stored:
        return False
    try:
        algorithm, iterations, salt_hex, hash_hex = stored.split("$")
        if algorithm != ALGORITHM:
            return False
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(hash_hex)
        actual = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, int(iterations))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)
```

- [ ] **Step 5: 运行测试确认通过**

Run: `./.venv/bin/python -m pytest tests/test_security.py -v`
Expected: 5 passed

- [ ] **Step 6: 提交**

```bash
git add app/core/security.py app/core/__init__.py app/features/__init__.py tests/
git commit -m "feat(core): 密码哈希与校验"
```

---

## Task 2: 新数据模型（core/db.py）

**Files:**
- Create: `app/core/db.py`
- Create: `tests/test_core_db.py`

**Interfaces:**
- Consumes: 无（自带 Fernet 加密，复用 `data/secret.key` 逻辑）
- Produces:
  - `db.init_db() -> None`
  - `db.create_user(username: str, password_hash: str, display_name: str) -> User`
  - `db.get_user_by_username(username: str) -> User | None`
  - `db.get_user(user_id: int) -> User | None`
  - `db.issue_session(user_id: int, ua: str = "") -> tuple[str, int]`
  - `db.user_by_session(token: str) -> User | None`
  - `db.revoke_session(token: str) -> None`
  - `db.load_credential(user_id: int, feature_key: str) -> Credential`
  - `db.save_credential(user_id: int, feature_key: str, payload: dict, state: str) -> None`
  - `db.set_credential_state(user_id: int, feature_key: str, state: str, error: str = "") -> None`
  - `db.load_feature_config(user_id: int, feature_key: str) -> FeatureConfig`
  - `db.save_feature_config(user_id: int, feature_key: str, cfg: FeatureConfig) -> None`
  - `db.fdata_get(user_id: int, feature_key: str, key: str, default=None) -> Any`
  - `db.fdata_set(user_id: int, feature_key: str, key: str, value: Any) -> None`
  - `db.record_job_run(user_id: int, feature_key: str, job_id: str, ok: bool, message: str, detail: dict) -> None`
  - `db.read_job_runs(user_id: int, feature_key: str, limit: int = 20) -> list[dict]`
  - `db.enabled_feature_pairs() -> list[tuple[int, str]]`
  - `db.User(id, username, password_hash, display_name, created_at)`
  - `db.Credential(feature_key, payload: dict, state: str, last_ok_at: int, last_error: str)`
  - `db.FeatureConfig(feature_key, enabled: bool, dry_run: bool, schedule: dict, options: dict)`

- [ ] **Step 1: 写失败的测试**

创建 `tests/test_core_db.py`：

```python
import pytest

from app.core import db


@pytest.fixture(autouse=True)
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(db, "KEY_PATH", tmp_path / "test.key")
    db.init_db()
    yield


def test_create_and_fetch_user():
    u = db.create_user("alice", "HASH", "爱丽丝")
    assert u.id > 0
    assert u.username == "alice"
    assert db.get_user_by_username("alice").display_name == "爱丽丝"
    assert db.get_user_by_username("nobody") is None


def test_username_is_unique():
    db.create_user("alice", "HASH", "A")
    with pytest.raises(Exception):
        db.create_user("alice", "HASH2", "A2")


def test_session_roundtrip():
    u = db.create_user("alice", "HASH", "A")
    token, exp = db.issue_session(u.id, ua="test")
    assert len(token) == 64
    assert db.user_by_session(token).username == "alice"
    db.revoke_session(token)
    assert db.user_by_session(token) is None


def test_multiple_sessions_coexist():
    u = db.create_user("alice", "HASH", "A")
    t1, _ = db.issue_session(u.id)
    t2, _ = db.issue_session(u.id)
    assert db.user_by_session(t1) is not None
    assert db.user_by_session(t2) is not None


def test_credential_encrypted_at_rest():
    u = db.create_user("alice", "HASH", "A")
    db.save_credential(u.id, "meican", {"token": "SECRET_TOKEN"}, "active")
    with db.conn() as c:
        raw = c.execute(
            "SELECT payload FROM feature_credentials WHERE user_id=? AND feature_key=?",
            (u.id, "meican"),
        ).fetchone()["payload"]
    assert "SECRET_TOKEN" not in raw
    assert db.load_credential(u.id, "meican").payload["token"] == "SECRET_TOKEN"


def test_credential_state_update():
    u = db.create_user("alice", "HASH", "A")
    db.save_credential(u.id, "meican", {"a": 1}, "active")
    db.set_credential_state(u.id, "meican", "expired", "登录失效")
    c = db.load_credential(u.id, "meican")
    assert c.state == "expired"
    assert c.last_error == "登录失效"


def test_feature_config_defaults():
    u = db.create_user("alice", "HASH", "A")
    cfg = db.load_feature_config(u.id, "timesheet")
    assert cfg.enabled is False
    assert cfg.schedule == {}
    assert cfg.options == {}


def test_feature_config_roundtrip():
    u = db.create_user("alice", "HASH", "A")
    cfg = db.FeatureConfig("timesheet", True, True, {"weekly": {"hour": 20}}, {"project_id": "6001"})
    db.save_feature_config(u.id, "timesheet", cfg)
    got = db.load_feature_config(u.id, "timesheet")
    assert got.enabled is True
    assert got.schedule["weekly"]["hour"] == 20
    assert got.options["project_id"] == "6001"


def test_fdata_kv():
    u = db.create_user("alice", "HASH", "A")
    assert db.fdata_get(u.id, "timesheet", "projects", default=[]) == []
    db.fdata_set(u.id, "timesheet", "projects", [{"id": "6001"}])
    assert db.fdata_get(u.id, "timesheet", "projects")[0]["id"] == "6001"


def test_job_run_log():
    u = db.create_user("alice", "HASH", "A")
    db.record_job_run(u.id, "timesheet", "weekly", True, "成功", {"filled": 5})
    runs = db.read_job_runs(u.id, "timesheet")
    assert len(runs) == 1
    assert runs[0]["ok"] is True
    assert runs[0]["detail"]["filled"] == 5


def test_enabled_feature_pairs():
    u = db.create_user("alice", "HASH", "A")
    db.save_feature_config(u.id, "meican", db.FeatureConfig("meican", True, True, {}, {}))
    pairs = db.enabled_feature_pairs()
    assert (u.id, "meican") in pairs
```

- [ ] **Step 2: 运行测试确认失败**

Run: `./.venv/bin/python -m pytest tests/test_core_db.py -v`
Expected: FAIL — `ImportError` / `AttributeError: module 'app.core.db' has no attribute ...`

- [ ] **Step 3: 实现 core/db.py**

创建 `app/core/db.py`：

```python
"""SQLite 存储层：系统用户、会话、功能凭据、配置、KV、运行记录。

- 凭据用 Fernet 加密落库，密钥来自 data/secret.key（或 MC_SECRET_KEY 环境变量）
- 测试时通过 monkeypatch 替换 DB_PATH / KEY_PATH 指向临时文件
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "app.db"
KEY_PATH = DATA_DIR / "secret.key"

SESSION_DAYS = 30
SESSION_REFRESH_THRESHOLD = 7 * 86400
MAX_SESSIONS_PER_USER = 10


# ---------------- 加密 ----------------

def _load_key() -> bytes:
    env = os.environ.get("MC_SECRET_KEY")
    if env:
        return hashlib.sha256(env.encode()).digest()
    if KEY_PATH.exists():
        return hashlib.sha256(KEY_PATH.read_bytes()).digest()
    raw = secrets.token_bytes(32)
    KEY_PATH.write_bytes(raw)
    try:
        os.chmod(KEY_PATH, 0o600)
    except OSError:
        pass
    return hashlib.sha256(raw).digest()


from cryptography.fernet import Fernet

_fernet = Fernet(base64.urlsafe_b64encode(_load_key()))


def encrypt(s: str) -> str:
    return _fernet.encrypt(s.encode()).decode() if s else ""


def decrypt(s: str) -> str:
    if not s:
        return ""
    try:
        return _fernet.decrypt(s.encode()).decode()
    except Exception:  # noqa: BLE001
        return ""


# ---------------- 连接 ----------------

def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def conn() -> Iterator[sqlite3.Connection]:
    c = _connect()
    try:
        yield c
        c.commit()
    except Exception:
        c.rollback()
        raise
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


# ---------------- 会话 ----------------

def issue_session(user_id: int, ua: str = "") -> tuple[str, int]:
    token = secrets.token_hex(32)
    exp = int(time.time()) + SESSION_DAYS * 86400
    now = int(time.time())
    with conn() as c:
        c.execute("INSERT INTO sessions(token,user_id,created_at,exp,ua) VALUES(?,?,?,?,?)",
                  (token, user_id, now, exp, ua[:200]))
        old = c.execute(
            "SELECT token FROM sessions WHERE user_id=? ORDER BY created_at DESC"
            " LIMIT -1 OFFSET ?", (user_id, MAX_SESSIONS_PER_USER)).fetchall()
        if old:
            c.executemany("DELETE FROM sessions WHERE token=?", [(r["token"],) for r in old])
    return token, exp


def user_by_session(token: str) -> User | None:
    if not token:
        return None
    now = time.time()
    with conn() as c:
        s = c.execute("SELECT * FROM sessions WHERE token=?", (token,)).fetchone()
        if not s:
            return None
        if s["exp"] < now:
            c.execute("DELETE FROM sessions WHERE token=?", (token,))
            return None
        if s["exp"] - now < SESSION_REFRESH_THRESHOLD:
            c.execute("UPDATE sessions SET exp=? WHERE token=?",
                      (int(now) + SESSION_DAYS * 86400, token))
        row = c.execute("SELECT * FROM users WHERE id=?", (s["user_id"],)).fetchone()
    return _row_to_user(row) if row else None


def revoke_session(token: str) -> None:
    with conn() as c:
        c.execute("DELETE FROM sessions WHERE token=?", (token,))


def list_sessions(user_id: int) -> list[dict]:
    with conn() as c:
        rows = c.execute(
            "SELECT created_at, exp, ua FROM sessions WHERE user_id=? AND exp>?"
            " ORDER BY created_at DESC", (user_id, int(time.time()))).fetchall()
    return [{"created_at": r["created_at"], "exp": r["exp"], "ua": r["ua"]} for r in rows]


# ---------------- 功能凭据 ----------------

@dataclass
class Credential:
    feature_key: str
    payload: dict = field(default_factory=dict)
    state: str = "not_configured"
    last_ok_at: int = 0
    last_error: str = ""


def load_credential(user_id: int, feature_key: str) -> Credential:
    with conn() as c:
        row = c.execute(
            "SELECT * FROM feature_credentials WHERE user_id=? AND feature_key=?",
            (user_id, feature_key)).fetchone()
    if not row:
        return Credential(feature_key)
    try:
        payload = json.loads(decrypt(row["payload"]) or "{}")
    except json.JSONDecodeError:
        payload = {}
    return Credential(feature_key, payload, row["state"],
                      row["last_ok_at"] or 0, row["last_error"] or "")


def save_credential(user_id: int, feature_key: str, payload: dict, state: str) -> None:
    now = int(time.time())
    with conn() as c:
        c.execute(
            "INSERT INTO feature_credentials"
            " (user_id,feature_key,payload,state,last_ok_at,last_error,updated_at)"
            " VALUES(?,?,?,?,?,?,?) ON CONFLICT(user_id,feature_key) DO UPDATE SET"
            " payload=excluded.payload, state=excluded.state,"
            " last_ok_at=excluded.last_ok_at, last_error=excluded.last_error,"
            " updated_at=excluded.updated_at",
            (user_id, feature_key, encrypt(json.dumps(payload, ensure_ascii=False)),
             state, now if state == "active" else 0, "", now),
        )


def set_credential_state(user_id: int, feature_key: str, state: str,
                         error: str = "") -> None:
    with conn() as c:
        c.execute(
            "INSERT INTO feature_credentials"
            " (user_id,feature_key,state,last_error,updated_at) VALUES(?,?,?,?,?)"
            " ON CONFLICT(user_id,feature_key) DO UPDATE SET"
            " state=excluded.state, last_error=excluded.last_error,"
            " updated_at=excluded.updated_at",
            (user_id, feature_key, state, error, int(time.time())),
        )


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
    return FeatureConfig(
        feature_key, bool(row["enabled"]), bool(row["dry_run"]),
        json.loads(row["schedule_json"] or "{}"),
        json.loads(row["options_json"] or "{}"),
    )


def save_feature_config(user_id: int, feature_key: str, cfg: FeatureConfig) -> None:
    with conn() as c:
        c.execute(
            "INSERT INTO feature_configs"
            " (user_id,feature_key,enabled,dry_run,schedule_json,options_json)"
            " VALUES(?,?,?,?,?,?) ON CONFLICT(user_id,feature_key) DO UPDATE SET"
            " enabled=excluded.enabled, dry_run=excluded.dry_run,"
            " schedule_json=excluded.schedule_json, options_json=excluded.options_json",
            (user_id, feature_key, int(cfg.enabled), int(cfg.dry_run),
             json.dumps(cfg.schedule, ensure_ascii=False),
             json.dumps(cfg.options, ensure_ascii=False)),
        )


def enabled_feature_pairs() -> list[tuple[int, str]]:
    with conn() as c:
        rows = c.execute(
            "SELECT user_id, feature_key FROM feature_configs WHERE enabled=1").fetchall()
    return [(r["user_id"], r["feature_key"]) for r in rows]


# ---------------- 插件私有 KV ----------------

def fdata_get(user_id: int, feature_key: str, key: str, default: Any = None) -> Any:
    with conn() as c:
        row = c.execute(
            "SELECT value FROM feature_data WHERE user_id=? AND feature_key=? AND key=?",
            (user_id, feature_key, key)).fetchone()
    if not row:
        return default
    try:
        return json.loads(row["value"])
    except json.JSONDecodeError:
        return default


def fdata_set(user_id: int, feature_key: str, key: str, value: Any) -> None:
    with conn() as c:
        c.execute(
            "INSERT INTO feature_data(user_id,feature_key,key,value,updated_at)"
            " VALUES(?,?,?,?,?) ON CONFLICT(user_id,feature_key,key) DO UPDATE SET"
            " value=excluded.value, updated_at=excluded.updated_at",
            (user_id, feature_key, key,
             json.dumps(value, ensure_ascii=False), int(time.time())))


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
```

- [ ] **Step 4: 运行测试确认通过**

Run: `./.venv/bin/python -m pytest tests/test_core_db.py -v`
Expected: 10 passed

- [ ] **Step 5: 提交**

```bash
git add app/core/db.py tests/test_core_db.py
git commit -m "feat(core): 多用户数据模型（用户/会话/功能凭据/配置/KV/运行记录）"
```

---

## Task 3: Feature 契约（core/feature.py）

**Files:**
- Create: `app/core/feature.py`
- Create: `tests/test_feature_contract.py`

**Interfaces:**
- Consumes: 无
- Produces:
  - `feature.CredentialKind`（`PASSWORD` / `INTERACTIVE`）
  - `feature.FeatureState`（`NOT_CONFIGURED` / `ACTIVE` / `EXPIRED` / `ERROR`）
  - `feature.FieldSpec(name, label, type="text", secret=False, required=True)`
  - `feature.JobSpec(job_id, name, trigger, default, jitter=0, editable=True)`
  - `feature.OptionSpec(name, label, type="select", source="", refreshable=False)`
  - `feature.JobContext(user_id, feature_key, job_id, dry_run, options, get_data, set_data)`
  - `feature.JobResult(ok, message="", detail={})`
  - `feature.Feature` 抽象基类
  - `feature.FeatureLoginError(Exception)`

- [ ] **Step 1: 写失败的测试**

创建 `tests/test_feature_contract.py`：

```python
import pytest

from app.core.feature import (CredentialKind, Feature, FeatureLoginError,
                              FieldSpec, JobContext, JobResult, JobSpec,
                              OptionSpec)


class DummyFeature(Feature):
    key = "dummy"
    name = "示例"
    description = "测试用"
    credential_kind = CredentialKind.PASSWORD
    credential_fields = [FieldSpec("username", "用户名")]
    default_jobs = [JobSpec("tick", "示例任务", "interval", {"minutes": 5})]

    async def login(self, user_id, payload):
        if payload.get("username") != "ok":
            raise FeatureLoginError("用户名不对")
        return {"display": "已登录"}

    async def check_alive(self, user_id):
        return True

    async def run_job(self, user_id, job_id, ctx):
        return JobResult(True, f"执行 {job_id}")


def test_feature_metadata():
    f = DummyFeature()
    assert f.key == "dummy"
    assert f.credential_kind is CredentialKind.PASSWORD
    assert f.dry_run_default is False
    assert f.extra_options == []


def test_cannot_instantiate_abstract():
    with pytest.raises(TypeError):
        Feature()


def test_jobspec_defaults():
    j = JobSpec("x", "任务", "cron", {"hour": 1})
    assert j.jitter == 0
    assert j.editable is True


def test_optionspec_defaults():
    o = OptionSpec("pid", "项目")
    assert o.type == "select"
    assert o.refreshable is False


def test_login_raises_typed_error():
    import asyncio
    f = DummyFeature()
    with pytest.raises(FeatureLoginError):
        asyncio.run(f.login(1, {"username": "bad"}))


def test_job_context_reads_and_writes(tmp_path, monkeypatch):
    from app.core import db
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr(db, "KEY_PATH", tmp_path / "t.key")
    db.init_db()
    ctx = JobContext(1, "dummy", "tick", True, {"p": "1"})
    assert ctx.dry_run is True
    assert ctx.options["p"] == "1"
    assert ctx.get_data("k", default="d") == "d"
    ctx.set_data("k", {"v": 1})
    assert ctx.get_data("k")["v"] == 1
```

- [ ] **Step 2: 运行测试确认失败**

Run: `./.venv/bin/python -m pytest tests/test_feature_contract.py -v`
Expected: FAIL — `ModuleNotFoundError: app.core.feature`

- [ ] **Step 3: 实现 core/feature.py**

创建 `app/core/feature.py`：

```python
"""功能插件契约。

core 只定义"一个功能长什么样、怎么被调用"，不关心具体业务。
插件实现 Feature 抽象基类即可接入平台。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class CredentialKind(str, Enum):
    """凭据类型，决定登录态失效后的策略。"""
    PASSWORD = "password"        # 可用已存凭据自动重登
    INTERACTIVE = "interactive"  # 必须人工介入（如短信验证码）


class FeatureState(str, Enum):
    NOT_CONFIGURED = "not_configured"
    ACTIVE = "active"
    EXPIRED = "expired"
    ERROR = "error"


class FeatureLoginError(Exception):
    """登录/凭据校验失败。消息会直接展示给用户。"""


@dataclass
class FieldSpec:
    name: str
    label: str
    type: str = "text"          # text | password
    secret: bool = False        # 是否加密存储（payload 整体加密，此项用于界面掩码）
    required: bool = True


@dataclass
class JobSpec:
    job_id: str
    name: str
    trigger: str                # cron | interval
    default: dict                # cron: {"hour":15,"minute":30} / interval: {"minutes":20}
    jitter: int = 0             # 随机抖动分钟，仅 interval
    editable: bool = True


@dataclass
class OptionSpec:
    name: str
    label: str
    type: str = "select"
    source: str = ""
    refreshable: bool = False


@dataclass
class JobResult:
    ok: bool
    message: str = ""
    detail: dict = field(default_factory=dict)


class JobContext:
    """任务执行上下文。插件通过它拿配置与读写私有缓存，不直接碰数据库。"""

    def __init__(self, user_id: int, feature_key: str, job_id: str,
                 dry_run: bool, options: dict):
        self.user_id = user_id
        self.feature_key = feature_key
        self.job_id = job_id
        self.dry_run = dry_run
        self.options = options or {}

    def get_data(self, key: str, default: Any = None) -> Any:
        from . import db
        return db.fdata_get(self.user_id, self.feature_key, key, default)

    def set_data(self, key: str, value: Any) -> None:
        from . import db
        db.fdata_set(self.user_id, self.feature_key, key, value)


class Feature(ABC):
    key: str
    name: str
    description: str = ""
    credential_kind: CredentialKind
    credential_fields: list[FieldSpec] = []
    default_jobs: list[JobSpec] = []
    extra_options: list[OptionSpec] = []
    dry_run_default: bool = False

    @abstractmethod
    async def login(self, user_id: int, payload: dict) -> dict:
        """校验凭据并持久化（由调用方落库）。失败抛 FeatureLoginError。

        返回展示信息，至少包含 {"display": "..."}。
        """

    @abstractmethod
    async def check_alive(self, user_id: int) -> bool:
        """检查登录态是否仍然有效。"""

    @abstractmethod
    async def run_job(self, user_id: int, job_id: str, ctx: JobContext) -> JobResult:
        """执行任务。"""

    async def relogin(self, user_id: int) -> bool:
        """用已存凭据自动重登。仅 CredentialKind.PASSWORD 需要实现。"""
        return False

    async def refresh_options(self, user_id: int) -> list[dict]:
        """刷新可选项（如下拉数据）。返回 [{"value":..., "label":...}]。"""
        return []
```

- [ ] **Step 4: 运行测试确认通过**

Run: `./.venv/bin/python -m pytest tests/test_feature_contract.py -v`
Expected: 6 passed

- [ ] **Step 5: 提交**

```bash
git add app/core/feature.py tests/test_feature_contract.py
git commit -m "feat(core): Feature 插件契约"
```

---

## Task 4: 注册表与统一执行器

**Files:**
- Create: `app/core/registry.py`
- Create: `app/core/runner.py`
- Create: `tests/test_registry_runner.py`

**Interfaces:**
- Consumes: `core.feature.Feature`, `core.feature.JobContext`, `core.feature.JobResult`, `core.db`
- Produces:
  - `registry.register(feature: Feature) -> None`
  - `registry.get(key: str) -> Feature | None`
  - `registry.all() -> list[Feature]`
  - `registry.clear() -> None`（测试用）
  - `runner.execute(user_id: int, feature_key: str, job_id: str) -> JobResult`
  - `runner.run_login(user_id: int, feature_key: str, payload: dict) -> dict`

- [ ] **Step 1: 写失败的测试**

创建 `tests/test_registry_runner.py`：

```python
import asyncio

import pytest

from app.core import db, registry, runner
from app.core.feature import (CredentialKind, Feature, FeatureLoginError,
                              JobContext, JobResult, JobSpec)


class FakeFeature(Feature):
    key = "fake"
    name = "假功能"
    credential_kind = CredentialKind.PASSWORD
    default_jobs = [JobSpec("j", "任务", "interval", {"minutes": 5})]

    def __init__(self):
        self.alive = True
        self.relogin_ok = True
        self.executed = 0

    async def login(self, user_id, payload):
        if payload.get("bad"):
            raise FeatureLoginError("凭据错误")
        return {"display": "ok"}

    async def check_alive(self, user_id):
        return self.alive

    async def run_job(self, user_id, job_id, ctx: JobContext):
        self.executed += 1
        return JobResult(True, f"done dry_run={ctx.dry_run}")

    async def relogin(self, user_id):
        return self.relogin_ok


class InteractiveFeature(FakeFeature):
    key = "inter"
    credential_kind = CredentialKind.INTERACTIVE


@pytest.fixture(autouse=True)
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr(db, "KEY_PATH", tmp_path / "t.key")
    db.init_db()
    registry.clear()
    yield
    registry.clear()


def _user():
    return db.create_user("u", "HASH", "U")


def test_register_and_get():
    f = FakeFeature()
    registry.register(f)
    assert registry.get("fake") is f
    assert registry.get("nope") is None
    assert len(registry.all()) == 1


def test_run_login_persists_credential():
    u = _user()
    registry.register(FakeFeature())
    result = runner.run_login(u.id, "fake", {"username": "ok"})
    assert result["display"] == "ok"
    assert db.load_credential(u.id, "fake").state == "active"


def test_run_login_failure_does_not_persist_active():
    u = _user()
    registry.register(FakeFeature())
    with pytest.raises(FeatureLoginError):
        runner.run_login(u.id, "fake", {"bad": True})
    assert db.load_credential(u.id, "fake").state == "not_configured"


def test_execute_skips_when_not_configured():
    u = _user()
    f = FakeFeature()
    registry.register(f)
    r = runner.execute(u.id, "fake", "j")
    assert r.ok is False
    assert "未配置" in r.message
    assert f.executed == 0


def test_execute_runs_when_active():
    u = _user()
    f = FakeFeature()
    registry.register(f)
    runner.run_login(u.id, "fake", {"username": "ok"})
    r = runner.execute(u.id, "fake", "j")
    assert r.ok is True
    assert f.executed == 1


def test_execute_passes_dry_run_from_config():
    u = _user()
    f = FakeFeature()
    registry.register(f)
    runner.run_login(u.id, "fake", {"username": "ok"})
    db.save_feature_config(u.id, "fake", db.FeatureConfig("fake", True, False, {}, {}))
    r = runner.execute(u.id, "fake", "j")
    assert "dry_run=False" in r.message


def test_execute_autorelogin_for_password_kind():
    u = _user()
    f = FakeFeature()
    registry.register(f)
    runner.run_login(u.id, "fake", {"username": "ok"})
    f.alive = False
    f.relogin_ok = True
    r = runner.execute(u.id, "fake", "j")
    assert r.ok is True
    assert f.executed == 1


def test_execute_marks_expired_when_relogin_fails():
    u = _user()
    f = FakeFeature()
    registry.register(f)
    runner.run_login(u.id, "fake", {"username": "ok"})
    f.alive = False
    f.relogin_ok = False
    r = runner.execute(u.id, "fake", "j")
    assert r.ok is False
    assert db.load_credential(u.id, "fake").state == "expired"
    assert f.executed == 0


def test_execute_interactive_kind_never_relogins():
    u = _user()
    f = InteractiveFeature()
    registry.register(f)
    runner.run_login(u.id, "fake" if False else "inter", {"username": "ok"}) if False else None
    runner.run_login(u.id, "inter", {"username": "ok"})
    f.alive = False
    r = runner.execute(u.id, "inter", "j")
    assert r.ok is False
    assert db.load_credential(u.id, "inter").state == "expired"


def test_execute_records_job_run():
    u = _user()
    f = FakeFeature()
    registry.register(f)
    runner.run_login(u.id, "fake", {"username": "ok"})
    runner.execute(u.id, "fake", "j")
    runs = db.read_job_runs(u.id, "fake")
    assert len(runs) == 1
    assert runs[0]["ok"] is True
```

- [ ] **Step 2: 运行测试确认失败**

Run: `./.venv/bin/python -m pytest tests/test_registry_runner.py -v`
Expected: FAIL — `ModuleNotFoundError: app.core.registry`

- [ ] **Step 3: 实现 registry.py**

创建 `app/core/registry.py`：

```python
"""插件注册表。

启动时由 main.py 逐个 register，框架其余部分只通过 key 取插件。
"""
from __future__ import annotations

from .feature import Feature

_registry: dict[str, Feature] = {}


def register(feature: Feature) -> None:
    if feature.key in _registry:
        raise ValueError(f"功能 key 重复注册：{feature.key}")
    _registry[feature.key] = feature


def get(key: str) -> Feature | None:
    return _registry.get(key)


def all() -> list[Feature]:
    return list(_registry.values())


def clear() -> None:
    """仅测试使用。"""
    _registry.clear()
```

- [ ] **Step 4: 实现 runner.py**

创建 `app/core/runner.py`：

```python
"""统一任务执行流程。

把"登录态校验 → 按凭据类型决定是否自动重登 → 执行任务 → 记录"固化在这里，
每个插件只实现自己的机制，策略由框架统一。
"""
from __future__ import annotations

from . import db, registry
from .feature import (CredentialKind, FeatureLoginError, FeatureState,
                      JobContext, JobResult)


def run_login(user_id: int, feature_key: str, payload: dict) -> dict:
    """校验凭据并落库。失败抛 FeatureLoginError，且不写入 active 状态。"""
    import asyncio

    feature = registry.get(feature_key)
    if not feature:
        raise FeatureLoginError(f"未知功能：{feature_key}")
    info = asyncio.run(feature.login(user_id, payload))
    db.save_credential(user_id, feature_key, payload, FeatureState.ACTIVE.value)
    return info or {}


def execute(user_id: int, feature_key: str, job_id: str) -> JobResult:
    """执行一次任务。同步接口，内部用 asyncio.run 驱动插件协程。

    注意：本函数设计为在后台线程中调用（见 scheduler），不要在线程池外嵌套事件循环。
    """
    import asyncio

    feature = registry.get(feature_key)
    if not feature:
        return JobResult(False, f"未知功能：{feature_key}")

    cred = db.load_credential(user_id, feature_key)
    if cred.state == FeatureState.NOT_CONFIGURED.value:
        return JobResult(False, "该功能未配置凭据，请先登录")
    if cred.state == FeatureState.EXPIRED.value:
        return JobResult(False, "凭据已失效，请在功能页面重新登录")

    async def _run() -> JobResult:
        if not await feature.check_alive(user_id):
            if feature.credential_kind == CredentialKind.PASSWORD:
                if not await feature.relogin(user_id):
                    db.set_credential_state(user_id, feature_key,
                                            FeatureState.EXPIRED.value, "自动重登失败")
                    return JobResult(False, "登录态失效且自动重登失败，请重新登录")
            else:
                db.set_credential_state(user_id, feature_key,
                                        FeatureState.EXPIRED.value, "登录态失效")
                return JobResult(False, "登录态失效，请重新登录该功能")

        cfg = db.load_feature_config(user_id, feature_key)
        ctx = JobContext(user_id, feature_key, job_id, cfg.dry_run, cfg.options)
        try:
            return await feature.run_job(user_id, job_id, ctx)
        except Exception as exc:  # noqa: BLE001
            return JobResult(False, f"{type(exc).__name__}: {exc}")

    result = asyncio.run(_run())
    db.record_job_run(user_id, feature_key, job_id, result.ok,
                      result.message, result.detail)
    return result
```

- [ ] **Step 5: 运行测试确认通过**

Run: `./.venv/bin/python -m pytest tests/test_registry_runner.py -v`
Expected: 9 passed

- [ ] **Step 6: 提交**

```bash
git add app/core/registry.py app/core/runner.py tests/test_registry_runner.py
git commit -m "feat(core): 插件注册表与统一执行器"
```

---

## Task 5: 美餐插件迁移（client + preference）

**Files:**
- Create: `app/features/meican/__init__.py`
- Move: `app/meican.py` → `app/features/meican/client.py`
- Move: `app/preference.py` → `app/features/meican/preference.py`
- Create: `tests/test_meican_preference.py`

**Interfaces:**
- Consumes: 无
- Produces: `meican.client.MeicanClient`、`meican.client.MeicanError`、`meican.preference.PreferenceModel` 等（保持原签名，仅路径变化）

- [ ] **Step 1: 复制文件到新位置**

```bash
cd /Users/admin/meican-auto
mkdir -p app/features/meican
touch app/features/meican/__init__.py
git mv app/meican.py app/features/meican/client.py
git mv app/preference.py app/features/meican/preference.py
```

- [ ] **Step 2: 修正 preference.py 的导入**

`app/features/meican/preference.py` 无外部导入，保持原样即可。确认无 `from app.` 语句：

Run: `grep -n "^from app\|^import app" app/features/meican/preference.py`
Expected: 无输出

- [ ] **Step 3: 写 preference 回归测试**

创建 `tests/test_meican_preference.py`：

```python
from datetime import date

from app.features.meican.preference import (DishCandidate, PreferenceModel,
                                            name_similarity, rank_candidates)


def test_name_similarity_identical_is_one():
    assert name_similarity("香酥小烤鸭套餐", "香酥小烤鸭套餐") == 1.0


def test_name_similarity_unrelated_is_low():
    assert name_similarity("香酥小烤鸭套餐", "糙米罗勒烤鸡胸碗") < 0.3


def test_model_prefers_similar_history():
    model = PreferenceModel.from_history([
        {"date": "2026-11-16", "restaurant": "金百万", "dish": "烤鸭套餐"},
        {"date": "2026-11-10", "restaurant": "陕味食族", "dish": "油泼面"},
    ])
    day = date(2026, 11, 17)
    duck = DishCandidate("金百万", "r1", 1, "香酥小烤鸭套餐", 25.0)
    noodle = DishCandidate("祐禾", "r2", 2, "半个牛奶吐司套餐", 25.0)
    model.score(duck, day)
    model.score(noodle, day)
    assert duck.score > noodle.score


def test_recent_duplicate_is_penalized():
    model = PreferenceModel.from_history([
        {"date": "2026-11-16", "restaurant": "金百万", "dish": "烤鸭套餐"},
    ])
    day = date(2026, 11, 17)
    same = DishCandidate("金百万", "r1", 1, "烤鸭套餐", 25.0)
    model.score(same, day)
    assert same.score < 0          # 1 天前刚吃过，重罚


def test_rank_returns_limit():
    model = PreferenceModel.from_history([])
    cands = [DishCandidate("A", "1", i, f"菜品{i}", 25.0) for i in range(10)]
    top = rank_candidates(cands, date(2026, 11, 17), model, limit=3)
    assert len(top) == 3
```

- [ ] **Step 4: 运行测试确认通过**

Run: `./.venv/bin/python -m pytest tests/test_meican_preference.py -v`
Expected: 5 passed

- [ ] **Step 5: 检查 client.py 的导入路径**

Run: `grep -n "^from \.\|^from app" app/features/meican/client.py`

`client.py` 只依赖 httpx 与标准库，应当无内部导入。若有 `from . import` 之外的问题，按实际修正。

- [ ] **Step 6: 提交**

```bash
git add app/features/meican/ tests/test_meican_preference.py
git commit -m "refactor: 美餐 client 与 preference 迁入 features/meican"
```

---

## Task 6: 美餐插件实现（jobs + feature）

**Files:**
- Create: `app/features/meican/jobs.py`（由 `app/service.py` + `app/keepalive.py` 合并改造）
- Create: `app/features/meican/feature.py`
- Create: `tests/test_meican_feature.py`

**Interfaces:**
- Consumes: `meican.client.MeicanClient`、`meican.preference.*`、`core.feature.*`、`core.db`
- Produces:
  - `meican.jobs.fetch_history(payload: dict, cache: dict) -> list[dict]`
  - `meican.jobs.recommend(payload: dict, cache: dict, target_day: date, limit: int) -> dict`
  - `meican.jobs.do_order(payload: dict, cache: dict, dish: dict | None, dry_run: bool) -> dict`
  - `meican.jobs.keepalive(payload: dict) -> bool`
  - `meican.feature.MeicanFeature`（实现 `Feature`）

- [ ] **Step 1: 写失败的测试**

创建 `tests/test_meican_feature.py`：

```python
import pytest

from app.core.feature import CredentialKind, FeatureLoginError, JobContext
from app.features.meican.feature import MeicanFeature


def test_metadata():
    f = MeicanFeature()
    assert f.key == "meican"
    assert f.credential_kind is CredentialKind.INTERACTIVE
    assert {x.name for x in f.credential_fields} == {"phone", "code"}
    ids = {j.job_id for j in f.default_jobs}
    assert ids == {"order", "keepalive"}
    keepalive = next(j for j in f.default_jobs if j.job_id == "keepalive")
    assert keepalive.editable is False


def test_login_requires_phone_and_code(monkeypatch):
    import asyncio
    f = MeicanFeature()
    with pytest.raises(FeatureLoginError):
        asyncio.run(f.login(1, {"phone": ""}))
    with pytest.raises(FeatureLoginError):
        asyncio.run(f.login(1, {"phone": "176", "code": ""}))


def test_login_stores_token_from_client(monkeypatch):
    import asyncio
    f = MeicanFeature()

    class FakeClient:
        def login_with_sms(self, phone, code):
            return {"ticket": "T", "signature": "S",
                    "userList": [{"accountVersion": "AccountThreeDotZero",
                                  "snowflakeId": "SF", "id": "1", "name": "张三"}]}

        def choose_account(self, ticket, signature, snowflake_id=None, **kw):
            return {"accessToken": "AT", "refreshToken": "RT",
                    "expiry": "3600", "tokenType": "bearer"}

    monkeypatch.setattr("app.features.meican.feature.MeicanClient", lambda: FakeClient())
    monkeypatch.setattr("app.features.meican.feature.db.save_credential", lambda *a, **k: None)
    info = asyncio.run(f.login(1, {"phone": "17600610776", "code": "1234"}))
    assert info["display"] == "张三"


def test_run_job_dispatches_by_id(monkeypatch):
    import asyncio
    from app.core.feature import JobResult
    f = MeicanFeature()
    monkeypatch.setattr("app.features.meican.feature.jobs.do_order",
                        lambda *a, **k: JobResult(True, "ordered"))
    ctx = JobContext(1, "meican", "order", True, {})
    r = asyncio.run(f.run_job(1, "order", ctx))
    assert r.ok is True
    assert r.message == "ordered"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `./.venv/bin/python -m pytest tests/test_meican_feature.py -v`
Expected: FAIL — `ModuleNotFoundError: app.features.meican.feature`

- [ ] **Step 3: 创建 jobs.py**

创建 `app/features/meican/jobs.py`，内容为**把现有 `app/service.py` 与 `app/keepalive.py` 的逻辑改写为无全局状态的纯函数**，凭据由 `payload` 传入，缓存由 `cache` dict 传入：

```python
"""美餐业务逻辑：订餐、保活。

与旧 service.py 的区别：
- 不再从全局 db 读凭据，改为由 payload（解密后的凭据 dict）传入
- 餐厅名缓存由调用方以 dict 传入/传出，落库交给 feature 层
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

from ...core.feature import JobResult
from .client import MeicanClient, MeicanError
from .preference import DishCandidate, PreferenceModel, rank_candidates, summarize

STATUS_ORDERED = "ORDER"


def _client(payload: dict) -> MeicanClient:
    token = payload.get("accessToken") or ""
    if not token:
        raise MeicanError("凭据缺失，请重新登录")
    return MeicanClient(token)


def fetch_history(payload: dict, cache: dict, days: int = 45) -> list[dict]:
    """拉历史订单，顺带补全 cache 里的餐厅 id→名字。"""
    client = _client(payload)
    end = date.today()
    start = end - timedelta(days=days)
    records: list[dict] = []
    mid = start + timedelta(days=days // 2)
    raw: list[tuple[str, dict]] = []
    for b, e in ((start, mid), (mid + timedelta(days=1), end)):
        if b > e:
            continue
        for day in client.calendar_items(b, e, with_order=True):
            d = day.get("date")
            for item in day.get("calendarItemList") or []:
                if not item.get("corpOrderUser"):
                    continue
                raw.append((d, item))
                tab = (item.get("userTab") or {}).get("uniqueId")
                if tab:
                    try:
                        for r in client.restaurants(tab, _target_time_str(item)):
                            rid, rname = r.get("uniqueId", ""), r.get("name", "")
                            if rid and rname:
                                cache[rid] = rname
                    except MeicanError:
                        pass
    for d, item in raw:
        order = item["corpOrderUser"]
        for ri in order.get("restaurantItemList") or []:
            rid = ri.get("uniqueId", "")
            rname = cache.get(rid) or "(未知餐厅)"
            for di in ri.get("dishItemList") or []:
                dish = di.get("dish") or {}
                records.append({
                    "date": d, "restaurant": rname, "restaurant_id": rid,
                    "dish": dish.get("name", ""), "dish_id": dish.get("id"),
                    "price": dish.get("priceInCent", 0) / 100,
                    "count": di.get("count", 1),
                    "corp_order_status": order.get("corpOrderStatus"),
                })
    records.sort(key=lambda r: r["date"] or "")
    return records


def _target_time_str(item: dict) -> str:
    ts = item.get("targetTime")
    if ts:
        return datetime.fromtimestamp(ts / 1000).strftime("%Y-%m-%d %H:%M")
    opening = item.get("openingTime") or {}
    return f"{date.today().isoformat()} {opening.get('closeTime', '18:00')}"


def find_meal_item(items: list[dict]) -> dict | None:
    if not items:
        return None
    for it in items:
        if "晚餐" in (it.get("title") or ""):
            return it
    return items[0]


def build_candidates(client: MeicanClient, item: dict, max_price: float) -> list[DishCandidate]:
    tab = (item.get("userTab") or {}).get("uniqueId")
    tt = _target_time_str(item)
    out: list[DishCandidate] = []
    for r in client.restaurants(tab, tt):
        try:
            menu = client.restaurant_menu(r["uniqueId"], tab, tt)
        except MeicanError:
            continue
        for dish in menu.get("dishList") or []:
            if dish.get("isSection"):
                continue
            price = dish.get("priceInCent", 0) / 100
            if max_price and price > max_price:
                continue
            out.append(DishCandidate(r.get("name", ""), r.get("uniqueId", ""),
                                     dish.get("id"), dish.get("name", ""), price))
    return out


def check_status(payload: dict, target_day: date) -> dict:
    client = _client(payload)
    days = client.calendar_items(target_day, target_day, with_order=True)
    if not days or not days[0].get("calendarItemList"):
        return {"date": target_day.isoformat(), "has_item": False, "ordered": False}
    item = find_meal_item(days[0]["calendarItemList"])
    if not item:
        return {"date": target_day.isoformat(), "has_item": False, "ordered": False}
    return {
        "date": target_day.isoformat(), "has_item": True,
        "ordered": bool(item.get("corpOrderUser")) or item.get("status") == STATUS_ORDERED,
        "status": item.get("status"), "reason": item.get("reason", ""),
        "title": item.get("title"),
        "opening_time": item.get("openingTime"),
        "order_detail": _extract_order(item),
        "raw_item": item,
    }


def _extract_order(item: dict) -> dict | None:
    order = item.get("corpOrderUser")
    if not order:
        return None
    dishes = []
    for ri in order.get("restaurantItemList") or []:
        for di in ri.get("dishItemList") or []:
            d = di.get("dish") or {}
            dishes.append({"name": d.get("name"),
                           "price": d.get("priceInCent", 0) / 100,
                           "count": di.get("count")})
    return {"dishes": dishes, "pay_status": order.get("payStatus"),
            "corp_order_status": order.get("corpOrderStatus"),
            "postbox": order.get("postbox"), "address": order.get("corpAddress")}


def recommend(payload: dict, cache: dict, target_day: date, limit: int = 5,
              max_price: float = 25.0) -> dict:
    client = _client(payload)
    days = client.calendar_items(target_day, target_day, with_order=True)
    if not days or not days[0].get("calendarItemList"):
        return {"date": target_day.isoformat(), "error": "该日期没有可订餐次"}
    item = find_meal_item(days[0]["calendarItemList"])
    if not item:
        return {"date": target_day.isoformat(), "error": "未找到餐次"}
    model = PreferenceModel.from_history(fetch_history(payload, cache))
    cands = build_candidates(client, item, max_price)
    top = rank_candidates(cands, target_day, model, limit=limit)
    return {
        "date": target_day.isoformat(), "title": item.get("title"),
        "status": item.get("status"),
        "ordered": bool(item.get("corpOrderUser")),
        "candidates_total": len(cands),
        "recommendations": [
            {"restaurant": c.restaurant_name, "dish": c.dish_name, "price": c.price,
             "score": c.score, "reasons": c.reasons, "dish_id": c.dish_id,
             "restaurant_id": c.restaurant_id} for c in top],
        "model": summarize(model),
    }


def _last_known_address(client: MeicanClient) -> str | None:
    end = date.today()
    try:
        for day in client.calendar_items(end - timedelta(days=30), end, with_order=True):
            for item in day.get("calendarItemList") or []:
                addr = (item.get("corpOrderUser") or {}).get("corpAddress") or {}
                if addr.get("uniqueId"):
                    return addr["uniqueId"]
    except MeicanError:
        return None
    return None


def do_order(payload: dict, cache: dict, target_day: date,
             dish: dict | None, dry_run: bool) -> JobResult:
    client = _client(payload)
    st = check_status(payload, target_day)
    if st.get("ordered"):
        return JobResult(True, f"{target_day.isoformat()} 已订餐，跳过", {"skipped": True})
    if dish is None:
        rec = recommend(payload, cache, target_day, limit=1)
        if rec.get("error") or not rec.get("recommendations"):
            return JobResult(False, rec.get("error", "没有可用推荐"))
        dish = rec["recommendations"][0]
    item = st.get("raw_item")
    if not item:
        return JobResult(False, "找不到餐次信息")
    target_time = _target_time_str(item)
    tab_id = (item.get("userTab") or {}).get("uniqueId")
    addr = ((item.get("corpOrderUser") or {}).get("corpAddress") or {})
    address_id = addr.get("uniqueId") or _last_known_address(client) or ""
    if not address_id:
        return JobResult(False, "找不到取餐地址，请先手动在网页订一次")
    plan = {"date": target_day.isoformat(), "target_time": target_time,
            "tab_unique_id": tab_id, "address_id": address_id, "dish": dish}
    if dry_run:
        return JobResult(True, f"演练：将下单 {dish.get('dish')}", {"plan": plan, "dry_run": True})
    try:
        result = client.place_order(tab_unique_id=tab_id, target_time=target_time,
                                    dishes=[{"dishId": dish["dish_id"], "count": 1}],
                                    address_unique_id=address_id)
    except MeicanError as exc:
        return JobResult(False, exc.message, {"plan": plan})
    return JobResult(True, f"已下单 {dish.get('dish')}", {"plan": plan, "result": result})


def keepalive(payload: dict) -> bool:
    """轻量请求，续命 accessToken。"""
    try:
        client = _client(payload)
        client.calendar_items(date.today(), date.today(), with_order=False)
        return True
    except Exception:  # noqa: BLE001
        return False
```

- [ ] **Step 4: 创建 feature.py**

创建 `app/features/meican/feature.py`：

```python
"""美餐插件：交互型凭据（短信验证码）。
"""
from __future__ import annotations

import time
from datetime import date, timedelta

from ...core import db
from ...core.feature import (CredentialKind, Feature, FeatureLoginError,
                             FieldSpec, JobContext, JobResult, JobSpec)
from . import jobs
from .client import MeicanClient, MeicanError


class MeicanFeature(Feature):
    key = "meican"
    name = "美餐自动订餐"
    description = "每天自动检测并订餐，基于历史订单推测口味"
    credential_kind = CredentialKind.INTERACTIVE
    credential_fields = [FieldSpec("phone", "手机号"), FieldSpec("code", "短信验证码")]
    default_jobs = [
        JobSpec("order", "自动订餐", "cron", {"hour": 15, "minute": 30}),
        JobSpec("keepalive", "登录保活", "interval", {"minutes": 20},
                jitter=5, editable=False),
    ]
    dry_run_default = True

    async def login(self, user_id: int, payload: dict) -> dict:
        phone = (payload.get("phone") or "").strip()
        code = (payload.get("code") or "").strip()
        if not phone:
            raise FeatureLoginError("请填写手机号")
        if not code:
            raise FeatureLoginError("请填写短信验证码")

        client = MeicanClient()
        try:
            ld = client.login_with_sms(phone, code)
        except MeicanError as exc:
            raise FeatureLoginError(exc.message) from exc

        users = ld.get("userList") or []
        if not users:
            raise FeatureLoginError("该账号没有可用的美餐身份")
        acct = next((u for u in users
                     if u.get("accountVersion") == "AccountThreeDotZero"), users[0])
        try:
            token = client.choose_account(ld["ticket"], ld["signature"],
                                          snowflake_id=acct.get("snowflakeId"))
        except MeicanError as exc:
            raise FeatureLoginError(exc.message) from exc

        payload.update({
            "phone": phone,
            "accessToken": token["accessToken"],
            "refreshToken": token.get("refreshToken", ""),
            "tokenType": token.get("tokenType", "bearer"),
            "obtainedAt": int(time.time()),
            "snowflakeId": acct.get("snowflakeId", ""),
            "userName": acct.get("name", ""),
        })
        payload.pop("code", None)          # 验证码是一次性的，不落库
        return {"display": acct.get("name") or phone}

    async def check_alive(self, user_id: int) -> bool:
        cred = db.load_credential(user_id, self.key)
        return jobs.keepalive(cred.payload)

    async def run_job(self, user_id: int, job_id: str, ctx: JobContext) -> JobResult:
        cred = db.load_credential(user_id, self.key)
        payload = cred.payload
        cache = ctx.get_data("restaurant_names", default={})

        if job_id == "keepalive":
            ok = jobs.keepalive(payload)
            return JobResult(ok, "保活成功" if ok else "保活失败")

        if job_id == "order":
            days = int(ctx.options.get("target_offset_days", 1))
            target = date.today() + timedelta(days=days)
            result = jobs.do_order(payload, cache, target, None, ctx.dry_run)
            ctx.set_data("restaurant_names", cache)
            return result

        return JobResult(False, f"未知任务：{job_id}")
```

- [ ] **Step 5: 运行测试确认通过**

Run: `./.venv/bin/python -m pytest tests/test_meican_feature.py -v`
Expected: 4 passed

- [ ] **Step 6: 提交**

```bash
git add app/features/meican/ tests/test_meican_feature.py
git commit -m "feat(meican): 美餐插件（交互型凭据 + 订餐/保活任务）"
```

---

## Task 7: HEM 客户端（features/timesheet/client.py）

**Files:**
- Create: `app/features/timesheet/__init__.py`
- Create: `app/features/timesheet/client.py`
- Create: `tests/test_timesheet_client.py`

**Interfaces:**
- Produces:
  - `timesheet.client.HemError(Exception)`
  - `timesheet.client.HemClient(username, password)`
  - `.login() -> dict`（返回会话信息，含 `uid`、`username`、`name`）
  - `.list_projects() -> list[dict]`（每项含 `project_id`、`name`）
  - `.week_records(start: date, end: date) -> list[dict]`
  - `.submit(username: str, week_date: date) -> dict`
  - `timesheet.client.workdays_of_week(any_day: date) -> list[date]`（周一到周五）
  - `timesheet.client.monday_of(any_day: date) -> date`

- [ ] **Step 1: 写失败的测试（日期纯函数，不打网络）**

创建 `tests/test_timesheet_client.py`：

```python
from datetime import date

from app.features.timesheet.client import monday_of, workdays_of_week


def test_monday_of_wednesday():
    assert monday_of(date(2026, 11, 18)) == date(2026, 11, 16)


def test_monday_of_monday():
    assert monday_of(date(2026, 11, 16)) == date(2026, 11, 16)


def test_monday_of_sunday():
    assert monday_of(date(2026, 11, 22)) == date(2026, 11, 16)


def test_workdays_are_five():
    days = workdays_of_week(date(2026, 11, 18))
    assert [d.isoformat() for d in days] == [
        "2026-11-16", "2026-11-17", "2026-11-18", "2026-11-19", "2026-11-20"]


def test_workdays_exclude_weekend():
    days = workdays_of_week(date(2026, 11, 22))
    assert all(d.weekday() < 5 for d in days)
```

- [ ] **Step 2: 运行测试确认失败**

Run: `./.venv/bin/python -m pytest tests/test_timesheet_client.py -v`
Expected: FAIL — `ModuleNotFoundError: app.features.timesheet.client`

- [ ] **Step 3: 实现 client.py**

创建 `app/features/timesheet/client.py`：

```python
"""HEM 工时系统客户端（https://hem.100credit.cn）。

实测确认的接口：
- 登录        POST /api/login                {username, password} → Set-Cookie
- 本周工时    GET  /api/daily/week           start_time, end_time, new_menu=0
- 项目列表    GET  /api/localproject/list    page_size
- 提交审批    GET  /api/hem/weekly/user-submit  username, dateId, type

鉴权靠 HttpOnly cookie（session_id + token），有效期 10 天，可自动重登。
"""
from __future__ import annotations

from datetime import date, timedelta

import httpx

BASE = "https://hem.100credit.cn"
DEFAULT_HEADERS = {
    "accept": "application/json",
    "user-agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"),
}


class HemError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.message = message
        self.status = status


def monday_of(any_day: date) -> date:
    """返回该日期所在周的周一。"""
    return any_day - timedelta(days=any_day.weekday())


def workdays_of_week(any_day: date) -> list[date]:
    """返回该日期所在周的周一至周五。"""
    monday = monday_of(any_day)
    return [monday + timedelta(days=i) for i in range(5)]


class HemClient:
    def __init__(self, username: str, password: str):
        self.username = username
        self.password = password
        self._client = httpx.Client(base_url=BASE, timeout=30,
                                    headers=DEFAULT_HEADERS, follow_redirects=True)
        self._logged_in = False

    def login(self) -> dict:
        r = self._client.post("/api/login",
                              json={"username": self.username, "password": self.password})
        if r.status_code != 200:
            raise HemError(f"登录失败（HTTP {r.status_code}）", r.status_code)
        try:
            payload = r.json()
        except Exception as exc:  # noqa: BLE001
            raise HemError("登录返回非 JSON") from exc
        if str(payload.get("code")) != "0":
            raise HemError(payload.get("message") or "用户名或密码错误")
        self._logged_in = True
        return payload.get("data") or {}

    def _ensure_login(self) -> None:
        if not self._logged_in:
            self.login()

    def _get(self, path: str, params: dict | None = None) -> dict:
        self._ensure_login()
        r = self._client.get(path, params=params or {})
        if r.status_code in (401, 403):
            raise HemError("登录态失效", r.status_code)
        try:
            payload = r.json()
        except Exception as exc:  # noqa: BLE001
            raise HemError(f"接口返回非 JSON（HTTP {r.status_code}）") from exc
        if str(payload.get("code")) != "0":
            raise HemError(payload.get("message") or "请求失败")
        return payload

    def list_projects(self) -> list[dict]:
        payload = self._get("/api/localproject/list", {"page_size": 10000})
        data = payload.get("data") or {}
        items = data.get("list") if isinstance(data, dict) else data
        out = []
        for p in items or []:
            pid = p.get("project_id") or p.get("projectId")
            name = p.get("name") or ""
            if pid and name:
                out.append({"project_id": str(pid), "name": name})
        return out

    def week_records(self, start: date, end: date) -> list[dict]:
        payload = self._get("/api/daily/week", {
            "start_time": start.isoformat(),
            "end_time": end.isoformat(),
            "new_menu": 0,
        })
        data = payload.get("data") or {}
        return data.get("list") or []

    def submit(self, week_date: date, weekly_type: int = 1) -> dict:
        payload = self._get("/api/hem/weekly/user-submit", {
            "username": self.username,
            "dateId": week_date.isoformat(),
            "type": weekly_type,
        })
        return payload


def filled_dates(records: list[dict]) -> dict[str, float]:
    """从 week_records 提取「日期 → 已填小时数」。"""
    out: dict[str, float] = {}
    for rec in records:
        for item in rec.get("content") or []:
            d = item.get("date")
            hours = float(item.get("task_time") or 0)
            if d and hours > 0:
                out[d] = out.get(d, 0.0) + hours
    return out
```

- [ ] **Step 4: 运行测试确认通过**

Run: `./.venv/bin/python -m pytest tests/test_timesheet_client.py -v`
Expected: 5 passed

- [ ] **Step 5: 提交**

```bash
git add app/features/timesheet/ tests/test_timesheet_client.py
git commit -m "feat(timesheet): HEM 客户端与周日期工具"
```

---

## Task 8: 工时插件（jobs + feature）

**Files:**
- Create: `app/features/timesheet/jobs.py`
- Create: `app/features/timesheet/feature.py`
- Create: `tests/test_timesheet_jobs.py`

**Interfaces:**
- Consumes: `timesheet.client.HemClient`、`timesheet.client.workdays_of_week`、`timesheet.client.filled_dates`
- Produces:
  - `timesheet.jobs.plan_week(records: list[dict], days: list[date], default_hours: float) -> dict`
  - `timesheet.jobs.run_weekly_submit(username, password, project_id, dry_run, today) -> JobResult`
  - `timesheet.feature.TimesheetFeature`

- [ ] **Step 1: 写失败的测试（纯逻辑，不打网络）**

创建 `tests/test_timesheet_jobs.py`：

```python
from datetime import date

from app.features.timesheet.jobs import plan_week


def test_plan_fills_all_workdays_when_empty():
    days = [date(2026, 11, 16), date(2026, 11, 17)]
    plan = plan_week([], days, 8.0)
    assert plan["to_fill"] == ["2026-11-16", "2026-11-17"]
    assert plan["already"] == []
    assert plan["total_hours"] == 16.0


def test_plan_skips_already_filled():
    records = [{"content": [{"date": "2026-11-16", "task_time": 8.0}]}]
    days = [date(2026, 11, 16), date(2026, 11, 17)]
    plan = plan_week(records, days, 8.0)
    assert plan["already"] == ["2026-11-16"]
    assert plan["to_fill"] == ["2026-11-17"]


def test_plan_skips_weekend_days_not_in_input():
    # workdays_of_week 本身不含周末；这里验证只处理传入的天
    days = [date(2026, 11, 20)]
    plan = plan_week([], days, 8.0)
    assert plan["to_fill"] == ["2026-11-20"]


def test_plan_handles_partial_hours():
    records = [{"content": [{"date": "2026-11-16", "task_time": 4.0}]}]
    days = [date(2026, 11, 16)]
    plan = plan_week(records, days, 8.0)
    # 已有部分工时的日期仍算"已填"，不覆盖用户手填数据
    assert plan["already"] == ["2026-11-16"]
    assert plan["to_fill"] == []


def test_plan_all_workdays_five():
    days = [date(2026, 11, 16) + __import__("datetime").timedelta(days=i) for i in range(5)]
    plan = plan_week([], days, 8.0)
    assert len(plan["to_fill"]) == 5
    assert plan["total_hours"] == 40.0
```

- [ ] **Step 2: 运行测试确认失败**

Run: `./.venv/bin/python -m pytest tests/test_timesheet_jobs.py -v`
Expected: FAIL — `ModuleNotFoundError: app.features.timesheet.jobs`

- [ ] **Step 3: 实现 jobs.py**

创建 `app/features/timesheet/jobs.py`：

```python
"""工时填报任务逻辑。

业务规则（用户确认）：
- 只填周一至周五，每天 8 小时
- 已有工时的日期跳过，不覆盖用户手填数据
- 请假由 HEM 系统按 OA 请假单自动抵扣，插件不处理
- dry-run 时只出计划，不调提交接口
"""
from __future__ import annotations

from datetime import date

from ...core.feature import JobResult
from .client import HemClient, HemError, filled_dates, monday_of, workdays_of_week

DEFAULT_HOURS = 8.0


def plan_week(records: list[dict], days: list[date],
              default_hours: float = DEFAULT_HOURS) -> dict:
    """算出需要补填的日期。已有任何工时的日期一律跳过。"""
    already = filled_dates(records)
    to_fill: list[str] = []
    kept: list[str] = []
    for d in days:
        key = d.isoformat()
        if key in already:
            kept.append(key)
        else:
            to_fill.append(key)
    return {
        "to_fill": to_fill,
        "already": kept,
        "total_hours": len(to_fill) * default_hours,
        "default_hours": default_hours,
    }


def _pick_project(projects: list[dict], wanted: str | None) -> dict | None:
    """用户选的项目若存在则用，否则回退第一个。"""
    if not projects:
        return None
    if wanted:
        for p in projects:
            if str(p["project_id"]) == str(wanted):
                return p
    return projects[0]


def run_weekly_submit(username: str, password: str, project_id: str | None,
                      dry_run: bool, today: date) -> JobResult:
    client = HemClient(username, password)
    try:
        client.login()
    except HemError as exc:
        return JobResult(False, f"HEM 登录失败：{exc.message}")

    try:
        projects = client.list_projects()
    except HemError as exc:
        return JobResult(False, f"拉取项目列表失败：{exc.message}")

    project = _pick_project(projects, project_id)
    if not project:
        return JobResult(False, "项目列表为空，无法填报")

    monday = monday_of(today)
    days = workdays_of_week(today)
    try:
        records = client.week_records(days[0], days[-1])
    except HemError as exc:
        return JobResult(False, f"拉取本周工时失败：{exc.message}")

    plan = plan_week(records, days)
    detail = {
        "week": monday.isoformat(),
        "project": project,
        "plan": plan,
        "dry_run": dry_run,
    }

    # 只统计指定项目的工时；若本周记录里没有该项目，视为全部待填
    project_records = [r for r in records
                       if str(r.get("project_id") or r.get("projectId")) == project["project_id"]]
    if project_records:
        plan = plan_week(project_records, days)
        detail["plan"] = plan

    if not plan["to_fill"]:
        return JobResult(True, "本周工时已填满，无需操作", detail)

    if dry_run:
        return JobResult(
            True,
            f"演练：将在 {len(plan['to_fill'])} 个工作日填 {plan['default_hours']}h，共 {plan['total_hours']}h",
            detail)

    try:
        client.submit(monday)
    except HemError as exc:
        return JobResult(False, f"提交审批失败：{exc.message}", detail)

    return JobResult(True,
                     f"已提交审批（{len(plan['to_fill'])} 天 × {plan['default_hours']}h）",
                     detail)
```

- [ ] **Step 4: 实现 feature.py**

创建 `app/features/timesheet/feature.py`：

```python
"""工时插件：密码型凭据，可自动重登。"""
from __future__ import annotations

from datetime import date

from ...core import db
from ...core.feature import (CredentialKind, Feature, FeatureLoginError,
                             FieldSpec, JobContext, JobResult, JobSpec,
                             OptionSpec)
from . import jobs
from .client import HemClient, HemError


class TimesheetFeature(Feature):
    key = "timesheet"
    name = "工时填报"
    description = "每周五自动填报本周工时并提交审批"
    credential_kind = CredentialKind.PASSWORD
    credential_fields = [
        FieldSpec("username", "用户名"),
        FieldSpec("password", "密码", type="password", secret=True),
    ]
    default_jobs = [
        JobSpec("weekly_submit", "每周工时填报", "cron",
                {"day_of_week": "fri", "hour": 20, "minute": 0}),
    ]
    extra_options = [
        OptionSpec("project_id", "填报项目", "select",
                   source="projects", refreshable=True),
    ]
    dry_run_default = True

    async def login(self, user_id: int, payload: dict) -> dict:
        username = (payload.get("username") or "").strip()
        password = (payload.get("password") or "").strip()
        if not username:
            raise FeatureLoginError("请填写用户名")
        if not password:
            raise FeatureLoginError("请填写密码")

        client = HemClient(username, password)
        try:
            info = client.login()
        except HemError as exc:
            raise FeatureLoginError(exc.message) from exc

        payload.update({"username": username, "password": password,
                        "uid": info.get("uid"), "name": info.get("name", "")})
        # 登录成功后顺手缓存项目列表
        try:
            projects = client.list_projects()
            db.fdata_set(user_id, self.key, "projects", projects)
        except HemError:
            projects = []
        return {"display": info.get("name") or username,
                "projects": len(projects)}

    async def check_alive(self, user_id: int) -> bool:
        cred = db.load_credential(user_id, self.key)
        p = cred.payload
        if not p.get("username") or not p.get("password"):
            return False
        try:
            HemClient(p["username"], p["password"]).login()
            return True
        except HemError:
            return False

    async def relogin(self, user_id: int) -> bool:
        return await self.check_alive(user_id)

    async def run_job(self, user_id: int, job_id: str, ctx: JobContext) -> JobResult:
        if job_id != "weekly_submit":
            return JobResult(False, f"未知任务：{job_id}")

        cred = db.load_credential(user_id, self.key)
        p = cred.payload
        if not p.get("username") or not p.get("password"):
            return JobResult(False, "凭据缺失，请重新登录")

        project_id = ctx.options.get("project_id") or None
        result = jobs.run_weekly_submit(
            p["username"], p["password"], project_id, ctx.dry_run, date.today())

        # 项目失效时回退默认并回写配置
        detail = result.detail or {}
        used = (detail.get("project") or {}).get("project_id")
        if used and str(used) != str(project_id):
            cfg = db.load_feature_config(user_id, self.key)
            cfg.options["project_id"] = used
            db.save_feature_config(user_id, self.key, cfg)
        return result

    async def refresh_options(self, user_id: int) -> list[dict]:
        cred = db.load_credential(user_id, self.key)
        p = cred.payload
        client = HemClient(p.get("username", ""), p.get("password", ""))
        try:
            projects = client.list_projects()
        except HemError as exc:
            raise FeatureLoginError(f"拉取项目失败：{exc.message}") from exc
        db.fdata_set(user_id, self.key, "projects", projects)
        return [{"value": x["project_id"], "label": x["name"]} for x in projects]
```

- [ ] **Step 5: 运行测试确认通过**

Run: `./.venv/bin/python -m pytest tests/test_timesheet_jobs.py -v`
Expected: 5 passed

- [ ] **Step 6: 提交**

```bash
git add app/features/timesheet/jobs.py app/features/timesheet/feature.py tests/test_timesheet_jobs.py
git commit -m "feat(timesheet): 工时插件（周填报 + 项目选择 + dry-run）"
```

---

## Task 9: 调度器（core/scheduler.py）

**Files:**
- Create: `app/core/scheduler.py`
- Create: `tests/test_scheduler.py`

**Interfaces:**
- Consumes: `core.db`、`core.runner`、`core.registry`
- Produces:
  - `scheduler.start() -> None`
  - `scheduler.reschedule_user(user_id: int) -> None`
  - `scheduler.reschedule_all() -> None`
  - `scheduler.next_run(user_id: int, feature_key: str, job_id: str) -> str | None`
  - `scheduler.scheduler`（模块级 `AsyncIOScheduler` 实例）

- [ ] **Step 1: 写失败的测试**

创建 `tests/test_scheduler.py`：

```python
import pytest

from app.core import db, registry, scheduler
from app.core.feature import CredentialKind, Feature, JobResult, JobSpec


class SchedFeature(Feature):
    key = "sched"
    name = "调度测试"
    credential_kind = CredentialKind.PASSWORD
    default_jobs = [
        JobSpec("daily", "每日", "cron", {"hour": 9, "minute": 15}),
        JobSpec("tick", "间隔", "interval", {"minutes": 30}, jitter=3, editable=False),
    ]

    async def login(self, user_id, payload):
        return {}

    async def check_alive(self, user_id):
        return True

    async def run_job(self, user_id, job_id, ctx):
        return JobResult(True, "ok")


@pytest.fixture(autouse=True)
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr(db, "KEY_PATH", tmp_path / "t.key")
    db.init_db()
    registry.clear()
    registry.register(SchedFeature())
    yield
    for job in scheduler.scheduler.get_jobs():
        job.remove()
    registry.clear()


def test_build_job_id():
    assert scheduler.job_id("sched", "daily", 7) == "sched:daily:7"


def test_reschedule_creates_jobs():
    u = db.create_user("u", "H", "U")
    db.save_feature_config(u.id, "sched",
                           db.FeatureConfig("sched", True, True, {}, {}))
    db.issue_session(u.id)
    scheduler.reschedule_user(u.id)
    ids = {j.id for j in scheduler.scheduler.get_jobs()}
    assert "sched:daily:1" in ids
    assert "sched:tick:1" in ids


def test_disabled_feature_has_no_jobs():
    u = db.create_user("u", "H", "U")
    db.save_feature_config(u.id, "sched",
                           db.FeatureConfig("sched", False, True, {}, {}))
    scheduler.reschedule_user(u.id)
    assert scheduler.scheduler.get_jobs() == []


def test_user_schedule_override_applied():
    u = db.create_user("u", "H", "U")
    cfg = db.FeatureConfig("sched", True, True, {"daily": {"hour": 7, "minute": 5}}, {})
    db.save_feature_config(u.id, "sched", cfg)
    scheduler.reschedule_user(u.id)
    job = scheduler.scheduler.get_job(f"sched:daily:{u.id}")
    assert job is not None
    trigger = str(job.trigger)
    assert "hour='7'" in trigger
    assert "minute='5'" in trigger


def test_interval_job_created_with_jitter_range():
    u = db.create_user("u", "H", "U")
    db.save_feature_config(u.id, "sched",
                           db.FeatureConfig("sched", True, True, {}, {}))
    scheduler.reschedule_user(u.id)
    job = scheduler.scheduler.get_job(f"sched:tick:{u.id}")
    assert job is not None
    # jitter=3 表示 27~33 分钟
    assert "minute=" in str(job.trigger)
```

- [ ] **Step 2: 运行测试确认失败**

Run: `./.venv/bin/python -m pytest tests/test_scheduler.py -v`
Expected: FAIL — `ModuleNotFoundError: app.core.scheduler`

- [ ] **Step 3: 实现 scheduler.py**

创建 `app/core/scheduler.py`：

```python
"""按 用户 × 功能 × 任务 调度。

任务 id 格式：{feature_key}:{job_id}:{user_id}
执行统一走 runner.execute，放在线程池里跑（插件是同步实现）。
"""
from __future__ import annotations

import random

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from . import db, registry, runner

scheduler = AsyncIOScheduler(timezone="Asia/Shanghai")


def job_id(feature_key: str, jid: str, user_id: int) -> str:
    return f"{feature_key}:{jid}:{user_id}"


def _interval_trigger(spec, override: dict):
    minutes = int(override.get("minutes") or spec.default.get("minutes") or 20)
    if spec.jitter:
        minutes = max(1, minutes + random.randint(-spec.jitter, spec.jitter))
    return {"trigger": "interval", "minutes": minutes}


def _cron_trigger(spec, override: dict):
    params = {**spec.default, **override}
    params.pop("day_of_week", None) if False else None
    return {"trigger": "cron", **params}


def reschedule_user(user_id: int) -> None:
    """重建该用户名下所有功能的定时任务。"""
    for feature in registry.all():
        for spec in feature.default_jobs:
            jid = job_id(feature.key, spec.job_id, user_id)
            if scheduler.get_job(jid):
                scheduler.remove_job(jid)

    for feature in registry.all():
        cfg = db.load_feature_config(user_id, feature.key)
        if not cfg.enabled:
            continue
        for spec in feature.default_jobs:
            jid = job_id(feature.key, spec.job_id, user_id)
            override = cfg.schedule.get(spec.job_id, {}) if spec.editable else {}
            if spec.trigger == "interval":
                trigger = _interval_trigger(spec, override)
            else:
                trigger = _cron_trigger(spec, override)
            kw = dict(trigger)
            ttype = kw.pop("trigger")
            scheduler.add_job(
                _run_job, ttype, args=[user_id, feature.key, spec.job_id],
                id=jid, replace_existing=True, **kw,
            )


def reschedule_all() -> None:
    for user_id, _feature_key in db.enabled_feature_pairs():
        reschedule_user(user_id)


def next_run(user_id: int, feature_key: str, jid: str) -> str | None:
    job = scheduler.get_job(job_id(feature_key, jid, user_id))
    return str(job.next_run_time) if job else None


async def _run_job(user_id: int, feature_key: str, jid: str) -> None:
    import asyncio
    try:
        await asyncio.to_thread(runner.execute, user_id, feature_key, jid)
    except Exception as exc:  # noqa: BLE001
        db.record_job_run(user_id, feature_key, jid, False,
                          f"调度异常：{type(exc).__name__}: {exc}", {})


def start() -> None:
    if not scheduler.running:
        scheduler.start()
```

- [ ] **Step 4: 运行测试确认通过**

Run: `./.venv/bin/python -m pytest tests/test_scheduler.py -v`
Expected: 5 passed

- [ ] **Step 5: 清理 `_cron_trigger` 里的死代码**

`_cron_trigger` 中有一行无意义的表达式，改为：

```python
def _cron_trigger(spec, override: dict):
    params = {**spec.default, **override}
    return {"trigger": "cron", **params}
```

- [ ] **Step 6: 重新运行测试**

Run: `./.venv/bin/python -m pytest tests/test_scheduler.py -v`
Expected: 5 passed

- [ ] **Step 7: 提交**

```bash
git add app/core/scheduler.py tests/test_scheduler.py
git commit -m "feat(core): 按用户×功能×任务调度"
```

---

## Task 10: 认证与新路由（重写 main.py 骨架）

**Files:**
- Create: `app/core/web_auth.py`
- Modify: `app/main.py`（整体重写）
- Create: `tests/test_web_auth.py`

**Interfaces:**
- Consumes: `core.db`、`core.security`、`core.registry`、`core.runner`、`core.scheduler`、`core.feature`
- Produces: FastAPI 路由
  - `POST /api/login`、`POST /api/register`、`POST /api/logout`
  - `GET /api/me`
  - `GET /api/features`
  - `GET /api/features/{key}`
  - `POST /api/features/{key}/login`
  - `POST /api/features/{key}/config`
  - `POST /api/features/{key}/options/refresh`
  - `POST /api/features/{key}/run`
  - `GET /api/features/{key}/runs`

- [ ] **Step 1: 写失败的测试**

创建 `tests/test_web_auth.py`：

```python
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
    r = client.post("/api/login", json={"username": "alice", "password": "pw123456"})
    assert r.status_code == 200
    assert "mc_session" in r.cookies
    r = client.get("/api/me")
    assert r.json()["username"] == "alice"


def test_login_wrong_password(client):
    client.post("/api/register", json={"username": "bob", "password": "pw123456"})
    r = client.post("/api/login", json={"username": "bob", "password": "nope"})
    assert r.status_code == 401


def test_duplicate_username_rejected(client):
    client.post("/api/register", json={"username": "c", "password": "pw123456"})
    r = client.post("/api/register", json={"username": "c", "password": "other123"})
    assert r.status_code == 400


def test_register_disabled_by_env(client, monkeypatch):
    monkeypatch.setenv("ALLOW_REGISTER", "0")
    r = client.post("/api/register", json={"username": "d", "password": "pw123456"})
    assert r.status_code == 403


def test_features_require_login(client):
    assert client.get("/api/features").status_code == 401


def test_feature_login_flow(client):
    client.post("/api/register", json={"username": "e", "password": "pw123456"})
    client.post("/api/login", json={"username": "e", "password": "pw123456"})
    r = client.post("/api/features/test/login", json={"username": "x"})
    assert r.status_code == 200
    r = client.get("/api/features/test")
    assert r.json()["credential_state"] == "active"


def test_feature_config_update(client):
    client.post("/api/register", json={"username": "f", "password": "pw123456"})
    client.post("/api/login", json={"username": "f", "password": "pw123456"})
    r = client.post("/api/features/test/config",
                    json={"enabled": True, "dry_run": True})
    assert r.status_code == 200
    assert client.get("/api/features/test").json()["config"]["enabled"] is True
```

- [ ] **Step 2: 运行测试确认失败**

Run: `./.venv/bin/python -m pytest tests/test_web_auth.py -v`
Expected: FAIL（路由不存在）

- [ ] **Step 3: 实现 core/web_auth.py**

创建 `app/core/web_auth.py`：

```python
"""Web 会话认证依赖。"""
from __future__ import annotations

from fastapi import Cookie, HTTPException

from . import db

COOKIE_NAME = "mc_session"
COOKIE_MAX_AGE = db.SESSION_DAYS * 86400


async def current_user(mc_session: str | None = Cookie(default=None)) -> db.User:
    if not mc_session:
        raise HTTPException(401, "未登录")
    user = db.user_by_session(mc_session)
    if not user:
        raise HTTPException(401, "会话已过期，请重新登录")
    return user
```

- [ ] **Step 4: 重写 main.py**

先备份旧文件，再写入新内容：

```bash
cd /Users/admin/meican-auto
cp app/main.py docs/legacy-main.py.bak
```

然后用下面的完整内容**替换** `app/main.py`：

```python
"""多功能平台 Web 服务。

启动：./.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8765
"""
from __future__ import annotations

import os
import time
from pathlib import Path

from fastapi import Cookie, Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .core import db, registry, runner, scheduler, security, web_auth
from .core.feature import FeatureLoginError, FeatureState

BASE_DIR = Path(__file__).resolve().parent
app = FastAPI(title="办公自动化平台")
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")


# ---------------- 启动装配 ----------------

def _register_features() -> None:
    from .features.meican.feature import MeicanFeature
    from .features.timesheet.feature import TimesheetFeature
    for f in (MeicanFeature(), TimesheetFeature()):
        registry.register(f)


@app.on_event("startup")
def startup() -> None:
    db.init_db()
    if not registry.all():
        _register_features()
    scheduler.start()
    scheduler.reschedule_all()


# ---------------- 页面 ----------------

@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    return HTMLResponse((BASE_DIR / "templates" / "index.html").read_text("utf-8"))


@app.get("/login", response_class=HTMLResponse)
def login_page() -> HTMLResponse:
    return HTMLResponse((BASE_DIR / "templates" / "login.html").read_text("utf-8"))


@app.get("/feature/{key}", response_class=HTMLResponse)
def feature_page(key: str) -> HTMLResponse:
    return HTMLResponse((BASE_DIR / "templates" / "feature.html").read_text("utf-8"))


# ---------------- 账号 ----------------

class AuthBody(BaseModel):
    username: str
    password: str


@app.post("/api/register")
def register(body: AuthBody, response: Response):
    if os.environ.get("ALLOW_REGISTER", "1") == "0":
        raise HTTPException(403, "注册已关闭")
    username = body.username.strip()
    if len(username) < 2:
        raise HTTPException(400, "用户名至少 2 个字符")
    if len(body.password) < 6:
        raise HTTPException(400, "密码至少 6 位")
    if db.get_user_by_username(username):
        raise HTTPException(400, "用户名已存在")
    user = db.create_user(username, security.hash_password(body.password), username)
    token, _ = db.issue_session(user.id)
    response.set_cookie(web_auth.COOKIE_NAME, token, max_age=web_auth.COOKIE_MAX_AGE,
                        httponly=True, samesite="lax", path="/")
    return {"ok": True, "username": user.username}


@app.post("/api/login")
def login(body: AuthBody, response: Response, request: Request):
    user = db.get_user_by_username(body.username.strip())
    if not user or not security.verify_password(body.password, user.password_hash):
        time.sleep(1)                       # 防暴力猜解
        raise HTTPException(401, "用户名或密码错误")
    token, _ = db.issue_session(user.id, ua=request.headers.get("user-agent", ""))
    response.set_cookie(web_auth.COOKIE_NAME, token, max_age=web_auth.COOKIE_MAX_AGE,
                        httponly=True, samesite="lax", path="/")
    scheduler.reschedule_user(user.id)
    return {"ok": True, "username": user.username}


@app.post("/api/logout")
def logout(response: Response, mc_session: str | None = Cookie(default=None),
           user: db.User = Depends(web_auth.current_user)):
    if mc_session:
        db.revoke_session(mc_session)
    response.delete_cookie(web_auth.COOKIE_NAME, path="/")
    return {"ok": True}


@app.get("/api/me")
def me(user: db.User = Depends(web_auth.current_user)):
    return {"id": user.id, "username": user.username, "display_name": user.display_name}


# ---------------- 功能 ----------------

def _feature_or_404(key: str):
    from .core import registry as reg
    f = reg.get(key)
    if not f:
        raise HTTPException(404, f"未知功能：{key}")
    return f


@app.get("/api/features")
def list_features(user: db.User = Depends(web_auth.current_user)):
    out = []
    for f in registry.all():
        cred = db.load_credential(user.id, f.key)
        cfg = db.load_feature_config(user.id, f.key)
        jobs = []
        for spec in f.default_jobs:
            jobs.append({
                "job_id": spec.job_id, "name": spec.name,
                "trigger": spec.trigger, "editable": spec.editable,
                "next_run": scheduler.next_run(user.id, f.key, spec.job_id),
            })
        out.append({
            "key": f.key, "name": f.name, "description": f.description,
            "credential_kind": f.credential_kind.value,
            "credential_state": cred.state,
            "enabled": cfg.enabled, "dry_run": cfg.dry_run,
            "jobs": jobs,
        })
    return {"features": out}


@app.get("/api/features/{key}")
def feature_detail(key: str, user: db.User = Depends(web_auth.current_user)):
    f = _feature_or_404(key)
    cred = db.load_credential(user.id, key)
    cfg = db.load_feature_config(user.id, key)
    return {
        "key": f.key, "name": f.name, "description": f.description,
        "credential_kind": f.credential_kind.value,
        "credential_state": cred.state,
        "credential_display": cred.payload.get("userName")
        or cred.payload.get("name") or cred.payload.get("username") or "",
        "last_ok_at": cred.last_ok_at,
        "last_error": cred.last_error,
        "fields": [{"name": x.name, "label": x.label, "type": x.type,
                    "required": x.required} for x in f.credential_fields],
        "jobs": [{"job_id": s.job_id, "name": s.name, "trigger": s.trigger,
                  "default": s.default, "editable": s.editable,
                  "next_run": scheduler.next_run(user.id, key, s.job_id)}
                 for s in f.default_jobs],
        "extra_options": [{"name": o.name, "label": o.label, "type": o.type,
                           "refreshable": o.refreshable} for o in f.extra_options],
        "config": {"enabled": cfg.enabled, "dry_run": cfg.dry_run,
                   "schedule": cfg.schedule, "options": cfg.options},
        "option_values": db.fdata_get(user.id, key, "projects", default=[]),
        "runs": db.read_job_runs(user.id, key, limit=20),
        "dry_run_default": f.dry_run_default,
    }


@app.post("/api/features/{key}/login")
def feature_login(key: str, payload: dict,
                  user: db.User = Depends(web_auth.current_user)):
    _feature_or_404(key)
    try:
        info = runner.run_login(user.id, key, payload)
    except FeatureLoginError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True, "info": info}


class ConfigBody(BaseModel):
    enabled: bool | None = None
    dry_run: bool | None = None
    schedule: dict | None = None
    options: dict | None = None


@app.post("/api/features/{key}/config")
def feature_config(key: str, body: ConfigBody,
                   user: db.User = Depends(web_auth.current_user)):
    _feature_or_404(key)
    cfg = db.load_feature_config(user.id, key)
    data = body.model_dump(exclude_none=True)
    if "schedule" in data:
        cfg.schedule = data["schedule"]
    if "options" in data:
        cfg.options.update(data["options"])
    cfg.enabled = data.get("enabled", cfg.enabled)
    cfg.dry_run = data.get("dry_run", cfg.dry_run)
    db.save_feature_config(user.id, key, cfg)
    scheduler.reschedule_user(user.id)
    return {"ok": True, "config": {"enabled": cfg.enabled, "dry_run": cfg.dry_run,
                                   "schedule": cfg.schedule, "options": cfg.options}}


@app.post("/api/features/{key}/options/refresh")
def feature_refresh_options(key: str, user: db.User = Depends(web_auth.current_user)):
    f = _feature_or_404(key)
    import asyncio
    try:
        items = asyncio.run(f.refresh_options(user.id))
    except FeatureLoginError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"{type(exc).__name__}: {exc}") from exc
    return {"ok": True, "options": items}


class RunBody(BaseModel):
    job_id: str


@app.post("/api/features/{key}/run")
def feature_run(key: str, body: RunBody,
                user: db.User = Depends(web_auth.current_user)):
    _feature_or_404(key)
    result = runner.execute(user.id, key, body.job_id)
    return {"ok": result.ok, "message": result.message, "detail": result.detail}


@app.get("/api/features/{key}/runs")
def feature_runs(key: str, limit: int = 20,
                 user: db.User = Depends(web_auth.current_user)):
    _feature_or_404(key)
    return {"runs": db.read_job_runs(user.id, key, limit)}
```

- [ ] **Step 5: 运行测试确认通过**

Run: `./.venv/bin/python -m pytest tests/test_web_auth.py -v`
Expected: 7 passed

- [ ] **Step 6: 提交**

```bash
git add app/main.py app/core/web_auth.py tests/test_web_auth.py docs/
git commit -m "feat(web): 系统账号认证与功能通用路由"
```

---

## Task 11: 前端页面

**Files:**
- Create: `app/templates/login.html`
- Create: `app/templates/index.html`（整体替换）
- Create: `app/templates/feature.html`
- Create: `app/templates/_style.html`（共用样式片段，由其余模板 include）

**Interfaces:**
- Consumes: Task 10 的所有 `/api/*` 路由
- Produces: 三个页面

- [ ] **Step 1: 创建共用样式**

创建 `app/templates/_style.html`：

```html
<style>
  :root{
    --bg:#0f1115; --panel:#171a21; --panel-2:#1d212a; --line:#272c37;
    --text:#e6e8ee; --muted:#8b93a7; --accent:#ff7a45; --accent-2:#ffb020;
    --ok:#3ecf8e; --warn:#e8b424; --err:#ec5b5b; --radius:14px;
  }
  *{box-sizing:border-box}
  body{
    margin:0;background:radial-gradient(1200px 600px at 20% -10%,#1b2030 0%,var(--bg) 55%);
    color:var(--text);font:15px/1.55 -apple-system,BlinkMacSystemFont,"PingFang SC",sans-serif;
    min-height:100vh;padding:28px 20px 60px;
  }
  .wrap{max-width:1000px;margin:0 auto}
  header{display:flex;align-items:center;gap:12px;margin-bottom:24px;flex-wrap:wrap}
  h1{font-size:21px;margin:0}
  .badge{font-size:12px;padding:3px 10px;border-radius:999px;border:1px solid var(--line);
    color:var(--muted);background:var(--panel)}
  .badge.on{color:var(--ok);border-color:#1f4d3a;background:#10231b}
  .badge.warn{color:var(--warn);border-color:#4d3f1f;background:#231f10}
  .badge.err{color:var(--err);border-color:#5a2323;background:#2a1717}
  .card{background:linear-gradient(180deg,var(--panel),var(--panel-2));
    border:1px solid var(--line);border-radius:var(--radius);padding:20px;margin-bottom:16px}
  .card h2{font-size:13px;margin:0 0 14px;color:var(--muted);letter-spacing:.8px;
    text-transform:uppercase;font-weight:600}
  label{display:block;font-size:13px;color:var(--muted);margin:12px 0 6px}
  input,select{width:100%;padding:11px 13px;border-radius:10px;border:1px solid var(--line);
    background:#0e1116;color:var(--text);font-size:15px;outline:none}
  input:focus,select:focus{border-color:var(--accent);box-shadow:0 0 0 3px rgba(255,122,69,.14)}
  button{cursor:pointer;border:none;border-radius:10px;padding:11px 18px;font-size:14px;
    font-weight:600;background:linear-gradient(135deg,var(--accent),var(--accent-2));
    color:#191207}
  button:disabled{opacity:.45;cursor:not-allowed}
  button.ghost{background:transparent;border:1px solid var(--line);color:var(--text);font-weight:500}
  .row{display:flex;gap:10px;flex-wrap:wrap;align-items:center}
  .muted{color:var(--muted);font-size:13px}
  .kv{display:flex;justify-content:space-between;padding:7px 0;
    border-bottom:1px dashed #232833;font-size:14px}
  .kv:last-child{border-bottom:none}
  .kv span:first-child{color:var(--muted)}
  .grid{display:grid;gap:14px}
  @media(min-width:760px){.cols{grid-template-columns:1fr 1fr}}
  .toast{position:fixed;bottom:24px;left:50%;transform:translateX(-50%) translateY(20px);
    background:#20242e;border:1px solid var(--line);padding:13px 20px;border-radius:12px;
    opacity:0;transition:.25s;pointer-events:none;max-width:90vw;z-index:99}
  .toast.show{opacity:1;transform:translateX(-50%) translateY(0)}
  .toast.err{border-color:#5a2323;color:#ffb3b3}
  .toast.ok{border-color:#1f4d3a;color:#a8f0c8}
  .switch{display:flex;align-items:center;gap:10px}
  .switch input{width:auto}
  .logline{padding:5px 0;border-bottom:1px dashed #1e232c;color:#a9b1c3;font-size:12.5px}
  .logline .t{color:#5d6578;margin-right:8px}
</style>
```

- [ ] **Step 2: 创建 login.html**

创建 `app/templates/login.html`：

```html
<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>登录 · 办公自动化</title>
{% include '_style.html' %}
</head>
<body>
<div class="wrap" style="max-width:420px;margin-top:8vh">
  <h1 style="text-align:center;margin-bottom:24px">🗂 办公自动化平台</h1>
  <div class="card">
    <h2 id="formTitle">登录</h2>
    <label>用户名</label>
    <input id="username" autocomplete="username">
    <label>密码</label>
    <input id="password" type="password" autocomplete="current-password">
    <div class="row" style="margin-top:18px">
      <button id="btnSubmit" style="flex:1">登录</button>
    </div>
    <p class="muted" style="margin-top:14px;text-align:center">
      <a href="#" id="toggleMode" style="color:var(--accent)">没有账号？注册</a>
    </p>
  </div>
</div>
<div class="toast" id="toast"></div>
<script>
let mode = 'login';
const $ = s => document.querySelector(s);
let toastTimer;
function toast(msg, kind='') {
  const t = $('#toast'); t.textContent = msg; t.className = 'toast show ' + kind;
  clearTimeout(toastTimer); toastTimer = setTimeout(() => t.className = 'toast', 3200);
}
$('#toggleMode').onclick = e => {
  e.preventDefault();
  mode = mode === 'login' ? 'register' : 'login';
  $('#formTitle').textContent = mode === 'login' ? '登录' : '注册';
  $('#btnSubmit').textContent = mode === 'login' ? '登录' : '注册并登录';
  $('#toggleMode').textContent = mode === 'login' ? '没有账号？注册' : '已有账号？去登录';
};
$('#btnSubmit').onclick = async () => {
  const username = $('#username').value.trim();
  const password = $('#password').value;
  if (!username || !password) return toast('用户名和密码都要填', 'err');
  $('#btnSubmit').disabled = true;
  try {
    const r = await fetch('/api/' + mode, {
      method: 'POST', headers: {'content-type':'application/json'},
      body: JSON.stringify({username, password})
    });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(d.detail || '请求失败');
    location.href = '/';
  } catch (e) { toast(e.message, 'err'); }
  finally { $('#btnSubmit').disabled = false; }
};
$('#password').addEventListener('keydown', e => { if (e.key === 'Enter') $('#btnSubmit').click(); });
</script>
</body>
</html>
```

- [ ] **Step 3: 创建 index.html**

创建 `app/templates/index.html`：

```html
<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>办公自动化平台</title>
{% include '_style.html' %}
<style>
  .cardlink{display:block;text-decoration:none;color:inherit;transition:.15s}
  .cardlink:hover{transform:translateY(-2px);border-color:#3a4256}
  .fname{font-size:16px;font-weight:600;margin-bottom:6px}
  .fdesc{font-size:13px;color:var(--muted);margin-bottom:12px}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>🗂 办公自动化平台</h1>
    <span class="badge" id="userBadge"></span>
    <button class="ghost" id="btnLogout" style="margin-left:auto">退出</button>
  </header>
  <div class="grid cols" id="features"></div>
</div>
<div class="toast" id="toast"></div>
<script>
const $ = s => document.querySelector(s);
let toastTimer;
function toast(m,k=''){const t=$('#toast');t.textContent=m;t.className='toast show '+k;
  clearTimeout(toastTimer);toastTimer=setTimeout(()=>t.className='toast',3200);}
async function api(p,o={}){
  const r=await fetch(p,{headers:{'content-type':'application/json'},...o});
  const d=await r.json().catch(()=>({}));
  if(r.status===401){location.href='/login';throw new Error('未登录');}
  if(!r.ok) throw new Error(d.detail||('HTTP '+r.status));
  return d;
}
const STATE_LABEL={not_configured:['未配置',''],active:['已启用','on'],
  expired:['登录失效','err'],error:['异常','err']};
async function boot(){
  try{
    const me = await api('/api/me');
    $('#userBadge').textContent = me.display_name || me.username;
    const {features} = await api('/api/features');
    $('#features').innerHTML = features.map(f=>{
      const [label, cls] = STATE_LABEL[f.credential_state] || ['未知',''];
      const next = f.jobs.map(j=>j.next_run).filter(Boolean)[0];
      return `<a class="cardlink card" href="/feature/${f.key}">
        <div class="fname">${f.name} <span class="badge ${cls}">${label}</span></div>
        <div class="fdesc">${f.description||''}</div>
        <div class="kv"><span>状态</span><span>${f.enabled?'已开启定时':'定时关闭'}</span></div>
        <div class="kv"><span>下次执行</span><span>${next||'—'}</span></div>
      </a>`;
    }).join('');
  }catch(e){ toast(e.message,'err'); }
}
$('#btnLogout').onclick = async () => {
  try { await api('/api/logout',{method:'POST'}); location.href='/login'; }
  catch(e){ location.href='/login'; }
};
boot();
</script>
</body>
</html>
```

- [ ] **Step 4: 创建 feature.html**

创建 `app/templates/feature.html`：

```html
<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>功能面板</title>
{% include '_style.html' %}
</head>
<body>
<div class="wrap">
  <header>
    <a href="/" class="ghost" style="text-decoration:none;padding:8px 14px;border:1px solid var(--line);border-radius:10px;color:var(--text)">← 返回</a>
    <h1 id="fname">加载中…</h1>
    <span class="badge" id="stateBadge"></span>
  </header>

  <div class="grid cols">
    <div class="card">
      <h2>登录凭据</h2>
      <div class="muted" id="credHint" style="margin-bottom:12px"></div>
      <div id="fields"></div>
      <div class="row" style="margin-top:16px">
        <button id="btnLogin">保存并登录</button>
      </div>
      <div class="kv" style="margin-top:16px"><span>当前身份</span><span id="credDisplay">—</span></div>
    </div>

    <div class="card">
      <h2>自动化设置</h2>
      <div class="switch" style="margin-bottom:10px">
        <input type="checkbox" id="enabled"><label for="enabled" style="margin:0">启用定时任务</label>
      </div>
      <div class="switch">
        <input type="checkbox" id="dryRun"><label for="dryRun" style="margin:0">演练模式（不真正提交）</label>
      </div>
      <div id="jobTimes" style="margin-top:14px"></div>
      <div id="options" style="margin-top:14px"></div>
      <div class="row" style="margin-top:16px">
        <button id="btnSave">保存设置</button>
      </div>
    </div>
  </div>

  <div class="card">
    <h2>立即执行</h2>
    <div class="row" id="runButtons"></div>
  </div>

  <div class="card">
    <h2>运行记录</h2>
    <div id="runs" class="muted">暂无记录</div>
  </div>
</div>
<div class="toast" id="toast"></div>
<script>
const $ = s => document.querySelector(s);
let toastTimer;
function toast(m,k=''){const t=$('#toast');t.textContent=m;t.className='toast show '+k;
  clearTimeout(toastTimer);toastTimer=setTimeout(()=>t.className='toast',3200);}
const KEY = location.pathname.split('/').pop();
async function api(p,o={}){
  const r=await fetch(p,{headers:{'content-type':'application/json'},...o});
  const d=await r.json().catch(()=>({}));
  if(r.status===401){location.href='/login';throw new Error('未登录');}
  if(!r.ok) throw new Error(d.detail||('HTTP '+r.status));
  return d;
}
const STATE_LABEL={not_configured:['未配置',''],active:['已登录','on'],
  expired:['登录失效','err'],error:['异常','err']};

let detail = null;

function renderFields() {
  $('#fields').innerHTML = detail.fields.map(f=>`
    <label>${f.label}</label>
    <input data-field="${f.name}" type="${f.type==='password'?'password':'text'}">`).join('');
}

function renderJobs() {
  $('#jobTimes').innerHTML = detail.jobs.map(j=>{
    const sch = (detail.config.schedule || {})[j.job_id] || j.default;
    const label = j.trigger==='cron'
      ? `每天 ${String(sch.hour??'').padStart(2,'0')}:${String(sch.minute??'').padStart(2,'0')}`
      : `每 ${sch.minutes||20} 分钟`;
    return `<div class="kv"><span>${j.name}</span><span>${
      j.editable ? `<input data-sched="${j.job_id}" value="${label}" style="width:150px;padding:5px 8px;font-size:13px">` : label
    }</span></div>
    <div class="muted" style="font-size:12px;margin-bottom:6px">下次：${j.next_run||'—'}</div>`;
  }).join('');
}

function renderOptions() {
  const opts = detail.option_values || [];
  const cur = (detail.config.options||{}).project_id || '';
  $('#options').innerHTML = detail.extra_options.map(o=>`
    <label>${o.label}</label>
    <div class="row">
      <select data-opt="${o.name}" style="flex:1">
        ${opts.map(x=>`<option value="${x.project_id}" ${String(x.project_id)===String(cur)?'selected':''}>${x.name}</option>`).join('')}
      </select>
      ${o.refreshable?`<button class="ghost" id="btnRefreshOpts">刷新</button>`:''}
    </div>`).join('');
  const btn = $('#btnRefreshOpts');
  if (btn) btn.onclick = async () => {
    btn.disabled = true;
    try { await api(`/api/features/${KEY}/options/refresh`, {method:'POST'});
      toast('已刷新项目列表','ok'); await load(); }
    catch(e){ toast(e.message,'err'); }
    finally { btn.disabled = false; }
  };
}

function renderRuns() {
  const runs = detail.runs || [];
  if (!runs.length) { $('#runs').textContent = '暂无记录'; return; }
  $('#runs').innerHTML = runs.map(r=>`
    <div class="logline">
      <span class="t">${r.time.slice(5,16)}</span>
      ${r.ok?'✅':'❌'} ${r.message}
    </div>`).join('');
}

function renderRunButtons() {
  $('#runButtons').innerHTML = detail.jobs.map(j=>
    `<button class="ghost" data-run="${j.job_id}">立即执行：${j.name}</button>`).join('');
  document.querySelectorAll('[data-run]').forEach(b=>{
    b.onclick = async () => {
      b.disabled = true;
      try {
        const d = await api(`/api/features/${KEY}/run`, {method:'POST',
          body: JSON.stringify({job_id: b.dataset.run})});
        toast(d.message || (d.ok?'完成':'失败'), d.ok?'ok':'err');
        await load();
      } catch(e){ toast(e.message,'err'); }
      finally { b.disabled = false; }
    };
  });
}

async function load() {
  detail = await api('/api/features/' + KEY);
  $('#fname').textContent = detail.name;
  const [label, cls] = STATE_LABEL[detail.credential_state] || ['未知',''];
  $('#stateBadge').textContent = label;
  $('#stateBadge').className = 'badge ' + cls;
  $('#credHint').textContent = detail.credential_kind === 'interactive'
    ? '该功能需要短信验证码，每次失效需重新登录。'
    : '该功能使用账号密码，失效后可自动重登。';
  $('#credDisplay').textContent = detail.credential_display || '—';
  $('#enabled').checked = detail.config.enabled;
  $('#dryRun').checked = detail.config.dry_run;
  renderFields(); renderJobs(); renderOptions(); renderRuns(); renderRunButtons();
}

$('#btnLogin').onclick = async () => {
  const payload = {};
  document.querySelectorAll('[data-field]').forEach(i => payload[i.dataset.field] = i.value.trim());
  $('#btnLogin').disabled = true;
  try {
    const d = await api(`/api/features/${KEY}/login`, {method:'POST', body: JSON.stringify(payload)});
    toast('登录成功' + (d.info?.display ? '：' + d.info.display : ''), 'ok');
    await load();
  } catch(e){ toast(e.message,'err'); }
  finally { $('#btnLogin').disabled = false; }
};

function parseTimeInput(text, prev) {
  // 支持 "HH:MM" 或 "每 N 分钟"
  const m = text.match(/每\s*(\d+)\s*分钟/);
  if (m) return { minutes: parseInt(m[1], 10) };
  const t = text.match(/(\d{1,2})\s*:\s*(\d{1,2})/);
  if (t) return { hour: parseInt(t[1], 10), minute: parseInt(t[2], 10) };
  return prev;
}

$('#btnSave').onclick = async () => {
  const schedule = { ...(detail.config.schedule || {}) };
  document.querySelectorAll('[data-sched]').forEach(i => {
    schedule[i.dataset.sched] = parseTimeInput(i.value, schedule[i.dataset.sched] || {});
  });
  const options = { ...(detail.config.options || {}) };
  document.querySelectorAll('[data-opt]').forEach(i => { options[i.dataset.opt] = i.value; });
  try {
    await api(`/api/features/${KEY}/config`, {method:'POST', body: JSON.stringify({
      enabled: $('#enabled').checked, dry_run: $('#dryRun').checked, schedule, options
    })});
    toast('设置已保存','ok');
    await load();
  } catch(e){ toast(e.message,'err'); }
};

load().catch(e => toast(e.message,'err'));
</script>
</body>
</html>
```

- [ ] **Step 5: 启动服务人工验证**

```bash
cd /Users/admin/meican-auto
pkill -f "uvicorn app.main" 2>/dev/null; sleep 1
nohup ./.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8765 > data/server.log 2>&1 &
sleep 5
curl -sS -o /dev/null -w '登录页 %{http_code}\n' http://127.0.0.1:8765/login
curl -sS -o /dev/null -w '首页   %{http_code}\n' http://127.0.0.1:8765/
```

Expected: 两个 200。浏览器打开 http://127.0.0.1:8765/login 注册并登录，能看到两张功能卡片。

- [ ] **Step 6: 提交**

```bash
git add app/templates/
git commit -m "feat(web): 登录页、功能总览、通用功能面板"
```

---

## Task 12: 清理旧代码与最终验证

**Files:**
- Delete: `app/db.py`、`app/service.py`、`app/keepalive.py`、`app/auth.py`、`app/meican.py`、`app/preference.py`（已迁移或废弃）
- Modify: `README.md`
- Modify: `run.sh`

**Interfaces:**
- Consumes: 全部
- Produces: 可运行的最终版本

- [ ] **Step 1: 确认旧模块无引用**

```bash
cd /Users/admin/meican-auto
grep -rn "from .db import\|from .auth import\|from .service import\|from .keepalive import\|from app.db\|from app.service\|from app.keepalive\|from app.auth" app/ tests/ || echo "无残留引用"
```

Expected: `无残留引用`

- [ ] **Step 2: 删除旧模块**

```bash
git rm app/db.py app/service.py app/keepalive.py app/auth.py 2>/dev/null
ls app/*.py
```

Expected: 只剩 `app/__init__.py` 与 `app/main.py`

- [ ] **Step 3: 全量测试**

Run: `./.venv/bin/python -m pytest tests/ -v`
Expected: 全部通过（约 47 项）

- [ ] **Step 4: 更新 run.sh**

```bash
cat > run.sh <<'EOF'
#!/bin/bash
cd "$(dirname "$0")"
exec ./.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8765 "$@"
EOF
chmod +x run.sh
```

- [ ] **Step 5: 更新 README**

创建 `README.md`（完整替换）：

```markdown
# 办公自动化平台

多人使用的办公自动化工具，功能以插件形式接入。

## 启动

```bash
./run.sh                      # 仅本机
./.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8765   # 内网共享
```

浏览器打开 http://127.0.0.1:8765 ，注册账号后登录。

## 已有功能

| 功能 | 凭据类型 | 说明 |
|---|---|---|
| 美餐自动订餐 | 交互型（短信验证码） | 每天 15:30 检测并订餐，20 分钟保活 |
| 工时填报 | 密码型（HEM 账号） | 每周五 20:00 填报本周工时并提交审批 |

## 架构

```
app/core/        框架：认证、数据库、插件契约、调度、执行器
app/features/    插件：每个功能一个目录
app/templates/   通用页面
```

`core/` 不依赖 `features/`。新增功能只需：

1. 在 `app/features/<name>/` 下实现 `Feature` 抽象基类
2. 在 `app/main.py` 的 `_register_features()` 里注册一行

## 凭据类型

- **PASSWORD**：可用已存凭据自动重登（如工时）
- **INTERACTIVE**：必须人工介入（如美餐短信验证码）

框架按类型自动决定登录态失效后的策略。

## 安全

- 系统密码：PBKDF2-HMAC-SHA256 加盐哈希
- 功能凭据：Fernet 加密存库，密钥 `data/secret.key`（600）
- 会话：HttpOnly cookie，30 天滑动续期，多设备并存
- 自助注册默认开放，`ALLOW_REGISTER=0` 可关闭
- 当前为 HTTP，仅限可信内网；上公网须加 HTTPS

## 测试

```bash
./.venv/bin/python -m pytest tests/ -v
```
```

- [ ] **Step 6: 重启服务做冒烟测试**

```bash
pkill -f "uvicorn app.main" 2>/dev/null; sleep 1
nohup ./.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8765 > data/server.log 2>&1 &
sleep 5
curl -sS -o /dev/null -w 'login=%{http_code} ' http://127.0.0.1:8765/login
curl -sS -o /dev/null -w 'index=%{http_code}\n' http://127.0.0.1:8765/
tail -3 data/server.log
```

Expected: `login=200 index=200`，日志无 ERROR

- [ ] **Step 7: 提交**

```bash
git add -A
git commit -m "chore: 清理旧模块，更新文档与启动脚本"
```

---

## Task 13: 项目改名

**Files:**
- Rename: 项目目录 `meican-auto` → `office-auto`

- [ ] **Step 1: 停服务**

```bash
pkill -f "uvicorn app.main" 2>/dev/null; sleep 1
```

- [ ] **Step 2: 改名**

```bash
cd /Users/admin
mv meican-auto office-auto
cd office-auto
```

- [ ] **Step 3: 验证**

```bash
pwd
./.venv/bin/python -m pytest tests/ -q
```

Expected: 路径为 `/Users/admin/office-auto`，测试全部通过

- [ ] **Step 4: 启动**

```bash
nohup ./.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8765 > data/server.log 2>&1 &
sleep 5
curl -sS -o /dev/null -w 'index=%{http_code}\n' http://127.0.0.1:8765/
```

- [ ] **Step 5: 提交**

```bash
git add -A
git commit -m "chore: 项目改名为 office-auto"
```

---

## 验收清单

全部任务完成后逐项确认：

- [ ] `pytest tests/ -v` 全部通过
- [ ] 能注册并登录系统账号，刷新页面不掉线
- [ ] 首页显示两张功能卡片，状态正确
- [ ] 美餐功能页：输入手机号+验证码可登录，卡片变「已登录」
- [ ] 工时功能页：输入 HEM 账号密码可登录，项目下拉有数据
- [ ] 工时「立即执行」在 dry-run 下产出计划，不调用提交接口
- [ ] 关闭 dry-run 后执行，真实提交（需人工确认时机）
- [ ] 修改触发时间后，`next_run` 变化正确
- [ ] 两个不同账号登录，数据互不可见
- [ ] `data/app.db` 里 `feature_credentials.payload` 不含明文
