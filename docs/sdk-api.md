# SDK 0.58.0 API 参考

所有以下类型均从 `xiaoyu_agent_sdk` 导入。使用说明与失效语义见 [SDK 指南](sdk.md)。

## 会话方法

| 同步入口 | 返回值；异步对应 |
|---|---|
| `run(prompt, options, *, output=None)` | `RunResult`；`await run_async(...)` |
| `Session(options, *, resume_from=None)` | 上下文管理器；`AsyncSession` 为异步上下文管理器 |
| `session.run(prompt, *, output=None)` | `RunResult`；异步版需 await |
| `session.stream(prompt, *, output=None)` | `Generator[UIEvent]`；异步版 `AsyncGenerator[UIEvent]` |
| `session.interrupt()` | 无返回；异步版同样是普通方法 |
| `session.close()` | 无返回；异步版需 await |
| `session.fork(*, options=None)` | 独立 `Session`；异步版 await 返回 `AsyncSession` |
| `session.checkpoints()` | `tuple[int, ...]`；异步版同样是普通方法 |
| `session.rewind(index, *, conversation=True, files=True)` | `RewindResult`；异步版需 await |
| `session.mcp_status()` | `tuple[McpServerStatus, ...]`；异步版同样是普通方法 |
| `session.closed` / `session.session_path` | bool / `Path | None` 属性 |
| `list_sessions(directory, *, limit=20, workspace=None)` | `list[SessionInfo]`；普通函数 |

`fork`、`checkpoints`、`rewind` 要求会话空闲。`mcp_status` 可在执行中和关闭后读取。
方法中的 prompt 是字符串，output 为 `OutputSpec | None`，恢复路径为 `Path | None`。

## 配置默认值

`ModelOptions(model, base_url="https://api.openai.com/v1", api_key="", protocol="chat",
request_timeout=120.0, client=None)`。client 是借用的同步 OpenAI 兼容对象，不由 SDK 关闭。

| `SessionOptions` 字段 | 默认值 |
|---|---|
| `model`, `workspace` | 必填 `ModelOptions`、`Path` |
| `system_prompt` | None，使用内核提示词 |
| `builtin_tools` | None，使用内核工具；空元组禁用这些工具 |
| `tools`, `hooks`, `mcp_servers`, `subagents`, `plugins` | 空元组 |
| `approver` | None，需要审批时拒绝 |
| `approval_timeout`, `close_timeout` | 120.0 秒、10.0 秒 |
| `max_iterations`, `budget_tokens` | 50、None；后者是会话 token 软预算 |
| `load_project_instructions`, `skill_directories` | False、空元组 |
| `session_dir` | None，不创建持久化日志 |
| `tool_env`, `deny_rules` | 空字典、空元组 |
| `event_buffer_size` | 128 |

配置数据类是 frozen；会话会复制工具 schema、MCP 凭据字典和环境字典。
请在构造后保持 options 和嵌套配置不变；不要用修改配置对象来热更新会话。

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

## 兼容矩阵

| SDK | 内核依赖 | Python | 当前验证 |
|---|---|---|---|
| 0.58.0（未发布） | `xiaoyu-agent[sdk]==0.58.0` | >=3.11 | 本机 macOS arm64 / 3.14.3 |

Linux/macOS/Windows × 3.11/3.14 已配置 CI，远端执行尚待完成。
不承诺低版本 Python、其他内核版本或未验证平台的操作系统沙箱能力。
0.x 破坏性变更升级 minor；移除既有接口前至少保留一个 minor 的弃用窗口。
