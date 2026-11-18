"""多功能平台数据层。

六张表：users / sessions / feature_credentials / feature_configs / feature_data / job_runs
功能数据一律按 (user_id, feature_key) 隔离；凭据用 Fernet 加密落盘。
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
    """读取/生成 Fernet 密钥。

    兼容三种历史形态，避免换格式导致已有凭据解不开：
    1. 合法 Fernet key（44 字节 url-safe base64）—— 直接用
    2. 旧版遗留的 32 字节原始随机字节 —— 按旧派生方式 base64(sha256(raw))
    3. 文件不存在 —— 生成新 key 并写盘（600）
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if KEY_PATH.exists():
        raw = KEY_PATH.read_bytes().strip()
        try:
            Fernet(raw)                 # 形态 1：校验是否已是合法 key
            return raw
        except (ValueError, TypeError):
            # 形态 2：旧版用 sha256 派生后再 base64，沿用同一派生保持可解密
            return base64.urlsafe_b64encode(hashlib.sha256(raw).digest())
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

CREATE TABLE IF NOT EXISTS global_data (
  key         TEXT PRIMARY KEY,
  value       TEXT NOT NULL DEFAULT '',
  updated_at  INTEGER NOT NULL DEFAULT 0
);
"""


# 结构版本：改了 SCHEMA 就 +1。只作对账记录，实际修复靠下方的
# 声明式 diff（DDL 是唯一真相），不依赖有序迁移脚本。
SCHEMA_VERSION = 1


@dataclass
class ColumnSpec:
    name: str
    type: str
    notnull: bool
    default: str | None
    pk: bool


@dataclass
class SchemaReport:
    """启动自检结果。有改动才逐条列出，正常时只有一行「校验通过」。"""
    db_created: bool = False
    created_tables: list[str] = field(default_factory=list)
    added_columns: list[str] = field(default_factory=list)     # "表.字段"
    extra_columns: list[str] = field(default_factory=list)     # 库里有、代码没有
    type_mismatches: list[str] = field(default_factory=list)   # 类型对不上
    problems: list[str] = field(default_factory=list)          # 无法自动修复
    version: int = SCHEMA_VERSION
    seeded: list[str] = field(default_factory=list)            # 初始化数据

    @property
    def changed(self) -> bool:
        return bool(self.created_tables or self.added_columns or self.db_created)

    def lines(self) -> list[str]:
        out = []
        if self.db_created:
            out.append(f"数据库不存在，已新建：{DB_PATH}")
        if self.created_tables:
            out.append("新建表：" + "、".join(self.created_tables))
        if self.added_columns:
            out.append("补齐字段：" + "、".join(self.added_columns))
        if self.extra_columns:
            out.append("⚠ 库中多出字段（代码未使用，保守起见未删除）："
                       + "、".join(self.extra_columns))
        if self.type_mismatches:
            out.append("⚠ 字段类型与代码定义不一致（未自动改，请人工确认）："
                       + "、".join(self.type_mismatches))
        if self.problems:
            out.extend("⚠ " + p for p in self.problems)
        if self.seeded:
            out.extend(self.seeded)
        if (not self.changed and not self.extra_columns
                and not self.type_mismatches and not self.problems):
            out.append(f"表结构校验通过，无需变更（schema v{self.version}）")
        return out


# SQLite 的类型松散，INT/INTEGER、VARCHAR/TEXT 等价。按亲和性归并，
# 避免把兼容写法误报成「结构不一致」。
_TYPE_FAMILY = {
    "INT": "INTEGER", "INTEGER": "INTEGER", "TINYINT": "INTEGER",
    "SMALLINT": "INTEGER", "MEDIUMINT": "INTEGER", "BIGINT": "INTEGER",
    "UNSIGNED BIG INT": "INTEGER",
    "TEXT": "TEXT", "VARCHAR": "TEXT", "CHAR": "TEXT", "CLOB": "TEXT",
    "REAL": "REAL", "FLOAT": "REAL", "DOUBLE": "REAL", "NUMERIC": "REAL",
    "DECIMAL": "REAL",
    "BLOB": "BLOB", "": "BLOB",
}


def _type_family(t: str | None) -> str:
    base = (t or "").upper().split("(")[0].strip()
    return _TYPE_FAMILY.get(base, base)


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _table_names(c: sqlite3.Connection) -> list[str]:
    return [r[0] for r in c.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
        " AND name NOT LIKE 'sqlite_%' ORDER BY name")]


def _read_columns(c: sqlite3.Connection, table: str) -> dict[str, ColumnSpec]:
    rows = c.execute(f"PRAGMA table_info({_quote(table)})").fetchall()
    return {r[1]: ColumnSpec(r[1], (r[2] or "").upper(), bool(r[3]), r[4], bool(r[5]))
            for r in rows}


def expected_schema() -> dict[str, dict[str, ColumnSpec]]:
    """在内存库里跑一遍 SCHEMA，得到「应该有」的结构。

    以 DDL 文本为唯一真相，不手写第二份字段清单——那样迟早漂移。
    """
    mem = sqlite3.connect(":memory:")
    try:
        mem.executescript(SCHEMA)
        return {t: _read_columns(mem, t) for t in _table_names(mem)}
    finally:
        mem.close()


def _add_column_ddl(table: str, col: ColumnSpec) -> str:
    """生成补列语句。

    SQLite 的 ADD COLUMN 有个硬限制：NOT NULL 必须带默认值。老库缺的
    若是 NOT NULL 且 DDL 没写默认值的字段，用类型零值兜底，保证已有
    数据行仍可读——宁可结构略有偏差，也不能让迁移失败。
    """
    ctype = col.type or "TEXT"
    default = col.default
    if col.notnull and default is None:
        default = "''" if _type_family(ctype) == "TEXT" else "0"
    sql = f"ALTER TABLE {_quote(table)} ADD COLUMN {_quote(col.name)} {ctype}"
    if default is not None:
        sql += f" DEFAULT {default}"
    if col.notnull:
        sql += " NOT NULL"
    return sql


def init_db() -> SchemaReport:
    """建库 + 结构对账。幂等，每次启动都该调用。

    做三件事：
    1. 缺失的表/索引直接建（CREATE TABLE IF NOT EXISTS）
    2. 已有表缺字段 → ALTER TABLE ADD COLUMN 自动补齐
    3. 多余字段、类型不一致 → 只报告不改（可能是代码回滚，删列会丢数据）
    """
    report = SchemaReport()
    report.db_created = not DB_PATH.exists()
    expected = expected_schema()

    with conn() as c:
        if not report.db_created and DB_PATH.stat().st_size == 0:
            report.db_created = True
        before_tables = _table_names(c)
        before_cols = {t: _read_columns(c, t) for t in before_tables}
        old_version = c.execute("PRAGMA user_version").fetchone()[0]

        # 1) 建缺失的表与索引
        c.executescript(SCHEMA)
        report.created_tables = [t for t in expected if t not in before_tables]

        # 2) 补缺失字段
        for table, cols in expected.items():
            actual = _read_columns(c, table)
            for name, spec in cols.items():
                if name in actual:
                    continue
                if spec.pk:
                    # 主键无法后补，只能人工重建表
                    report.problems.append(
                        f"{table}.{name} 是主键但库中缺失，SQLite 无法补主键，请人工重建表")
                    continue
                c.execute(_add_column_ddl(table, spec))
                report.added_columns.append(f"{table}.{name}")

        # 3) 对账：多余字段 / 类型不一致
        for table, cols in expected.items():
            actual = _read_columns(c, table)
            for name, spec in actual.items():
                if name not in cols:
                    report.extra_columns.append(f"{table}.{name}")
                elif _type_family(spec.type) != _type_family(cols[name].type):
                    report.type_mismatches.append(
                        f"{table}.{name}（库 {spec.type} / 代码 {cols[name].type}）")

        # 版本只进不退：代码回滚时不把新库降级标记
        c.execute(f"PRAGMA user_version = {max(old_version, SCHEMA_VERSION)}")
        report.version = max(old_version, SCHEMA_VERSION)
    return report


def seed_initial_data() -> list[str]:
    """新环境首次启动的初始化数据。

    只处理一件事：库里一个用户都没有时，按环境变量引导创建管理员。
    没设 ADMIN_USERNAME/ADMIN_PASSWORD 就什么都不做——常规流程仍是
    在登录页自助注册，这里只是让「ALLOW_REGISTER=0 的部署」也能进得去。
    幂等：已有用户则直接跳过。
    """
    if count_users() > 0:
        return []

    username = (os.environ.get("ADMIN_USERNAME") or "").strip()
    password = os.environ.get("ADMIN_PASSWORD") or ""
    if not username or not password:
        if os.environ.get("ALLOW_REGISTER", "1") == "0":
            return ["⚠ 当前无任何用户且 ALLOW_REGISTER=0：无人能登录。"
                    "请设置 ADMIN_USERNAME / ADMIN_PASSWORD 后重启。"]
        return []

    from . import security          # 延迟导入，避免模块循环依赖
    if len(username) < 2 or len(password) < 6:
        return ["⚠ ADMIN_USERNAME 至少 2 位、ADMIN_PASSWORD 至少 6 位，已跳过管理员创建。"]
    if get_user_by_username(username):
        return []
    create_user(username, security.hash_password(password), username)
    return [f"已按环境变量创建管理员账号：{username}"]


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


def credential_pairs() -> list[tuple[int, str]]:
    """所有保存过凭据的 (user, feature)，不看功能开关。

    常开任务（如登录保活）需要用它——用户可能从没保存过配置，
    但登录过，保活就该跑。
    """
    with conn() as c:
        rows = c.execute(
            "SELECT user_id, feature_key FROM feature_credentials").fetchall()
    return [(r["user_id"], r["feature_key"]) for r in rows]


def active_credential_payloads(feature_key: str) -> list[dict]:
    """某功能下所有「有效」凭据的明文 payload。

    共享缓存刷新用：它不针对某个用户，而是借用任意一份有效凭据
    把全企业共享的菜单/份数拉新。失效凭据打不通接口，直接跳过。
    """
    with conn() as c:
        rows = c.execute(
            "SELECT user_id, payload FROM feature_credentials"
            " WHERE feature_key=? AND state=?", (feature_key, STATE_ACTIVE)).fetchall()
    out = []
    for r in rows:
        p = decrypt_dict(r["payload"])
        if p:
            out.append(p)
    return out


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


# ---------------- 全局共享 KV（跨用户） ----------------

def gdata_get(key: str, default=None):
    with conn() as c:
        row = c.execute("SELECT value FROM global_data WHERE key=?", (key,)).fetchone()
    if not row:
        return default
    try:
        return json.loads(row["value"])
    except ValueError:
        return default


def gdata_set(key: str, value) -> None:
    with conn() as c:
        c.execute(
            "INSERT INTO global_data(key,value,updated_at) VALUES(?,?,?)"
            " ON CONFLICT(key) DO UPDATE SET"
            " value=excluded.value, updated_at=excluded.updated_at",
            (key, json.dumps(value, ensure_ascii=False), int(time.time())))


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