# 办公自动化平台

多人使用的办公自动化工具，功能以插件形式接入。

## 启动

```bash
./run.sh                      # 仅本机
./.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8765   # 内网共享
```

浏览器打开 http://127.0.0.1:8765 ，注册账号后登录。

首次部署先装依赖：

```bash
python3 -m venv .venv
./.venv/bin/pip install fastapi uvicorn httpx apscheduler cryptography jinja2 pytest
```

**不需要手工建库或跑迁移脚本**——启动时自动完成：

1. `data/` 不存在就创建，`platform.db` 不存在就新建
2. 逐表对账：缺表建表、缺字段自动 `ALTER TABLE ADD COLUMN`
3. 库里有代码不认识的字段或类型不符 → 只打印告警，不删不改（可能是代码回滚，删列会丢数据）
4. 空库按环境变量引导创建管理员（见下）

启动日志会把做了什么说清楚，健康的库只有一行：

```
[db] 表结构校验通过，无需变更（schema v1）
```

### 无人值守部署

默认仍可在登录页自助注册。若要用 `ALLOW_REGISTER=0` 关闭注册，必须在**空库的首次启动**时给出管理员凭据，否则没人进得去（日志会明确告警）：

```bash
ADMIN_USERNAME=admin ADMIN_PASSWORD='你的密码' ALLOW_REGISTER=0 ./run.sh
```

已有用户的库不会被这条覆盖，重复启动是幂等的。凭据建议放在 `~/.claude/secrets/` 或启动脚本的环境变量里，不要写进仓库。

## 已有功能

| 功能 | 凭据类型 | 默认定时 | 说明 |
|---|---|---|---|
| 美餐自动订餐 | 交互型（短信验证码） | 工作日 10:05 订餐；每 6 小时保活 | 学习历史口味自动挑菜下单 |
| HEM 工时填报 | 密码型（HEM 账号） | 周五 17:30 提交周报 | 检查本周工时是否填齐，默认演练模式 |

两个功能都默认 **演练模式（dry-run）**，只演算不真正提交。确认结果合理后再关掉。

## 架构

```
app/core/        框架：认证、数据库、插件契约、注册表、执行器、调度
app/features/    插件：每个功能一个目录
app/templates/   通用页面（按插件声明渲染）
```

`core/` 不依赖 `features/`。新增功能只需：

1. 在 `app/features/<name>/` 下实现 `Feature` 抽象基类
2. 在 `app/main.py` 的 `_register_features()` 里注册一行

前端无需改动——凭据字段、任务、可选项都由插件声明驱动渲染。

## 凭据类型

- **PASSWORD**：可用已存凭据自动重登（如工时）
- **INTERACTIVE**：必须人工介入（如美餐短信验证码）

框架按类型自动决定登录态失效后的策略：密码型先尝试重登，交互型直接置失效等人工重新登录。

## 数据隔离

所有功能数据按 `(user_id, feature_key)` 双维度隔离：凭据、配置、KV、运行记录互不可见。
主库 `data/platform.db`（SQLite WAL）。

## 安全

- 系统密码：PBKDF2-HMAC-SHA256 加盐哈希（26 万次迭代）
- 功能凭据：Fernet 加密存库，密钥 `data/secret.key`（600）
- 会话：HttpOnly cookie `mc_session`，SameSite=lax，30 天，多设备并存
- 自助注册默认开放，`ALLOW_REGISTER=0` 可关闭
- 当前为 HTTP，仅限可信内网；上公网须加 HTTPS
- **备份数据库时别把 `secret.key` 一起泄露**

## 测试

```bash
./.venv/bin/python -m pytest tests/ -v
```

测试全部使用临时库与临时密钥，不碰 `data/` 下的真实数据，也不打真实网络。

## 事件循环约束

`core/runner.execute` 与 `runner.run_login` 内部用 `asyncio.run`，**只能从同步上下文调用**：

- FastAPI 路由必须用 `def`（不是 `async def`）
- APScheduler 的任务经 `asyncio.to_thread` 包一层

违反会抛 `RuntimeError: asyncio.run() cannot be called from a running event loop`。

## 业务知识

美餐 token 保活规律、订餐窗口、验证码位数等领域知识见 `docs/business/`。