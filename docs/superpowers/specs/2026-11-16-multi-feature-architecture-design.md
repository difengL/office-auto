# 多功能平台架构设计

日期：2026-11-16
状态：待实现

> 契约代码片段省略了 `from dataclasses import dataclass, field` 等导入语句。

## 目标

把当前单一功能的「美餐自动订餐」改造为可扩展的多功能平台，支持：

1. **统一登录**：用户用一套系统账号（用户名 + 密码）登录，与各功能自身的登录解耦
2. **功能插件化**：每个功能独立管理自己的凭据、登录态、定时任务
3. **首期功能**：美餐自动订餐（已有）+ 工时自动填报（新增）
4. **为后续功能预留扩展位**

## 背景约束

### 美餐的凭据特性

美餐只能手机号 + 短信验证码登录，且：

- accessToken 的 TTL 是「最后一次使用后 1 小时」
- refreshToken 实测无效，无法续期
- ticket 一次性

因此美餐无法无人值守重登，必须人工介入输验证码。这是**交互型凭据**。

### HEM 的凭据特性（实测确认）

HEM 工时系统（https://hem.100credit.cn）可直接用账号密码登录：

- 登录：`POST /api/login`，body `{username, password}`
- 鉴权：下发 HttpOnly cookie（`session_id` + `token`），有效期 10 天
- 无需 CAS ticket、无需验证码

因此 HEM 可以无人值守重登。这是**密码型凭据**。

### 请假场景

HEM 页面明确说明：

> 本系统会自动获取您在 OA 提交的请假表单及审批状态，并且不可在本系统中修改和删除。
> 如您主动撤回请假表单或审批状态为不通过，本系统将自动删除对应工时。

**结论：请假由 HEM 系统自身处理，插件不需要判断请假。**

## 架构

### 目录结构

```
office-auto/                    （由 meican-auto 改名）
  app/
    core/                       框架层，不含任何业务逻辑
      auth.py                   系统账号登录
      db.py                     SQLite + Fernet 加密
      feature.py                Feature 抽象基类
      registry.py               插件注册表
      scheduler.py              按 用户 × 功能 × 任务 调度
      runner.py                 统一执行流程
    features/
      meican/                   美餐功能
        client.py               美餐 API 客户端（原 meican.py）
        preference.py           偏好模型（原样迁移）
        jobs.py                 订餐 / 保活任务
        feature.py              实现 Feature 契约
      timesheet/                工时功能
        client.py               HEM API 客户端
        feature.py              实现 Feature 契约
        jobs.py                 周填报任务
    main.py                     路由聚合 + 插件装配
    templates/
      base.html                 布局（顶栏 + 侧边栏）
      login.html                系统登录 / 注册
      index.html                功能卡片总览
      feature.html              通用功能面板（按插件声明渲染）
  data/
    app.db                      SQLite 主库
    secret.key                  加密密钥
```

**关键约束：`core/` 永远不 import `features/`。** 加新功能只动 `features/` 目录和注册表。

### 核心契约

