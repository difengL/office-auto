"""SQLite 存储层（多用户）。

设计要点：
- 一人一行 users，手机号唯一；能收到该号验证码即视为账号主人
- 凭据用 Fernet 加密落库（密钥来自环境变量 MC_SECRET_KEY，首次自动生成并写 data/secret.key）
- session_token 是随机 32 字节，30 天滑动续期
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "meican.db"
KEY_PATH = DATA_DIR / "secret.key"

SESSION_DAYS = 30
SESSION_REFRESH_THRESHOLD = 7 * 86400   # 剩余不足 7 天时续期


# ---------------- 加密 ----------------

def _load_key() -> bytes:
    """密钥优先取环境变量，否则读/建本地密钥文件（600 权限）。"""
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


try:
    from cryptography.fernet import Fernet
    _fernet = Fernet(__import__("base64").urlsafe_b64encode(_load_key()))
    _HAS_CRYPTO = True
except ImportError:      # 没装 cryptography 时退化为明文，并在启动时告警
    _fernet = None
    _HAS_CRYPTO = False


def encrypt(s: str) -> str:
    if not s:
        return ""
    if _fernet:
        return _fernet.encrypt(s.encode()).decode()
    return "PLAIN:" + s


def decrypt(s: str) -> str:
    if not s:
        return ""
    if s.startswith("PLAIN:"):
        return s[6:]
    if _fernet:
        try:
            return _fernet.decrypt(s.encode()).decode()
        except Exception:  # noqa: BLE001
            return ""
    return s


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
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  phone          TEXT UNIQUE NOT NULL,
  phone_masked   TEXT NOT NULL DEFAULT '',
  meican_user_id TEXT DEFAULT '',
  name           TEXT DEFAULT '',
  created_at     INTEGER NOT NULL
);

-- 一个用户可以有多个并发会话（手机/电脑/多个浏览器）
CREATE TABLE IF NOT EXISTS sessions (
  token      TEXT PRIMARY KEY,
  user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  created_at INTEGER NOT NULL,
  exp        INTEGER NOT NULL,
  ua         TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);

CREATE TABLE IF NOT EXISTS credentials (
  user_id      INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
  access_token TEXT DEFAULT '',
  refresh_token TEXT DEFAULT '',
  token_type   TEXT DEFAULT 'bearer',
  obtained_at  INTEGER DEFAULT 0,
  expires_at   INTEGER DEFAULT 0,
  last_ping_at INTEGER DEFAULT 0,
  snowflake_id TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS configs (
  user_id       INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
  enabled       INTEGER DEFAULT 0,
  schedule_time TEXT DEFAULT '15:30',
  order_target  TEXT DEFAULT 'next_day',
  max_price     REAL DEFAULT 25.0,
  dry_run       INTEGER DEFAULT 1
);

CREATE TABLE IF NOT EXISTS restaurant_names (
  user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  restaurant_id TEXT NOT NULL,
  name          TEXT NOT NULL,
  PRIMARY KEY (user_id, restaurant_id)
);

CREATE TABLE IF NOT EXISTS logs (
  id      INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL,
  ts      INTEGER NOT NULL,
  kind    TEXT NOT NULL,
  message TEXT NOT NULL,
  extra   TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_logs_user ON logs(user_id, id DESC);
"""


def init_db() -> None:
    with conn() as c:
        c.executescript(SCHEMA)


# ---------------- 用户与会话 ----------------

@dataclass
class User:
    id: int
    phone: str
    phone_masked: str
    meican_user_id: str
    name: str
    session_exp: int = 0      # 兼容字段：当前请求所用会话的过期时间


MAX_SESSIONS_PER_USER = 10    # 超出的旧会话自动清理


def mask_phone(phone: str) -> str:
    if len(phone) < 7:
        return phone
    return f"{phone[:3]}****{phone[-4:]}"


def _row_to_user(row) -> User:
    return User(row["id"], row["phone"], row["phone_masked"], row["meican_user_id"],
                row["name"], 0)


