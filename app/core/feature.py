"""功能插件契约。

新增一个功能 = 在 app/features/ 下写一个包，实现 Feature 子类并注册。
框架（core/）只认这里的接口，不认识任何具体业务。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class CredentialKind(str, Enum):
    """账号密码型：可自动重登；交互型：需人工介入（扫码等）。"""
    PASSWORD = "password"
    INTERACTIVE = "interactive"


class FeatureState(str, Enum):
    NOT_CONFIGURED = "not_configured"
    ACTIVE = "active"
    EXPIRED = "expired"
    ERROR = "error"


class FeatureLoginError(Exception):
    """凭据校验失败，message 直接展示给用户。"""


@dataclass
class FieldSpec:
    name: str
    label: str
    type: str = "text"          # text | password
    secret: bool = False
    required: bool = True


@dataclass
class JobSpec:
    job_id: str
    name: str
    trigger: str                # cron | interval
    default: dict               # cron: hour/minute/second/day_of_week；interval: minutes/hours
    jitter: int = 0             # 随机抖动秒数
    editable: bool = True


@dataclass
class OptionSpec:
    name: str
    label: str
    type: str = "select"        # select | text
    source: str = ""            # 选项来源说明（如 timesheet.projects）
    refreshable: bool = False


@dataclass
class JobResult:
    ok: bool
    message: str = ""
    detail: dict = field(default_factory=dict)


class JobContext:
    """传给插件的执行上下文，封装 dry-run、配置与该功能的 KV 读写。"""

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