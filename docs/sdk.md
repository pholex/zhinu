# Python SDK

`xiaoyu-agent-sdk` 是与 `xiaoyu-agent` 同仓库、同版本的独立发行包，导入名为
`xiaoyu_agent_sdk`。SDK 在宿主进程里调用现有执行内核，不启动 CLI 子进程。
旧 `import xiaoyu` 和 CLI 保持兼容。本页覆盖 0.59.0 起的平台接口及当前源码扩展。
`SessionOptions.asker` / `question_timeout`、类型化输入与插话、通知与状态观察、会话控制、可选计划工具为当前源码新增、尚未发布的接口；使用它们
需安装当前源码。工具结果变换 `result_transforms`、持久化待答 `questions` 同样尚未发布。平台扩展不包含在 0.58.0 中。

## 安装与首次调用

源码开发安装（Python 3.11+）：

```sh
python -m pip install -e '.[sdk]'
python -m pip install -e packages/xiaoyu-agent-sdk
```

SDK 精确依赖同版本的 `xiaoyu-agent[sdk]`，不安装 TUI、serve 或 browser extras。
发布后用户只需安装 `xiaoyu-agent-sdk`。两个 wheel 分别拥有 `xiaoyu/` 和
`xiaoyu_agent_sdk/`，没有覆盖关系。

```python
import os
from pathlib import Path
from xiaoyu_agent_sdk import ModelOptions, SessionOptions, run

options = SessionOptions(
    model=ModelOptions(
        model=os.environ["SDK_MODEL"],
        api_key=os.environ["SDK_API_KEY"],
        base_url=os.environ.get("SDK_BASE_URL", "https://api.openai.com/v1"),
    ),
    workspace=Path.cwd(),
)
result = run("概述这个项目", options)
print(result.text, result.stopped, result.usage)
```

## 公开契约与配置

| 入口 | 用途 |
|---|---|
| `run` / `run_async` | 一次性调用，自动关闭会话 |
| `Session` / `AsyncSession` | 多轮 `run`、`stream`、`interrupt`、`close` |
| `ModelOptions` | 显式模型、凭据、端点、协议、超时；可借用同步兼容 client |
| `SessionOptions` | 工作区、工具、审批、扩展、会话目录与预算 |
| `Tool` / `ToolResult` | Python 业务工具和可识别的工具失败 |
| `ResultTransform` / `ToolOutput` | 截断和 recall 落盘前的工具返回文本处理（当前源码新增） |
| `QuestionOptions` / `QuestionAnswer` | SQLite 持久化待答，显式提交、安全边界接纳（当前源码新增） |
| `Hook` / `HookDecision` | Python 生命周期回调 |
| `Asker` | 宿主提供的同步／异步澄清问题回调（当前源码新增） |
| `TextBlock` / `ImageBlock` / `Prompt` | 类型化文字和图片输入（当前源码新增） |
| `SteerAccepted` | 插话已进入会话上下文的事件（当前源码新增 SDK 导出） |
| `Notification` / `SessionSnapshot` | 不可变的待通知与会话展示快照（当前源码新增） |
| `PlanUpdated` / `ToolPurpose` | 内核计划与调用目的事件类型（当前源码新增 SDK 导出） |
| `McpServer` / `McpServerStatus` | 静态 MCP 清单与状态快照 |
| `Subagent` / `Plugin` | 子 Agent 隔离与显式 Python 插件选择 |
| `RewindResult` | 回滚状态、处理文件与冲突清单 |
| `OutputSpec` | 严格结果 schema 与修复次数 |
| `RunResult` / `RunCompleted` | 最终结果与流终结事件 |
| `list_sessions(directory)` / `SessionInfo` | 显式目录中的本地会话索引 |

SDK 不调用 `Config.from_env()`，不自动加载 `.env`、用户权限、插件、hooks、MCP 或
子 Agent 配置。项目 AGENTS.md 需 `load_project_instructions=True`；技能只扫描
`skill_directories` 显式列出的目录。SDK 不改进程 cwd、环境变量或全局默认配置。
`tool_env` 仅传给工具子进程。内置工具仍使用宿主操作系统环境、共享内核的沙箱与
临时目录机制；工作区配置不是宿主 Python 回调的安全隔离。

`protocol` 支持 `chat`、`responses`、`anthropic`。SDK 创建的 HTTP client 不读取
代理环境变量；需要自定义代理/传输时传 `ModelOptions.client`。这是借用的同步
OpenAI 兼容接口，宿主负责它的关闭及跨会话线程安全。SDK 不自动读其他模型的 key，
摘要沿用显式主模型。凭据字段不进配置 repr。

