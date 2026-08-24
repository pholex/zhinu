# 嵌入面：把小羽当库用

> 面向"把 xiaoyu 当执行引擎嵌进自己进程"的宿主：聊天机器人常驻进程、内部平台、
> 行业 agent 的壳。三张脸的选型见 [platform.md](platform.md)；本文只讲
> **Python 进程内嵌入**这一张，以及跨语言时的 `--wire` 协议。

## 稳定性承诺

**`xiaoyu.__all__` 列出的名字就是公开 API**——从顶层 `import xiaoyu` 拿到的
对象，按下面的规则维护；其余模块路径（`xiaoyu.agent`、`xiaoyu.tools`、……）
是内部实现，随时可能重构，宿主别吃。

- **版本号**：0.x 阶段，**minor 版本承担 semver 的 major 语义**（0.45 → 0.46
  可以含破坏性变更），patch 版本只修 bug、不改契约。
- **弃用期**：公开面的破坏性变更（删名字、改签名、改返回形态语义、改事件既有
  字段）**先在一个 minor 版本里发 `DeprecationWarning` 并在发版说明写明替代
  写法，至少隔一个 minor 版本再移除**——0.45 弃用，最早 0.47 拿掉。
- **不算破坏性**：新增导出名、新增可选参数、`RunResult` / 事件新增字段、新增
  事件 `kind`。消费事件流的宿主对不认识的 `kind` 跳过即可（前向兼容）。
- **不在承诺范围**：内部模块路径、`Agent` 的下划线属性、CLI 输出文案、system
  prompt 内容、工具 schema 细节、会话 JSONL 的逐字段格式（`load_messages` 是
  读它的公开入口，格式本身不是契约）。

导出表在 `xiaoyu/__init__.py` 的 `_EXPORTS`；`tests/test_public_api.py` 锁住
"表里每个名字可导入、本文清单与表双向一致、`import xiaoyu` 不拖起内核"三件事。

## 最小可用示例

```python
from pathlib import Path
import xiaoyu

xiaoyu.load_dotenv()                                  # 用户级 ~/.config/xiaoyu/.env 等，已有环境变量不覆盖
config = xiaoyu.Config.from_env(workspace=Path("/srv/repo"))

def approve(name: str, args: dict):
    """审批回调：在工作线程里被同步调用。返回形态见下节。"""
    if name == "bash" and args.get("command", "").startswith("git push"):
        return xiaoyu.Deny("不许推远端")                # 理由回灌模型 = 改指令
    return True

agent = xiaoyu.Agent(config, approver=approve)
result = xiaoyu.measured_send(agent, "把 README 的安装一节翻成英文")
print(result.text)          # 本轮交付正文
print(result.usage)         # 本轮 usage 增量：turns / prompt_tokens / completion_tokens / by_model
print(result.stopped)       # done / turn_cap / budget / interrupted
```

同一个 `Agent` 实例可以被反复 `measured_send()`，上下文自动接上——常驻宿主
不必每轮重建。不注入 `approver` 时默认**全放行**（等价 `--yolo`），只在
可信环境这么用。

宿主自己是 asyncio 程序（飞书 bot、FastAPI 服务）时用 `AsyncAgent`，
它把同步内核放进工作线程，不堵事件循环：

```python
import asyncio
import xiaoyu

async def approve(name: str, args: dict):
    card = await send_approval_card(name, args)       # 可以 await 任何依赖事件循环的东西
    return await wait_for_click(card)

async def main() -> None:
    loop = asyncio.get_running_loop()
    agent = xiaoyu.Agent(
        config,
        approver=xiaoyu.AsyncApprover(approve, loop, timeout=90),   # 超时/异常 = 拒绝（fail closed）
    )
    async_agent = xiaoyu.AsyncAgent(agent)
    result = await async_agent.send("用户第一句话")
    await async_agent.send("用户第二句话")             # 同一实例，上下文接上
    async_agent.interrupt()                            # 任意时刻、任意线程可调，非阻塞
    async_agent.recycle()                              # 清对话重开，对象不重建
```

