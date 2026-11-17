# ops-console：把小羽嵌进自己的产品

一个运营控制台的最小样本——**宿主应用自己的界面 + 自己的业务工具 + 自己的审批流**，
小羽只当执行引擎。两个文件，纯标准库：

| 文件 | 角色 | 真实宿主换成 |
|---|---|---|
| `shipments_mcp.py` | 运单系统的 MCP server（stdio） | 对自家 API 的封装，或远端 Streamable HTTP server |
| `console.py` | 控制台：建 agent → 开会话 → 异步提交 → 轮询/审批 → 渲染结构化报告 | 你的前端 / 工单系统 / 聊天机器人 |

```bash
pip install "xiaoyu-agent[serve]"
mkdir -p /tmp/ops-ws
xiaoyu serve --workspace /tmp/ops-ws --agent-mcp all --mode auto
#  另一个终端
python examples/ops-console/console.py "把所有延误的运单改派给顺丰"
```

会看到：模型先 `list_shipments` 查延误单（只读，auto 档免确认），再要调
`reroute_shipment`——这是有后果的动作，**挂起等控制台放行**，终端问你 `[y/N]`；
放行后模型继续，最后按 `REPORT_SCHEMA` 交回一个对象，控制台直接渲染成表。

四个抓手各对应 serve 的一个能力（细节见 [docs/platform.md](../../docs/platform.md)）：

1. **agent 对象**带 `mcp_servers`——业务工具随 agent 版本钉定，改配置出新版本、在跑的会话不受影响；
2. **`prompt_async` + `/status` + `/events`**——宿主自己的节奏轮询，进度事件喂自己的活动面板；
3. **`/permissions`**——审批决定从宿主 UI 回来，拒绝的 `reason` 原文回灌给模型；
4. **`output_schema`**——结果是对象不是正文，宿主不解析自然语言。

`--agent-mcp all` 是因为样本用 stdio server（本机子进程）；生产里业务工具多是远端
HTTP server，用 `--agent-mcp http` 即可，不在小羽所在机器上起任何进程。
