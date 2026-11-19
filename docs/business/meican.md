# 美餐领域知识

从旧版单功能 README 迁出的实测结论，重构后仍然适用。

## 长期免登录：remember cookie（2026-11-17 更正）

**早期结论「美餐没有长期凭据」是错的。** 当时只测了 refreshToken，
用错接口（v3/v4 GraphQL）去刷，自然一直失败。翻阅美餐前端 bundle
（`demeter-fe/aa22780.js`）后找到真实机制：

| 凭据 | 行为 |
|---|---|
| accessToken | TTL 是「**最后一次使用后** 1 小时」，不是签发后 1 小时 |
| refreshToken | 走的是 **OAuth `/oauth/token`**，不是 GraphQL；声明 1 小时 |
| **remember cookie** | **实测服务端下发 `expires` 约 2 个月**（2026-11-17 登录 → 2027-01-16 到期）。前端 `persistCookieAgain` 会把浏览器侧 maxAge 重写为 10 年，但服务端真实有效期以 Set-Cookie 的 expires 为准 |
| ticket | 一次性 |

### ⚠️ 两套验证码体系互不通用（最大的坑）

美餐有**两条独立的登录通道**，各自发各自的验证码，**互相不认**：

| 通道 | 发码接口 | 登录接口 | 验证码位数 | 能否拿 remember |
|---|---|---|---|---|
| v4 GraphQL | `GetPhoneVerificationCode`（gateway.meican.com） | `loginByAuthWay` | 6 位 | ❌ |
| **v3 REST** | `accounts/sendVerifyCode`（字段名 **`mobileNumber`**） | `oauth/token` | **4 位** | ✅ |

**要拿长期 remember cookie，发码和登录必须都走 v3。** 用 GraphQL 发的 6 位码
去调 v3 的 `oauth/token` 会一直报「验证码无效」——这不是码错了，是通道不匹配。
排查时曾因此浪费多次短信。平台现在发码（`send_sms_code_v3`）与登录
（`oauth_token_with_code`）都走 v3，配套一致。

### 三个关键接口（v3，均带 client_id / client_secret query）

基础地址 `https://www.meican.com/forward/api/v2.1`。

**1. 发码**

```
POST /accounts/sendVerifyCode
     mobileNumber=<手机号>          ← 注意是 mobileNumber，不是 mobile

→ {"resultCode":"OK","resultDescription":"SUCCESS"}
→ 限流时 {"resultCode":"FAIL","data":{"left":<剩余秒数>}}
```

**2. 登录拿 remember**

```
POST /oauth/token?remember=true
     grant_type=password
     meican_credential_type=mobile_verify_code
     mobile=<手机号>&verifyCode=<4位码>&x=true

→ 响应体：access_token / refresh_token / token_type / expires_in
→ 响应头：Set-Cookie: remember=<49字符>（expires 约 2 个月）
```

前端对应 `getTokenAndCookie()`。`meican_credential_type` 合法值（服务端报错泄漏）：
`password / mobile_temp_password / mobile_temp_password_with_signup /
card_inside_number / new_user / user_hashid / mobile_verify_code / cookie / simple_auth`。

**3. 用 remember 换新 token（无需验证码）**

```
POST /oauth/token
     grant_type=password
     meican_credential_type=cookie
     Cookie: remember=<49字符>

→ 新的 access_token / refresh_token（expires_in=3600）
```

前端对应 `getTokenUsingCookie()`。**已实测打通**：换出的新 token 能正常调
`accounts/show` 与 `calendarItems/list`。remember 失效时返回
`invalid_grant: Refresh token is expired or not valid`。

### 对平台的影响

- **登录双通道**：`MeicanFeature.login` 首选 v3 `oauth_token_with_code`（能拿
  remember）；失败回退 GraphQL `login_with_sms`（能登录但无长期凭据）。
  发码 `send_action` 走 v3 `send_sms_code_v3`，与首选登录通道配套。
- `MeicanFeature.can_relogin = True`：交互型凭据在**有长期凭据时**可以自动续期，
  框架据此允许 `runner` 调用 `relogin`，不再直接判定失效。
- `keepalive` 任务：探测失败时先尝试用 remember 续期（`relogin`），成功则继续，
  只有 remember 也失效才提示用户重新短信登录。**已实测**：把 accessToken 改成
  无效值后跑保活，能自动用 remember 换出新 token，消息为
  「token 已过期，已用长期凭据自动续期」。
- 登录返回 `long_term` 布尔，前端可据此提示用户是否获得了免登录能力。

### 保活仍然需要

accessToken 只活 1 小时，所以即便有 remember，也必须周期性任务换新 token，
否则到订餐时 token 已经死了。保活间隔 30 分钟（`always_on`，不受功能开关约束）。
remember 本身约 2 个月有效，到期前保活会一直用它续期；到期后才需要重新短信登录。

## 登录

- v3 通道验证码是 **4 位**；GraphQL 通道是 6 位。两者不通用（见上）。
- 发码有静默限流：接口返回 OK 但短信不发，同号当天超过 4 次容易触发；
  v3 限流会明确返回 `data.left` 剩余秒数。