⚠️ 把 `async def` 直接传给 `Agent(approver=...)` 是错的：传进去的是没被
await 的协程对象，被当真值 = 永远批准。异步审批必须经 `AsyncApprover`。

## 审批契约

`approver(tool_name, args)` 在**工作线程**里同步调用，返回：

| 返回值 | 含义 |
|---|---|
| `True` | 批准 |
| `(True, "附言")` / `Allow(note="附言")` | 批准，附言随工具结果回灌模型 |
| `Allow(updated_args={...})` | 批准并**整体替换**本次参数（宿主"包沙箱再放行"的通道） |
| `False` / `""` / `None` | 拒绝 |
| `"理由"` / `(False, "理由")` / `Deny("理由")` | 拒绝并把理由回灌模型——拒绝即改指令 |

规则：`Allow.updated_args` 改写后的参数**仍要过一遍 deny 规则**（deny 的
bypass-immune 承诺对审批改写同样成立）；`normalize_verdict()` 是把这些形态归一
的公开函数，宿主要在自己这边先做一层裁决时可复用。

审批之前还有两道闸，宿主可各自注入：

- **权限规则** `Permissions`：`allow` / `deny` 规则（`parse_rule("deny bash(git push*)")`），
  `Permissions.load(workspace)` 读用户级 + 工作区级规则文件，或
  `Permissions(workspace, rules=[...])` 显式构造。deny 规则任何模式下都不可绕过，
  命中 allow 的调用不再问 approver。`suggest_allow_rule` / `banned_allow_reason` /
  `user_rules_path` / `workspace_rules_path` 是"把用户这次的放行记成常批"要用的
  一组助手。
- **模式** `agent.set_mode("default" | "auto" | "plan")`：auto 档工作区内改文件、
  沙箱内跑命令免确认；plan 档只读规划。

`ask_user` 工具（模型向用户提选择题）走同款注入通道 `Agent(asker=...)`，签名见
`Asker`；不注入时工具不进 schema，模型看不见它。

## 事件消费

两条路，别同时依赖：

**① `stream()`**——逐事件驱动一轮，最后一个事件永远是 `RunCompleted(result=RunResult)`：

```python
import contextlib

async with contextlib.aclosing(async_agent.stream("任务")) as events:
    async for event in events:
        match event:
            case xiaoyu.TextDelta(text=t): ...           # 正文分片
            case xiaoyu.ToolRunning(name=n): ...         # 工具开跑
            case xiaoyu.RunCompleted(result=r): ...      # 收尾：元数据在这
```

提前退出要用 `contextlib.aclosing()` 包住再 `break`——裸 `break` 不会立刻关闭
异步生成器，退出即 `interrupt()` 这条保证只在 aclosing 下成立。`stream()`
进行中会临时接管 sink，事件只进迭代器。

**② `sink`**——构造时注入一个实现 `UISink` 协议（`emit(event) -> None`）的
对象，适合事件常年单向转发的宿主；`Agent.sink` 也可事后替换。

事件词汇（`UIEvent` 子类，`to_dict()` 即 `--output-format stream-json` 与
`--wire` 的线上形态，`kind` 是判别字段）：

| 事件 | kind | 字段 |
|---|---|---|
| `RequestStarted` / `RequestEnded` | `request.started` / `request.ended` | `model` |
| `TextDelta` / `TextEnd` | `text.delta` / `text.end` | `text` |
| `ToolPending` | `tool.pending` | `name`, `args` |
| `ToolPurpose` | `tool.purpose` | `name`, `purpose` |
| `ToolRunning` | `tool.running` | `name`, `args` |
| `ToolCompleted` | `tool.completed` | `name`, `output`, `ok`, `seconds` |
| `ToolDenied` | `tool.denied` | `name`, `by`（`rule` / `user`） |
| `SteerAccepted` | `steer.accepted` | `text` |
| `PlanUpdated` | `plan.updated` | `plan`, `explanation` |
| `Notice` | `notice` | `text`, `level`（`info` / `warn` / `error`） |
| `RunCompleted` | `run.completed` | `result`（仅 `stream()` 产出） |

