# HTTP API（`xiaoyu serve`）

把小羽接给**工作流编排器**——n8n、Dify、或者你自己的调度服务。

这是与 `--acp`（编辑器）、`--wire`（自家外壳）并列的第三条协议面，区别只在驱动方是谁。
内核不因为它改形态：TUI/CLI 仍然是进程内直驱 `Agent`，serve 只是外挂一层适配。

```bash
pip install "xiaoyu-agent[serve]"
xiaoyu serve --workspace ~/code/myrepo
#  → http://127.0.0.1:8420   文档 /docs   schema /openapi.json
```

---

## 为什么有两个 prompt 端点

coding agent 一轮动辄几分钟，而编排器的 HTTP 节点普遍有分钟级超时。

| 端点 | 行为 | 什么时候用 |
|---|---|---|
| `POST /session/{id}/prompt` | 跑完才返回，带 `result` | 几十秒能完的活 |
| `POST /session/{id}/prompt_async` | 立刻 `202`，进度自己取 | **长任务默认走这个** |

异步那条的配套是状态轮询和事件游标，见下面两节。

---

## 状态机（编排的主要抓手）

`GET /session/{id}/status`：

```json
{ "status": "running", "detail": "waiting_for_approval", "pending_approvals": [...] }
```

| status | detail | 含义 |
|---|---|---|
| `running` | `working` | 正在干活，继续等 |
| `running` | `waiting_for_approval` | **卡在等人放行**，去回 `/permissions` |
| `idle` | `finished` | 上一轮正常收尾，`last_result` 有正文与 usage |
| `idle` | `interrupted` | 上一轮被 `/abort` 收掉 |
| `error` | `failed` | 上一轮抛异常，`error` 字段有原文 |

`waiting_for_approval` 单独占一格是这一层最要紧的设计：编排器只看"还在跑"的话，
分不清**模型在想**和**它在等人点确认**——前者该继续等，后者该去放行或判超时。
把这个区分藏起来，编排侧的超时逻辑就没法写对。

---

## 拿进度：游标 or SSE

事件是**同一份缓冲的两个形态**，`seq` 同源。

**轮询（推荐给 n8n / Dify）**——它们对 SSE 支持都不好：

```
GET /session/{id}/events?from=1&limit=200&wait=20
```

- `from` 是游标，返回体里的 `next` 就是下次的 `from`，不重不漏
- `wait>0` 是 long-poll：没有新事件就挂到 `wait` 秒（上限 60）再空手返回，
  不必用固定间隔傻轮询
- `first_seq` / `dropped_events` 如实报告环形缓冲丢了多少——不做静默截断

**SSE（给编辑器 / 看板 / 自研客户端）**：

```
GET /session/{id}/events/stream?from=1            # 常驻推送
GET /session/{id}/events/stream?from=1&follow=false   # 推完这一轮就关流
```

`follow=false` 是给"提交一轮 → 跟完 → 收工"的一次性消费方用的：不必自己想办法中断连接。

事件的 `kind` 与 TUI / `--output-format stream-json` 完全同一套词汇
（`text.delta`、`tool.pending`、`tool.completed`、`run.completed`…），
换传输不换契约。超长字段（工具输出）截断处会带 `*_truncated` 与 `*_chars`。

---

## 审批回路

`ask` 档（默认）下，需要人工放行的工具调用会**挂起整一轮**，等 HTTP 侧回决定：

```bash
# 1. 状态停在 waiting_for_approval，pending_approvals 里有 request_id
curl -s $BASE/session/$SID/permissions -H "$AUTH"

# 2. 放行
curl -X POST $BASE/session/$SID/permissions -H "$AUTH" -H 'content-type: application/json' \
  -d '{"request_id":"...","decision":"allow"}'

# 3. 或者拒绝——reason 会原文回灌给模型，等于改指令
  -d '{"request_id":"...","decision":"deny","reason":"别动这个文件"}'
```

`updated_args` 非空时整体替换本次调用参数再执行——"批准但改写"的通道
（典型用法：把命令包一层再放行）。

**超时按拒绝处理**（`--approval-timeout`，默认 300 秒）。这是 fail closed：
安全闸门宁可多拦一次，不能在没人应答时静默放行。

无人值守又不想接这条回路，就 `--approval allow_all`（等价 `--yolo`）——
但那样就没有闸门了，只在你信得过工作区和任务的场景用。

---

## 接 n8n

n8n 自托管的话其实两条路都行：

- **HTTP Request 节点** → 打 `prompt_async`，再用 Wait + HTTP Request 轮询 `status`
- **Execute Command 节点** → 直接 `xiaoyu "..." --output-format json`，不用起服务

任务短、只要个结果，Execute Command 更省事；要看中间进度、要跨机、要多个工作流共享
同一个会话，才值得起 serve。

## 接 Dify

Dify 的 Code 节点跑在沙箱里起不了子进程，所以**必须走 HTTP**。

```bash
xiaoyu serve --print-openapi > xiaoyu.json
```

把这份贴进 Dify 的**自定义工具**（导入 OpenAPI schema），鉴权选 API Key /
Bearer，填 `--token` 的值。schema 是从代码生成的，改了端点重导一次就同步，
不会像手写的那样漂。

工作流形态建议：

```
[开始] → [HTTP: POST /session]        拿 session_id
       → [HTTP: POST .../prompt_async] 提交
       → [循环: HTTP GET .../status]   直到 status != running
       → [条件: detail == waiting_for_approval] → 走人工审批分支
       → [HTTP: GET .../events]        取正文与工具轨迹
```

---

## 安全边界

这个服务**会执行任意命令**，边界是硬的：

- 默认只绑 `127.0.0.1`。绑非回环地址时**必须**给 `--token`（或 `XIAOYU_SERVE_TOKEN`），
  否则拒绝启动
- `--workspace` 是 root，会话只能落在它或它的子目录里，越界 `400`
- folder trust 非交互判定（与 `--acp` 同一纪律）：没信任记录的目录不吃工作区级
  `.mcp.json` / `permissions` / `.env`，也绝不在协议通道上发问
- 要暴露到公网就自己在前面放反代 + TLS。这个服务本身不做 TLS，也不做限流

---

## 端点速查

| 方法 | 路径 | |
|---|---|---|
| GET | `/health` | 存活探针（不需要 token） |
| POST | `/session` | 新建会话（可指定 `workspace` / `model` / `mode`） |
| GET | `/session` | 列出会话 |
| GET · DELETE | `/session/{id}` | 详情 · 关闭 |
| POST | `/session/{id}/prompt` | 跑一轮，等结果 |
| POST | `/session/{id}/prompt_async` | 跑一轮，立刻返回 |
| GET | `/session/{id}/status` | 状态机 |
| GET | `/session/{id}/events` | 拉事件（游标 + long-poll） |
| GET | `/session/{id}/events/stream` | 同一份事件流的 SSE |
| GET · POST | `/session/{id}/permissions` | 挂起的审批 · 回决定 |
| POST | `/session/{id}/abort` | 打断这一轮（不杀会话） |
| POST | `/session/{id}/steer` | 向进行中的一轮插话 |

一个会话同一时刻只跑一轮（同步内核的既有约束），忙时再提交回 `409`——
静默排队会让编排器以为第二次提交立刻生效了。
