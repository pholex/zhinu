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

## 选择部署环境

普通主机使用 `--environment host`；服务本身已跑在专用隔离容器或虚拟机内时，
用一项参数省去内部命令沙箱与交互审批：

```bash
xiaoyu serve --workspace /workspace --environment sandbox
```

| 参数 | 内部命令沙箱 | 审批默认值 |
|---|---|---|
| `--environment host` | 开启 | `ask`：需要时挂起等 HTTP 回决定 |
| `--environment sandbox` | 关闭，依赖部署者提供的外部隔离 | `allow_all`：自动批准所有审批 |

`sandbox` 是对**已有隔离环境**的声明，不会创建容器或虚拟机，也不做自动检测。
它等效于原先的 `XIAOYU_SANDBOX=0 xiaoyu serve --approval allow_all`。
自动批准覆盖浏览器、MCP 等工具的审批，也覆盖显式 `ask` 规则触发的审批；
这些工具可能作用于容器外的服务或浏览器，外部隔离并不限制它们的业务权限。
需要保留审批回路时可覆盖预设：

```bash
xiaoyu serve --workspace /workspace --environment sandbox --approval ask
```

显式 `--environment` 优先于 `XIAOYU_SANDBOX`，`--approval` 优先于环境的审批预设。
未传 `--environment` 时采用 host 的审批默认值，并继续读取原有沙箱环境变量，旧命令无需改动。
启动提示会列出环境、内部命令沙箱和审批设置；鉴权后的 `GET /health` 也返回 `environment`。

两种环境均保留 token 鉴权、会话工作区范围、目录信任、deny 规则、不可逆命令拦截等既有保护，
不会自动开放 agent 自带的 MCP server。关闭内部命令沙箱后，其网络限制也不再生效；
文件挂载、进程权限与出网范围由外部容器或虚拟机控制。
HTTP 客户端不能修改 `environment`，agent 对象仍只能收紧服务端的审批和沙箱设置。

---

## 为什么有两个 prompt 端点

coding agent 一轮动辄几分钟，而编排器的 HTTP 节点普遍有分钟级超时。

| 端点 | 行为 | 什么时候用 |
|---|---|---|
| `POST /session/{id}/prompt` | 跑完才返回，带 `result` | 几十秒能完的活 |
| `POST /session/{id}/prompt_async` | 立刻 `202`，进度自己取 | **长任务默认走这个** |

异步那条的配套是状态轮询和事件游标，见下面两节。

**重发不重跑**：两个端点都认 `Idempotency-Key` 请求头。编排器等不到响应就会重发
（同步端点最常见——它自己的 HTTP 超时先到），而那一轮其实已经跑完或还在跑。带着同一个键
重发不会再开一轮：还在跑就等同一轮的结果，已经跑完就直接拿回那一轮的结果，响应里带
`"duplicate": true` 与轮号。同一个键配不同的指令是 409；服务重启前没跑完的那一轮，
重启后带着它的键重发也是 409（做到哪一步说不清，不替你决定重跑）。每个会话在内存里
保留最近 64 次提交的结果；键、指令摘要和轮号作为小回执随会话清单保存。结果已淘汰或
重启后不在内存时，相同键返回 409，`detail.code` 为 `submission_recorded`，不会再次执行。
回执保留到会话删除；`--no-persist` 只在当前进程有效。原子写保护进程重启，不承诺断电恢复。
回执写入失败返回 503，轮次不启动，调用方可以保留原键重试。

`POST /session` 同样接受 `Idempotency-Key`：同一键和相同配置返回原会话并带
`duplicate: true`；配置不同、或对应会话尚未恢复时返回 409。开启持久化时创建回执跨重启保留，
删除会话后失效。不同键代表明确的新建请求。

嵌入宿主可设置 `ServeConfig.persist_empty_sessions=False`（默认 `True`）：未提交任务、
无分支来源及中断记录的准备会话不保存恢复清单；其创建回执仅在当前进程有效。
首轮提交仍先保存执行回执再运行，之后遵循相同的持久化与重试规则。桌面宿主使用此选项，
未发送文字由应用按目录和窗口保存为输入缓存，不作为会话历史。

`POST /session/{id}/abort` 可带 `{"turn": N}`：只打断第 N 轮（轮号在 `/status` 的
`in_flight.turn`）。它已经结束时返回 `"aborted": false`，不会误伤后面那一轮。
`steer` 也可带 `turn`，目标轮次已变化时返回 409。两个端点均可带 `X-Xiaoyu-Instance`
请求头，值来自鉴权后的 `/health.instance_id`：服务重启后旧实例的 abort 返回
`aborted: false`，旧实例的 steer 返回 409，避免重启后轮号复用导致迟到操作误伤新任务。
未带这些字段的既有客户端保持原来的行为。

鉴权后的 `/health.desktop_protocol: 1` 表示服务具备上述创建/任务回执和实例校验能力。
桌面操作端还应检查 `persist: true`；仅看到存活探针不代表已具备写入条件。

两个端点都可带 `output_schema`（JSON Schema）：要求这一轮以符合它的**对象**收尾，
而不是一段正文。模型经 `structured_output` 工具交回，结果在 `result.output`
（`prompt` 直接返回；`prompt_async` 在 `GET /status` 的 `last_result.output`）。
模型没按要求给则为 `null`，编排侧据此判失败。校验只做 type / required /
enum / items 这一层，复杂关键字自己再校。