```python
class CredentialKind(str, Enum):
    PASSWORD    = "password"     # 可用已存凭据自动重登
    INTERACTIVE = "interactive"  # 必须人工介入

class FeatureState(str, Enum):
    NOT_CONFIGURED = "not_configured"
    ACTIVE         = "active"
    EXPIRED        = "expired"
    ERROR          = "error"

@dataclass
class FieldSpec:
    name: str
    label: str
    type: str = "text"      # text | password
    secret: bool = False    # 是否加密存储
    required: bool = True

@dataclass
class JobSpec:
    job_id: str
    name: str
    trigger: str            # "cron" | "interval"
    default: dict           # cron: {"hour":15,"minute":30} / interval: {"minutes":20}
    jitter: int = 0         # 随机抖动（分钟），仅 interval
    editable: bool = True   # 用户能否改触发时间

@dataclass
class JobContext:
    user_id: int
    feature_key: str
    job_id: str
    dry_run: bool
    options: dict              # feature_configs.options_json 解析结果
    # 插件私有 KV 读写，落 feature_data 表
    def get_data(self, key: str, default=None): ...
    def set_data(self, key: str, value) -> None: ...

@dataclass
class JobResult:
    ok: bool
    message: str = ""
    detail: dict = field(default_factory=dict)

@dataclass
class OptionSpec:
    name: str               # 存进 options_json 的键，如 "project_id"
    label: str              # 界面显示名
    type: str = "select"    # select
    source: str = ""        # 数据来源标识，插件自行解释（如 "projects"）
    refreshable: bool = False   # 是否显示「强制刷新」按钮

class Feature(ABC):
    key: str                        # 唯一标识，如 "meican"
    name: str                       # 显示名
    description: str
    credential_kind: CredentialKind
    credential_fields: list[FieldSpec]
    default_jobs: list[JobSpec]
    extra_options: list[OptionSpec] = []   # 额外配置项（如工时选项目）
    dry_run_default: bool = False           # 该功能是否默认演练模式

    @abstractmethod
    async def login(self, user_id: int, payload: dict) -> dict:
        """校验凭据并持久化。失败抛 FeatureLoginError。返回展示信息。"""

    @abstractmethod
    async def check_alive(self, user_id: int) -> bool:
        """检查登录态是否有效。"""

    @abstractmethod
    async def run_job(self, user_id: int, job_id: str, ctx: JobContext) -> JobResult:
        """执行任务。ctx 携带 dry_run、options、插件私有数据读写。"""

    async def relogin(self, user_id: int) -> bool:
        """用已存凭据自动重登。仅 PASSWORD 型需要实现。"""
        return False

    async def on_options_refresh(self, user_id: int) -> list[dict]:
        """刷新可选项目列表等。可选实现。"""
        return []
```

### 统一执行流程（runner.py）

所有定时任务的执行都走同一套逻辑：

```
execute(user_id, feature_key, job_id):
    cred = load_credential(user_id, feature_key)
    if cred.state == NOT_CONFIGURED:
        return skip("该功能未配置凭据")
    if cred.state == EXPIRED:
        return skip("凭据已失效，需重新登录")

    if not await feature.check_alive(user_id):
        if feature.credential_kind == PASSWORD:
            if await feature.relogin(user_id):
                # 重登成功，继续
                pass
            else:
                mark_state(EXPIRED); notify(); return fail()
        else:
            mark_state(EXPIRED); notify(); return fail()

    ctx = JobContext(user_id, feature_key, job_id,
                     dry_run=cfg.dry_run, options=parse_json(cfg.options_json))
    result = await feature.run_job(user_id, job_id, ctx)
    record_job_run(...)
    return result
```

这样每个插件只实现机制，策略由框架统一。

### 数据模型

```sql
CREATE TABLE users (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  username      TEXT UNIQUE NOT NULL,
  password_hash TEXT NOT NULL,
  display_name  TEXT NOT NULL DEFAULT '',
  created_at    INTEGER NOT NULL
);

CREATE TABLE sessions (
  token      TEXT PRIMARY KEY,
  user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  created_at INTEGER NOT NULL,
  exp        INTEGER NOT NULL,
  ua         TEXT DEFAULT ''
);
CREATE INDEX idx_sessions_user ON sessions(user_id);

-- 每个用户每个功能一行
CREATE TABLE feature_credentials (
  user_id         INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  feature_key     TEXT NOT NULL,
  payload         TEXT NOT NULL DEFAULT '',   -- Fernet 加密的 JSON
  state           TEXT NOT NULL DEFAULT 'not_configured',
  last_ok_at      INTEGER DEFAULT 0,
  last_error      TEXT DEFAULT '',
  updated_at      INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (user_id, feature_key)
);

CREATE TABLE feature_configs (
  user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  feature_key   TEXT NOT NULL,
  enabled       INTEGER NOT NULL DEFAULT 0,
  dry_run       INTEGER NOT NULL DEFAULT 1,   -- 初始化时取插件的 dry_run_default
  schedule_json TEXT NOT NULL DEFAULT '{}',   -- {"order":{"hour":15,"minute":40}}
  options_json  TEXT NOT NULL DEFAULT '{}',   -- {"project_id":"6001"}
  PRIMARY KEY (user_id, feature_key)
);

-- 插件私有缓存 KV
CREATE TABLE feature_data (
  user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  feature_key TEXT NOT NULL,
  key         TEXT NOT NULL,
  value       TEXT NOT NULL DEFAULT '',
  updated_at  INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (user_id, feature_key, key)
);

CREATE TABLE job_runs (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id     INTEGER NOT NULL,
  feature_key TEXT NOT NULL,
  job_id      TEXT NOT NULL,
  ts          INTEGER NOT NULL,
  ok          INTEGER NOT NULL,
  message     TEXT NOT NULL DEFAULT '',
  detail      TEXT NOT NULL DEFAULT ''
);
CREATE INDEX idx_job_runs ON job_runs(user_id, feature_key, id DESC);
```