def get_or_create_user(phone: str, meican_user_id: str = "", name: str = "") -> User:
    masked = mask_phone(phone)
    now = int(time.time())
    with conn() as c:
        row = c.execute("SELECT * FROM users WHERE phone=?", (phone,)).fetchone()
        if row:
            c.execute(
                "UPDATE users SET meican_user_id=COALESCE(NULLIF(?,''), meican_user_id),"
                " name=COALESCE(NULLIF(?,''), name) WHERE id=?",
                (meican_user_id, name, row["id"]),
            )
            uid = row["id"]
        else:
            cur = c.execute(
                "INSERT INTO users(phone, phone_masked, meican_user_id, name, created_at)"
                " VALUES(?,?,?,?,?)",
                (phone, masked, meican_user_id, name, now),
            )
            uid = cur.lastrowid
            c.execute("INSERT INTO configs(user_id) VALUES(?)", (uid,))
            c.execute("INSERT INTO credentials(user_id) VALUES(?)", (uid,))
        row = c.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    return _row_to_user(row)


def issue_session(user_id: int, ua: str = "") -> tuple[str, int]:
    """新建一个会话（不覆盖已有会话，支持多设备同时在线）。"""
    token = secrets.token_hex(32)
    exp = int(time.time()) + SESSION_DAYS * 86400
    now = int(time.time())
    with conn() as c:
        c.execute("INSERT INTO sessions(token, user_id, created_at, exp, ua) VALUES(?,?,?,?,?)",
                  (token, user_id, now, exp, ua[:200]))
        # 只保留最近的 N 个会话，其余清理
        old = c.execute(
            "SELECT token FROM sessions WHERE user_id=? ORDER BY created_at DESC LIMIT -1 OFFSET ?",
            (user_id, MAX_SESSIONS_PER_USER),
        ).fetchall()
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

        exp = s["exp"]
        # 滑动续期：剩余不足阈值时延长
        if exp - now < SESSION_REFRESH_THRESHOLD:
            exp = int(now) + SESSION_DAYS * 86400
            c.execute("UPDATE sessions SET exp=? WHERE token=?", (exp, token))

        row = c.execute("SELECT * FROM users WHERE id=?", (s["user_id"],)).fetchone()
    if not row:
        return None
    u = _row_to_user(row)
    u.session_exp = exp
    return u


def list_sessions(user_id: int) -> list[dict]:
    """列出某用户的活跃会话，便于在页面上查看/踢掉设备。"""
    with conn() as c:
        rows = c.execute(
            "SELECT token, created_at, exp, ua FROM sessions WHERE user_id=? AND exp>?"
            " ORDER BY created_at DESC", (user_id, int(time.time()))).fetchall()
    return [{"token": r["token"][:8] + "…", "created_at": r["created_at"],
             "exp": r["exp"], "ua": r["ua"]} for r in rows]


def revoke_session(token: str) -> None:
    with conn() as c:
        c.execute("DELETE FROM sessions WHERE token=?", (token,))


def clear_session(user_id: int) -> None:
    """退出登录：清掉该用户的全部会话。"""
    with conn() as c:
        c.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))


def get_user(user_id: int) -> User | None:
    with conn() as c:
        row = c.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    if not row:
        return None
    return User(row["id"], row["phone"], row["phone_masked"], row["meican_user_id"],
                row["name"], row["session_exp"])


def list_users() -> list[User]:
    with conn() as c:
        rows = c.execute("SELECT * FROM users ORDER BY id").fetchall()
    return [User(r["id"], r["phone"], r["phone_masked"], r["meican_user_id"], r["name"],
                 r["session_exp"]) for r in rows]


def enabled_user_ids() -> list[int]:
    with conn() as c:
        rows = c.execute("SELECT user_id FROM configs WHERE enabled=1").fetchall()
    return [r["user_id"] for r in rows]


# ---------------- 凭据 ----------------

@dataclass
class Creds:
    access_token: str = ""
    refresh_token: str = ""
    token_type: str = "bearer"
    obtained_at: int = 0
    expires_at: int = 0
    last_ping_at: int = 0
    snowflake_id: str = ""

    @property
    def age_hours(self) -> float:
        return (time.time() - self.obtained_at) / 3600 if self.obtained_at else 0.0

    @property
    def idle_minutes(self) -> float:
        base = self.last_ping_at or self.obtained_at
        return (time.time() - base) / 60 if base else 9999.0


def load_creds(user_id: int) -> Creds:
    with conn() as c:
        row = c.execute("SELECT * FROM credentials WHERE user_id=?", (user_id,)).fetchone()
    if not row:
        return Creds()
    return Creds(
        access_token=decrypt(row["access_token"]),
        refresh_token=decrypt(row["refresh_token"]),
        token_type=row["token_type"] or "bearer",
        obtained_at=row["obtained_at"] or 0,
        expires_at=row["expires_at"] or 0,
        last_ping_at=row["last_ping_at"] or 0,
        snowflake_id=row["snowflake_id"] or "",
    )


