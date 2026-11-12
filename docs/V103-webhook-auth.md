# V103 设计与部署说明

版本：v102 → v103

本说明聚焦 v103 的功能设计与实现，不依赖任何具体部署环境。

---

## 1. 投稿状态通知（Webhook）

### 1.1 问题

v98 起队列已经能自动等额度、自动解封、自动续投，主打「无需人工值守」。但代码里 grep 不到任何对外推送：**封禁解除、投稿失败、限流卡住时用户收不到任何提醒**，还是得盯着浏览器。这是「无人值守」闭环里缺的最后一环。

### 1.2 切入点

按需求在队列执行器的关键节点插入 `_notify()`，共四处：

| 位置 | 事件 | 触发内容 |
| --- | --- | --- |
| `worker._summary()` | `task` | 稿件终态：成功 / 失败 / 被拦下 / 卡死 / 已取消，带《标题》 |
| `worker.pause_for_quota()` | `quota` | 24h 额度满或 n+1 触发封禁、队列自动暂停、预计复查时刻 |
| `worker._maybe_resume_quota()` | `resume` | 封禁解除 / 额度归零，队列自动开投（不用人工点「继续队列」） |
| `worker._rate_limit_backoff()` | `rate` | 封面 -702 或上传 406/429 被限流、连续次数、是否需人工核对 |

`_summary()` 增加 `push=True` 形参；`_rate_limit_backoff()` 改用 `_summary(..., push=False)` 后单独发 `rate` 事件，避免同一条消息推两遍。

### 1.3 模块分层

- `app/notify.py`（新增，纯函数 + 标准库 `urllib`）：`normalize` / `detect_kind` / `compose` / `send_sync` / `dispatch`。只负责「怎么发」，不写本地状态、不依赖 worker。
- `app/worker.py`：`_notify()` 是入口（读配置、决定是否起线程、去重），`_notify_send()` 在守护线程里调用 `notify.dispatch()`。
- `app/server.py`：`POST /api/notify/test` 让用户在设置里先填地址直接试发，不必先保存。

### 1.4 可靠性三条硬约束

1. **不填配置就完全不触发**：`_notify()` 第一行 `notify.normalize(store.load_config())`，`url` 为空 / `enabled=false` / 该事件关闭时直接 `return`，连线程都不创建。`dispatch()` 里还有第二道同样的闸。
2. **失败绝不阻塞主队列**：发送在 `daemon=True` 的独立线程，6 秒超时；`send_sync()` 把所有异常转成 `(False, 原因)`；`_notify()` 与 `_notify_send()` 外层再各包一层 `try/except`。失败只在总日志里按 5 分钟间隔节流记一条告警。
3. **不刷屏**：同一 `(event, title, text)` 20 秒内只推一次；`_notify_last` 超过 200 条自动清空。

整条链路外层全部 `try/except` 包住，任何通知异常都不会让 `_summary()` 之后的逻辑（写日志、更新任务状态、清理文件）中断。

### 1.5 平台兼容（HTTP POST JSON）

`kind=auto` 时按地址识别，也可手动指定：

| 平台 | 识别特征 | 请求体 |
| --- | --- | --- |
| 企业微信 / 钉钉 | `qyapi.weixin.qq.com` / `oapi.dingtalk.com` | `{"msgtype":"text","text":{"content":"…"}}` |
| 飞书 | `open.feishu.cn` / `open.larksuite.com` | `{"msg_type":"text","content":{"text":"…"}}` |
| Server酱 | `ftqq.com` | `{"title":"…","desp":"…"}` |
| Telegram | `api.telegram.org` | `{"chat_id":"…","text":"…"}` |
| 通用（自建 / n8n） | 其它 | `{"app","event","level","title","text","content","timestamp"}` |

正文统一带级别图标、标题、时间；通用 JSON 额外给结构化字段方便自动化处理。URL 只接受 http/https，`send_sync` 会拒绝 `file://` 等非法 scheme。

### 1.6 配置项

`data/config.json` 的 `notify` 段（设置面板可编辑）：

```json
"notify": {
  "enabled": true,
  "url": "",
  "kind": "auto",
  "chat_id": "",
  "timeout": 6,
  "events": {"task": true, "rate": true, "quota": true, "resume": true}
}
```

`DEFAULT_CONFIG` 里写入同名默认值；`/api/config` 白名单加入 `notify`；`_public_config()` 用 `notify.normalize()` 下发规整后的值，前端不必自己兜底。

---

## 2. Web 端访问鉴权

### 2.1 问题

所有路由零鉴权，而 README 鼓励 `--host 0.0.0.0` 局域网使用。一旦暴露，同网段任何人都能读 Cookie、投稿、改配置。

### 2.2 实现

`app/server.py` 新增 `@app.before_request _require_web_auth` + `@app.after_request _persist_auth_cookie`，读 `config.web_auth`：

```json
"web_auth": {
  "enabled": false,
  "mode": "token",
  "token": "",
  "username": "",
  "password": ""
}
```

| 模式 | 校验方式 |
| --- | --- |
| `token` | query `?token=` / `?access_token=`、`X-Auth-Token` 头、`Authorization: Bearer`、Cookie `wb_auth` 任一通过；首次通过写 Cookie（30 天、HttpOnly、SameSite=Lax），后续静态资源与轮询自动带上 |
| `basic` | 标准 HTTP Basic，`hmac.compare_digest` 常量时间比较；401 带 `WWW-Authenticate: Basic realm="bili-publisher"` |

