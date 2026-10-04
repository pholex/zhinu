# SDK API 参考

所有以下类型均从 `xiaoyu_agent_sdk` 导入。使用说明与失效语义见 [SDK 指南](sdk.md)。

本页列出首版接口、0.59.0 新增能力及明确标注的 0.62.0 扩展。
`Asker`、`asker`、`question_timeout`、`TextBlock` / `ImageBlock` / `Prompt`、
`steer` / `drain_steers` 与 SDK 的 `SteerAccepted` 导出自 0.62.0 起提供。`SessionStore`、
`SessionWriter`、`SQLiteSessionStore`、`StoredSessionInfo`、`session_id` 与
`resume_id` 见 [存储扩展](sdk-storage.md)。
动态 MCP/OAuth、`TaskSpec` / `TaskHandle` / `TaskSnapshot` / `TaskManager`、
`BudgetOptions` / `ModelPrice` / `CostSnapshot`、扩展 Hook 和 OpenTelemetry 的
接口见 [平台能力](sdk-platform.md)。

通知、状态快照和 `PlanUpdated` / `ToolPurpose` 的 SDK 导出同样自 0.62.0 起提供。
能力覆盖范围见 [内核接入清单](sdk-kernel-map.md)。

0.62.0 还新增 `SessionMode`、`SessionOptions.mode` 和下列空闲控制方法，
以及可选计划工具 `SessionOptions.enable_plan` 与快照类型 `PlanStep`。
工具结果变换同为 0.62.0 新增，完整处理顺序与失败边界见
[工具结果文本变换](sdk-result-transforms.md)。

| 变换入口 | 契约 |
|---|---|
| `ResultTransform(name, callback, tool_name="")` | frozen 注册项；名称唯一，按注册顺序执行，tool_name 为精确筛选 |
| `ToolOutput` | frozen 输入；tool_name、text、is_error、session_id、run_id、task_id、tool_call_id |
| `ResultTransformHandler` | Callable[[ToolOutput], str 或 Awaitable[str]]；不返回 None |
| `ResultTransformError` | ExecutionError 子类；动作已执行、返回文本被扣留，不表示可以直接重试 |

## 会话方法

当前源码另新增持久化问答面：`QuestionOptions`、`QuestionOption`、`QuestionItem`、
`QuestionAnswer`、`QuestionSnapshot`、`QuestionEvent`、`QuestionManager`、`AsyncQuestionManager`、
`QuestionConflictError` 和 `QuestionNotFoundError`。`SessionOptions.questions` 默认
None，显式 QuestionOptions() 须配 SQLiteSessionStore；与 asker 互斥。
QuestionOptions.foreground_timeout_seconds 默认 0（立即待答），有限正数启用
前台等待；超时转 pending，及时回答也先 queued 再经安全边界接纳。
QuestionSnapshot.state 新增 open，QuestionEvent.kind 新增 question.opened。
`session.questions` 提供 get／list_pending／answer／cancel，异步会话四者均 await。
另有 questions.watch() 返回带版本的状态观察流，异步会话使用 async for；
初次含已有终态，慢观察者合并中间状态，会话关闭结束。
字段、幂等与事务契约见 [持久化待答指南](sdk-deferred-questions.md)，均自 0.62.0 起提供。

