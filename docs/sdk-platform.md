# SDK 平台能力：MCP、任务编排、预算与遥测

2026-10-01 工作区候选实现，尚未发布。PyPI 0.58.0 不包含本文新增接口。
所有示例都使用显式宿主配置；会话存储基础见 [存储说明](sdk-storage.md)。

## 动态 MCP 与 OAuth

```python
from xiaoyu_agent_sdk import McpServer

session.mcp_add(McpServer("knowledge", url="https://example.com/mcp"))
print(session.mcp_status())
session.mcp_manage("knowledge", "reconnect")
session.mcp_manage("knowledge", "stop")
session.mcp_manage("knowledge", "start")
session.mcp_manage("knowledge", "remove")
```

增删、启停、重连和 `approve_changes` 只在会话及子任务空闲时接受。运行中调用返回
`SessionBusyError`；控制操作占用会话执行槽，关闭时会等待或明确超时。
`start` 异步发现工具，用 `mcp_status()` 查询就绪状态。状态为 `ready` 不代表每个
工具都被批准：声明变化仍会隔离，宿主审阅后才能显式 `approve_changes`。
停止、删除后重加同名服务保留声明基线；环境中的自动信任开关不替 SDK 授权。

默认每个会话创建并拥有连接。宿主也可创建 `McpPool(servers, state_dir=...)`，通过
`SessionOptions(mcp_pool=pool)` 借给会话。一个池同时只能租给一个会话；租用期间通过
会话修改，会话关闭只归还租约。宿主最终调用 `pool.close()` 并检查
`pool.shutdown_pending()`。独立任务通过 `Subagent.mcp_servers` 显式选择可见服务。

`OAuthClient` 支持宿主已注册的公共客户端授权码 + PKCE、过期前刷新和撤销：

```python
from xiaoyu_agent_sdk import OAuthClient, McpServer

auth = OAuthClient(
    resource="https://example.com/mcp",
    client_id="registered-client",
    authorization_endpoint="https://auth.example.com/authorize",
    token_endpoint="https://auth.example.com/token",
    redirect_uri="http://127.0.0.1:8765/callback",
    revocation_endpoint="https://auth.example.com/revoke",
    issuer="https://auth.example.com",
    scope="read",
)
auth.authorize(host_authorization_callback)
session.mcp_add(McpServer("knowledge", url=auth.resource, oauth=auth))
```

宿主回调接收授权 URL，返回完整重定向 URL。SDK 验证 state、重定向地址、配置的
issuer 和返回 scope，并使用 PKCE S256；刷新失败清除本地凭据并要求重新授权，不扩大权限。
OAuth HTTP 请求及带 OAuth 的 MCP 请求不跟随重定向。SDK 不启动浏览器/监听器，
不自动发现授权服务器或动态注册客户端。端点、客户端注册和交互由宿主提供。

默认令牌仅在内存；自定义 `OAuthTokenStore.load/save/clear` 接入宿主凭据库。
每个存储实例必须绑定同一客户端、资源和 scope，并限制自身 I/O 等待时间。令牌
不进入 SessionStore、普通声明缓存或 repr。`revoke()` 尝试撤销刷新及访问令牌；
远端失败时也立即禁止本实例继续使用令牌，异常说明远端撤销尚未确认。已发送的
请求可能仍完成。需要停止使用服务时先停止对应 MCP，再撤销凭据。

可运行示例：[动态 MCP](../examples/sdk/mcp_management.py)。

## 依赖任务与恢复

```python
from xiaoyu_agent_sdk import TaskSpec

handles = session.tasks.submit((
    TaskSpec("research", "analyst", "收集事实"),
    TaskSpec("review", "reviewer", "核对上游事实", ("research",), parent="research"),
))
result = handles[-1].wait(timeout=120)
print(result.state, result.answer, result.task_id)
```

`agent` 引用 `SessionOptions.subagents` 中的声明。每批先验证完整 DAG，再一次写入
任务定义，最后开始执行。`max_parallel_tasks` 限制并行数量；依赖结果作为明确标注
的数据交给下游。子 Agent 继续使用内核的工具白名单、deny 规则、审批和 worktree
隔离。当前声明工具使用内核内置工具；SDK 自定义宿主工具不会自动复制进子 Agent。

本轮 SDK 任务句柄管理子 Agent；内核工具启动的后台 shell 进程不复用这些任务 ID。

`session.tasks.list/get/handle` 查询状态、答案、错误、父任务和子历史 ID；句柄支持
`cancel()`、`wait()`。状态依次为 `queued`、`running`，取消中为 `cancelling`，
终态为 `succeeded/failed/cancelled/blocked/lost`。取消幂等，只影响指定任务；依赖
未成功时下游 `blocked`。没有结束的同步回调仍占用任务，不能被伪报为已取消。
父会话运行和任务批次互斥，避免对话、预算和资源控制产生重叠。

外置存储和本地日志均保存任务定义、终态、结果和已收尾的子 Agent 历史。
恢复保留成功结果；未持久化终态的任务变为 `lost`，不会自动重新执行。
`retry(id, allow_uncertain=True)` 明确承担失败/取消/失联任务可能重复副作用的风险；
`continue_history=True` 使用已保存的子历史继续，而非新开上下文。没有历史时拒绝。
`TaskSpec.resume_from` 可创建一个使用既有子历史的新任务。成功任务不能原地重试。
下游修复后单独重试；不会偷偷重跑成功依赖。

