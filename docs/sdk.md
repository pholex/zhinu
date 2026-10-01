# Python SDK

`xiaoyu-agent-sdk` 是与 `xiaoyu-agent` 同仓库、同版本的独立发行包，导入名为
`xiaoyu_agent_sdk`。SDK 在宿主进程里调用现有执行内核，不启动 CLI 子进程。
旧 `import xiaoyu` 和 CLI 保持兼容。当前代码版本为 0.58.0，尚未发布到 PyPI。

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
| `Hook` / `HookDecision` | Python 生命周期回调 |
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

四个 Python hook 事件是 `PreToolUse`、`PostToolUse`、`UserPromptSubmit`、`Stop`。
callback 收到带 `event` 的 payload，返回 `HookDecision(blocked, reason)`。
Pre/User 的 block 阻止执行，Post 的 block 反馈给模型但不撤销副作用，Stop 最多顶回
一次。hook 故障按 block 处理。`tool_name` 可选精确匹配，不使用 shell hooks。

`McpServer` 显式提供 stdio command/args/env 或 HTTP url/headers；会话启动 manager，
工具异步发现，走同一权限和审批路径。`session.mcp_status()` 返回
`tuple[McpServerStatus, ...]`，只含名称和状态，不含服务端错误正文或凭据。
常见状态为 loading/cached/ready/failed/blocked/closing/closed；调用方需容忍新增状态。
SDK 为每个会话分配独立的临时 MCP 缓存、工具声明记录和日志目录，不读取或修改 CLI
的这些状态；服务完全关闭后才清理目录。因此重启会话会重新发现服务与工具声明。
OAuth、动态增删和宿主接管 MCP 生命周期留待后续版本。

`Subagent` 使用内核委托工具，限定内置工具、提示词、模型和迭代数，继承父会话
审批与 deny 规则。`isolation="worktree"` 要求 Git worktree；创建失败就返回工具失败，
模型不能将它降级为 `none`。默认 `none` 共享工作区。worktree 隔离的是工作文件，
不隔离宿主 Python、环境或外部系统；本版不暴露复杂任务编排。

`plugins=(Plugin(name="entry_name", distribution="installed-package"),)` 仅加载指定
发行包内的 `xiaoyu.tools` 入口点，缺失、歧义或工厂失败在构造会话时抛
`ConfigurationError`。已安装但未选择的入口点不会执行。插件沿用已有 Python 插件
工厂契约（Config → 内核 Tool 或其列表），是可信的进程内代码，所创建资源由插件
负责回收。项目插件 bundle 的自动发现仍关闭；普通业务工具优先使用公开 `Tool`。

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

工作区中的未发布 P4 存储扩展见 [外置 SessionStore](sdk-storage.md)。
动态 MCP/OAuth、依赖任务、成本预算和 OpenTelemetry 见 [平台能力](sdk-platform.md)。