设计取舍：

- **零影响默认**：`enabled=false`、token 为空、basic 凭据没配齐时一律直接 `return None`，绝不会因配置写一半把自己锁在门外。
- **统一拦截**：除 `/api/health`（健康检查）外所有路由，含页面、静态资源、全部 API。
- **敏感字段脱敏**：`_public_config()` 把 `token` / `password` 用 `_mask()` 回显；`/api/config` 保存时忽略含 `***` 或空的值，只改其它设置不会把真凭据覆盖成掩码。
- **首次启用的平滑过渡**：Token 模式下带 `?token=` 打开会通过 401 登录页（保留原 query，例如 `sharecode`），输入后跳转；`/api/config` 刚启用 token 时直接给响应种 Cookie，因此前端保存后紧跟的 `/api/bootstrap` 不会 401。
- **前端提示**：`api()` 遇到 401 弹出明确提示「请用 ?token=你的token 重新打开页面」。

---

## 3. 说明删除与过期文案

- 删除 `web/index.html` 设置面板中上回重新加回的整段 hint（「达到上限 +1 会触发 B站 24h 投稿封禁…」）。该说明是 v102 为兼容旧测试断言加回的，现已不需要。
- 「定时稿件过期超过 N 分钟不自动投稿」改为「**本地**定时稿件过期超过 N 分钟不自动投稿」。原因：B站定时稿在到点前就已提交给平台，实际只有「本地到点自动上传」模式会出现「程序停机导致过期未投」，文案加限定更准确。
- `worker._handle()` 里对应的 message / log / summary 三处文案同步加「本地定时稿件」。逻辑未改。

---

## 4. B站定时最小提前量：4h30m → 10 分钟

### 4.1 依据

B站定时发布实际只要求**至少提前 5 分钟**，不是旧代码注释里写的 4 小时。旧版 `SCHEDULE_MIN_AHEAD = 4*3600+600`（4 小时 30 分）导致提前 10 分钟到 4 小时的稿件被当成「太近」而本地等待，不能交给平台定时。取平台下限的一倍余量，改为 **10 分钟**。

### 4.2 改动

| 位置 | 改动 |
| --- | --- |
| `app/worker.py` 常量 | `SCHEDULE_MIN_AHEAD = 10 * 60`，注释写明平台下限 5 分钟 + 余量 |
| `worker._schedule_action()` | `if delta > 600: return "wait"` → `if delta > 0: return "wait"`。否则 10 分钟内的稿件会在创建瞬间被即时投放，而不是候到点 |
| `_handle()` / `_refresh_countdowns()` | 等待态文案改为「距发布不足 10 分钟，本地候到点投放」 |
| `submit_dtime` | 条件不变（`delta > SCHEDULE_MIN_AHEAD`），自动跟随新常量 |
| `README.md` | 功能列表与「定时发布怎么走」表格整段重写 |
| `tests/verify_fixes.py` | 「B站定时余量回到 10 分钟」断言改为 `== 10*60`；「2 小时本地等」改为「2 小时也交B站」；新增「5 分钟本地候到点」 |

### 4.3 行为对照

| 距发布时间 | v102 | v103 |
| --- | --- | --- |
| 2 小时 | 本地等待（不交给B站定时） | 立即上传并带 dtime 交给B站定时 |
| 5 小时 | 交B站定时 | 交B站定时 |
| 5 分钟 | 立即投放 | 本地候到点再即时投放 |
| > 15 天 | 失败 | 失败（不变） |
| 本地定时模式 | 一律本地等到点 | 不变 |

`next_pending()` 的「到点前 10 分钟进入候选」是另一套窗口（语义不同、数值恰好相同），保持不变，并在 `_schedule_wait_delay()` 注释里显式区分，避免后续误改。

---

## 5. 升级与回滚注意事项

- 本次**未改 config.json 既有字段**；`notify` / `web_auth` 由 `DEFAULT_CONFIG` 补默认值。因此升级重启后默认行为与旧版一致：通知不推送、鉴权不启用。若之前手工改过 config.json，升级不会覆盖，新字段缺失时自动补默认值。
- 升级前请完整备份 `data/`（含 SQLite 的 `publish.db` / `publish.db-wal` / `publish.db-shm`，必须整目录复制）。
- 回滚：停服务 → 用备份覆盖 `app/`、`web/`、`tests/` → 重启。注意**不要**用旧备份覆盖 `data/`，除非确实要回退数据。

---

## 6. 变更文件清单

| 文件 | 说明 |
| --- | --- |
| `app/notify.py` | 新增：Webhook 底层（识别 / 组包 / POST / dispatch） |
| `app/worker.py` | 新增 `_notify()` / `_notify_send()`、四处调用点、`_summary(push=)`；定时常量与近端等待判断；过期文案 |
| `app/server.py` | 版本号 v103、鉴权中间件、配置脱敏与白名单、`/api/notify/test`、保存后种 Cookie |
| `app/store.py` | `DEFAULT_CONFIG` 增加 `notify` / `web_auth` |
| `web/index.html` | 静态资源 v103、删说明、过期文案、通知与鉴权两张设置卡片 |
| `web/app.js` | 设置读写、联动显示、测试通知、401 提示 |
| `README.md` / `使用说明.txt` | 定时规则与新增功能文档 |
| `tests/verify_fixes.py` / `verify_ui_v96.py` | 定时断言与版本号同步 |
| `tests/verify_v103.py` / `verify_ui_v103.py` | 新增：71 + 37 项验证 |