不变量：每个 `tool.pending` 最终恰好收到一个终态（`completed` 或 `denied`）；
每个 `request.started` 恰好对应一个 `request.ended`。

## 单轮结算 `RunResult`

`measured_send()` / `AsyncAgent.send()` 返回；`stream()` 在 `RunCompleted.result` 里给：

| 字段 | 含义 |
|---|---|
| `text` | 本轮最后一条 assistant 正文（只在本轮新增消息里取，不会把上一轮交付误报成本轮） |
| `usage` | **本轮增量**：`turns` / `prompt_tokens` / `completion_tokens` / `by_model`（按 `provider/model` 分列，含子 agent 与摘要调用） |
| `duration_seconds` | 墙钟耗时 |
| `context_tokens` | 跑完后的上下文水位，宿主据此决定何时 `recycle()` |
| `interrupted` | 被 `interrupt()` 收掉为 `True`——打断是宿主自己的动作，不以异常弹回 |
| `stopped` | `done` / `turn_cap` / `budget` / `interrupted` |
| `output` | 带 `output_schema` 跑的那一轮，模型交回的结构化对象；否则 `None` |

真正的错误（网络、配置、bug）照常抛出；`Interrupted` 只在直接调 `Agent.send()`
时会看到，经 `measured_send` 已吸收。

**结构化收尾**：`measured_send(agent, task, output_schema={...JSON Schema...})`
（`AsyncAgent.send/stream` 同参）只对这一轮挂 `structured_output` 工具，结果在
`RunResult.output`；轮末自动撤掉，下一轮不受影响。

## 会话：resume / recycle / 落盘

- **落盘**：`Agent(session_log=xiaoyu.SessionLog.create(model, str(workspace), directory=自己的目录))`。
  常驻宿主**显式传 `directory`**，与用户 CLI 手动会话彻底隔离；缺省落在按工作区
  分区的公共目录。
- **resume**：重启后 `messages = xiaoyu.load_messages(path)`，
  `agent.restore(messages, source=str(path))`——与 CLI `xiaoyu resume` 同一条路径；
  默认把历史复制进新会话文件（新文件自包含），`copy=False` 表示续写历史所在的
  那个文件，只接上下文不再抄一遍。`list_sessions(workspace=...)` 列可续的会话。
- **recycle**：`AsyncAgent.recycle()` / `Agent.reset()`——清对话重开，对象 /
  registry / config 都不重建，trace、已加载技能等状态一并归零。
- **打断与插话**（线程安全、非阻塞，任意线程可调）：`interrupt()` 在下一个 chunk
  边界收尾、半截话入历史；`steer(text)` 让模型在下一个 step 边界看到这句话再继续
  （本轮已结束时下一次 `send()` 开头丢弃，要保留用 `agent.drain_steers()`）；
  `notify(text, key="")` 投递系统通知，搭下一条工具结果送达、同 key 只送一次。
- **并发纪律**：同一个 `Agent` 同时只能有一轮在跑；`AsyncAgent` 的每轮入口会
  先等上一轮工作线程真正收尾，宿主不必自己等。要并发就开多个实例。
- `Agent.messages` / `Agent.usage` / `Agent.trace` 都持久在实例上，宿主随时可读。

## 工具面：`Toolbox` 与 MCP

`Agent(config, toolbox=xiaoyu.Toolbox(config))`；不传就按 config 构造。
`Toolbox(config, only=[...])` 得到受限子集；宿主把业务动作包成 MCP server 挂上去
（`Toolbox(mcp_view=...)`）的做法见 [platform.md](platform.md)「把业务动作交给 agent：宿主自有工具」与 [examples/ops-console](../examples/ops-console/)——那一层的
`ServerSpec` / `launch_specs` 暂**不在**冻结面里，用时按 platform.md 的写法走。

