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
    # 该字段需要先触发一个额外动作（如「发送验证码」）才能填。
    # 值为动作标识，插件在 send_action() 里按此分发。
    action: str = ""
    action_label: str = ""


@dataclass
class JobSpec:
    job_id: str
    name: str
    trigger: str                # cron | interval
    default: dict               # cron: hour/minute/second/day_of_week；interval: minutes/hours
    jitter: int = 0             # 随机抖动秒数
    editable: bool = True
    # 只允许用户改时刻，星期/日期由插件固定（如「每周五提交周报」——
    # 改成周三毫无意义）。这些键会被调度器忽略，始终以 default 为准。
    fixed_fields: tuple[str, ...] = ()
    # 基础设施型任务（如登录保活）：不跟着「启用定时任务」开关，只要凭据
    # 有效就自动运行。用户不需要理解它，也不该因为忘了打勾就掉登录态。
    always_on: bool = False


@dataclass
class OptionSpec:
    name: str
    label: str
    type: str = "select"        # select | text
    source: str = ""            # 选项来源说明（如 timesheet.projects）
    refreshable: bool = False


@dataclass
class ViewSpec:
    """只读查看视图（如「今日推荐」「可订菜品」「是否已订」）。

    与 run_job 的区别：view 不产生副作用、不记运行记录，纯粹给用户看。
    kind 决定前端渲染方式：status | candidates | recommend。
    """
    view_id: str
    name: str
    kind: str = "status"


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
    views: list[ViewSpec] = []
    dry_run_default: bool = False
    # 是否在页面提供「立即执行」按钮。有些任务只在特定时点做才合理
    # （如周五提交周报），给个按钮反而容易误触发，这类功能置 False。
    manual_run: bool = True

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
        """用已存凭据自动重登。

        PASSWORD 型通常实现为「账号密码再登一次」。
        INTERACTIVE 型默认不支持——它需要人给验证码；
        但如果站点下发了长期凭据（如美餐的 remember cookie），
        插件可以覆写本方法实现无人值守续期，并把 can_relogin 置为 True。
        """
        return False

    # 交互型凭据是否支持自动续期。默认否，覆写了 relogin 的插件置 True。
    can_relogin: bool = False

    async def refresh_options(self, user_id: int) -> list[dict]:
        """刷新可选项（如下拉数据）。返回 [{"value":..., "label":...}]。"""
        return []

    async def get_view(self, user_id: int, view_id: str, params: dict) -> dict:
        """返回只读视图数据。默认无视图。params 可含 date 等查询条件。"""
        raise FeatureLoginError(f"该功能没有查看视图：{view_id}")

    async def do_action(self, user_id: int, action: str, params: dict) -> JobResult:
        """执行用户主动触发的动作（如「订这道菜」），区别于定时 run_job。

        返回 JobResult；是否记运行记录由插件决定。默认不支持。
        """
        raise FeatureLoginError(f"该功能不支持操作：{action}")

    async def send_action(self, user_id: int, action: str, payload: dict) -> str:
        """执行登录前的辅助动作（如发送短信验证码）。

        登录尚未完成，此时没有已存凭据，所需信息从 payload 现取
        （如手机号）。返回给用户看的提示文案。
        """
        raise FeatureLoginError(f"不支持的操作：{action}")

    def migrate_config(self, cfg) -> bool:
        """修正已保存的配置，使其符合当前插件语义。返回是否有改动。

        默认值变更后，老用户库里存的旧值会继续覆盖新默认值——这在
        「默认时刻从 17:30 改成 20:00」这类调整时会让新默认形同虚设。
        插件在这里说明怎么迁，core 只负责调用与落库。
        """
        return False

    # ---------- 功能专属设置（如 HEM 的逐日工时） ----------

    def get_settings(self, user_id: int) -> dict | None:
        """返回该功能的自定义设置，供界面渲染。None 表示没有此类设置。

        这些设置会随功能详情一起下发，界面按返回的结构自行渲染。
        """
        return None

    def save_settings(self, user_id: int, body: dict) -> dict:
        """保存自定义设置，返回保存后的结构（与 get_settings 同形）。"""
        raise FeatureLoginError("该功能没有可保存的设置")