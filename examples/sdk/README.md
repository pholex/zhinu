# SDK examples

先按 [SDK 指南](../../docs/sdk.md) 安装两个本地包。所有业务代码仅使用
`xiaoyu_agent_sdk` 公开入口。以下命令无需 API 凭据：

```sh
python examples/sdk/sync.py --demo
python examples/sdk/async_service.py --demo
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

`orchestration.py` 演示并发依赖任务、结果恢复、请求预算和遥测。
`mcp_management.py` 启动测试专用 stdio 服务，演示动态添加、调用、启停和删除。
它们使用临时工作区，不依赖外部 MCP 服务，接口说明见 [平台能力](../../docs/sdk-platform.md)。

去掉 `--demo` 并显式设置 `SDK_MODEL`、`SDK_API_KEY`、可选的 `SDK_BASE_URL` 可调用
真实模型。业务示例只提供只读订单查询工具，示例 approver 对它直接批准；实际接入时
替换为宿主 UI 或授权策略。