def save_creds(user_id: int, cred: Creds) -> None:
    with conn() as c:
        c.execute(
            "INSERT INTO credentials(user_id, access_token, refresh_token, token_type,"
            " obtained_at, expires_at, last_ping_at, snowflake_id)"
            " VALUES(?,?,?,?,?,?,?,?)"
            " ON CONFLICT(user_id) DO UPDATE SET"
            " access_token=excluded.access_token, refresh_token=excluded.refresh_token,"
            " token_type=excluded.token_type, obtained_at=excluded.obtained_at,"
            " expires_at=excluded.expires_at, last_ping_at=excluded.last_ping_at,"
            " snowflake_id=excluded.snowflake_id",
            (user_id, encrypt(cred.access_token), encrypt(cred.refresh_token),
             cred.token_type, cred.obtained_at, cred.expires_at, cred.last_ping_at,
             cred.snowflake_id),
        )


def touch_ping(user_id: int) -> None:
    with conn() as c:
        c.execute("UPDATE credentials SET last_ping_at=? WHERE user_id=?",
                  (int(time.time()), user_id))


# ---------------- 配置 ----------------

@dataclass
class Config:
    enabled: bool = False
    schedule_time: str = "15:30"
    order_target: str = "next_day"
    max_price: float = 25.0
    dry_run: bool = True


def load_config(user_id: int) -> Config:
    with conn() as c:
        row = c.execute("SELECT * FROM configs WHERE user_id=?", (user_id,)).fetchone()
    if not row:
        return Config()
    return Config(bool(row["enabled"]), row["schedule_time"] or "15:30",
                 row["order_target"] or "next_day", row["max_price"] or 25.0,
                 bool(row["dry_run"]))


def save_config(user_id: int, cfg: Config) -> None:
    with conn() as c:
        c.execute(
            "INSERT INTO configs(user_id, enabled, schedule_time, order_target, max_price, dry_run)"
            " VALUES(?,?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET"
            " enabled=excluded.enabled, schedule_time=excluded.schedule_time,"
            " order_target=excluded.order_target, max_price=excluded.max_price,"
            " dry_run=excluded.dry_run",
            (user_id, int(cfg.enabled), cfg.schedule_time, cfg.order_target,
             cfg.max_price, int(cfg.dry_run)),
        )


# ---------------- 餐厅名缓存 ----------------

def load_restaurant_names(user_id: int) -> dict[str, str]:
    with conn() as c:
        rows = c.execute("SELECT restaurant_id, name FROM restaurant_names WHERE user_id=?",
                         (user_id,)).fetchall()
    return {r["restaurant_id"]: r["name"] for r in rows}


def save_restaurant_names(user_id: int, mapping: dict[str, str]) -> None:
    if not mapping:
        return
    with conn() as c:
        c.executemany(
            "INSERT INTO restaurant_names(user_id, restaurant_id, name) VALUES(?,?,?)"
            " ON CONFLICT(user_id, restaurant_id) DO UPDATE SET name=excluded.name",
            [(user_id, k, v) for k, v in mapping.items() if k and v],
        )


# ---------------- 日志 ----------------

def log(user_id: int, kind: str, message: str, extra: dict[str, Any] | None = None) -> None:
    with conn() as c:
        c.execute("INSERT INTO logs(user_id, ts, kind, message, extra) VALUES(?,?,?,?,?)",
                  (user_id, int(time.time()), kind, message,
                   json.dumps(extra or {}, ensure_ascii=False)))


def read_logs(user_id: int, limit: int = 100) -> list[dict]:
    with conn() as c:
        rows = c.execute("SELECT * FROM logs WHERE user_id=? ORDER BY id DESC LIMIT ?",
                         (user_id, limit)).fetchall()
    return [{
        "ts": r["ts"],
        "time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(r["ts"])),
        "kind": r["kind"],
        "message": r["message"],
        "extra": json.loads(r["extra"]) if r["extra"] else {},
    } for r in rows]


def clear_logs(user_id: int) -> None:
    with conn() as c:
        c.execute("DELETE FROM logs WHERE user_id=?", (user_id,))
