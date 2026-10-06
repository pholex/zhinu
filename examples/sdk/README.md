# SDK examples

先安装 SDK（会按同版本自动带上 `xiaoyu-agent`），再在仓库根目录运行示例。
从源码开发时的可编辑安装方式见 [SDK 指南](../../docs/sdk.md)。

```sh
pip install xiaoyu-agent-sdk
```

所有业务代码仅使用 `xiaoyu_agent_sdk` 公开入口。以下命令无需 API 凭据：

```sh
python examples/sdk/sync.py --demo
python examples/sdk/async_service.py --demo
python examples/sdk/questions.py --demo
python examples/sdk/deferred_questions.py --demo
python examples/sdk/inputs.py --demo
python examples/sdk/observation.py --demo
python examples/sdk/controls.py --demo
python examples/sdk/planning.py --demo
python examples/sdk/result_transforms.py --demo
python examples/sdk/cache_report.py --demo
python examples/sdk/storage.py --demo
python examples/sdk/orchestration.py --demo
python examples/sdk/mcp_management.py --demo
python examples/sdk/business.py --demo --workspace /path/to/existing/workspace
```

业务示例在工作区的 `.sdk-sessions/` 写入日志，展示审批、异步工具、事件消费、一次
schema 修复和关闭。复制其打印的路径，在新 Python 进程恢复：

```sh
python examples/sdk/business.py --demo --workspace /same/workspace --resume /printed/path.jsonl
```

`storage.py` 使用临时 SQLite 数据库演示会话 ID 与重新打开后恢复，示例结束时清理
临时数据。此接口为 0.59.0 的平台扩展，见 [存储契约](../../docs/sdk-storage.md)。

`questions.py` 演示宿主提问回调；`--demo` 使用脚本答案，不请求输入或联网。
去掉 `--demo` 后通过控制台收集回答。此示例依赖 0.62.0 起提供的 `asker`
接口，问答与工具审批独立，见 [SDK 指南](../../docs/sdk.md)。

`deferred_questions.py` 演示问题持久化、关闭后恢复、显式提交、版本观察与下一轮接纳；--demo 还演示前台等待期间提交并在同轮接纳。
使用临时 SQLite 数据库和 0.62.0 起提供的接口；`--demo` 显式提交脚本答案，真实模式
由控制台输入回答，不把默认选中项当成用户提交。

通过 HTTP 接入的宿主使用独立的 [serve 示例](../serve/questions.py)，无需 SDK 包。
`python examples/serve/questions.py --demo` 提供离线验证；真实模式的创建、观察、
显式回答与继续命令见 [HTTP 问答指南](../../docs/serve-questions.md)。

`inputs.py` 演示类型化图片输入、运行中插话与接纳事件；`--demo` 不联网，使用内置
微型 PNG。`--image /path/image.png` 显式读取宿主图片，去掉 `--demo` 需配置支持视觉的
真实模型。依赖 0.62.0 起提供的接口；初始化 hook 中的等待仅用于确定性演示投递时机。

`observation.py` 演示空闲通知、合并变化后的待通知快照，以及会话状态和不可变历史
投影。通知本身不会启动模型，示例由宿主显式发起轮次；依赖 0.62.0 起提供的接口。

`controls.py` 演示空闲时切换模式、同一端点的模型名、token 软预算，以及保留身份／
用量的对话重置。`--demo` 完全离线；真实调用可用 `--model` 指定同端点的另一模型。
依赖 0.62.0 起提供的接口，详见 SDK 指南的会话控制部分。

`orchestration.py` 演示并发依赖任务、结果恢复、请求预算和遥测。
`planning.py` 演示显式启用计划工具、接收 `PlanUpdated`、读取不可变计划快照和重置；
依赖 0.62.0 起提供的接口，`--demo` 完全离线。
`result_transforms.py` 演示业务工具返回文本脱敏，使用 0.62.0 起提供的 ResultTransform。
`cache_report.py` 独立消费 RequestEnded，报告已上报的 prompt cache 使用量；不改变
执行或推算费用。两者分别移除注册项／观察调用即可关闭，不依赖内核私有对象。
数据边界见 [变换契约](../../docs/sdk-result-transforms.md)。
`mcp_management.py` 启动测试专用 stdio 服务，演示动态添加、调用、启停和删除。
它们使用临时工作区，不依赖外部 MCP 服务，接口说明见 [平台能力](../../docs/sdk-platform.md)。

去掉 `--demo` 并显式设置 `SDK_MODEL`、`SDK_API_KEY`、可选的 `SDK_BASE_URL` 可调用
真实模型。业务示例只提供只读订单查询工具，示例 approver 对它直接批准；实际接入时
替换为宿主 UI 或授权策略。