本轮文件交付在 `result.artifacts`（异步为 `last_result.artifacts`），没有文件时为空
数组；非空时事件流另发 `artifacts.ready`。每项包含工作区相对路径、文件大小、SHA-256、
创建/更新/显式交付类型与本轮完整性状态。文件工具自动记录，bash 等工具不会触发
目录扫描。它是文件清单，不是下载接口；详见 [文件产物协议](embedding.md#单轮结算-runresult)。

```bash
curl -X POST :8420/session/$SID/prompt -d '{"text":"评估这个 PR 能不能合",
  "output_schema":{"type":"object","properties":{"mergeable":{"type":"boolean"},
  "reasons":{"type":"array","items":{"type":"string"}}},"required":["mergeable"]}}'
#  → {..., "result": {"text": "...", "output": {"mergeable": false, "reasons": [...]}, ...}}
```

---

## 持久化问答

创建会话时传 `questions: {foreground_timeout_seconds: 60}` 可开启前台等待；
`questions: {}` 立即待答。问题查询、幂等提交／取消走 `/session/{id}/questions`，
版本变化进入已有 events／SSE。只支持持久化会话，空闲时提交回答不会自动跑模型，
问答不替代 permissions 审批。完整端点、重连与恢复契约见
[HTTP 持久化问答](serve-questions.md)，自 0.62.0 起提供。

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
| `idle` | `interrupted_by_restart` | 同上，但重启时**有一轮正在跑**：它没走到收尾，`lost_turn` 说明是第几轮、何时开始 |
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

# 4. 或者放行并且本会话内同范围不再问（TUI 确认框答 a 的那一档）
  -d '{"request_id":"...","decision":"allow_session"}'

# 5. 同上，但选挂起项 session_scopes 里更粗的一档（一次放开本会话全部浏览器写操作）
  -d '{"request_id":"...","decision":"allow_session","scope":"browser:*"}'
```

`updated_args` 非空时整体替换本次调用参数再执行——"批准但改写"的通道
（典型用法：把命令包一层再放行）。它不能与 `allow_session` 同用（`400`）：会话授权的
范围按原调用推导，改写过的调用只能 `allow`。

**会话内放行**（`allow_session`）：范围**由服务端推导**、随挂起项的 `session_scope` 字段
下发——非命令类工具是工具名（`write_file`、`browser_type`），`bash` 是命令键（`git status`
这种头，与 TUI 的会话授权同一口径）；`session_scope` 为 `null` 的调用推不出范围，只能
`allow` / `deny`（回 `allow_session` 是 `400`）。客户端只能选档，带不进自定义范围：持
token 的一方放得开的，只有模型真实尝试过、且范围推得出的那一类调用。授权随会话消失。
响应多一个 `granted` 字段（落下的范围）。

**选更粗的档位**：挂起项另带 `session_scopes`，细到粗列出可选档位，每项 `{scope, label}`，
首项就是 `session_scope`。目前只有浏览器桥一族有第二档——`browser:*`（文案「本会话全部
浏览器操作」），一次放开七件写类工具，省得 open / navigate / click / type / key / back / close
各点一次。`allow_session` 带 `scope` 选档，不带即首项；`scope` 不在这次下发的档位里是
`400`，挂起项不消费。只认 `session_scope` 的老客户端照旧工作。粗档位落下后 `granted.tools`
里记的就是 `browser:*`，`DELETE …/granted/browser:*` 收回。

已放开哪些、怎么收回：

```bash
curl -s $BASE/session/$SID/permissions -H "$AUTH"
# → {"pending": [...], "granted": {"tools": ["browser_type"], "commands": ["git status"]}, "rules": ["allow write_file(src/*)", ...]}
curl -X DELETE $BASE/session/$SID/permissions/granted/browser_type -H "$AUTH"   # 204；没有这条是 404
```

`granted` 是本会话的授权（可经 `DELETE` 收回）；`rules` 是持久规则的只读视图，改它请改规则
文件——HTTP 面刻意不开写持久规则的口。

事件流里对应两条：`permission.requested`（字段同 `pending_approvals` 里的一项，含
`session_scope` 与 `session_scopes`）与 `permission.resolved`（`request_id` + `decision`：`allow` /
`allow_session` / `deny` / `timeout`；`allow_session` 时另带 `granted`）。

**超时按拒绝处理**（`--approval-timeout`，默认 300 秒）。这是 fail closed：
安全闸门宁可多拦一次，不能在没人应答时静默放行。

无人值守又不想接这条回路，可用 `--approval allow_all` 自动批准所有审批。
专用容器或虚拟机中的服务可直接选 `--environment sandbox`，同时关闭内部命令沙箱，
详见上面的「选择部署环境」。deny 规则与不可逆命令拦截仍有效。

---

## 后台任务跑完了，会话却是空闲的

模型可以把长命令放到后台跑（`bash` 的 `run_in_background`、`monitor`），然后照常
收尾这一轮。任务跑完时会话多半已经空闲：完成通知是给模型看的，要等**下一轮**才
送得到。不告诉编排方的话，它只能干等，或者定时发一轮去问——每问一次都是一次
模型调用。

所以通知入队的那一刻，事件流里会有一条 `notification.pending`：

```json
{ "kind": "notification.pending", "idle": true, "wake": true,
  "key": "task-done-bg-1", "text": "后台任务 \"bg-1\" 已完成（exit 0）。…" }
```

`GET /session/{id}/status` 的 `pending_notifications` 列着当前还没送达模型的全部通知
（轮询型的编排器看这个就够了，不必接事件流）。

| 字段 | 含义 |
|---|---|
| `idle` | 入队时会话是不是空闲。`false` = 有一轮在跑，通知搭下一条工具结果就送到了，**不必做什么**；`true` = 没有下一步，得有人再提交一轮它才送得到 |
| `wake` | `true` = 值得为它开一轮（后台任务完成、monitor 有新输出、宸枢成员收工）；`false` = 捎带的公告（如 MCP server 上线），不值得专门叫醒模型 |
| `key` | 同一个 key 整个会话只告知一次、只送达一次 |
| `text` | 模型将会看到的那段话 |

**服务端只告知，不会自己开一轮。** 要不要为它叫醒模型由编排方定——通常是看到
`idle=true` 且 `wake=true` 就 `POST /session/{id}/prompt_async` 一句"后台任务有结果了，
接着做"。下一轮开跑、通知送达之后，`pending_notifications` 就空了。

后台任务随 serve 进程一起结束，重启之后不会接着跑，也就没有通知可等。

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

起服务时同样带上 `--public-url`（如反代后的 `https://agent.example.com`），运行中的
`/openapi.json` 就也带 `servers`，编排器可以直接按 URL 导入，不必离线导出。不带时运行中的
schema 不含 `servers`，只适合同源访问（`/docs` 调试）。

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
curl -X POST :8420/agent -d '{"name":"写手","config":{"model":"claude-opus-5-5","mode":"auto",
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
  服务端启动参数，`model` / `mode` 可临时覆盖。`mode` 谁都没给时与 CLI 一致，起手是
  `auto`（沙箱兜得住的工具调用不挂起等审批；想每条都过 `/permissions` 就 `--mode default`
  起服务或按会话传 `"mode": "default"`）。
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

## 分叉：从另一个会话的历史起步

```bash
curl -X POST :8420/session -d '{"fork_from":"sess-…"}'                          # 带走全部历史
curl -X POST :8420/session -d '{"fork_from":{"session_id":"sess-…","turns":3}}'  # 只带前 3 轮
```

新会话拿到的是来源历史的**副本**，写进它自己的会话日志；来源会话不受影响，之后关掉、
删掉也不影响分叉出去的那个。适合"调研做完了，接下来分几路各试一种做法"：前面的
上下文不必每路重跑一遍。

- 没另给 `workspace` / `agent` / `model` / `mode` 就沿用来源的工作区与 agent 版本；
  给了就以这次请求为准（比如同一段历史换个模型接着做）。
- **预算不沿用**：新会话的用量从零记起，要设预算在这次请求里给。
- 来源正在跑一轮时回 `409`——轮中的历史里有没配对的工具调用，抄走的是半截状态。
- `turns` 超出来源的轮数回 `400`，错误里写明可取范围。
- 新会话的事件流以 `session.forked`（带来源 id 与消息条数）开头。

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

agent 对象、会话清单、会话日志默认落盘在用户配置目录下的 `serve/<root slug>/`——
Linux/macOS 为 `~/.config/xiaoyu/serve/<root slug>/`（设了 `XDG_CONFIG_HOME` 则跟随它），
Windows 为 `%APPDATA%\xiaoyu\serve\<root slug>\`；启动后 `GET /health` 的 `state_dir` 字段给出实际路径（服务配了 token 时请求要带上）
（`--state-dir` 改位置，`--no-persist` 全放内存）。serve 重启后：

- 会话自动接回（`detail=recovered`），历史来自会话日志（`Agent.restore`，未配对的
  tool_call 会补"结果未知"），配置、agent 引用、预算、`turns` 随清单回来；
- **已花的用量接着累计**：`usage` / `spend` 从会话日志里最后一次轮末记账接上，到线的
  会话重启后仍是 `409`。被打断在半途的那一轮没来得及记账，会少算不到一轮；
- **在途的那一轮会被标出来**：开轮时清单就记下在途标记，收尾时清掉。重启时标记还在，
  说明那一轮随进程一起没了——会话以 `detail=interrupted_by_restart` 接回，`/status` 的
  `lost_turn`（`{"turn": 3, "started_at": …}`）与 `session.recovered` 事件里都带着它。
  那一轮做到哪一步、副作用落没落地，服务端说不清：历史里能看到它已经发出的工具调用
  （没等到结果的会补"结果未知"），要不要重做由编排方判断。标记留到下一轮开跑才清，
  连着重启两次也不会丢；
- **事件缓冲不落盘**——重启前的事件计入 `dropped_events`，`seq` 从上次水位**接着编号**：
  客户端手里的游标仍单调，拉到的是"中间缺一段"（协议里本来就有表达），不是"序号倒流"。
  水位按块预留落盘：优雅停机后编号紧接着上次；进程中途被杀则从预留线接，`seq` 会跳过
  一段（不到一个块），同样不会回头；
- 恢复失败的清单（工作区没挂上、provider 没配）留在盘上、stderr 打一行、跳过——不删，
  因为失败可能是暂时的。`DELETE /session/{id}` 会删清单；会话日志作为留痕保留。
- 会话日志同一时刻只允许一个进程续写（日志旁的 `*.jsonl.lock` 上的内核锁，进程退出
  即释放）。重启时若该会话正被别的进程写着（另一个 serve、CLI `--session-id`、编辑器
  经 ACP 接着同一个会话），按上一条"恢复失败"处理：stderr 写明持锁进程 pid、跳过、
  清单留盘，那边结束后再重启即可接回。
- 会话日志中段有记录损坏（读不出来的行被跳过）时，接回的会话事件流里会有一条
  `notice`（warn）说明第几行坏了；只有最后一行的半行（崩溃留下的）静默跳过。

文件即真相：`agents/agent-*.json`、`sessions/sess-*.json`、`logs/*.jsonl`，`cat` 即可审计。

---

## 安全边界

这个服务**会执行任意命令**，边界是硬的：

- 默认只绑 `127.0.0.1`。绑非回环地址时**必须**给 `--token`（或 `XIAOYU_SERVE_TOKEN`），
  否则拒绝启动
- 回环且不设 token 时，挡的是**本机浏览器里的网页**：`Host` 不是回环主机名（`127.0.0.1` /
  `localhost` / `::1` / `host.docker.internal`）一律 `403`，防 DNS rebinding；带 `Origin` 的
  请求与 WebSocket 握手只放行本服务自己的 origin 和 `--cors-origin` 白名单，防跨站驱动会话。
  curl / n8n / SDK 这类不带 `Origin` 的客户端不受影响；经反代用别的主机名访问时请加 `--token`
- 默认 `--workspace` 是 root，会话只能落在它或它的子目录里，越界 `400`。
  嵌入宿主可提供 `ServeConfig.project_resolver(project_id, workspace, snapshot)`，由宿主
  把已登记项目解析成 `{project_id, workspace, folders}`。此时 `POST /session` 可传
  `project_id`，客户端不能直接指定任意附加目录；未知项目、与主目录不符的 workspace
  均拒绝。会话响应附带项目身份与文件夹，恢复时使用宿主验证的原范围快照。
  没有 resolver 的普通服务不接受 project_id，也不放宽 root 边界。
- folder trust 非交互判定（与 `--acp` 同一纪律）：没信任记录的目录不吃工作区级
  `.mcp.json` / `permissions` / `.env`，也绝不在协议通道上发问
- 要暴露到公网，首选前面放反代做 TLS；没有反代可放时 `--tls-cert/--tls-key` 让服务自己以
  https 监听（见下节）。限流不做
- CORS：**Chrome 扩展 origin（`chrome-extension://<id>`）默认放行**，装了扩展填上地址和 token
  就能连，不用逐个列 id（开发者模式每台机器的 id 不同、商店版又是另一个）；`--no-cors-extensions`
  关掉。其它浏览器客户端（自研 Web 控制台）默认**不发 CORS 头**，要用 `--cors-origin https://console.example.com`
  （可重复）加进白名单——这只是"浏览器肯不肯把响应交给页面脚本"的门，token 仍照常校验。
  Chrome 访问回环地址的 Private Network Access 预检也一并应答
- 浏览器桥（`/session/{id}/browser`）是反过来的：扩展连上来后 agent 能操作**用户登录态的
  浏览器**。写类动作（开页 / 跳转 / 点击 / 输入）默认走审批回路，只读三件免审；
  token 在第一帧里校验，会话私有，断线即注销——见 [docs/browser-bridge.md](browser-bridge.md)

### TLS：`--tls-cert` / `--tls-key`

```bash
xiaoyu serve --host 0.0.0.0 --token "$TOKEN" --tls-cert fullchain.pem --tls-key privkey.pem
#  → https://0.0.0.0:8420
```

两个文件（PEM）必须成对给，只给一个直接拒绝启动——静默退回 http 会让人以为已加密。
证书由 uvicorn 直接加载（`ssl_certfile` / `ssl_keyfile`），自签证书照样可用，客户端自己决定
信不信。`--public-url` 记得写 `https://…`，OpenAPI schema 里的 `servers` 才对得上。
绑非回环地址仍然必须给 `--token`：TLS 管的是链路加密，不是鉴权。

---

## 端点速查

| 方法 | 路径 | |
|---|---|---|
| GET | `/health` | 存活探针（不需要 token；服务配了 token 而请求没带对时只回 `ok` 与 `version`，工作区路径等部署细节要带 token 才给） |
| GET | `/diagnostics` | 进程自诊断：RSS / 线程 / fd + 在册计量（会话、在途请求、MCP 连接、后台任务）。有 token 门——它暴露负载形态 |
| POST · GET | `/agent` | 新建 agent 对象 · 列出 |
| GET · POST · DELETE | `/agent/{id}` | 详情（`?versions=true`）· 更新→新版本 · 归档 |
| POST | `/session` | 新建会话（`workspace` / `model` / `mode`，或 `agent` 引用 + `budget`；`fork_from` 从另一个会话分叉；`adopt_from` 原地继续一段终端会话） |
| GET | `/session` | 列出会话 |
| GET · DELETE | `/session/{id}` | 详情 · 关闭 |
| POST | `/session/{id}/prompt` | 跑一轮，等结果 |
| POST | `/session/{id}/prompt_async` | 跑一轮，立刻返回 |
| GET | `/session/{id}/status` | 状态机（含 `pending_approvals`、`pending_notifications`） |
| GET | `/session/{id}/events` | 拉事件（游标 + long-poll） |
| GET | `/session/{id}/events/stream` | 同一份事件流的 SSE |
| GET · POST | `/session/{id}/permissions` | 挂起的审批与已放开的范围 · 回决定（`allow` / `allow_session` / `deny`） |
| DELETE | `/session/{id}/permissions/granted/{name}` | 收回一条会话授权 |
| POST | `/session/{id}/budget` | 改/撤预算（`budget_reached` 后的出路） |
| POST | `/session/{id}/abort` | 打断这一轮（不杀会话） |
| POST | `/session/{id}/steer` | 向进行中的一轮插话（会话空闲时 `409`） |
| WS | `/session/{id}/browser` | 浏览器桥：浏览器扩展连上来，agent 就能读页 / 点击 / 截图（[docs/browser-bridge.md](browser-bridge.md)） |
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

## 随桌面应用迭代的接口（实验性）

从这里到文末的接口由桌面应用的需求推动，仍在迭代：字段和语义可能在小版本之间调整，
不在 semver 承诺之内。每一组在授权后的 `/health` 里有自己的协议号，客户端应先核对
协议号再调用，而不是按 xiaoyu 的版本号推断。上文各节的接口不受此说明影响。

### 持久化正文与历轮结算

授权后的 `/health` 返回 `history_protocol: 1` 表示支持以下接口；匿名探针仍不提供部署细节。
两条接口沿用会话鉴权，只接受已有会话 ID，不提供任意日志路径读取。

- `GET /session/{id}/history?cursor=0&limit=100`：按时间正序返回
  `messages[{id, role, content, created_at, run_id, has_attachments}]`、`next_cursor`
  （末页为 `null`）、`total`、`persisted`、`warning`、`live_from_seq`。
  `cursor` 是从零开始的条目偏移，`limit` 为 1–500。`id` 是稳定的日志行标识。
  可选 `include_activity=true` 将工具结果（`role: tool`，含 `name/args/ok`）和实际模型切换（`role: model`，含 `from_model/model`）加入同一分页时间线；默认正文契约不变。工具输出是未受信内容，客户端须转义显示。
  正文来自原始日志中的 user/assistant 消息，不用压缩后的模型上下文代替完整历史；
  不返回 system、工具结果、宿主注入和同伴消息。图像保留 `[图片]` 占位，其他
  无文本附件保留 `[附件]`；`has_attachments` 表示此接口没有提供附件预览内容。
  清空、回退之前的原始消息仍在时间线中，`warning` 说明它不等于当前模型上下文。
- `GET /session/{id}/runs?cursor=0&limit=50`：相同分页字段，集合名为 `runs`，
  `limit` 为 1–200。每项为
  `{id, turn, status, started_at, finished_at, result, error}`。
  `result` 保留完整轮次结果，包含 `artifacts`；不验证成果文件此刻仍存在或内容未改变。
  `status` 为 `running`、`completed`、`interrupted`、`failed`、`stopped` 或
  `interrupted_by_restart`。预算、轮数上限、截断、结构化输出未完成等属于 `stopped`，
  原因见 `result.stopped`，不能渲染成任务已完成。

会话详情与状态新增 `title`、`history_persisted`、`history_warning`。
标题从首次提交提取并保存，`updated_at` 不因恢复本身更新；正常结算后的 `last_result`
可跨重启恢复，包括文件成果。恢复后的正常会话保留 `detail=recovered`；失败与中断
保留真实终态，逐轮细节以 `/runs` 为准。`turn` 对每次启动的执行计数，包括失败尝试。

**历史与实时消息的合并规则：** history 排除当前执行轮的全部正文，返回该轮
`run.started` 对应的 `live_from_seq`；没有在途轮时返回快照时的 `next_seq`。
客户端替换历史区域后，只追加 `seq >= live_from_seq` 的实时正文，轮结束或重连时
重新获取历史与结算。若分页期间边界改变，应重新加载历史基线；事件缓冲不足时必须
显示缺口，不能把有限事件当成完整历史。读取在工作线程逐行进行，并固定字节快照边界，
不会因读取过程中新一轮写入而与实时正文重复。当前实现仍需线性扫描日志；超大日志的
索引和更细粒度分页优化留待后续，调用方应在选中会话、翻页和轮次变化时按需请求。

轮边界与结算以 `serve.run.started` / `serve.run.settled` 记录追加到原日志，结算后
同步保存，避免另造一份消息真相。结算写入失败时当前会话显示 `persistence_failed`
与明确警告；不能据此认为执行副作用未发生，也不能盲目重试。提交回执仍防止相同
幂等键重复执行。重启发现没有可靠结算的在途轮会显示 `interrupted_by_restart`，
不自动续跑。原始正文或结算未能可靠读取/保存时 `persisted=false`；非持久化服务也为
`false`。旧日志与接入历史缺少轮次结算时另以 `warning` 说明缺口，既有正文仍可读取，
不会编造旧轮结果、工具成果或任务成功状态。

### 终端会话目录：查看、原地继续与显式分支

授权 `/health` 的 `catalog_protocol: 1` 表示支持本机终端会话目录。首版要求安全目录
句柄与禁止跟随链接的文件打开能力；不支持的平台返回 `0`，不降级为无保护读取。
普通 CLI 的 `resume` 与具名会话续写行为不变。

`GET /catalog/sessions?cursor=0&limit=100`（limit 1–200）返回
`{sessions, next_cursor, total, warning}`。每项包含：

```text
catalog_id, source="cli", title, workspace, model, started_at, updated_at,
revision, in_scope, locked, session_id, can_fork, fork_reason, can_continue, continue_reason
```

三个判定各管一件事：`locked` = 此刻有别的进程持有这份日志的写锁（正文仍可读）；
`can_continue` = 日志已正常结束且无人持有，可以原地继续；`can_fork` = 能取到一份
一致的快照另开分支。`session_id` 非空表示这份日志已由本服务的那场会话原地继续着，
此时 `locked=false`、两个 `can_*` 都为 false，客户端应直接打开那场会话。

只发现配置目录中标准 `sessions/` 日志，来源工作区必须存在且位于当前服务 root 内。
宿主以 `ServeConfig(catalog_everywhere=True)` 启动时（本机单用户的桌面宿主如此；`xiaoyu serve`
缺省不开），root 之外的工作目录里的会话也会列出并可查看，标 `in_scope=false`；它们在本服务
永远不能继续或分支，客户端应转到对应目录的服务去操作。工作目录已不存在的仍不列出。
列出与查看不会在日志旁新建锁文件；解析结果按文件修订缓存，占用状态每次现探。
原始日志路径不对前端开放；`catalog_id` 是服务端计算并重新校验的标识，不能用客户端
路径替代。服务和桌面自己的日志目录不参与此目录。文件/目录符号链接、非普通文件和
多链接日志拒绝读取。目录暂不可读或扫描期间变化会产生缺口警告。

`GET /catalog/sessions/{catalog_id}/history?cursor=0&limit=100`（limit 1–500）返回
该条目录信息及 `messages`、`next_cursor`、`total`、`persisted`、`warning`。
正文形状与普通 history 相同，但这里是只读字节快照，没有实时事件游标。
锁住的来源可以查看，`locked=true`、`can_continue=false`；查看不取得它的写锁。
写者停在回合边界（最后一条可回放消息是已完成的助手回复）时 `can_fork=true`，
轮中则为 false 并在 `fork_reason` 里说明。
若预览期间来源变化，返回 409，刷新后再查看。分页时 revision 改变也应重置正文基线。
首版发现/预览需线性扫描来源日志，应按需调用，不作为高频轮询接口。

通过原有 `POST /session` 原地继续——不复制历史，本服务取得该日志的写锁并续写**同一个
文件**（与具名会话的续写同一条路）：

```json
{"adopt_from":{"catalog_id":"cli-…","revision":"预览响应中的版本"}}
```

- 日志被别的进程持有时返回 `423`，`detail` 为 `{code:"writer_conflict", message}`；
  对方结束后刷新预览、按新 `revision` 重试，或改用 `fork_from` 另开分支。
- revision 变化、日志损坏/半行、没有正常退出标记、工具调用不配对时返回 409。
- 不接受 `agent`、`project_id`，也不能与 `fork_from` 同给；`workspace` 只能等于来源工作区。
  `model` / `mode` / `budget` / `questions` 照常，运行配置取当前服务或本次请求。
- 新会话的 `fork_origin` 为 `{kind:"cli_adopted",catalog_id,title,workspace}`，事件流以
  `session.adopted` 开头，日志里先落一条 `reopened`。持有期间终端打不开这份日志
  （具名会话续写、改名都会得到占用报错）；普通 `resume` 读取并复制历史不受影响。
- 关闭这场会话（`DELETE /session/{id}`）= 交还：写正常结束标记并放锁，目录里重新可继续。
  宿主删除它的历史时也只交还，不删除终端这份日志。服务重启后按清单重新取得写锁接回。
- 同样支持 `Idempotency-Key`；同键重试返回原会话，不会再抢一次锁。

通过原有 `POST /session` 创建独立分支：

```json
{"fork_from":{"catalog_id":"cli-…","revision":"预览响应中的版本"}}
```

创建请求应带 `Idempotency-Key`；同键重试、断线后重试、服务重启后重试返回原有新会话，
不会再次分支。来源后续变化也不会使同一已完成创建被重新执行；同键不同配置返回 409。
`fork_from` 的此形态严格只接受 `catalog_id` 与 `revision`。不指定 workspace 时用来源
工作区；显式目标也必须位于服务 root 内。来源 revision 变化、无法建立严格写锁、
日志损坏/半行、上下文工具调用不配对时拒绝分支；无人持有的来源还须有正常退出标记，
被持有的来源须停在回合边界（复制不改动来源，读取前后修订不一致即拒绝）。
查看权限不等于分支准入；最终准入始终在源锁内重新判断，不采信前端 `can_fork`。

新会话使用新 ID 和新日志，详情里的 `fork_origin` 保存
`{kind:"cli",catalog_id,revision,title,workspace}`。只复制可恢复消息上下文；
源的 system prompt、模型/provider 配置、运行模式、临时授权、凭据、活进程、
子任务运行档案及文件回退快照不会随分支迁移。运行配置来自当前服务或本次显式请求。
原日志不修改；协调锁旁车可能被创建。历史中已有文件路径或文本不会被复制成实体文件，
分支也不撤销文件副作用。同工作区分支仍可能操作同一批文件。

### 七襄批次的只读视图

授权 `/health` 返回 `weave_protocol: 1`。`GET /session/{id}/weaves?cursor=0&limit=20`
返回 `{session_id,batches,next_cursor,total,persisted,warning}`，`limit` 为 1–64。
批次按创建时间倒序，最多保留最近 64 批；超过时明确告知显示范围。
每批包含 `batch_id`、`spec`、递增 `revision`、`final`、`cancelled`、父轮 `run_id` 和 `items`。
每项包含稳定 `id`、从零开始的 `index`、`label`、`status`、`model`、`resume_from`、
子任务续跑句柄 `run_id`、`answer`、`error`、`worktree`、`started` 和截断标识。
条目状态为 `queued/running/completed/partial/failed/aborted/error`；
重启恢复时缺少可靠结算的排队或执行项标为 `interrupted_by_restart`。

内核事件 `qixiang.updated` 提供同样的批次快照；状态变化时发布，交接正文仅在
`final=true` 时提供，单项最多 4000 字符（大批次按报告预算继续缩小），
`answer_truncated=true` 表示不是完整原文。客户端不应解析 Notice 或文本报告推断状态。
事件投递到服务时以 `serve.qixiang.updated` 写入原会话日志，完整快照独立于有界实时
事件缓冲；重启恢复不重新执行子任务。批次未正常收束时，即使单项已完成，
交接正文也可能尚未记录，不能把缺失正文当作完整成果。

快照写入失败保留内存结果并返回 `persisted=false` 与警告。展示句柄不保证对应子任务
存档仍可用；本接口没有发起、选择续跑、逐项取消或工作副本清理操作。

### 结构化发起与续跑七襄（`weave_command_protocol: 1`）

这些端点与普通会话 API 使用相同鉴权。界面提交结构化参数，服务直接调用已加载的
`qixiang` 工具；不需要父模型把自然语言转成调用。执行仍经过普通工具权限、规划态、
审批、hook、沙箱及预算，审批只对当前请求有效。该入口不新增公开 Python API。

- `GET /session/{id}/weaves/capabilities`：返回 `available`、`reason`、`can_start`、
  `can_resume`、`min_items`（2）、`max_items`（64）、`specs`（`name`、`description`、
  `readonly`、`default_isolation`），以及 `batches[{batch_id, items[{id,can_resume,resume_reason}]}]`。
  只广告当前会话实际加载的规格；不可续跑项有具体原因。执行中不能提交或预览，草稿可留在客户端。
- `POST /session/{id}/weaves/preview`：新批传
  `{"spec":"reader","prompt_template":"核对 {{item}}","items":["甲","乙"]}`；续跑传
  `{"source_batch_id":"…","item_ids":["…"],"instruction":"继续核对遗漏"}`。
  返回 `operation`（`new` / `resume`）、`spec`、`count`、`items[{label,task}]`、`preview_token`。
  新批 2–64 项，模板必须包含 `{{item}}`，展开后不得重复；续跑 1–64 个不同条目。
- `POST /session/{id}/weaves`：原预览请求加 `preview_token`，必须带 1–200 字符
  `Idempotency-Key`。202 返回 `accepted`、`session_id`、`run_id` 和会话状态；
  `in_flight.command` / 历轮 `command` 记录 `kind:qixiang`、`operation`、`spec`，
  续跑另含 `source_batch_id`、`item_ids`。停止仍用现有 `/abort`。

预览 token 钉住请求、已加载规格、模型、工作区与全部选中存档的摘要，只用于一致性校验，
不代表授权。客户端不能提供文件路径或委托 `run_id`：服务从本会话已可靠结算的批次条目
映射存档，逐项检查规格、原模型、上下文容量和工作区后才接受整批。失档、来源不符、
上下文过大、已丢失/移动/不属于原仓库的隔离工作树均拒绝，不静默重建工作树。非只读新批
要求有有效 Git HEAD；实际执行如遇隔离失败，仍按工具自身 fail-closed 行为报告错误。

续跑生成新批，保留旧批；新批额外带 `source_batch_id`、`source_item_ids`，条目保留
`resume_from` 关联。相同来源可多次明确续跑，每次使用新提交键。任一条目失败或部分完成时，
父轮结算为 `stopped` / `batch_incomplete`，不能把调度结束当成全部成功。

重复提交先查持久回执，再检查预览与 busy。当前进程能取得原记录时返回同一 `run_id`、
`turn` 和 `duplicate:true`；重启后若只有接收回执，返回 409 `submission_recorded`，
不使用过期预览重新执行。客户端应查看 `/runs` 和 `/weaves` 核对结果。执行开始前的校验失败
不会消费提交键；持久化失败则保守保留可能已写下的回执。

首版能力查询会验证存档并读取 Git 元数据，规模与已保留批次有关。预算在接受前、事件边界
及结算时检查；多个正在进行的子请求可能产生少量预算超出，取消在子请求/工具可中断边界生效。

直接命令会触发父会话 `UserPromptSubmit` 及正常工具 hooks；子任务照常执行自己的模型
与 `Stop` hook。父会话没有模型申请收尾，不额外触发父 `Stop` 或隐式调用模型续写。
这类执行没有父模型消费插话的步骤，`/steer` 返回 409；需要调整时先停止，再选择条目
填写续跑说明。工具前后保留取消检查点，已执行的工具结果先入历史再结束本轮。

### 登记成果检查

`health.artifact_protocol=1` 表示支持按登记成果 ID 查询：

- `GET /session/{id}/artifacts/{artifact_id}?run_id=...`：主会话某轮成果。
- 同一路径用 `batch_id=...&item_id=...`：七襄某条目的真实执行目录与成果。
- 同一路径加 `/location` 并保留所属查询参数：复核安全文件后返回绝对路径与工作区，
  供原生宿主在 Finder 定位。仅返回已登记文件，不接受任意文件路径，也不执行文件。

返回 `artifact`、`version`（`delivered/current_changed/missing/unavailable`）、
`delivered/current`（`available/text/truncated/note`，current 另含 sha256）、
`diff`（`available/text/truncated/label/note`）、`can_reveal`、`warning`。
差异比较“本次执行前 → 交付时”，必须有两个文本版本；不把当前文件当作交付快照。
交付快照有 SHA-256 对账，旧日志仅在当前文件匹配登记摘要时可重建交付文本。

每文件文本快照最多 64 KiB，每轮最多 16 份、合计 256 KiB 编码正文；七襄最终批次
快照合计最多 2 MiB。当前文件最多读取 8 MiB 计算摘要，文本显示最多 64 KiB。
二进制不显示文本，差异限制 2,000 行及 24,000 字符；缺失或超限明确说明。
列表和实时事件不携带快照正文，检查时按需读取；快照保存在私有会话结算日志中。
文件读取使用目录句柄并拒绝软链接、硬链接、特殊文件和 `.git` 路径；不支持这些
安全原语的平台明确不提供文件读取。查看成果不是内容验收或自动采纳文件改动。

### 斗巧与用户采纳（`contest_protocol: 1`）

斗巧表示同一任务的多个独立候选及判官评审。模型推荐、用户采纳和实际文件变更是不同事实：
采纳只追加一条用户决定，不覆盖判官推荐，不复制、合并或应用候选文件，不自动执行下一轮。
下列端点使用既有会话鉴权；所有路径均以当前会话持有的比赛与候选标识定位。

- `GET /session/{id}/contests?cursor=0&limit=20`：返回 `session_id`、`contests`、
  `next_cursor`、`total`、`persisted`、`warning`。limit 最大 64，首版保留最近 64 场视图。
- 每场包含 `contest_id`、`run_id`、`spec`、`task` / `task_truncated`、
  `criteria` / `criteria_truncated`、`revision`、`phase`（`competing` / `judging` / `settled`）、
  `final`、`cancelled`、`candidates`、`judge`、`selection`、`selections`、`selection_revision`。
  `revision` 来自内核快照；`selection_revision` 从 0 起，只随用户决定增加，两者互不覆盖。
- 候选含稳定 `id`、从 1 起的 `index`、`status`、实际 `model`、`run_id`、`workspace`、
  `worktree`、`answer` / `answer_truncated`、`error`、`started`、`artifacts`。
  状态包括 `queued`、`running`、`completed`、`partial`、`failed`、`aborted`、`error`；
  未结算记录在重启后额外标记 `interrupted_by_restart`，不能据此采纳。
- `judge` 包含 `status`、`model`、`run_id`、`answer` / `answer_truncated`、`reason`、
  `recommendation_id`。评审状态明确区分 `pending`、`running`、`completed`、`failed`、
  `skipped`、`invalid`、`aborted`；失败、部分回答或无效裁决不冒充推荐。
  快照存于日志并可重启恢复；正文与候选状态不依赖有限实时事件窗口。
  成果快照正文只在内部保存，常规列表与事件不包含 `artifact_previews`。

`GET /session/{id}/contests/capabilities` 返回 `available`、`reason`、`can_start`、
`specs[{name,description,default_model,default_isolation}]`、`models`、`default_model`、
`default_judge_model`、`min_candidates:2`、`max_candidates:6`。
模型清单仅来自已配置或已缓存记录，不触发联网发现；它用于输入提示，提交时仍以 registry
解析为准。只广告实际挂载的规格；忙碌、规划模式、预算耗尽或持久化不可用时不接受执行。

`POST /session/{id}/contests/preview` 接受：

```json
{"spec":"reader","task":"独立给出完整方案","models":["model-a","model-b"],
 "criteria":"正确性、覆盖范围与验证证据","judge_model":"model-c","capability_mode":"read-only"}
```

`spec`、`task`、`models` 必填。`models` 需要 2–6 个名称，可重复同一模型以独立上下文参赛。
任务上限 64000 字符，标准上限 16000 字符。`criteria` 缺省为空，`judge_model` 缺省随父会话；
`capability_mode` 可省略，或使用工具既有的 `read-only` / `read-write` / `execute` / `all`。
预检全部模型、规格和隔离条件后，返回规范化输入、`count`、`isolation`、`preview_token`。
任何会写的有效工具集都要求可用 Git HEAD，不会在预检失败时先跑部分候选。

`POST /session/{id}/contests` 使用原请求加 `preview_token`，必须带 `Idempotency-Key`，
202 返回 `accepted`、`session_id`、`run_id` 和会话状态。执行使用既有 host-tool 路径，
`in_flight.command.kind` 为 `douqiao`；权限、审批、取消及预算与普通工具一致。
直接命令的父轮不接收插话，停止仍用 `/abort`。候选或判官没有可靠完成时，轮次结算为
`stopped` / `contest_incomplete`，详细状态以候选与判官记录为准。重复执行回执、重启后的
409 `submission_recorded` 与七襄相同，不重新运行已接收操作。

`POST /session/{id}/contests/{contest_id}/selection` 必须带独立 `Idempotency-Key`，正文为：

```json
{"candidate_id":"比赛标识:1","reason":"经补充核对，更符合当前验收条件","expected_revision":0}
```

`expected_revision` 对应当前 `selection_revision`。只能在会话空闲且比赛已经可靠 final 时，
选择该比赛真正 `completed` 的候选；部分完成、执行中、失效来源与过期 revision 返回 409。
判官失败不妨碍用户明确选择已完成候选。理由为最多 4000 字符的文本，可为空。
200 返回 `contest_id`、`selection`、`selection_revision`、`duplicate`。决定记录包含
`id`、`candidate_id`、`reason`、`source:"user"`、`created_at`（Unix 秒）、`sequence`。
先严格提交日志，再更新视图、发布 `douqiao.selection`；持久化确认失败返回 503，并暂停
新的采纳，直到重启按日志核对。每会话最多保存 1000 次采纳，超限显式拒绝，不淘汰旧回执。

改选使用新键及新的预期 revision，保留全部 `selections` 历史。原键重试先核对回执，
即使已改选或服务重启，也返回原决定且 `duplicate:true`，不新增序号；当前选择以随后
`GET /contests` 为准。相同键配不同参数返回 409。比赛列表滚动淘汰不删除已保存的采纳回执。

### 宸枢只读观察（`fleet_protocol: 1`）

`GET /session/{id}/fleets?cursor=0&limit=20` 返回当前会话已观察到的宸枢快照。
只读端点使用既有鉴权，limit 为 1–64；返回 `session_id`、`fleets`、`next_cursor`、
`total`、`persisted`、`warning`、`available`、`reason`。首版最多保留最近 64 个运行时的
最新快照，超过范围明确提示。`available` 表示当前会话已挂载宸枢能力；未启用与尚无活动
有不同说明，关闭能力也不会隐藏已保存的历史。

读取只访问会话的持久视图；不会读取或收养工作区的宸枢 `state.json`，不会初始化运行时、
刷新 Git 状态或发起任何控制操作。init、plan、spawn、resume、merge 和 teardown 使用下文结构化命令入口；
评审仍由已有领域工具提交；精确成员消息和停止见下节。现有主轮 `/abort` 仍只表示停止主轮或等待，不代表后台成员已经停止。

每个快照包含：

- `board_id`（每个 runtime 的稳定标识）、递增 `revision`、`active`、`base_branch`、
  `observed_at`，以及 `source{runtime_id,state_id,origin,workspace,protocol_root}`。
  新 runtime 有新 board_id；同一协议状态可由 state_id 关联。origin 为 `new` 或 `adopted`，
  接管只表示内核显式执行了已有收养路径，GET 本身不会接管。
- `missions`：id、title、kind、scope、deps、status、owner、branch、worktree、notes，
  及相应截断标识。任务状态是协议事实，不等于成员已经完整完成，也不是可合并许可。
- `members`：name、kind、mission_id、review_target、原始 status、execution_state、
  execution_outcome、thread_alive、run_id、model、effort、handoff、handoff_truncated、
  stop_reason、error。当前 execution_state 区分 running、starting、settled、unknown、retired；
  outcome 区分 running、completed、partial、failed、unknown。父会话空闲仍可能存在运行成员。
- `reviews`：id、mission_id、from、round、verdict、summary、reviewed_commit、recorded_at、
  commit_check。`commit_check:not_rechecked` 明确表示只保存评审提交时的版本，未核对当前 tip，
  不可直接作为当前合并许可。
- `findings`：id、from、mission_id、kind、title、body、recorded_at；`activity` 包含
  id、at、actor、action、details。`limits` 分别记录 missions、members、reviews、findings、
  activity 被省略的条数；`gaps` 说明来源缺口或能力边界。接管旧协议不会把可变 Markdown
  文件解析成仿佛本会话已经观察到的新历史。

HTTP 另附 `source_run_id`、`live`、`recovered`、`runtime_state`。source_run_id 固定为该
runtime 首次在本会话出现时的父轮；后台后续事件不会改绑另一轮，来源不明则为空。
`live:true` / `runtime_state:observed` 只表示当前服务进程收到的内核观察，不表示整个团队
都在运行，也不提供控制权限。完整视图独立于有限实时事件窗口，保存失败时明确
`persisted:false` 并显示警告。

服务重启后只恢复日志，所有旧快照标记 `live:false`、`recovered:true`、
`runtime_state:unknown_after_restart`。原始 active/status 保留为历史事实；所有成员
thread_alive 变为 null，原值保留在 recorded_thread_alive。先前 running/starting 成员的
execution_state/outcome 标为 unknown，原值保留在 recorded_execution_state/outcome；
已可靠 settled 的 completed/partial/failed 结论仍保留，即使最终事件发出时工作线程尚在
收尾。此处不声称成员已被取消、已重新接管或仍在执行。

成员工具与审批继续使用宸枢自身的身份和工作区写入守卫；支持控制的服务会按下节明确标识成员审批。


### 宸枢成员控制（`fleet_control_protocol: 1`）

仅面向本会话、本进程已观察到的有效成员代；不会初始化、收养或恢复旧成员。
两个端点必须带 `Idempotency-Key`（1–200 字符）：

- `POST /session/{sid}/fleets/{board_id}/members/{name}/messages`：
  `{"run_id":"精确成员代","text":"补充指令"}`，text 为 1–8000 字符非空文本。
- `POST /session/{sid}/fleets/{board_id}/members/{name}/stop`：
  `{"run_id":"精确成员代"}`，只请求协作停止，保留该成员工作树和存档。

新操作返回 202 `{operation,duplicate:false}`，同键同参数返回 200 且 duplicate 为 true；
同键不同参数返回 409。先将 reservation 严格提交到会话日志，再调用内核精确代控制；
保存失败不执行新操作，拒绝后续写入。每会话最多 1000 个回执，满后显式拒绝，不淘汰旧键。
回执包含 operation_id、kind（message/stop）、board_id、member_name、member_run_id、
run_id（同 member_run_id）、text、status、created_at、updated_at、reason。
时间字段可能为 Unix 秒或内核 ISO 时间字符串。幂等键及内部 digest 不出现在响应中。

status 区分 reserved、queued、accepted、not_consumed、stop_requested、stopped、failed、unknown。
202 只代表操作回执：queued 尚未被成员接纳，accepted 仅由内核真正消费输入的事件证明，
不表示模型已完成回答；成员结束仍未消费的消息为 not_consumed。stop_requested 不表示
已经停止，stopped 仅由内核结束事实产生。精确代失效会返回 status:failed 的回执和原因，
原键重试不会转移到同名新代。内核执行或保存结果不确定则返回 unknown，不盲目重放。

`GET /session/{sid}/fleets` 的 members 另附 controls{can_message,can_stop,reason}、
stop_requested 和 operations（仅属于该成员当前 run_id 的操作）。完整性故障、未持久化、
恢复的历史看板、非当前运行时、已结束或停止中的成员都不可发送新消息。
`GET /session/{sid}/fleet-operations/{operation_id}` 返回 `{operation}`，可查同会话旧代回执；
不存在或跨会话返回 404。`fleet.control` 为操作回执事件；后续真实接纳/结束由
`chenshu.updated.controls` 更新。内核快照优先保留未结算 control，再补最近已结算记录，
合计最多 128 条，limits.controls 明确省略数；
服务端单独保留所有已登记回执。重启时 replay 已保存事件，只保留可靠终态，未确定的
reserved/queued/stop_requested 标 unknown 并附 recorded_status；原键查询始终不重放。

桌面成员继承宿主运行模式，并在既有权限检查和工作区写入守卫后接宿主审批。
`GET /permissions` 的成员 pending 与 permission.requested/resolved 事件附
identity{board_id,member_name,member_run_id,mission_id,workspace}。成员 pending 的
allowed_decisions 为 [allow,deny]，session_scope 为 null，session_scopes 为空。
成员审批拒绝 allow_session、scope 及 updated_args，不将成员决定授予父会话。
既有 allow/deny/ask、沙箱、硬写入边界没有因看板放宽；CLI 未安装宿主桥时维持原语义。

父轮 `/abort` 与父轮结束不取消成员审批。精确停止会唤醒对应成员等待；关闭会话或服务
会请求所有当前成员协作停止并拒绝尚未处理的审批，给最终快照最多 0.5 秒收尾窗口。
阻塞的提供方不能被强制结束，超过窗口不伪称停止，重启仍按历史未知处理；不删除工作树。


### 宸枢编排命令（`fleet_command_protocol: 1`）

`GET /session/{sid}/fleet-commands/capabilities` 提供当前真实能力与拒绝原因：
available、reason、board_id、revision、can_init、can_adopt、can_plan、can_spawn、
models、default_model、efforts、missions，以及 members 的 can_resume/resume_reason。
GET 不初始化、不收养工作区，不开启成员。模型选项来自已配置或缓存的注册信息。

先向 `POST /session/{sid}/fleet-commands/preview` 提交结构化动作：

- 初始化：`{action:"init",mode:"new"|"adopt"}`，新建与显式收养分别选择。
- 登记任务：`{action:"plan",board_id,missions:[{title,kind:"build"|"survey",scope:[...],deps:[...]}]}`。
- 启动成员：`{action:"spawn",board_id,name,kind:"worker"|"reviewer",mission_id?,review_target?,model?,effort?,instructions?}`。
- 原上下文续跑：`{action:"resume",board_id,name,run_id,effort?,instructions?}`。
  role、模型及任务归属从确切成员和存档推导，不能由客户端改指另一任务。

预览返回实际工具、参数、说明与 preview_token。将同一请求加 preview_token 提交到
`POST /session/{sid}/fleet-commands`，必须使用 Idempotency-Key。202 返回 accepted、
status、in_flight、run_id；accepted 仅说明父轮已登记，不表示审批通过或成员已完成。
命令直接调用既有 chenshu_init、chenshu_plan 或 chenshu_spawn 工具，遵守 Agent 的权限、
规划模式、hooks、沙箱、审批、预算、停止与持久轮次规则，不调用模型解释表单。
工具成功即可完成父轮，成员实际后台执行状态仍以宸枢快照为准。

预览 token 绑定请求、运行时、工作区与来源快照。提交时重新核对，并在等待审批后、
实际修改前再次核对；状态、Git HEAD 或来源存档变化时拒绝执行，要求重新预览。
同工作区其他会话已占有运行时，或同时提交初始化抢占失败时，不会收养对方正在使用的花名册。

幂等核对先于 busy、预览和当前运行时检查。同键同参数在本进程返回原轮次回执，
不会再执行；不同参数返回 409。重启后返回 submission_recorded 或未完成轮次的明确
409，保留已有记录，不用旧预览自动重放。持久化故障、规划模式、预算耗尽及来源归属失效
均阻止新动作。父轮历史记录 command.kind:chenshu、action 与实际工具，便于核对来源。

结构化初始化取得协议目录的独占 host-owner 锁，并在宿主管理的状态中标记 host_protocol。
其他遵循该协议的服务持锁时，预览和执行均拒绝接管；服务崩溃由系统释放锁，重启后的
明确 adopt 才能重新接管。关闭时仅在成员实际全退出后释放，未退成员不会被另一个宿主
悄悄收养。旧 CLI 未采用此宿主归属锁，不能据锁空闲推断旧 CLI 一定退出；旧状态还记录
running 成员时拒绝接管，其余旧状态明确提示需先结束原拥有者。此版本不支持与旧 CLI
同时修改同一宸枢协议目录，也不将宿主锁宣称为针对未遵循协议进程的强制互斥。


### 当前评审差异、合并与清理（`fleet_review_protocol: 1`）

`GET /session/{sid}/fleets/{board_id}/missions/{mission_id}/changes` 只读核对当前会话
真实拥有的有效运行时。返回 board_id、revision、mission_id、can_merge、
blockers（`[{code,message}]`）和 merge：

- mission、base_branch、base_head、target_branch、target_head、merge_base；
- review 的 round、verdict、reviewed_commit、current，以及 deps、changed_files；
- diff{text,truncated,available,note} 与 conflict_check:not_checked。

历史看板和其他会话的 board_id 不能用于读取当前工作区或执行；GET 不初始化、不接管，
不接受任意文件路径。差异以核验后的提交 SHA 为输入，禁用外部 diff/textconv；有界输出
明确报告截断。评审是否仍有效由当前版本重新核对，不沿用旧快照的 not_rechecked 标记。
can_merge 表示已检查的准入条件通过，仍不保证 Git 合并无冲突。

沿用 `/fleet-commands/preview` 和 `/fleet-commands` 增加两个动作：
`{action:"merge",board_id,mission_id}` 与 `{action:"teardown",board_id}`。
存在合并或清理阻断时，预览仍可返回 200、can_execute:false、blockers 和可读的事实；
提交被阻断的请求返回 409，不会产生新执行。成功预览的 token 绑定当前版本、评审、
工作树和任务状态；等待工具审批后还要在真正修改前复核，审批不能放行已变化的预览。
两者均使用已有 Agent 工具链、权限规则和持久幂等回执；不开放 force 或自选路径。

merge 只合并已评审且仍匹配当前 tip、依赖与 scope 满足要求的分支，工作树继续保留。
主工作区必须满足安全合并条件；失败时准确报告现场，只有确实回滚并核对成功才声明已回滚。
重复提交原键、丢回执及服务重启均不会再次执行同一次合并。

teardown 是独立的“清理工作树并结束协作”：可能清理尚未合并但已经提交且分支仍保留的
工作树，保留分支、成员存档与审计记录。有任意活成员、脏文件（含 ignored）、归属无法
验证或状态未知时，整次清理禁止执行；不会隐式停止成员，不会强制删除。
真实移除失败的路径必须保留在记录中，不能增加成功计数或伪装已清理。清理与成员停止、
合并是三个不同动作，客户端应分别展示预览和明确确认。

### 七襄条目执行控制（0.9）

鉴权后的 `/health` 提供 `weave_control_protocol: 1`。归档的 `run_id` 不具有执行控制权：
每次真实子执行实例都有独立 `execution_id`；首次执行与自动补充执行分别为
`execution_phase: primary | follow_up`，换代生成新标识。排队但尚未创建实例的条目不能插话或停止。

- `POST /session/{sid}/weaves/{batch_id}/items/{item_id}/messages`：
  `{execution_id, text}`，非空文本最多 8000 字符。
- `POST /session/{sid}/weaves/{batch_id}/items/{item_id}/stop`：`{execution_id}`。
- `GET /session/{sid}/weave-operations/{operation_id}`：只读核对已登记操作，包括旧代操作。

两个 POST 必须提供 `Idempotency-Key`（最多 200 字符），拒绝额外字段。服务验证当前会话、
批次、条目与仍存在的精确实例；不接受路径、归档句柄或自动转投下一代。同键同体重复先返回原回执，
即使实例已结束或服务重启也不重新执行；同键异体返回 409。新操作返回 202，重复返回 200：

```json
{
  "operation": {
    "operation_id": "...", "kind": "message",
    "batch_id": "...", "item_id": "...", "execution_id": "...",
    "text": "请核对边界", "status": "queued",
    "created_at": 0, "updated_at": 0, "reason": "",
    "persisted": true, "warning": ""
  },
  "duplicate": false
}
```

时间为服务时间戳；内核后续观察的 `updated_at` 可以为 ISO 8601 文本。
`GET` 返回相同 `operation`，不带 `duplicate`。`persisted` / `warning` 位于 operation 内。

消息只有实际进入该实例上下文才从 `queued` 变为 `accepted`；`accepted` 不代表模型已经回答。
实例退出前未消费的消息为 `not_consumed`，不会移到补充执行或重试的新实例。
停止为 `requested → settled`：后者只说明实例执行结束，不能据此断言一定因这次停止而中断。
停止单个条目会阻止该条目的自动补充执行，其他条目继续；父轮停止仍停止整批。

普通新消息必须先将 `reserved` 回执可靠写入会话日志再产生作用。预留失败返回 503，当前进程保留
`unknown` 回执，不盲目重试；执行结果保存失败同样明确标未知。重启只恢复终态事实，未决操作为
`unknown`（含 `recorded_status`），不会自动重放。恢复的历史实例一律不可控制。
每会话最多 1000 条普通控制记录；停止不受消息额度阻挡，但每个真实执行实例只能登记一次停止，
再次使用不同键请求停止会被拒绝，原键仍可核对。

日志损坏、缺失或保存失败时，新消息禁用；对本进程仍存在的精确实例仍可请求安全停止。
此时回执明确 `persisted:false` 和警告，只有进程内去重，不承诺重启后可核对。此例外不能创建新任务、
不能控制历史或已换代实例，也不改变审批权限。审批拒绝始终可以完成。

`GET /session/{sid}/weaves` 的条目附 `execution_id`、`execution_phase`、`execution_state`
（`starting | running | settled`，重启未决为 `unknown`）、`stop_requested`、`live`、`recovered`，
以及 `controls: {can_message, can_stop, reason, message_reason, stop_reason}` 与当前代 `operations`。
按钮应分别遵循 `can_message` / `can_stop`，不能仅凭顶层持久化状态禁用安全停止。
`execution_workspace` / `execution_model` 表示该实例的实际位置和模型；条目原有
`workspace` / `model` 仍表示批次结果来源，避免补充执行覆盖原成果的归属。
执行观察通过独立 `qixiang.execution.updated` 事件按实例及递增 revision 持久恢复；晚到的批次快照
不会覆盖已知实例事实。批次 `source_run_id`（兼容 `run_id`）固定为最初父轮，不随之后会话轮次变化。
只保留最近 64 批导致的 `truncated:true` 与日志完整性分别表示，正常截断不冒充保存失败。

条目审批身份为 `{kind:"qixiang", batch_id, item_id, execution_id, label, workspace}`，
只提供单次 `allow` / `deny`，不接受会话授权、改参数或作用于宿主的授权范围。
审批仍经过原有 deny、模式、沙箱、写入保护与工具 hooks；条目停止、父批停止或会话关闭会唤醒
对应审批并拒绝执行。关闭给父轮与成员最终观察一个有界排空窗口，超时不会伪造终态。

### 执行所有权（桌面协议 0.11）

`health.execution_owner_protocol = 1` 表示服务对持久状态目录和每个会话日志取得独占写锁。
状态目录锁在整个服务生命周期内持有；同一目录的第二个服务不能启动。
新建与恢复会话均要求日志锁成功，不降级为无锁写入。恢复失败的会话清单保留，
`health.recovery_errors` 以固定 `session_id/code/reason` 记录缺失来源或恢复失败。
普通 CLI 的历史复制与续接策略不变。

`status.writable` 与 `write_blocked_reason` 表示当前能否开始新的写入。
失去状态目录锁、日志锁或可靠日志，禁止新任务、结构化命令、成员消息、候选采纳、
允许审批、插话、回答问题及预算调整；已记录的同键回执仍可查询。
拒绝审批、取消问题、停止和关闭仍可用。精确成员停止在持久化不可用时返回
`operation.persisted: false` 和 `operation.warning`，不能据此承诺重启后幂等。

异步任务取消不代表底层执行线程已经退出。发生无法证明全体执行线程已结束的中断后，
该会话禁止新写；关闭服务也保留它的日志与状态目录锁直到进程退出。
`health.shutdown_requires_process_exit` 和 `ownership_note` 明确提示这一状态。
同一服务内其他正常会话仍可工作；完全空闲且无上述不确定性的服务关闭会正常释放锁。
嵌入宿主遇到保留锁状态时须退出服务所在进程，再恢复持久会话，不能在原进程强行接管。

普通轮次停止后，服务收到实际父执行线程返回的 `RunCompleted`，且该轮没有调用委托工具、
没有存活的协作成员线程时，会确认已收尾并允许继续会话或调整预算。成功完成的旧委托
不会使下一轮普通停止永久不可用；当前轮委托中断仍无法确认子执行收尾时才保留不确定状态。

### 桌面偏好、能力与会话整理（应用私有）

以下 `/desktop/*` 端点由桌面宿主装配，不属于公开 REST schema；均要求同一 bearer 令牌，响应禁止缓存。

- `GET /desktop/preferences` 返回 `revision, values, options, effective, scope, write_target, warning`。
  `values` 包含 `default_model`、`default_mode`（空串跟随服务）、IANA `timezone`、五类通知偏好，
  以及外观/发送键/导航/默认工作方式/会话排序/`session_meta` 的 `ui` 配置。
  `session_meta` 最多 500 项，值为 `{group, read_seq}`，只记录桌面分组和阅读进度。
  模型/模式的有效来源明确为桌面偏好、显式服务选项或核心配置；模型列举不触发远端或本地模型发现。
- `POST /desktop/preferences {revision, values}` 以完整值和预期版本保存；冲突 409，所有权或存储失败 503。
  写入桌面专有偏好文件，不改核心环境、正在运行的模型/权限或已有工具授权。新建会话时客户端把
  默认模型和模式显式传入原有 `POST /session`，服务仍执行原来的权限约束。
- `GET /desktop/capabilities?session_id=...` 返回实际加载的技能、已安装插件的公开描述和工作方式。
  未选会话时仅列用户来源的已安装技能，不能声称项目技能已受信任或已加载。
  插件中的连接声明不等于已连接/授权；不返回连接环境变量或请求头值。
- `POST /desktop/sessions/{sid}/skills/reload {}` 要求匹配 `X-Xiaoyu-Instance`，经过现有会话管理空闲门，
  调用核心显式技能重载。核心关闭技能或未信任项目的规则保持不变，不产生模型请求或工具授权。
  插件安装沿核心 `xiaoyu plugin add <来源>` 的审阅流程；能力页不在后台下载安装代码。
- `POST /desktop/sessions/{sid}/title {title}` 保存 1–160 字符的标题；只允许可写且父轮空闲的会话，
  先保存清单再更新内存，重启恢复。相同标题重复提交不重新写入。
- `GET /desktop/sessions/{sid}/export?format=markdown|json` 返回 `{filename,mime_type,content,warning}`。
  只导出所选注册会话的用户/助手原始正文，不包含 system、工具秘密参数或配置。
  在请求时固定日志边界，执行中拒绝导出；超过 16 MiB 的原始日志提示使用核心导出命令。
  压缩不替代原正文，附件以文本提示保留，不把文本导出称为附件归档。
- 会话分支复用 `POST /session {fork_from:{session_id,turns?}}` 和创建幂等键。
  新日志、新会话，不改变来源；它复制当前可恢复模型上下文，完整旧时间线仍在来源会话，
  不承诺把过去所有压缩前正文作为新模型上下文。

`GET /desktop/providers` 返回核心用户配置中自定义服务商与进程环境的有效公开字段：
`revision` 为文件摘要；每项含 `id,base_url,models,protocol,has_key,saved_has_key,source,saved,endpoint_hidden`。
端点含用户信息/查询/片段时隐藏；密钥绝不回显。`POST /desktop/providers` 接受
`{revision,provider:{id,base_url,models,protocol,api_key_action,api_key?}}`，协议仅 `chat/responses/anthropic`，
密钥操作为 `keep/set/clear`。固定写核心用户 `.env`，沿核心 dotenv 编解码保留无关配置，
以文件摘要检测编辑冲突、独立普通文件检查、0600 临时文件及写前/替换前所有权闸保护。
不改进程环境或已实例化的路由；响应明确 `scope:after_service_restart`。重启后的真实环境、
项目配置或服务显式选项仍可能优先，保存成功不能冒充当前模型路由已变化。

### 桌面日历自动化

既有单次与间隔调度兼容。自动化定义的 `schedule` 可选
`{kind:"calendar", timezone:"Asia/Tokyo", time:"09:00", weekdays:[0,1,2,3,4]}`，
星期一为 0、星期日为 6。采用 IANA 时区按当地日期逐次计算；夏令时缺失时刻跳过，
重复时刻只取第一次。停机错过的周期不补跑；运行预留与未知结果仍沿原持久化/人工核对协议，
不重叠提交，不自动重放。

自动化 GET 附 `calendar`（各任务最多八个未来时间戳）、`schedule_policy` 和最多 100 条
带稳定 `id` 的结果通知记录；这是应用内可呈现的数据，不代表系统通知已经送达。
从会话创建自动化只形成可编辑草稿，必须保存禁用配置，再独立启用。
服务只在应用打开期间调度，没有安装操作系统后台守护任务。

### 桌面用量与数据管理

`GET /desktop/usage` 从注册会话的线程安全用量快照返回 `sessions,by_model,totals`，
调用次数、输入和输出 token 按实际 provider/model 路由累计。`scope:registered_session_counters`
明确不含未登记会话，也不补齐提供方未返回的用量；`cost_available:false`，不推算账单。

`GET /desktop/data` 统计宿主固定声明的受管目录/文件，不接受客户端路径、不跟随符号链接，
每个统计范围最多检查 10000 个条目，截断和读取失败分别明确。返回 `storage,index,revisions,backup_scope`。
`POST /desktop/data/clear-index {}` 仅清内存搜索索引，与正在进行的索引重建串行；下一次搜索可重建，
不删除会话、页面或成果。真正失去应用状态所有权时禁止管理写入。

`GET /desktop/data/backup?kind=preferences|workspace` 导出单组件 JSON，固定结构
`{schema:"yuan-metadata-v1",kind,values}`。偏好含用户界面分组与新会话默认值；项目元数据仅含
`projects` 的上下文和 `sessions` 的固定/归档状态。可能包含个人项目说明，界面在导出前说明范围。
不导出完整会话正文、执行回执、内容库页面、成果文件、连接凭据或核心 `.env`。

`POST /desktop/data/import {kind,revision,backup}` 使用当前组件修订号进行 schema 校验和原子恢复，
备份不携带可生效的旧修订号，不接受额外 `outbox`/授权/路径字段；每次只恢复一个组件，
避免多文件部分写入冒充整体成功。界面必须先检查备份、显示组件与新会话默认模式，
再明确确认替换。未知回包不自动重试，先读取核对；导入不启动任务、不回放历史或授权。

桌面 `ui.shortcuts` 保存五个动作的完整映射：`new_task/search/settings/sidebar/panel`，
默认分别为 `mod+n/mod+k/mod+,/mod+b/mod+shift+b`。空串关闭单项；支持 Mod 与可选 Shift 加
字母、数字、逗号、句点或斜杠，系统退出/关闭/编辑与发送组合保留。保存前大小写规范化，
重复组合与非法动作拒绝，不涉及核心 TUI 按键表。旧偏好缺此字段时仅在读取结果补默认，
不重写旧文件或增加修订号；下一次用户明确保存才持久化。侧栏和工具面板快捷键在文本编辑区
不触发，输入法组合键和重复按键不触发全局动作。