| 同步入口 | 返回值；异步对应 |
|---|---|
| `run(prompt, options, *, output=None)` | `RunResult`；`await run_async(...)` |
| `Session(options, *, resume_from=None)` | 上下文管理器；`AsyncSession` 为异步上下文管理器 |
| `session.run(prompt, *, output=None)` | `RunResult`；异步版需 await |
| `session.stream(prompt, *, output=None)` | `Generator[UIEvent]`；异步版 `AsyncGenerator[UIEvent]` |
| `session.interrupt()` | 无返回；异步版同样是普通方法 |
| `session.steer(text)`（0.62.0 起） | bool，是否排队；异步版同样是普通方法 |
| `session.drain_steers()`（0.62.0 起） | `list[str]`，取回未接纳插话；异步版同样是普通方法 |
| `session.notify(text, key="", wake=True)`（0.62.0 起） | None，投递通知；异步版同样是普通方法 |
| `session.pending_notifications()`（0.62.0 起） | `tuple[Notification, ...]`；异步版同样是普通方法 |
| `session.watch_notifications()`（0.62.0 起） | `Generator[tuple[Notification, ...]]`；异步版 `AsyncGenerator` |
| `session.status()`（0.62.0 起） | `SessionState` 字符串；异步版同样是普通方法 |
| `session.snapshot()`（0.62.0 起） | `SessionSnapshot`；异步版需 await |
| `session.reset()`（0.62.0 起） | None，清对话但保留身份、资源与累计消耗；异步版需 await |
| `session.set_mode(mode)`（0.62.0 起） | str，内核模式说明；异步版需 await |
| `session.switch_model(model)`（0.62.0 起） | None，同端点／client 换模型名；异步版需 await |
| `session.set_budget_tokens(budget)`（0.62.0 起） | None，累计 token 软预算，正整数或 None；异步版需 await |
| `session.close()` | 无返回；异步版需 await |
| `session.fork(*, options=None)` | 独立 `Session`；异步版 await 返回 `AsyncSession` |
| `session.checkpoints()` | `tuple[int, ...]`；异步版同样是普通方法 |
| `session.rewind(index, *, conversation=True, files=True)` | `RewindResult`；异步版需 await |
| `session.mcp_status()` | `tuple[McpServerStatus, ...]`；异步版同样是普通方法 |
| `session.closed` / `session.session_path` | bool / `Path | None` 属性 |
| `list_sessions(directory, *, limit=20, workspace=None)` | `list[SessionInfo]`；普通函数 |

`fork`、`checkpoints`、`rewind` 要求会话空闲。`mcp_status` 可在执行中和关闭后读取。
方法中的 prompt 为 `Prompt`，output 为 `OutputSpec | None`，恢复路径为 `Path | None`。

