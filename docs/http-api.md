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

## ⚠️ 先解决网络：编排器多半在容器里

**这是接入时最常翻车的一步。** Dify 和 n8n 通常跑在 Docker 里，容器里的
`127.0.0.1` 指的是**容器自己**，不是你的机器。默认的 `xiaoyu serve` 只绑回环，
容器**连不上**。

所以给编排器用时要这样起：

```bash
xiaoyu serve --host 0.0.0.0 --token "$(openssl rand -hex 16)" --workspace ~/code/myrepo
```

（绑非回环地址时 token 是强制的，不给直接拒绝启动。）

然后编排器那边填的地址是：

| 编排器所在 | 填什么 |
|---|---|
| Docker Desktop（macOS / Windows） | `http://host.docker.internal:8420` |
| Linux 上的 Docker | `http://172.17.0.1:8420`（docker0 网关，用 `ip addr show docker0` 确认） |
| 与 xiaoyu 同一台裸机 | `http://127.0.0.1:8420` |
| 另一台机器 | `http://<那台机器的 IP>:8420`，且**前面放反代 + TLS** |

## 接 Dify

Dify 的 Code 节点跑在沙箱里起不了子进程，所以**必须走 HTTP**。

**1. 导出 schema**（`--public-url` 必须填 Dify 真正能访问到的地址）：

```bash
xiaoyu serve --print-openapi --public-url http://host.docker.internal:8420 > xiaoyu.json
```

**2. 导入**：Dify →「工具」→「自定义」→「创建自定义工具」→ 粘贴 `xiaoyu.json` →
鉴权方式选 **API Key**，Header 名 `Authorization`，值 `Bearer <你的 token>`。

导进去会得到 14 个工具，名字就是 `operationId`：

```
create_session  prompt  prompt_async  get_status  get_events
respond_permission  list_permissions  abort  steer  close_session  …
```

**3. 工作流形态**：

```
[开始] → [工具 create_session]        拿 session_id
       → [工具 prompt_async]          提交，立刻返回
       → [循环 ↺]
            [工具 get_status]
            [条件] status == "running" → 继续循环
                   detail == "waiting_for_approval" → 走人工审批分支 → respond_permission
       → [工具 get_events]            取正文与工具轨迹
       → [结束]
```

Agent 节点里直接挂这些工具也行——但**建议只给它 `prompt` / `get_status` / `get_events`**，
别把 `respond_permission` 给模型，否则它会自己给自己放行，审批闸门就白设了。

## 接 n8n

n8n 自托管的话有两条路，**先想清楚要哪条**：

**A. Execute Command 节点（不用起服务）** —— 任务短、只要个结果时最省事：

```bash
xiaoyu "修复 src/foo.py 的类型错误并跑测试" --workspace /data/repo --mode auto --output-format json
```

前提：n8n 容器里得装上 xiaoyu 和 provider key。

**B. HTTP Request 节点（走 serve）** —— 要看中间进度、要跨机、要多个工作流共享
同一个会话时用：

```
[HTTP Request] POST http://host.docker.internal:8420/session
               Header: Authorization: Bearer <token>
               Body:   {"workspace": "myrepo", "mode": "auto"}
               → 取 {{ $json.session_id }}

[HTTP Request] POST .../session/{{ $json.session_id }}/prompt_async
               Body: {"text": "{{ $json.task }}"}

[Wait 10s] → [HTTP Request] GET .../session/{{ ... }}/status
           → [IF] {{ $json.status }} == "running" → 回到 Wait
                  {{ $json.detail }} == "waiting_for_approval" → 审批分支

[HTTP Request] GET .../session/{{ ... }}/events?from=1&limit=500
```

轮询那一环用 `?wait=20` 的 long-poll 端点更省资源，比固定 Wait 节点响应更快：

```
GET .../events?from={{ $json.next }}&wait=20
```

n8n 的 HTTP Request 节点默认超时是 300s，够 long-poll 的 60s 上限用。

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