- 验证码一次性使用，`MeicanFeature.login` 成功后会从 payload 里 `pop("code")`，不落库。
- 一个手机号下可能有多个账号，GraphQL 回退路径优先选 `accountVersion == "AccountThreeDotZero"`。
- `accounts/show` 对 v3 账号返回空字典（不报错），**不能**用它判断 token 存活；
  保活探测靠 HTTP 401，业务验证用 `calendarItems/list`。

## 订餐窗口

- 订餐窗口 09:00–16:00，定时任务时间必须落在窗口内。
- 平台默认工作日 10:05（带 180 秒抖动）。
- **定时任务订的是当天，不是次日。** 美餐在当天 09:00 才开放当天餐次；
  次日餐次此时状态为 `NOT_YET`，餐厅列表里每家 `open` 都是 `false`。
  若订次日，任务会天天失败并报「餐厅均订满」，而当天其实全部可订
  （2026-11-19 踩过这个坑）。

## 餐次状态 vs 餐厅状态

两层状态必须分开判断，否则会得出与事实相反的结论：

| 层级 | 字段 | 取值 | 含义 |
|---|---|---|---|
| 餐次 | `calendarItem.status` | `AVAILABLE` / `ORDER` | 可下单 |
| | | `NOT_YET` | 还没到预订时间 |
| | | `CLOSED` | 该餐次已关闭 |
| | | `ALL_RESTAURANT_EXCEED` | 全部餐厅售罄 |
| 餐厅 | `open` | `false` | 餐厅不在供餐时段 |
| | `availableDishCount` | `0` | 已超过点餐限额 |
| | `restaurantStatus` | `1` / `2` | 总状态，仅在前两字段缺失时兜底 |

**先判餐次，再判餐厅。** 餐次 `NOT_YET` 时餐厅列表的 `open` 全为 `false`，
若跳过餐次判断直接看餐厅，就会把「还没开放」误报成「餐厅打烊」。

### `open` 有两种含义（2026-11-19 踩坑）

`restaurants/list` 返回的 `open` 不能单独当「餐厅是否打烊」：

| 场景 | open | 份数 | 真实含义 |
|---|---|---|---|
| 未订餐、窗口内 | `true` | >0 | 正常可订 |
| 未订餐、窗口外 | `false` | >0 | 餐厅已关闭点餐 |
| **已订餐** | **`false`** | **>0** | **我下过单，入口关闭**，餐厅仍营业 |

前端 `aa22780.js` 明确写着：
```js
return "AVAILABLE" !== o.status && (o.isAvailable = !1), o
```
餐次状态一旦变成 `ORDER`，整列表 `isAvailable` 被置 false，下发的 `open`
也就全 false。实测 12:30 已订餐时 9 家 `open` 全 false，但份数仍有 37~86、
`restaurantStatus` 仍为 0。

**所以判断顺序是**：
1. 份数为 0 → `sold_out`（与订没订无关）
2. 不在订餐窗口内 → `closed`
3. `restaurantStatus` 明示关闭/超限 → 对应原因（已订餐也要认，换到关闭的店没意义）
4. `open=false` 且未订餐 → `closed`；**已订餐则放行**（可更换）

订餐窗口取餐次的 `openingTime.openTime/closeTime`（本项目 09:00–16:00），
**不能用餐厅 `open` 判断时间**——已订餐时它恒为 false。

### 共享缓存不能存用户态

`available` 取决于「我自己有没有订餐」，而菜单缓存是全企业共享的。
因此缓存只存客观字段（份数、`open`、`restaurantStatus`），读取时由
`apply_availability(raw, item)` 结合当前用户的订餐状态与窗口时间现算。
采缓存那个用户若已订餐，其 `open` 全 false 且不可信，写进缓存时记为
`None`（未知）而非 `False`，避免污染其他用户。缓存键带 `v2` 版本号，
老结构条目自然失效重拉。

前端对应 `82b73ae.js` 的 `switch(t.status)` 与 `generateRestaurantSections`；
餐次文案见 `afd1e10.js` 的 `order_status_not_yet`「未开放」/
`order_status_close`「已关闭」/ `order_status_sold_out`「已售罄」。

`change_order` 必须先过餐次判断再退旧单——否则退了旧单下不了新单，
当天就没饭吃了。

## 偏好算法

`app/features/meican/preference.py`，纯规则、无 AI 调用、可解释：

- **主信号**：与历史吃过的菜名做字符二元组 Jaccard 相似度，14 天半衰期
- **次信号**：食材/口味关键词频次，按总词频归一化
- **惩罚项**：3 天内吃过同款 −12 分，5 天内高度相似 −5 分
- **餐厅信号**：历史点过的店加分（但每天可选餐厅几乎完全不同，作用有限）

## 已知限制

- 历史订单只存餐厅 id 不存名字，且餐厅列表仅当天可查。餐厅名靠 `restaurant_names`
  这个功能 KV 逐步积累，新用户首次运行时可能显示「未知餐厅」。
- 历史订单缓存 12 小时，存在功能 KV 里，按用户隔离。

## 旧数据

重构不迁移旧库。旧数据归档在 `data/legacy/`（`meican.db` + 4 个 JSON），
新库是 `data/platform.db`，需要重新注册账号并重新短信登录。