`builtin_tools=None` 使用共享内核的内置工具集合；元组则限定工具箱中的内置工具，
空元组适合纯业务调用。内核仍可能提供上下文管理工具，技能/子 Agent/业务工具按
显式配置另行挂载。此字段不等价于操作系统权限白名单。

## 生命周期、并发和流

```python
from contextlib import aclosing
from xiaoyu_agent_sdk import AsyncSession, RunCompleted, TextDelta

async def handle(options):
    async with AsyncSession(options) as session:
        async with aclosing(session.stream("分析问题")) as events:
            async for event in events:
                if isinstance(event, TextDelta):
                    print(event.text, end="")
                elif isinstance(event, RunCompleted):
                    print(event.result.stopped)
        return await session.run("继续")
```

一个会话只执行一轮；重叠提交抛 `SessionBusyError`，不会隐式中断或排队上一轮。
不同会话可并行。异步会话绑定首次使用的事件循环。事件复用内核的类型与 `kind`，
成功或正常停止的流最后产生一个 `RunCompleted`；模型/内核异常从迭代器抛出。
事件队列默认容量 128，慢消费者向执行线程施加背压。文本片段不是已验证的业务结果。

`ToolPreparing`（`tool.preparing`）提前报告工具参数生成进度：`index` 是当前请求内
的调用序号，`argument_chars` 是累计参数 JSON 字符数，`path` 与 `purpose` 是
已收齐的短字段。SDK 附加 session/run/request 关联 ID。用请求 ID 与 index 区分
多个预览，收到 `RequestEnded` 就清除该请求的全部预览；此时工具可能因中断、
参数错误或审批拒绝而不执行。详细边界见[事件契约](embedding.md#事件消费)。

持久化日志可用[会话诊断](session-inspection.md)只读查看。
完整的超时与迟到回答协议另见[设计稿](sdk-deferred-questions-design.md)，SQLite
安全边界接纳及版本观察已实现，见[持久化待答](sdk-deferred-questions.md)。下文的基础
`asker` 支持同步／异步宿主回调，但本轮会等待其完成，
超时不会创建待答队列或在稍后自动接纳答案。

同步提前退出用 `contextlib.closing`，异步用 `aclosing`。单纯 `break` 不保证 Python
立即关闭生成器。中断是协作式的：保留历史、补齐未闭合的工具消息，之后可继续。
取消 `AsyncSession.run()` 会请求中断并等待本轮和回调清理，然后传播 `CancelledError`。

会话拥有 worker、内置工具后台任务、自己创建的 HTTP clients、MCP manager 和本地
日志锁；业务回调与借用的 client 由宿主提供。`close()` 可重复调用。无法及时结束
模型请求或 Python 同步回调时抛 `CloseTimeoutError`，`closed` 仍为 false；会话进入
closing 状态，拒绝新任务，宿主应等待后重试关闭。Python 线程无法强制停止宿主任意
代码。资源收尾在会话 worker 中执行；每个关闭阶段最多等待 `close_timeout`，所以它
不是整个 `close()` 的总耗时上限。超时后保留正在清理的 future；再次关闭等待同一任务。
MCP 进程/读线程、HTTP 请求/取消通知线程、管道关闭线程、启动/服务线程或后台
任务 watcher 未结束时，继续保留资源引用和
日志锁，不报告 `closed=True`。底层关闭异常以 `SessionStorageError` 和 cause 交给宿主。
宿主异常退出、任意第三方插件自行创建的资源不在这项正常关闭保证内。

## 审批、工具和 hooks

```python
from xiaoyu_agent_sdk import Allow, Tool, ToolResult

async def approve(name, arguments):
    # 在这里等待宿主自己的 UI；返回 Deny("原因") 可拒绝。
    return Allow(updated_args=arguments)

def lookup(order_id: str):
    return ToolResult({"order_id": order_id, "status": "ready"})

lookup_tool = Tool(
    name="lookup_order", description="查询订单状态",
    parameters={"type": "object", "properties": {"order_id": {"type": "string"}},
                "required": ["order_id"], "additionalProperties": False},
    handler=lookup,
)
```

`Tool.requires_approval` 默认 true。没有 approver、审批抛异常或超时都拒绝执行。
可以返回 `Allow`、`Deny`、bool、拒绝理由字符串或 `(bool, reason)`；参数改写仍经过
内核 deny 检查。`deny_rules` 只接受 `deny ...` 规则，优先于审批批准。
只读工具可显式声明不审批，内核的工作区边界检查仍生效。

同步回调在会话拥有的线程池执行；`async def` 回调由 `AsyncSession` 调度到宿主事件
循环。超时/取消会取消异步任务；同步回调可能仍在执行，会话保留其所有权，直到结束。
工具参数按声明 schema 校验，不自动转换类型。字符串结果原样返回，JSON 可序列化
结果转成 JSON；`ToolResult(is_error=True)` 回灌工具错误。未捕获的业务异常被转换
为不含异常正文的 `ERROR: Host tool failed`，模型可以调整后续操作。

基础 Python hook 事件是 `PreToolUse`、`PostToolUse`、`UserPromptSubmit`、`Stop`。
callback 收到带 `event` 的 payload，返回 `HookDecision(blocked, reason)`。
Pre/User 的 block 阻止执行，Post 的 block 反馈给模型但不撤销副作用，Stop 最多顶回
一次。hook 故障按 block 处理。`tool_name` 可选精确匹配，不使用 shell hooks。
工具类事件的 payload 带 `tool`、`args` 与 `call_id`（同一次调用的 Pre / Post / ToolFailed
同值，宿主靠它把前后对上）；Post 与 ToolFailed 另带 `ok`、`output`。
平台接口还支持会话、子任务、工具失败与压缩前后的事件；完整清单及通知异常语义见
[生命周期和 OpenTelemetry](sdk-platform.md#生命周期和-opentelemetry)。

`McpServer` 显式提供 stdio command/args/env 或 HTTP url/headers；会话启动 manager，
工具异步发现，走同一权限和审批路径。`session.mcp_status()` 返回
`tuple[McpServerStatus, ...]`，只含名称和状态，不含服务端错误正文或凭据。
常见状态为 loading/cached/ready/failed/blocked/closing/closed；调用方需容忍新增状态。
SDK 为每个会话分配独立的临时 MCP 缓存、工具声明记录和日志目录，不读取或修改 CLI
的这些状态；服务完全关闭后才清理目录。因此重启会话会重新发现服务与工具声明。
动态增删、启停、重连、OAuth 与宿主借用 `McpPool` 已在平台接口提供，见
[动态 MCP 与 OAuth](sdk-platform.md#动态-mcp-与-oauth)。

`Subagent` 使用内核委托工具，限定内置工具、提示词、模型和迭代数，继承父会话
审批与 deny 规则。`isolation="worktree"` 要求 Git worktree；创建失败就返回工具失败，
模型不能将它降级为 `none`。默认 `none` 共享工作区。worktree 隔离的是工作文件，
不隔离宿主 Python、环境或外部系统。宿主可通过 `session.tasks` 提交有依赖的并行
任务、取消和恢复；父轮次与任务批次互斥，见[依赖任务与恢复](sdk-platform.md#依赖任务与恢复)。

`plugins=(Plugin(name="entry_name", distribution="installed-package"),)` 仅加载指定
发行包内的 `xiaoyu.tools` 入口点，缺失、歧义或工厂失败在构造会话时抛
`ConfigurationError`。已安装但未选择的入口点不会执行。插件沿用已有 Python 插件
工厂契约（Config → 内核 Tool 或其列表），是可信的进程内代码，所创建资源由插件
负责回收。项目插件 bundle 的自动发现仍关闭；普通业务工具优先使用公开 `Tool`。

## 通知与状态观察（当前源码新增）

`session.notify(text, key="", wake=True)` 向主会话投递后台结果或环境变化，空闲与执行中
均可调用，线程安全。两个会话类上都是普通方法，无需 await。
通知不自动启动模型，也不是审批答复；它沿用内核的普通通知通道进入后续工具结果
或步骤边界，不提高输入权威。子任务不会自动消费父会话通知。

`text` / `key` 去除首尾空白，空正文忽略。同一非空 key 在当前内核会话生命周期内
只接受首次投递，重复正文和 wake 值不覆盖首次值；空 key 不去重。
`wake=True` 允许正在运行的轮次为通知再执行一步，受既有预算和迭代限制；空闲时
仍需宿主决定是否开启一轮。`wake=False` 只在已有工具结果或已安排的步骤上捎带，
不会单独为了该通知延长轮次。关闭中／已关闭时拒绝新投递。

`pending_notifications()` 返回 `tuple[Notification, ...]`，每项有 key、text、wake。
这是不可变、非破坏性快照，运行中及关闭后均可查询。
`watch_notifications()` 返回同步／异步观察流：

```python
from contextlib import aclosing
from xiaoyu_agent_sdk import AsyncSession

async def observe(options):
    async with AsyncSession(options) as session:
        async with aclosing(session.watch_notifications()) as changes:
            print(await anext(changes))  # 初始待通知快照
            session.notify("后台报告已生成", key="report-ready")
            print(await anext(changes))  # 当前待通知，不消耗给模型的通知
        result = await session.run("检查已有的后台结果")  # 宿主显式启动
        view = await session.snapshot()
        return result, view
```

观察流从首次迭代时订阅，先给出初始快照；新通知入队、主轮次结束时提示重新读取。
每个订阅只保留一个变化信号，慢消费者收到合并后的最新待通知列表，可能为空；
不保证每条通知的中间状态都被观察到，不能用它作投递审计日志。
通知被内核消费后，查询可立即看见最新状态，观察流在后续上述触发点刷新。
多个观察者互不抢占，不阻塞通知生产者；关闭开始时全部观察流结束，即使资源
关闭稍后超时也如此。取消一个观察者不会取消会话、其他观察者或待通知。
同步流等待变化时阻塞调用线程；异步流在宿主事件循环等待，不额外创建等待线程。
提前停止消费用 `closing` / `aclosing` 释放订阅。

待通知队列、去重 key 与订阅仅在内存中，不随 fork 复制、不跨进程恢复。
已消费通知作为对话内容正常保存；key 去重不是跨重启的恰好一次保证。

`session.status()` 是线程安全、无需 await 的轻量生命周期查询：

| 状态 | 含义 |
|---|---|
| `idle` | SDK 执行槽空闲 |
| `running` | 主轮次、事件流或 MCP 控制操作仍占用执行槽 |
| `tasks` | SDK 子任务批次或其资源尚未结束 |
| `settling` | 主轮次已结束，宿主回调仍在清理 |
| `broken` | 会话持久化已报告故障 |
| `closing` / `closed` | 正在关闭（可能需要重试）／已关闭 |

`idle` 不代表没有后台 shell 活动或待通知。查询不作为提交锁，后续调用仍可能因
另一个宿主动作抢先开始而返回 `SessionBusyError`。

同步 `session.snapshot()`、异步 `await session.snapshot()` 返回不可变 `SessionSnapshot`。
仅在会话空闲、没有待收尾回调时接受；执行中抛 `SessionBusyError`，关闭后抛
`SessionClosedError`，存储已坏抛 `SessionStorageError`。不触发模型请求。

- 会话 ID、当前模型、模式、token 预算、上下文 token 估算及最后一条助手正文。
- `usage` 为累计测得的用量，含共享内核账本中的子 Agent 调用；
  `model_calls` 是计入账本的模型调用次数，不是用户轮次数。每模型明细为不可变元组。
  fork 复制源会话用量基线，恢复读取最后一个有效累计检查点；不从对话文本推算
  未记录的消耗。跨重启请求／费用记录另见 `cost`，详细边界见下文会话控制。
- `history` 为当前上下文的不可变展示投影：role、text、image_count、tool_names、
  tool_call_id。排除 system 消息，不输出图片字节、推理块和原始调用参数；
  不能把它当作完整转录或恢复格式，完整持久化记录仍走 SessionStore／会话日志。
- `pending_notifications` 为读取时的待通知快照；后台通知可独立到达，因此整个
  返回值不是跨所有后台组件的一次全局事务快照。

可运行示例：[observation.py](../examples/sdk/observation.py)。内核能力的接入状态、
替代接口和剩余项目统一见 [接入清单](sdk-kernel-map.md)。`PlanUpdated` 的类型导出
本身不启用工具，需显式配置 `enable_plan=True`；`ToolPurpose` 会在对应工具审批事件流中出现。

## 工具结果文本变换（当前源码新增）

`SessionOptions.result_transforms` 显式注册 `ResultTransform`，按顺序处理完整的工具
返回文本，再进行截断、recall 落盘、事件与历史记录。支持同步／异步回调，SDK
子 Agent 继承；失败扣留原文并停止本次执行，不自动重跑已执行的动作。
默认关闭，每项超时由 `result_transform_timeout` 指定，默认 30 秒。

这与 PostToolUse 的附加反馈是两个契约，也不覆盖工具参数、媒体或工具内部日志。
接口、数据流、异常与关闭语义见 [工具结果文本变换](sdk-result-transforms.md)。

## 可选计划工具（当前源码新增）

`SessionOptions(enable_plan=True, ...)` 启用内核 `update_plan`，默认 False。
它与 `builtin_tools` 独立：即使基础工具设为空元组，也能单独启用任务清单。
启用后 `update_plan` 名称由内核持有，宿主工具／插件同名会抛 `ConfigurationError`；
关闭时保留宿主使用该名称的兼容行为。

```python
from dataclasses import replace
from xiaoyu_agent_sdk import PlanUpdated, Session

with Session(replace(options, enable_plan=True)) as session:
    for event in session.stream("先列出审查步骤，再开始分析"):
        if isinstance(event, PlanUpdated):
            print(event.plan, event.explanation)
    print(session.snapshot().plan)  # tuple[PlanStep, ...]
```

`PlanStep(step, status)` 为不可变视图；status 为 pending / in_progress / completed。
事件中的 plan 是独立副本，宿主修改它不会修改内核计划。异步宿主通过
`async for` 消费事件、`await snapshot()` 查询状态；计划更新不额外调用模型。

任务清单与 `mode="plan"` 的权限模式独立。在三种模式都可更新清单，更新本身
免确认，但 `deny_rules=("deny update_plan",)` 和 hooks 仍能拦截。启用任务清单
不会切换权限模式、批准退出 plan 或授予文件写权限。清单也不创建 SDK DAG 任务，
不会执行步骤；模型填写 completed 只是进度声明，不是测试已通过的证据。

恢复与 fork 从成功调用或压缩后的计划记录重建清单。恢复时工具是否启用仍以
宿主 options 为准：可以只读出历史计划而不开工具。reset 清空清单；对话 rewind
恢复保留历史对应的计划，纯文件 rewind 不改计划。子 Agent 不继承此开关。
清单不等于 plan 模式的 Markdown 计划文件，也不提供宿主直接修改内核计划的接口。

离线示例：[planning.py](../examples/sdk/planning.py)。其他可选能力的选择与验收条件见
[接入取舍](sdk-kernel-map.md#可选能力与部分接入的取舍)。

## 图片输入与运行中插话（当前源码新增）

`run`、`run_async` 与两个会话类的 `run` / `stream` 均接受 `Prompt`：普通字符串，
或由 `TextBlock`、`ImageBlock` 组成的非空 list/tuple。旧字符串调用保持不变。

```python
from pathlib import Path
from xiaoyu_agent_sdk import ImageBlock, Session, TextBlock

with Session(options) as session:
    result = session.run([
        TextBlock("说明这张截图里的问题。"),
        ImageBlock(Path("screenshot.png").read_bytes()),
    ])
```

`ImageBlock(data: bytes)` 接收编码后的 PNG/JPEG/GIF/WebP 文件内容，按文件头识别格式，
每张最多 7 MiB；宿主负责提供有效图片及支持视觉的模型。SDK 不自动下载 URL、
读取路径或创建 CLI 全局媒体缓存。音频、视频、PDF 和原始协议字典暂不在输入契约内。
输入在轮次启动前校验并转换为独立内容块；修改原 list 不会修改当前轮次。
错误输入抛 `ConfigurationError`，不触发会话 hooks、历史写入或模型请求。
流式接口在开始迭代时执行该校验，异步 run 在协程开始执行时校验。

图片以内联 data URL 随历史保存，JSONL、SessionStore 恢复与 fork 不依赖外部缓存文件。
因此会话记录包含图片本身，体积也会增加；宿主负责存储容量与数据保留策略。
hooks 接收文字投影和图片占位标记，不接收图片字节。

运行中的主轮次可以调用 `session.steer("补充约束") -> bool`，同步与异步会话上
都是普通线程安全方法，不需要 await：

- `True` 仅表示已排队；内核在工具批次结束或正文收尾等 step 边界写入用户历史，
  发出 `SteerAccepted` / `steer.accepted`，带原文和 session/run 关联 ID。
  事件表示进入上下文，不保证后续模型请求成功，也不保证模型采纳该要求。
- `False` 表示未排队：主轮次尚未初始化完成、已结束、已请求取消，或文字为空白。
  宿主保留该输入。仅子任务运行时也不接收；初始化可在 `UserPromptSubmit` 回调
  或主轮次第一个 `RequestStarted` 之后确认。关闭中或已关闭抛 `SessionClosedError`。
- 插话只接受文字，去除首尾空白；不打断当前动作、不修改权限、不批准待审工具。
  它不是新轮次提交，也不是基础提问回调的回答通道。
- 等 run 返回或流结束后，用 `session.drain_steers() -> list[str]` 取走没赶上本轮的
  文字，显示在宿主输入框供用户决定下一步。该方法在两个会话类上也都无需 await。
  轮次未结束时抛 `SessionBusyError`；关闭后仍可取回。取回是破坏性读取，重复调用返回空列表。
- 未接纳的文字仅保留在本会话内存，跨轮次保留到宿主取回，但不自动投递下一轮、
  不随 fork 复制，也不跨进程恢复；已接纳的文字作为用户历史正常持久化。

完整同步／异步调用中的图片与插话契约一致。可运行的异步宿主示例见
[inputs.py](../examples/sdk/inputs.py)，持久化待答首版见下节。

## 持久化待答（当前源码新增）

`SessionOptions.questions=QuestionOptions()` 配合 `SQLiteSessionStore` 开启独立
问题服务，与 asker 回调互斥。默认立即返回 pending，可用
QuestionOptions(foreground_timeout_seconds=60) 开启有界等待；超时转 pending，
及时回答也先 queued 再接纳。宿主通过 session.questions
查询并幂等提交回答；运行中的安全 step 边界或下一次显式 run／stream 将 queued
回答接入普通用户历史。get／list_pending／answer／cancel 在 AsyncSession 上需 await，
questions.watch() 使用 async for 观察版本变化。空闲时不自动启动模型，不批准工具。

serve 端点见 [HTTP 持久化问答](serve-questions.md)。前台等待、恢复、reset、fork 及暂不支持的
对话 rewind 边界见 [持久化待答指南](sdk-deferred-questions.md)。

## 宿主提问回调（当前源码新增）

通过 `SessionOptions(asker=..., question_timeout=120.0)` 为内核 `ask_user` 工具接入
宿主界面。未配置时不向模型广告该工具。同步 `Session` 接受同步函数；
`AsyncSession` 还支持在宿主事件循环执行的 `async def` 回调。

`Asker` 接收归一化的问题列表，每题包含 `question`、`options`（每项包含 `label`、
`description`）、`multi_select`。回调返回 `{问题原文: 答案字符串}`，可返回自由文本；
多选答案由宿主合成字符串。SDK 给回调的是独立副本，界面修改不会改写模型的问题。
可省略未回答的问题，返回 `{}` 表示用户明确跳过全部问题。

```python
from dataclasses import replace
from xiaoyu_agent_sdk import Asker, Session

def ask_in_console(questions):
    answers = {}
    for question in questions:
        print(question["question"])
        print(" / ".join(option["label"] for option in question["options"]))
        answer = input("回答（留空跳过）：").strip()
        if answer:
            answers[question["question"]] = answer
    return answers

asker: Asker = ask_in_console
with Session(replace(options, asker=asker, question_timeout=120.0)) as session:
    result = session.run("先询问报告语言和详细程度，再给出报告提纲。")
```

完整离线示例见 [questions.py](../examples/sdk/questions.py)。`question_timeout` 必须为
有限正数，只控制单次问答等待，不与 `approval_timeout` 共用。

回调异常、超时或返回格式错误成为不含宿主异常正文的工具错误，不伪装成用户跳过，
不生成默认答案。未知问题键与非字符串答案被拒绝。中断沿用会话取消机制；异步
回调会收到取消请求，同步回调无法强杀，未结束时仍归会话管理并阻止新轮次；
`close()` 超时不报告已关闭，宿主应解除阻塞后重试。超时后返回的答案会被丢弃。

问题回答只作为普通工具结果回灌，不授予权限；副作用工具仍需独立经过审批与 deny
检查。`fork()` 默认沿用该回调，恢复时宿主重新提供；子 Agent 不自动继承提问界面。
回调对象不序列化，但问题与回答属于对话内容，开启持久化时会进入会话记录。

## 结构化结果与失败

```python
from xiaoyu_agent_sdk import OutputSpec

result = session.run("返回订单号", output=OutputSpec(
    {"type": "object", "properties": {"order_id": {"type": "string"}},
     "required": ["order_id"], "additionalProperties": False},
    max_retries=2,
))
if result.output_status == "valid":
    use_business_result(result.output)
else:
    handle_failure(result.output_status, result.output_errors)
```

仅 `output_status == "valid"` 表示已验证。`output=None` 本身无法区分合法 null 与失败。
支持状态：`not_requested`、`valid`、`missing`、`invalid`（无修复额度）、
`retries_exhausted`、`budget_exhausted`、`interrupted`。`stopped` 独立表示执行如何停止；
截断或 hook 提前阻止可能得到 `missing`。异常不会伪装成结果。

验证使用 JSON Schema 2020-12 的明确子集：`$schema`、`title`、`description`、
`default`、`examples`、`type`、`enum`、`const`、`properties`、`required`、
`additionalProperties`、`items`、`minItems`、`maxItems`、`uniqueItems`、
`minProperties`、`maxProperties`、`minLength`、`maxLength`、`pattern`、`minimum`、
`maximum`、`exclusiveMinimum`、`exclusiveMaximum`、`multipleOf`、`allOf`、`anyOf`、
`oneOf`、`not`。根必须是 schema 对象，子 schema 可为布尔值。注解不填默认值，
数字字符串不转换为数字。`$ref`、`$defs`、`format` 及其他未知关键字在发请求前拒绝，
不会联网解析 schema。复杂/恶意正则的资源限制不在本版保证内。

首个候选后最多允许 `max_retries` 次额外修复机会。失败的校验路径和约束名反馈给
模型；错误摘要不回显被拒绝的值。修复与原任务共用历史、工具、模型迭代预算和 usage，
不会由 SDK 重跑整个业务任务。模型仍可能再次请求相同工具，业务方需自己实现幂等键，
本版不承诺外部副作用 exactly-once。严格输出撞迭代上限/软 token 预算时直接停止，
不追加一次无结构正文收尾请求。`budget_tokens` 是共享内核的会话软预算（小于内核
5,000 token 阈值不启用）；不是精确计费上限，也不覆盖在途请求超额。

| 异常 | 宿主处理 |
|---|---|
| `ConfigurationError` / `OutputSchemaError` | 修正配置或 schema |
| `SessionBusyError` / `SessionClosedError` | 等上一轮/回调结束，或新建会话 |
| `ExecutionError` | 请求/内核失败；原始异常在 `__cause__`，重试由宿主决定 |
| `CloseTimeoutError` | 尚未完成清理；保留对象并重试 close |
| `SessionLockedError` / `SessionStorageError` | 处理日志竞争、损坏或存储故障 |

不要向外部用户无筛选地展示 `__cause__` 或完整 traceback，它们可能包含供应商错误正文。

## 会话控制与用量连续性（当前源码新增）

`reset()`、`set_mode(mode)`、`switch_model(model)`、`set_budget_tokens(budget)`
均为仅空闲时接受的控制操作；AsyncSession 对应方法都需 await。
主轮次、尚未消费完的事件流、子任务批次、MCP 控制或未收尾回调占用会话时抛
`SessionBusyError`。它们占用同一执行槽，不会与下一轮交叉执行。

```python
from xiaoyu_agent_sdk import Session

with Session(options) as session:
    session.set_mode("plan")
    result = session.run("先调研并给出计划")
    session.set_mode("default")  # 宿主显式决定切回；模型自行退出仍须审批
    session.switch_model("another-model-on-the-same-endpoint")
    session.set_budget_tokens(100000)
    session.reset()
```

`SessionOptions.mode` 默认 `"default"`（确认档），允许 `"default"`、`"auto"`、
`"plan"`，对应类型为 `SessionMode`。`set_mode` 返回内核的说明文本。
auto 仅按既有沙箱和工具规则免确认，deny 与硬红线继续生效，普通宿主业务工具仍需
自己的审批。plan 限制写操作与委托；SDK 的任务提交和重试也拒绝在 plan 中执行。
模型的 `exit_plan_mode` 仍走审批，结束后 SDK 当前选项与实际模式同步。
本接口不启用内核的 `update_plan` 清单工具，也不提供 yolo 开关。

plan 文件采用会话自己的位置：JSONL 日志旁，或无本地日志时的独立临时目录。
后者随会话资源关闭而清理，避免多个 SDK 会话共享工作区的 `.xiaoyu/plan.md`。
fork／恢复进入 plan 时追加当前会话路径说明。文件写入仍遵循内核的 plan 专用边界。

`switch_model` 只切换现有端点、凭据、协议与 client 上的模型名，主模型与摘要模型
一起更新，历史和账本保留；不探测模型、不发网络请求、不关闭或替换借用 client。
显式指定了模型的子 Agent 保留其选择，默认跟随主模型的后续任务使用新名称。
需要换端点、凭据、协议或 client 时，新建会话或用显式 `ModelOptions` 分叉。

`set_budget_tokens` 接受正整数或 None；None 关闭 token 软预算。值表示累计消耗的
目标上限，不是“从现在再给这么多”。它保留已花用量，重置也不会返还额度。
沿用内核软预算：至少 5,000 tokens 才启用节奏与收尾，小于该值不提供硬限额；
即使达到上限也可能有最后一次收尾请求。严格请求数／费用约束另用 `BudgetOptions`，
两套预算相互独立。
token 软预算在主轮次边界判断，共享用量包含子任务，但不是子任务各请求的硬闸；
需要约束并行任务的请求数／费用时，使用共享的 `BudgetOptions`。

`reset()` 清当前对话、计划状态、内存回滚点、未接纳插话和当时的待通知／去重 key，
保留会话 ID、工具／MCP／client、回调、观察订阅、累计用量、费用账本、预算与
已结束的任务／子历史档案。它不回滚工作区文件，也不删除本地 plan 文件。
plan 模式按内核规则退回先前模式，auto／default 保持；生命周期 hooks 不重新开始。
活跃后台进程或 watcher 尚未结束时拒绝重置；重置过程中 SDK 通知投递返回 busy。

累计用量在主轮次和子任务收尾时写入检查点；JSONL 与 SessionStore 均可恢复。
读取最近一次累计记录，不逐条求和，因此多次恢复不会重复累计。fork 复制当时基线，
随后父子独立累计；费用账本的 `BudgetOptions.history` 不改变 token 用量基线。
旧日志没有用量记录时无法重建；模型不报告 usage、进程在检查点前崩溃的消耗也无法
凭空补齐，这不是跨崩溃的精确计费系统。格式损坏的累计记录会阻止恢复，不静默归零。

恢复时模型、模式、预算上限、凭据和回调以宿主提供的 options 为准；日志只恢复
历史和已记录消耗，不从日志自动恢复更宽权限或凭据。要沿用运行期选择，宿主应保存
最新的配置；同步会话的 `session.options` 会更新，AsyncSession 可通过 snapshot
读取 model／mode／budget_tokens 后更新自己的配置。历史里的 plan 状态会与宿主选项
协调，避免执行模式与模型看到的说明不一致。

异步控制一旦开始，取消等待不代表撤销。SDK 等待操作收尾再传播取消；超过关闭
等待上限则保留执行所有权并报超时，不能立即提交另一操作。控制或写日志失败后
会话进入 broken，关闭并检查记录后再重新打开，不能继续使用可能不一致的状态。
可运行示例见 [controls.py](../examples/sdk/controls.py)。

## 本地恢复、分叉和回滚

`session_dir` 开启本地 JSONL 持久化；`session.session_path` 是可保存的恢复句柄。
新进程用 `Session(options, resume_from=path)` 或 `AsyncSession` 恢复，配置/凭据/业务
回调需宿主重新提供。恢复要求同一工作区，显式目录中的日志支持单写者锁；锁失败或
损坏日志不会无声降级。日志包含对话和工具结果，存储目录应由宿主管理访问权限。

空闲会话的 `fork(options=...)` 复制对话并创建独立资源/日志，不复制工作区文件或
文件检查点。要改工作区，传新的 options。`checkpoints()` 返回本进程的快照编号，
`rewind(index, conversation=True, files=True)` 返回 `RewindResult`。状态为
`completed`、`partial`、`conflict`、`failed`、`unavailable` 或 `noop`；文件冲突时
保留文件和对话，由宿主决定如何处理。结果分别给出 `conversation_rewound`、
`files_rewound`、`restored_files`、`removed_files`、`conflicts`、`skipped_files` 和
`uncertain_files`。文件恢复失败时保守地将所有目标列为 uncertain，需核对后重试。
超过快照大小上限、对话已压缩等情况可得到 partial；不要只凭调用返回宣称全部回滚。
仅内核记录的编辑工具有文件快照，bash/业务工具/外部系统的副作用不受其保护。
重启后文件快照不可恢复，`checkpoints()` 为空，不存在的点返回 `unavailable`。

## 验证与发布

完整示例见 [examples/sdk](../examples/sdk/README.md)，支持无凭据的 `--demo`。
本次验证范围与发布前待办见 [验收记录](sdk-validation.md)。
`tests/test_sdk.py` 通过脚本模型验证失败分支，`tests/sdk_wheel_smoke.py` 验证安装制品。
构建与制品检查见 [scripts/build_sdk.py](../scripts/build_sdk.py)。版本源为
`xiaoyu.__version__`；SDK 构建生成本地版本文件和精确依赖，sdist 能独立重建。

双包发布必须先通过回归、隔离安装与目标平台检查，再发布内核，待索引可获取后发布
SDK。两次上传不是原子事务；SDK 失败时保留制品与日志，核对索引再重试，不覆盖同版本。
标签发布流程已接入双包顺序上传与哈希核对，参见 [发布操作](sdk-release.md)。
本机修改不会触发上传。PyPI 名称/权限、目标平台 CI、真实模型
覆盖和完整可靠性矩阵属于发布门禁，不能用脚本模型测试代替。

0.x 期间破坏性变更用 minor，移除既有接口前保留至少一个 minor 的弃用窗口。
首版支持 Python；跨语言继续使用 REST/MCP/wire，TypeScript SDK 后续评估。

公开方法和选项速查见 [API 参考](sdk-api.md)。

0.59.0 起提供的存储扩展见 [外置 SessionStore](sdk-storage.md)。
动态 MCP/OAuth、依赖任务、成本预算和 OpenTelemetry 见 [平台能力](sdk-platform.md)。
