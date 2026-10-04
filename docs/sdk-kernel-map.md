# 内核公开能力与 SDK 接入清单

核对日期：2026-10-03。范围是 [嵌入公开契约](embedding.md#公开面清单) 中
`xiaoyu.__all__`、`Agent` 与 `AsyncAgent` 的承诺接口，并补列已有工具和配置入口。
不以 CLI 每个命令或内部方法都获得同名 SDK 方法作为完成标准。

状态含义：**已接入**有 SDK 入口；**替代入口**以宿主管理的等价流程提供；
**待接入**有真实能力缺口；**不直接暴露**说明为何保留 SDK 所有权边界。
本页描述当前源码，提问、图片／插话、通知／状态观察、会话控制、可选计划工具及新事件导出尚未发布。

## 执行与会话

| 内核公开面 | SDK 对应 | 状态与边界 |
|---|---|---|
| `Agent.send`、`AsyncAgent.send` / `stream`、`measured_send` | `Session` / `AsyncSession.run` / `stream`，`run` / `run_async` | 已接入；统一单轮结算，支持文字与图片 |
| `RunResult`、`RunCompleted` | 同名类型 | 已接入 |
| `Interrupted`、`interrupt` | `session.interrupt()`、`RunResult.interrupted` | 替代入口；同步返回中断终态，异步宿主取消清理后传播取消异常 |
| `steer`、`drain_steers` | 同名方法 | 已接入；只向就绪主轮次排队，未接纳文字可取回 |
| `notify`、`pending_notifications` | 同名方法、`Notification` | 已接入；运行中与空闲均可投递，通知不授予权限 |
| `on_notification`、`AsyncAgent.watch_notifications` | `watch_notifications()` 观察流 | 替代入口；每个观察者独立，合并变化后查询当前待通知快照，无宿主回调占用内核线程 |
| `reset`、`AsyncAgent.recycle` | `reset()`；异步版 await | 已接入；仅重置对话及相关内存状态，保留身份、资源、累计消耗及终态任务档案；不另增同义方法 |
| `restore`、`SessionLog`、`load_messages` | 构造时 `resume_from` / `resume_id`，`SessionStore` | 替代入口；恢复由会话生命周期管理，不向忙碌会话原地导入历史 |
| 会话分叉、文件／对话回滚 | `fork`、`checkpoints`、`rewind`、`RewindResult` | 已接入；文件快照跨重启持久化尚未实现 |
| `SessionLockedError`、`SessionInfo`、`list_sessions` | 同名入口 | 已接入；宿主显式指定目录 |
| `set_output_schema`、`structured_output` | `OutputSpec`、`RunResult.output` / `output_status` | 替代入口；按轮次设置并校验，避免遗留到下一轮 |

## 控制、观察与资源

| 内核公开面 | SDK 对应 | 状态与边界 |
|---|---|---|
| `set_mode` | `SessionOptions.mode`、`set_mode()` | 已接入 default／auto／plan；仅空闲可切换；plan 拒绝任务提交／重试，审批与 deny 继续生效 |
| `switch_model` | `ModelOptions`、`switch_model()` | 已接入同端点／client 换模型名；换端点、协议、凭据或 client 使用新会话／显式分叉，保留所有权边界 |
| `set_budget_tokens` | `SessionOptions.budget_tokens`、`set_budget_tokens()` | 已接入空闲调整；累计检查点可恢复，重置不清额度；沿用内核主轮次软预算，不是子任务各请求硬闸 |
| `context_tokens`、`last_assistant_text` | `snapshot().context_tokens` / `last_assistant_text` | 已接入；空闲时读取；单轮还有 `RunResult.context_tokens` / `text` |
| `messages` | `snapshot().history` | 替代入口；不可变展示投影，省略 system、图片字节、推理块及原始调用参数，不作恢复格式 |
| `usage` | `snapshot().usage`、`RunResult.usage` | 已接入累计与单轮增量；恢复／fork 接回已记录基线，主轮次与子任务收尾写检查点。缺失 usage 或检查点前崩溃的消耗不能重建；费用账本另由 `cost` 提供 |
| `trace` | 工具事件流、可选遥测与会话日志 | 替代入口；不直接暴露可变内核 trace 列表 |
| `sink`、`UISink` | `stream()` | 替代入口；SDK 持有内部 sink，用于背压、关联 ID 和遥测，不允许宿主替换 |
| `Agent`、`AsyncAgent`、`AsyncAgent.agent`、`Toolbox` | `Session` / `AsyncSession`、`Tool`、显式选项 | 不直接暴露可变内核对象；防止绕过执行互斥、资源所有权、存储和审批 |
| `Config` | `ModelOptions` / `SessionOptions` | 部分接入；SDK 选项覆盖明确支持的宿主场景。其余配置逐项评估，不透传整份环境配置 |
| `__version__` | SDK 同名入口 | 已接入；双包精确依赖策略保持不变 |

`session.status()` 在运行中提供生命周期状态；`snapshot()` 仅允许空闲时生成，
异步会话使用 `await snapshot()`。状态和快照方法不触发模型调用，不启动新任务。

## 审批、权限与配置加载

| 内核公开面 | SDK 对应 | 状态与边界 |
|---|---|---|
| `Allow`、`Deny`、`Approver`、`AsyncApprover`、`approver` | `Allow` / `Deny`、SDK `Approver` / `Approval`、`SessionOptions.approver` | 已接入；SDK 的回调类型统一同步／异步，配置缺失时拒绝需要审批的工具 |
| `Asker` | SDK `Asker`、`SessionOptions.asker` | 已接入；基础问答，暂不含持久化迟到回答 |
| 持久化待答 | QuestionOptions、session.questions | 首版已接入 SQLite；可选前台等待，安全 step／下一轮接纳与版本观察，详见持久化待答指南；与 asker 互斥 |
| `normalize_verdict` | SDK 内部沿用内核审批处理 | 替代入口；宿主提供类型化批准／拒绝，无需手工规范化 |
| `Permissions`、`Rule`、`parse_rule` | `deny_rules` 与审批回调 | 部分接入；deny 为显式配置，权限对象不直接暴露。动态 allow／ask 规则管理需单独设计 |
| `suggest_allow_rule`、`banned_allow_reason` | 无 SDK 同名入口 | 不直接暴露；SDK 不管理 CLI 的持久化授权建议，宿主用自己的审批策略 |
| `user_rules_path`、`workspace_rules_path` | 无自动加载 | 不直接暴露；SDK 不隐式读取 CLI 权限文件 |
| `load_dotenv`、`user_env_path`、`MissingConfig`、`MissingApiKey` | 显式模型／会话选项、`ConfigurationError` | 替代入口；SDK 不自动载入宿主环境文件，凭据由宿主注入 |

## 事件与扩展

| 能力 | SDK 对应 | 状态与边界 |
|---|---|---|
| `UIEvent`、`RequestStarted`、`RequestEnded`、`TextDelta`、`TextEnd` | 同名导出与事件流 | 已接入 |
| `ToolPending`、`ToolPurpose`、`ToolRunning`、`ToolCompleted`、`ToolDenied` | 同名导出与事件流 | 已接入；`ToolPurpose` 为本轮补齐导出，复用原类型 |
| `SteerAccepted`、`PlanUpdated`、`Notice` | 同名类型导出 | 已接入；计划工具需显式 enable_plan，快照通过 plan / PlanStep 提供任务清单 |
| 内置工具与宿主 Python 工具 | `builtin_tools`、`Tool` / `ToolResult` | 已接入；部分内核上下文管理工具不受 builtin_tools 空元组控制 |
| 生命周期 hooks | `Hook` / `HookDecision` | 已接入已有宿主事件；返回文本替换另由 ResultTransform 提供，PostToolUse 仍为附加反馈 |
| 工具返回文本变换 | `result_transforms` / `ToolOutput` | 当前源码新增；截断与 recall 落盘前按序处理，失败扣留原文，详见独立契约 |
| MCP、技能、插件、子 Agent | MCP 管理方法、技能目录、显式插件和子 Agent 配置 | 已接入基础与平台能力；宿主工具不自动复制给子 Agent |
| 后台任务与通知 | 模型工具、通知观察流；依赖任务另用 `session.tasks` | 部分接入；后台 shell 任务与 SDK DAG 任务是不同对象，不能用同一任务 ID 管理 |
| CLI 展示与交互组件 | 宿主 UI 消费事件 | 不直接暴露；核心 SDK 不引入终端 UI 依赖 |
| 可选计划工具 | SessionOptions.enable_plan | 已接入；默认关闭，独立于基础工具选择和权限模式，支持事件及不可变快照 |
| 内建探索、通用网页搜索／浏览器、内建协作／跨会话消息 | 继续显式关闭内核自动入口 | 取舍及替代入口见下表；尚不承诺自动配置这些能力 |

## 可选能力与部分接入的取舍

配置按宿主场景逐项开放，SDK 不透传整个 Config 或加载 CLI 环境配置。
以下为本轮实现取舍，不将替代入口说成同名内建工具已支持。

| 能力 | 当前选择及理由 | 后续引入条件／验收 |
|---|---|---|
| 任务清单 | 本批开放 enable_plan；无新增 client、进程或凭据 | 事件、快照、权限、恢复／fork／rewind／reset 契约测试；见 SDK 指南 |
| 内建 explore | 暂不开自动入口；用显式 Subagent，tools 限定 read_file / grep / list_files，按需指定同端点模型 | 内建路径须补齐父级取消、hooks、子运行归档及事件关联；只读子 Agent 仍受 plan 模式禁止委托的边界，不宣称与 explore 完全等价 |
| 网页／社交搜索、远端研究 | 暂不开内建入口；宿主通过 Tool 或显式 MCP 接服务，自行持有凭据 | 独立显式端点／凭据／client 所有权、费用入账、超时与取消；远端异步任务需有重连、关闭后处理和未知结果契约 |
| 浏览器 | 暂不开内建入口；宿主显式 MCP 或 Tool 包装已有浏览器服务 | 定义浏览器进程／profile 所有权、借用资源不得误关、逐动作审批、取消和关闭等待；不自动使用宿主登录态 |
| 内建协作／跨会话消息 | 保持关闭；用 subagents / session.tasks 编排，由宿主路由 notify | 不自动扫描本机会话目录；独立设计地址、授权、幂等、投递确认及存储隔离。notify 当前不持久化、不自动唤醒模型 |
| 动态 allow／ask 权限管理 | 暂不增加可变 Permissions；用显式 deny_rules、approver 和 PreToolUse hook | 只读免确认工具若也需询问须在 hook 中实现；后续规则 API 须定义优先级、并发切换与恢复授权来源，不能用普通 approver 宣称覆盖全部 ask 规则 |
| 后台 shell 与 DAG 统一管理 | 保持两类身份独立；DAG 用 tasks，shell 用模型任务工具及通知 | 确有统一管理场景时增加带类型的句柄与各自取消／退出契约，不直接合并任务 ID |

同时补上 SDK 对新增 x_search / deep_research 的显式关闭，防止内核默认开关扩大
SDK 工具面。显式的宿主 Tool / MCP 配置仍有效。以上延后项有替代路径或明确
接入前提，不阻塞下一阶段工具结果变换设计；也不意味着完整内核能力已经对齐。

## 剩余验收

- [x] P6.1 基础问答；P6.2 类型化输入与插话。
- [x] P6.2b 第一批：通知投递／查询／观察、空闲不可变快照、生命周期查询、事件导出。
- [x] P6.2b 第二批：重置、模式切换、同端点模型切换、动态 token 软预算及已记录用量的恢复／fork 连续性。
- [x] 对“部分接入”项目给出明确取舍、替代入口和后续验收条件；本批补齐可选计划工具，不把本清单等同于全部已完成。
- [ ] 当前源码新增接口提交、远端多平台验证与发布；本机回归通过不能替代发布状态。

工具结果文本变换首版已实现，见 [变换契约](sdk-result-transforms.md)；持久化待答
已实现 SQLite、可选前台等待、安全边界接纳与版本观察，见 [问答契约](sdk-deferred-questions.md)。
serve 首版接入见 [HTTP 问答](serve-questions.md)；文件快照持久化与更广 schema 支持仍分别排期，不作为已有内核能力接入的
替代项。当前使用方式见 [SDK 指南](sdk.md)。

2026-10-03 本机验证：内核顶层公开导出均在本清单列出；通知／观察新增 13 项
契约测试通过，全量离线 unittest 运行 3,682 项，跳过 1 项，无失败；SDK 与示例
共 22 个源文件 mypy、离线观察示例通过。尚未执行真实模型和远端多平台验证。

第二批本机验证：新增会话控制测试 14 项通过，与既有 SDK／存储／任务／账本测试
合计 108 项通过；23 个源文件 mypy、离线控制示例通过；本机全量回归
3,696 项通过（跳过 1 项，221.381 秒）。

第三批本机验证：新增计划契约测试 10 项通过，与控制／观察／回滚测试合计 57 项
通过；24 个源文件 mypy、离线计划示例通过；全量回归 3,706 项通过
（跳过 1 项，214.631 秒）。测试与示例已加入 SDK CI，远端验证和发布未执行。