凭据的 `payload` 用 Fernet 加密整体存储（密钥同现有 `data/secret.key` 机制）。

### 调度

任务 id 格式：`{feature_key}:{job_id}:{user_id}`

启动时：
1. 遍历所有启用了功能的用户
2. 对每个 (用户, 功能)，读 `default_jobs`，用 `schedule_json` 覆盖默认值
3. 注册任务

用户修改触发时间时，只重建该用户该功能的任务。

任务执行统一走 `runner.execute`。

### 界面

**登录页 `/login`**
- 用户名 + 密码登录
- 自助注册（同一页面切换）
- 密码用 bcrypt 或 PBKDF2 哈希

**首页 `/`**
- 功能卡片列表
- 每张卡片显示：图标、名称、状态点（已启用/未配置/失效）、下次执行时间

**功能面板 `/feature/{key}`**
- 通用模板，按插件的 `credential_fields` 渲染凭据表单
- 登录态卡片：状态 + 最近成功时间 + （交互型）重新登录按钮
- 任务列表：开关 + 触发时间（`editable` 为 true 时可改）
- `extra_options` 渲染额外配置（如工时的项目下拉 + 强制刷新）
- 最近运行记录（读 `job_runs`）

### 美餐插件

```python
class MeicanFeature(Feature):
    key = "meican"
    name = "美餐自动订餐"
    credential_kind = INTERACTIVE
    credential_fields = [
        FieldSpec("phone", "手机号"),
        FieldSpec("code", "短信验证码"),   # 交互型：每次登录都要填
    ]
    default_jobs = [
        JobSpec("order", "自动订餐", "cron", {"hour": 15, "minute": 30}),
        JobSpec("keepalive", "登录保活", "interval", {"minutes": 20}, jitter=5, editable=False),
    ]
```

现有 `service.py` 的订餐/推荐逻辑迁入 `features/meican/jobs.py`，`meican.py` 改名为 `client.py`，`preference.py` 原样保留。

### 工时插件

```python
class TimesheetFeature(Feature):
    key = "timesheet"
    name = "工时填报"
    credential_kind = PASSWORD
    credential_fields = [
        FieldSpec("username", "用户名"),
        FieldSpec("password", "密码", type="password", secret=True),
    ]
    default_jobs = [
        JobSpec("weekly_submit", "每周工时填报", "cron",
                {"day_of_week": "fri", "hour": 20, "minute": 0}),
    ]
    extra_options = [
        OptionSpec("project_id", "填报项目", "select", source="projects", refreshable=True),
    ]
    dry_run_default = True       # 提交类功能默认演练，不真提交
```

**HEM API 契约**（实测确认）：

| 用途 | 方法与路径 | 参数 |
|---|---|---|
| 登录 | `POST /api/login` | `{username, password}` → Set-Cookie |
| 读本周工时 | `GET /api/daily/week` | `start_time`, `end_time`, `new_menu=0` |
| 项目列表 | `GET /api/localproject/list` | `page_size=10000` |
| 提交审批 | `GET /api/hem/weekly/user-submit` | `username`, `dateId`, `type` |

工时数据结构：

```json
{
  "list": [{
    "name": "项目名",
    "project_id": "6001",
    "total": 40.0,
    "state": "pending",
    "content": [
      {"date": "2026-11-16", "task_time": 8.0, "audit_state": "pending", "id": 2601337}
    ]
  }],
  "page_param": {...},
  "total": 1
}
```

