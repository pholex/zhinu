# HTTP API（`xiaoyu serve`）

把小羽接给**工作流编排器**——n8n、Dify、或者你自己的调度服务。

> 消费方是 LangChain / LangGraph 这类 **MCP client**？serve
> 同时在 `/mcp` 挂着 MCP server 面（同一会话层的第二张脸），见
> [docs/mcp-server.md](mcp-server.md)。

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

两个端点都可带 `output_schema`（JSON Schema）：要求这一轮以符合它的**对象**收尾，
而不是一段正文。模型经 `structured_output` 工具交回，结果在 `result.output`
（`prompt` 直接返回；`prompt_async` 在 `GET /status` 的 `last_result.output`）。
模型没按要求给则为 `null`，编排侧据此判失败。校验只做 type / required /
enum / items 这一层，复杂关键字自己再校。

```bash
curl -X POST :8420/session/$SID/prompt -d '{"text":"评估这个 PR 能不能合",
  "output_schema":{"type":"object","properties":{"mergeable":{"type":"boolean"},
  "reasons":{"type":"array","items":{"type":"string"}}},"required":["mergeable"]}}'
#  → {..., "result": {"text": "...", "output": {"mergeable": false, "reasons": [...]}, ...}}
```

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
| `idle` | `budget_reached` | **预算耗尽**（`budget_reason` 有原因），再提交 `409`，去 `/budget` 调高或撤掉 |
| `idle` | `recovered` | serve 重启后从清单接回来的会话，还没跑过新的一轮 |
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
**注意顺序**：它必须在**提交 prompt 之后**开——会话空闲且没有新事件时它立刻关流，
先开会拿到一个空流（200 但零事件）。

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

## 控制面：agent 对象（版本化配置）

会话的配置（模型、模式、追加的 system prompt、审批档、沙箱、预算、定价）可以先打包成一个
**持久化、带版本的 agent 对象**，会话创建时引用它：

```bash
# 建一次
curl -X POST :8420/agent -d '{"name":"写手","config":{"model":"claude-opus-5","mode":"auto",
  "append_system_prompt":"你是技术文档写手","budget":{"tokens":300000}}}'
#  → {"agent_id":"agent-3f9c…","version":1,...}

# 每次运行只引用
curl -X POST :8420/session -d '{"agent":"agent-3f9c…"}'                 # 最新版本
curl -X POST :8420/session -d '{"agent":{"id":"agent-3f9c…","version":1}}'  # 钉版本

# 改配置 = 出新版本；已在跑的会话仍钉着创建时那一版，不受影响
curl -X POST :8420/agent/agent-3f9c… -d '{"config":{"mode":"plan"}}'  # → version 2
```

规则：

- **引用了 agent 的会话不能再单独给 `model` / `mode`**（`400`）——钉版本的意义是"这个
  会话的配置可完整复现"，旁路覆盖会让版本号失去含义。不给 `agent` 就是老路：跟随
  服务端启动参数，`model` / `mode` 可临时覆盖。
- **agent 只能比服务端启动参数更严**：服务端 `--approval ask` 时 agent 不能设
  `allow_all`；不能把沙箱关掉、不能放开沙箱网络。放宽是 `400`，不是静默钳位。
- `append_system_prompt` 是**叠加**：服务端那份（宿主级身份/纪律）在前，agent 那份
  （用法级人格）在后。
- `budget`（既有的硬闸）现在**兼作模型知情的软预算**：设了 `{tokens: N}` 后模型会收到
  倒计时并在硬闸之前优雅收尾（`run.completed` 的 `stopped=budget`），到线后仍需
  `POST /session/{id}/budget` 调高才能再跑。美元预算只硬闸、不折算成模型可见的 token 节奏。
- `effort`：推理深度（`low / medium / high / xhigh / max`，OpenAI 线另有 `none / minimal`），
  覆盖服务端 `--effort`；不给则随服务端，服务端也没给就不传、随上游默认。
