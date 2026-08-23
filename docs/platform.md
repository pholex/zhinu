# 把小羽当平台：三张脸怎么选

> **小羽是织手（Agent），但她带来的织机（Platform）更值得你看。**
> *Xiaoyu is a weaver (agent) — but it's her loom (platform) you can build on.*

小羽不只是一个命令行工具。它的内核——会话状态、流式执行、工具调用、沙箱与审批
策略——可以嵌进你自己的产品里：运营控制台、安全台、客服后台、CI 流水线。你保留
自己的界面、业务上下文和边界，小羽只当执行引擎。

三条集成面，同一个内核：

| 你是谁 | 用哪张脸 | 入口 | 文档 |
|---|---|---|---|
| **脚本 / CI / 定时任务**：跑一次、拿结果、退出 | 一次性执行 | `xy "…" --output-format json [--output-schema …]` | [README](../README.md#用) |
| **Python 进程**：agent 跑在你的进程里，自己管生命周期 | 嵌入 SDK | `xiaoyu.embedding.AsyncAgent`（`send` / `stream` / `interrupt` / `steer` / `restore`） | [embedding.py](../xiaoyu/embedding.py) 模块 docstring |
| **别的语言 / 别的进程 / 编排器**（n8n、Dify、LangChain、自研平台） | 服务 | `xiaoyu serve`：REST + `/mcp` | [http-api.md](http-api.md) · [mcp-server.md](mcp-server.md) |
| **编辑器 / IDE** | ACP | `xiaoyu --acp`（stdio JSON-RPC） | [acp-registry/](acp-registry/) |

选型一句话：**能在同一个 Python 进程里就嵌入；跨进程/跨语言就 serve；只要结果不要
过程就一次性执行。** ACP 是给编辑器的，应用集成不走它。

---

## 三张脸共用的四件东西

换传输不换契约——下面四样在哪张脸上都是同一套语义。

**1. 事件流**：`text.delta` / `tool.pending` / `tool.completed` / `run.completed`…
TUI、`--output-format stream-json`、`AsyncAgent.stream()`、serve 的 `/events` 与 SSE
用的是同一份词汇。宿主的活动面板只需学一次。

**2. 审批回路**：需要放行的工具调用**挂起整一轮**，决定从宿主回来——一次性模式下是
终端确认或 `/allow` 规则，嵌入时是 `AsyncApprover` 协程，serve 上是 `POST /permissions`。
拒绝可以带 `reason`，原文回灌给模型，等于改指令。fail-closed：超时按拒绝。

**3. 结构化收尾（`output_schema`）**：给一个 JSON Schema，模型以符合它的对象收尾，
而不是一段正文。一次性模式 `--output-schema`，嵌入 `send(…, output_schema=)` →
`RunResult.output`，serve 的 `prompt` 请求体 `output_schema` → `result.output`。模型没给
则为 `null`，宿主据此判失败，不去解析自然语言。

**4. 边界**：沙箱（bash 在内核级沙箱里，只能写工作区）、folder trust（不信任的目录不吃
仓库里的 `.mcp.json` / hooks）、预算硬闸（serve 的 token/usd 预算越线即打断）。
这些在嵌入与 serve 上都开着，不因为"是程序在调"就放松。

---

## 把业务动作交给 agent：宿主自有工具

宿主最关心的不是让 agent 读文件，而是让它**操作自己的系统**——查运单、改工单、
调内部 API。正道是 **MCP**：把业务动作包成一个 MCP server，挂在 agent 上。

- **嵌入**：宿主自己构造 `ServerSpec` + `mcp.launch_specs()`，经 `Toolbox(mcp_view=…)`
  接线；或进程内直接注册 `xiaoyu.tools.Tool`（Python 函数即工具）。
- **serve**：agent 对象的 `mcp_servers` 字段（形状同 `.mcp.json` 的 `mcpServers`），
  随版本钉定，会话私有，关会话即收。服务端 `--agent-mcp http|all` 开放（默认不收，
  stdio 是沙箱外的本机子进程）。

所有 MCP 工具默认 **requires_approval**：业务动作一定经审批回路，由宿主 UI 决定放不放。
只读查询想免确认，用 `--mode auto` 配合 `/allow` 规则放行具体工具名。

agent 对象还承载人格（`append_system_prompt`，叠在服务端那份之上）、模式、审批档、
沙箱、预算——**配置即版本**：改了出新版本，在跑的会话钉着旧版不受影响，
审计时 `cat agents/agent-*.json` 即可。

---

## 一个完整样本

[`examples/ops-console/`](../examples/ops-console/)：运营控制台，两个纯标准库文件——
运单系统的 MCP server + 驱动 serve 的控制台（建 agent → 开会话 → 异步提交 →
审批走终端 → 渲染结构化报告）。把它当模板：换掉 MCP server 里的工具和控制台里的
UI，就是你的产品。

---

## 刻意不做的

- **不出别的语言的 SDK**：跨语言走 serve 的 REST / MCP（OpenAPI 由代码生成），
  一份契约比 N 份 SDK 可靠。
- **不另起一套"应用协议"**：REST 给编排器，MCP 给 agent 框架，ACP 给编辑器——三类消费方
  各有事实标准，不发明第四种。
- **不托管**：小羽跑在你的机器上，serve 默认只绑回环，对外必须带 token。
  多租户隔离、密钥托管这类平台层能力属企业版范围。