`Config` 的 `enable_skills` / `enable_agents` / `enable_hooks` / `enable_plugins` /
`enable_mcp` / `enable_explore` 开关决定内核挂哪些能力；嵌入宿主要"行为确定"
（测试、审计）时逐个关掉。

## 跨语言：`--wire` 的 JSON-RPC 契约

宿主不是 Python 时，起 `xiaoyu --wire` 子进程，stdin/stdout 一行一条 JSON-RPC 2.0
（协议 version `1.0`）。**单进程、无常驻 server、无 HTTP**。

client → server（request，带 `id`）：

| 方法 | 参数 | 返回 |
|---|---|---|
| `initialize` | `{}` | `{protocol_version, server:{name,version}, model, workspace, messages}`（`messages` = 已在场的历史条数） |
| `prompt` | `{text}` | 整轮结束后 `{status, result, usage}`，`status ∈ finished \| interrupted \| error`；一次只跑一轮，忙时错误码 `-32000` |
| `steer` | `{text}` | `{}` |
| `cancel` | `{}` | `{}`；打断当前轮并把挂起审批全按拒绝解决 |

server → client：

- **事件**（notification，无 id）：`{"method":"event","params":{"kind":"text.delta",...}}`，
  `params` 就是上表事件的 `to_dict()`。
- **审批**（request，带 id，等响应）：
  `{"id":"srv-1","method":"request","params":{"type":"approval","payload":{"name":"bash","args":{...}}}}`，
  client 回 `{"id":"srv-1","result":{"verdict":"allow"|"deny","note":…,"reason":…,"updated_args":{…}}}`
  ——语义与 `Allow` / `Deny` 完全对齐；回包缺失或畸形一律按拒绝处理。

EOF / 连接关闭：挂起审批全部拒绝、打断当前轮、等工作线程收尾。
`--session-id` 让子进程续上命名会话。完整说明见 `xiaoyu/wire.py` 模块 docstring，
黑盒用例在 `tests/test_e2e_wire.py`。

## 公开面清单

`xiaoyu.__all__`，按用途分组（改表必须同步本清单）：

- **内核对象**：`Agent`、`AsyncAgent`、`Config`、`Toolbox`
- **审批 / 提问**：`Allow`、`Deny`、`Approver`、`Asker`、`AsyncApprover`、`normalize_verdict`
- **单轮结算**：`RunResult`、`RunCompleted`、`measured_send`、`Interrupted`
- **事件**：`UIEvent`、`UISink`、`RequestStarted`、`RequestEnded`、`TextDelta`、
  `TextEnd`、`ToolPending`、`ToolPurpose`、`ToolRunning`、`ToolCompleted`、
  `ToolDenied`、`SteerAccepted`、`PlanUpdated`、`Notice`
- **会话**：`SessionLog`、`load_messages`、`list_sessions`
- **权限**：`Permissions`、`Rule`、`parse_rule`、`suggest_allow_rule`、
  `banned_allow_reason`、`user_rules_path`、`workspace_rules_path`
- **配置**：`load_dotenv`、`user_env_path`、`MissingConfig`、`MissingApiKey`
- **版本**：`__version__`

`Agent` 上属于契约的方法与属性：`send` / `interrupt` / `steer` / `drain_steers` /
`notify` / `reset` / `restore` / `set_output_schema` / `set_mode` / `switch_model` /
`set_budget_tokens` / `context_tokens` / `last_assistant_text`，以及
`messages` / `usage` / `trace` / `sink` / `approver` / `structured_output`。
`AsyncAgent` 上：`send` / `stream` / `interrupt` / `steer` / `notify` / `recycle`
（`reset` 同义）/ `restore` / `agent`。