fork 复制可恢复子历史，生成新会话身份，不复制任务调度。工具副作用已发生而终态
未持久化仍可能无法判定；SDK 不承诺 exactly-once。文件/worktree 也不随数据库迁移。
AsyncSession 提供 `submit_tasks/wait_task/cancel_task/retry_task/task_status` 对应入口。

可运行示例：[依赖编排与恢复](../examples/sdk/orchestration.py)。

## 请求与费用预算

`SessionOptions.budget=BudgetOptions(...)` 在实际模型请求入口计量，父轮次、子任务、
摘要及结构化修复共享限额；请求重试单独计数。`max_requests` 是请求数量硬闸，
`max_usd` 是按已收到用量和宿主价格估算的后续请求闸门。通过
`prices={model: ModelPrice(input_per_million, output_per_million, cached_input_per_million, source)}`
提供带来源/版本的价格，SDK 不自行猜供应商价格。
有缓存写入费用的模型另提供 `ModelPrice(..., cache_creation_per_million=...)`。
该值对应宿主实际使用的缓存策略；存在写入 token 却缺少写入单价时，费用记为未知，
启用美元限额后拒绝下一请求，不能把写入当作免费或混入普通输入费率。

`session.cost` 返回每次请求的身份、输入/输出/缓存 token、费用估算、价格来源和状态。
Chat、Responses 和 Anthropic 的归一化保留缓存读取细分，Anthropic 另保留
`cache_creation_tokens`；缺失 usage/部分计量不合成为零用量。
缓存读取用量按归一化供应商响应提供的 cached_tokens 计算；未提供缓存细分时按全部
普通输入估算，不能据此推导实际缓存命中或最终账单。用量缺失和未知价格的费用为
`None`，不会假记为零。启用美元限额时，未知模型价格或既有未知用量会拒绝下一请求。
超限抛出 `BudgetExceededError`，不会启动新的模型请求。

`history="include"` 默认计入恢复/fork 的历史账目；宿主可明确选择 `"reset"` 重新
计算当前实例的预算。崩溃时在途的请求恢复为未知费用。已在途的并行请求会正常收尾
记账，因此美元限额可能超出一个或多个请求的费用；并发上限控制在途数量，SDK
不承诺供应商侧金额硬封顶。

## 生命周期和 OpenTelemetry

扩展 Hook 事件为 `SessionStart/SessionEnd/SubagentStart/SubagentEnd/ToolFailed/BeforeCompact/AfterCompact`，
原有四类事件继续可用。事件带会话身份，运行中带 run/task 身份。SessionStart 在
首次执行前触发；阻止/异常拒绝启动。BeforeCompact 可阻止压缩。结束及失败通知
发生在动作之后，不能撤销已发生动作；通知异常可由 `session.hook_errors` 检查。
SessionEnd 在关闭时触发一次，结束 hook 不阻止资源继续收尾。

`TelemetryOptions(exporter, queue_size=32, max_spans=512)` 提供有界完成轨迹导出。
默认只记录操作种类、时间、状态、模型/工具名和关联 ID，不记录提示词、工具参数、
结果正文或凭据。运行、模型请求、工具、审批等待和子 Agent 跨度结束后导出。
`session.telemetry_status` 暴露丢弃和导出异常数量；队列满丢弃整个完成轨迹，不拖住
业务。长轮次超过跨度上限时丢弃后续跨度，计数可见。关闭等待导出，超时保留资源
所有权，宿主可在解除阻塞后重试。

SDK 流式事件带 `session_id/run_id/request_id/tool_call_id/task_id`（不适用的字段为空），
工具 ID 对应内核调用和历史记录，模型请求 ID 对应成本记录。子任务的 trace 通过
run/task/parent_task_id 关联；单个 trace 中的模型、工具、审批使用实际父子跨度。

安装 `xiaoyu-agent-sdk[telemetry]` 后使用 `OpenTelemetryExporter(host_provider)`。
宿主自行安装、配置 OpenTelemetry SDK/exporter；SDK 不替换全局 provider，也不
flush 或 shutdown 借来的 provider。适配依据见
[OpenTelemetry Python 官方文档](https://opentelemetry.io/docs/languages/python/instrumentation/)。

宿主已安装 `opentelemetry-sdk` 时，最小接入如下（`options` 为宿主的会话配置）：

```python
from dataclasses import replace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import ConsoleSpanExporter, SimpleSpanProcessor
from xiaoyu_agent_sdk import OpenTelemetryExporter, Session, TelemetryOptions

provider = TracerProvider()
provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))
try:
    configured = replace(options, telemetry=TelemetryOptions(OpenTelemetryExporter(provider)))
    with Session(configured) as session:
        session.run("返回整数 42")
finally:
    provider.shutdown()  # 宿主负责最终关闭自己的 provider
```

完整可靠性结论以 [验收矩阵](sdk-reliability.md) 和对应原始报告为准。功能测试、
本机脚本负载、真实供应商和六组平台/Python 验证分别记录。