- `mcp_servers`：宿主应用**自有的 MCP server**，形状同 `.mcp.json` 的 `mcpServers`
  （`{"名": {"command","args","env"} | {"url","headers"}}`）。随版本钉定；会话创建时
  按那一版拉起一个**会话私有**的 manager（与工作区配置发现合并、同名以 agent 为准），
  关会话即收掉。这是"把业务动作交给 agent"的正道：仪表盘/工单系统把自己的 API 包成
  MCP server 挂在 agent 上，审批仍走 `/permissions`。
  - 服务端默认**不收**：启动加 `--agent-mcp http`（只收远端 Streamable HTTP，不在本机
    起进程）或 `--agent-mcp all`（stdio 也收——那是沙箱之外的本机子进程，确认 token
    持有方可信再开）。未开放时 `400`。
  - 形状错、老式 sse、缺 command/url：`400` 带 server 名，不静默少一个。
  - 准入与 ACP 同一套：stdio 过 `mcp_guard.admission_violation`，远端明文 `http://`
    只许回环。`${VAR}` **不展开**——值是宿主算好的终值，也不给对端一条读服务端环境的路。

  ```bash
  xiaoyu serve --agent-mcp http
  curl -X POST :8420/agent -d '{"name":"运单助手","config":{"mode":"auto",
    "mcp_servers":{"shipments":{"url":"https://ops.internal/mcp",
                                "headers":{"Authorization":"Bearer …"}}}}}'
  ```
- `DELETE /agent/{id}` 是**归档**不是删除：只读、不再接受新会话、老会话照跑、历史版本
  可查（`GET /agent/{id}?versions=true`）。

## 预算（硬闸，不是提醒）

```bash
curl -X POST :8420/session -d '{"agent":"agent-…","budget":{"tokens":200000}}'
curl -X POST :8420/session/sess-…/budget -d '{"budget":{"tokens":500000}}'   # 调高
curl -X POST :8420/session/sess-…/budget -d '{"budget":null}'                # 撤掉
```

- 币种以 **token 为主**（prompt + completion 累计，含子 agent 与摘要调用）。小羽记账到
  provider/model 的 token 为止，**没有内置价格表**——跨十几家 provider 维护一张表既不准
  也没人更新。
- **美元预算是可选的**：agent 的 `pricing` 给出 `{模型: {"input": 美元/百万, "output":
  美元/百万}}`（键可以是 `provider/model` 全限定名或裸模型名），设了 `budget.usd` 才会
  按它结算。**用到没定价的模型按超支处理**（fail closed）——钱的事上"算不出来"不能
  悄悄变成"不设限"。
- 一轮里模型可能调用几十次，所以**轮中每个事件后都结一次账**，越线即 `interrupt`
  （下一个 chunk 边界收尾，半截话入历史，与 `/abort` 同一条路），轮末状态
  `idle/budget_reached`，事件流里有 `budget.reached`。之后再提交 `409`，只有经
  `/budget` 调高或撤掉才能继续。
- 会话级 `budget` 优先于 agent 的默认 `budget`；`/status` 里 `usage`（累计账本）、
  `spend`（按预算币种结算的结果）、`budget`、`budget_reason` 四个字段一起看。

## 重启恢复

agent 对象、会话清单、会话日志默认落盘在 `~/.xiaoyu/serve/<root slug>/`
（`--state-dir` 改位置，`--no-persist` 全放内存）。serve 重启后：

- 会话自动接回（`detail=recovered`），历史来自会话日志（`Agent.restore`，未配对的
  tool_call 会补"结果未知"），配置、agent 引用、预算、`turns` 随清单回来；
- **事件缓冲不落盘**——重启前的事件计入 `dropped_events`，`seq` 从上次水位**接着编号**：
  客户端手里的游标仍单调，拉到的是"中间缺一段"（协议里本来就有表达），不是"序号倒流"；
- 恢复失败的清单（工作区没挂上、provider 没配）留在盘上、stderr 打一行、跳过——不删，
  因为失败可能是暂时的。`DELETE /session/{id}` 会删清单；会话日志作为留痕保留。