**周填报任务逻辑**：

1. 计算本周周一到周五的日期（`weekDate` = 本周周一，格式 `YYYY-MM-DD`）
2. 拉取已有工时（`/api/daily/week`）
3. 对每个工作日：
   - 已有记录（`content` 中该日期存在且 `task_time > 0`）→ 跳过，不覆盖
   - 无记录 → 填入 8 小时
4. 周末（周六、周日）不填
5. 提交审批（默认 dry-run 时不提交）
6. 记录结果

**项目选择规则**（用户确认）：
- 默认取项目列表第一个
- 用户在下拉中选择后，持久保存该选择
- 若已选项目从列表中消失，回退到默认第一个
- 项目列表缓存到 `feature_data`，界面提供「强制刷新」按钮

**请假处理**：不主动处理，交由 HEM 系统按 OA 请假单自动抵扣。

### 安全

| 项 | 做法 |
|---|---|
| 系统密码 | PBKDF2-SHA256 哈希（Python 标准库 `hashlib.pbkdf2_hmac`），加随机 salt |
| 功能凭据 | Fernet 加密存 `feature_credentials.payload`，密钥 `data/secret.key`（600 权限） |
| 会话 | 随机 32 字节 token，HttpOnly cookie，30 天滑动续期，多设备并存，上限 10 个 |
| 自助注册 | 默认开放，环境变量 `ALLOW_REGISTER=0` 可关闭 |
| 登录失败 | 延迟 1 秒响应，防暴力猜解 |

### 错误处理

| 场景 | 行为 |
|---|---|
| 交互型凭据失效 | 该功能置为 `expired`，任务跳过，界面红色标记 |
| 密码型凭据失效 | 先 `relogin()`，失败才置 `expired` |
| 任务执行异常 | 记 `job_runs(ok=0)`；连续 3 次失败 → 界面红色 |
| 项目列表为空 | 工时任务跳过并记录错误 |

通知：本期不做消息推送，靠界面红点 + 运行日志。

### 测试

| 层次 | 内容 |
|---|---|
| 单元 | 密码哈希、`preference.py` 打分、日期计算（本周周一~周五） |
| 契约 | `FakeFeature` 验证 runner 的状态流转（未配置/有效/失效/异常） |
| 集成 | 美餐接口（手动，需验证码）；HEM 接口（可用真实账号跑 dry-run） |

### 迁移

按用户选择：**重开新库**。

1. 新建 `data/app.db`
2. 旧 `data/meican.db` 与 JSON 备份原样保留（可回退）
3. 注册系统账号（用户名 + 密码）
4. 在美餐功能页重新输入手机号 + 验证码
5. 在工时功能页输入 HEM 用户名 + 密码
6. 旧餐厅名缓存（46 条）不迁移，首次跑推荐会显示「未知餐厅」，多次运行自动补回

### 明确不做（YAGNI）

- 角色 / 权限系统
- 多组织 / 多租户
- 插件热加载（改代码需重启）
- 通用浏览器自动化框架
- 消息推送（邮件 / webhook）
- 密码找回（内网，忘了找管理员改库）
- 工时按天自定义小时数（统一 8 小时）
- 请假判断（HEM 自行处理）

## 风险

| 风险 | 说明 |
|---|---|
| HEM 接口无文档 | 参数结构靠前端代码与实测反推，若后端变更需重新调研 |
| 提交不可逆 | HEM 提示「提交后周报内容将不可修改，且不支持重复提交」。因此 dry-run 默认开启 |
| 美餐验证码限流 | 同号当天发码超过约 4 次会静默失败（接口返回 OK 但短信不发） |
| 工时提交时机 | 周五 20:00 执行时，若当周仍有未审批的请假单，HEM 可能按当时状态计算 |

## 交付顺序

1. 框架层（core/）：auth、db、feature、registry、scheduler、runner + 测试
2. 美餐插件迁移 + 回归验证
3. 工时插件（HEM client + jobs）+ dry-run 验证
4. 界面（登录页、首页、通用功能面板）
5. 文档与启动脚本