`Prompt = str | list[TextBlock | ImageBlock] | tuple[TextBlock | ImageBlock, ...]`。
`TextBlock(text: str)`、`ImageBlock(data: bytes)` 为 frozen 数据类，内容列表不能为空。
图片支持 PNG/JPEG/GIF/WebP 编码字节，每张至多 7 MiB；在启动轮次前校验、复制，
不接受原始协议字典。图片以内联内容随会话持久化，使用方式见
[图片输入与插话](sdk.md#图片输入与运行中插话)。

`steer` 返回 True 只表示排队；以 `SteerAccepted(text)` 确认进入用户历史。
未接纳项在轮次结束后通过 `drain_steers` 取回，不自动进入下一轮，也不跨重启恢复。
主轮次未就绪、已结束、已取消或文字为空时返回 False；关闭后抛 `SessionClosedError`。
取回操作要求当前轮次已结束，关闭后仍可调用；执行中抛 `SessionBusyError`。

通知、去重 key 与观察订阅不跨重启恢复，也不随 fork 复制。
观察流给出初始及合并变化后的待通知快照，在关闭开始时结束；不是逐条投递日志。
`snapshot` 要求会话空闲；`status` 与 `pending_notifications` 可在运行中和关闭后查询。
完整生命周期说明见 [通知与状态观察](sdk.md#通知与状态观察)。

## 只读视图类型

下列数据类均为 frozen，嵌套集合为 tuple，不持有可变内核对象。

| 类型 | 字段 |
|---|---|
| `Notification` | key: str, text: str, wake: bool |
| `ModelUsageSnapshot` | model: str, calls: int, prompt_tokens: int, completion_tokens: int |
| `UsageSnapshot` | model_calls: int, prompt_tokens: int, completion_tokens: int, by_model: tuple[ModelUsageSnapshot, ...] |
| `MessageSnapshot` | role: str, text: str, image_count: int, tool_names: tuple[str, ...], tool_call_id: str |
| `PlanStep` | step: str, status: str（pending / in_progress / completed） |
| `SessionSnapshot` | session_id: str, model: str, mode: str, budget_tokens: int 或 None, context_tokens: int, usage: UsageSnapshot, history: tuple[MessageSnapshot, ...], last_assistant_text: str, pending_notifications: tuple[Notification, ...] |

`SessionState` 为 `Literal["idle", "running", "tasks", "settling", "broken", "closing", "closed"]`。
`SessionSnapshot` 另有 `plan: tuple[PlanStep, ...] = ()`，表示当前任务清单。
它与权限模式 `mode="plan"` 独立；事件及恢复语义见 [可选计划工具](sdk.md#可选计划工具)。
usage 包含恢复／分叉的已记录累计基线及后续实际用量，不从对话文本推算；history 是当前上下文的展示
投影，不包含 system、图片字节和原始调用参数，不作恢复数据格式。

## 配置默认值

`ModelOptions(model, base_url="https://api.openai.com/v1", api_key="", protocol="chat",
request_timeout=120.0, client=None)`。client 是借用的同步 OpenAI 兼容对象，不由 SDK 关闭。

| `SessionOptions` 字段 | 默认值 |
|---|---|
| `model`, `workspace` | 必填 `ModelOptions`、`Path` |
| `mode`（0.62.0 起） | `"default"`；`SessionMode = Literal["default", "auto", "plan"]` |
| `enable_plan`（0.62.0 起） | False；显式启用内核 `update_plan`，独立于 builtin_tools 的选择 |
| `result_transforms`（0.62.0 起） | 空元组；显式的 ResultTransform 序列 |
| `result_transform_timeout`（0.62.0 起） | 30.0 秒；每项回调的有限正超时 |
| `system_prompt` | None，使用内核提示词 |
| `builtin_tools` | None，使用内核工具；空元组禁用这些工具 |
| `tools`, `hooks`, `mcp_servers`, `subagents`, `plugins` | 空元组 |
| `approver` | None，需要审批时拒绝 |
| `asker`（0.62.0 起） | None，不向模型广告 `ask_user` |
| `question_timeout`（0.62.0 起） | 120.0 秒，有限正数 |
| `approval_timeout`, `close_timeout` | 120.0 秒、10.0 秒 |
| `max_iterations`, `budget_tokens` | 50、None；后者是会话 token 软预算 |
| `load_project_instructions`, `skill_directories` | False、空元组 |
| `session_dir` | None，不创建持久化日志 |
| `tool_env`, `deny_rules` | 空字典、空元组 |
| `event_buffer_size` | 128 |

配置数据类是 frozen；会话会复制工具 schema、MCP 凭据字典和环境字典。
请在构造后保持 options 和嵌套配置不变；不要用修改配置对象来热更新会话。
控制方法成功后由 SDK 替换当前 options 快照。恢复时仍由宿主提供模式、模型和预算
上限；不会从旧日志自动授权更宽模式。控制均要求空闲，失败可能使会话进入 broken，
完整语义见 [会话控制与用量连续性](sdk.md#会话控制与用量连续性)。

## 扩展与结果

| 类型 | 构造字段 |
|---|---|
| `Tool` | name, description, parameters, handler, requires_approval=True |
| `ToolResult` | content, is_error=False |
| `Hook` | event, callback, tool_name="" |
| `McpServer` | name, command="", args=(), env={}, url="", headers={}, timeout=60.0；command/url 二选一 |
| `Plugin` | name, distribution |
| `Subagent` | name, description, system_prompt, tools, model="", max_iterations=12, isolation="none" |
| `OutputSpec` | schema, max_retries=2 |

`RunResult` 提供 text、interrupted、duration_seconds、usage、context_tokens、output、
stopped、output_status、output_errors、output_retries。usage 是本轮增量（含子任务和修复）；
context_tokens 是当前上下文估算。仅 output_status 为 valid 时消费 output。
`RunCompleted.result` 携带同一个结果。其余事件按类型或 kind 消费，允许跳过未知事件。

审批类型为 `Approver` / `Approval`，批准/拒绝用 `Allow` / `Deny`；hook 返回
`HookDecision`。工具 handler、审批和 hook 支持同步函数或异步函数；同步 Session
只能使用同步回调。错误类型及 `RewindResult` 字段语义见指南。

`Asker = Callable[[list[dict[str, Any]]], dict[str, str] | Awaitable[dict[str, str]]]`。
问题是归一化的独立副本，答案按问题原文索引，可省略未回答项；空字典表示主动跳过。
同步 Session 不接受异步 asker。超时、错误返回和回调异常作为工具错误回灌；
提问不代替审批。完整生命周期与使用方式见 [宿主提问回调](sdk.md#宿主提问回调)。

## 兼容矩阵

| SDK | 内核依赖 | Python | 当前验证 |
|---|---|---|---|
| 0.59.0 | `xiaoyu-agent[sdk]==0.59.0` | >=3.11 | 六组跨平台 CI 与 wheel 安装检查 |

CI 覆盖 Linux/macOS/Windows × Python 3.11/3.14，发布流程以全部通过为前提。
不承诺低版本 Python、其他内核版本或未验证平台的操作系统沙箱能力。
0.x 破坏性变更升级 minor；移除既有接口前至少保留一个 minor 的弃用窗口。