文件即真相：`agents/agent-*.json`、`sessions/sess-*.json`、`logs/*.jsonl`，`cat` 即可审计。

---

## 安全边界

这个服务**会执行任意命令**，边界是硬的：

- 默认只绑 `127.0.0.1`。绑非回环地址时**必须**给 `--token`（或 `XIAOYU_SERVE_TOKEN`），
  否则拒绝启动
- `--workspace` 是 root，会话只能落在它或它的子目录里，越界 `400`
- folder trust 非交互判定（与 `--acp` 同一纪律）：没信任记录的目录不吃工作区级
  `.mcp.json` / `permissions` / `.env`，也绝不在协议通道上发问
- 要暴露到公网就自己在前面放反代 + TLS。这个服务本身不做 TLS，也不做限流
- 默认**不发 CORS 头**。浏览器里跑的客户端（浏览器扩展、自研 Web 控制台）要用
  `--cors-origin chrome-extension://<id>`（可重复）把它的 origin 加进白名单——这只是
  "浏览器肯不肯把响应交给页面脚本"的门，token 仍照常校验。Chrome 访问回环地址的
  Private Network Access 预检也一并应答

---

## 端点速查

| 方法 | 路径 | |
|---|---|---|
| GET | `/health` | 存活探针（不需要 token） |
| GET | `/diagnostics` | 进程自诊断：RSS / 线程 / fd + 在册计量（会话、在途请求、MCP 连接、后台任务）。有 token 门——它暴露负载形态 |
| POST · GET | `/agent` | 新建 agent 对象 · 列出 |
| GET · POST · DELETE | `/agent/{id}` | 详情（`?versions=true`）· 更新→新版本 · 归档 |
| POST | `/session` | 新建会话（`workspace` / `model` / `mode`，或 `agent` 引用 + `budget`） |
| GET | `/session` | 列出会话 |
| GET · DELETE | `/session/{id}` | 详情 · 关闭 |
| POST | `/session/{id}/prompt` | 跑一轮，等结果 |
| POST | `/session/{id}/prompt_async` | 跑一轮，立刻返回 |
| GET | `/session/{id}/status` | 状态机 |
| GET | `/session/{id}/events` | 拉事件（游标 + long-poll） |
| GET | `/session/{id}/events/stream` | 同一份事件流的 SSE |
| GET · POST | `/session/{id}/permissions` | 挂起的审批 · 回决定 |
| POST | `/session/{id}/budget` | 改/撤预算（`budget_reached` 后的出路） |
| POST | `/session/{id}/abort` | 打断这一轮（不杀会话） |
| POST | `/session/{id}/steer` | 向进行中的一轮插话（会话空闲时 `409`） |
| POST | `/mcp` | 同一会话层的 MCP server 面（[docs/mcp-server.md](mcp-server.md)，`--no-mcp` 可关） |

## 并发与限额

- **一个会话同一时刻只跑一轮**（同步内核的既有约束），忙时再提交回 `409`——
  静默排队会让编排器以为第二次提交立刻生效了。
- **同时能跑的会话数由 `--max-sessions` 定（默认 32）**，它就是工作线程池大小。
  ⚠️ **等审批期间线程也被占着**，所以它同时是"能同时挂起等审批的会话数"上限。
  超出之后新提交的轮次会排队等线程——`/status` 仍报 `running`，但实际没开始跑。
  会话数多、审批又慢的场景把它调大。
- `steer` 只在会话 `busy` 时有意义：空闲期入队的插话会在下一轮开头被 drain 掉，
  所以空闲时直接回 `409` 而不是假装成功。
- **会话不会自动回收**：`DELETE /session/{id}` 是唯一的释放口。长跑的服务要自己
  在编排流程末尾调它，否则会话（含完整消息历史与事件缓冲）会一直占着内存——
  而且默认落盘，重启也会被接回来。
