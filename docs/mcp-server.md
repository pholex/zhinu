# MCP server（`xiaoyu serve` 的 `/mcp`）

把小羽**整个 agent 当一个工具**，暴露给 MCP client——LangChain / LangGraph
（经官方 `langchain-mcp-adapters`）、以及任何吃 streamable HTTP
的 MCP 消费方。

这是 serve（[HTTP API](http-api.md)）之上的第二张脸，不是又一条协议面：
REST 与 MCP **共用同一个会话注册表、同一套状态机与审批回路**。REST 面向
n8n / Dify 这类"发 HTTP、看字段"的编排器；MCP 面向"给模型挂工具"的 agent
框架。同一个会话从哪边建的，另一边都看得见、管得着。

```bash
pip install "xiaoyu-agent[serve]"
xiaoyu serve --workspace ~/code/myrepo
#  → REST http://127.0.0.1:8420  ·  MCP http://127.0.0.1:8420/mcp
```

不想要这张脸就 `--no-mcp`，serve 退回纯 REST。

---

## 工具面：三个工具，会话接力

照 agent 级 MCP serve 的成熟形态（两工具 + 收尾）做小而全：

| 工具 | 参数 | 干什么 |
|---|---|---|
| `xiaoyu` | `prompt`，可选 `workspace` / `model` / `mode` | 新会话跑一轮，返回结果 + `session_id` |
| `xiaoyu_reply` | `session_id`、`prompt` | 在已有会话续聊（上下文完整保留） |
| `xiaoyu_close` | `session_id` | 释放会话（唯一的回收口，收尾记得调） |

**对话身份放在参数里的 `session_id`，不在 MCP 传输层。** 这台 server 是
无状态的：不发 `Mcp-Session-Id`，`initialize` 幂等。langchain-mcp-adapters
默认每次工具调用新建一条连接——在这种消费方式下，靠传输层会话的 server
根本接不了续聊，靠参数的天然免疫。

返回值同时给两种形态：`content` 里的纯文本给模型读，`structuredContent`
（有 `outputSchema` 背书）给代码解析——`session_id` / `text` / `status` /
`detail` / `turns` / `error`。

## 长任务：progress 通知续命

coding agent 一轮动辄几分钟，MCP client 普遍有请求超时。`tools/call` 一律
以 SSE 应答，请求的 `_meta.progressToken` 带上后，事件流会折成
`notifications/progress` 逐站上报（`tool.pending foo` / `tool.completed foo` /
`permission.requested` / `run.completed`…，`text.delta` 这类碎屑不报）——
多数客户端以 progress 为超时重置依据。全量事件仍在 REST 的
`GET /session/{id}/events`。

## 审批：MCP 侧挂起，REST 侧放行

`ask` 档（默认）下，需要人工放行的工具调用会让 `tools/call` 挂在
`waiting_for_approval`（progress 里报 `permission.requested` 这一站），由
REST 侧兑现：

```bash
curl -s $BASE/session/$SID/permissions -H "$AUTH"          # 看挂起的审批
curl -X POST $BASE/session/$SID/permissions -H "$AUTH" \
  -d '{"request_id":"...","decision":"allow"}'             # 放行
```

超时（`--approval-timeout`，默认 300s）按拒绝收场——fail closed，这一轮
正常收尾，只是被拦的工具没执行。

**刻意不把 approval 做成工具参数**：MCP 工具的参数是模型填的，让模型能给
自己选免审批档，等于把闸门的钥匙挂在闸门上。无人值守场景在**启动期**由人
决定：`xiaoyu serve --approval allow_all`。

## 接 LangChain / LangGraph

```bash
xiaoyu serve --host 0.0.0.0 --token "$(openssl rand -hex 16)" --workspace ~/code/myrepo
pip install langchain-mcp-adapters langgraph
```

```python
from langchain_mcp_adapters.client import MultiServerMCPClient

client = MultiServerMCPClient({
    "xiaoyu": {
        "transport": "streamable_http",
        "url": "http://127.0.0.1:8420/mcp",
        "headers": {"Authorization": "Bearer <token>"},
    }
})
tools = await client.get_tools()   # → xiaoyu / xiaoyu_reply / xiaoyu_close

# 直接喂给 LangGraph 的 agent：
from langgraph.prebuilt import create_react_agent
agent = create_react_agent("anthropic:claude-sonnet-5", tools)
result = await agent.ainvoke(
    {"messages": [{"role": "user", "content": "让小羽修掉 src/foo.py 的类型错误并跑测试"}]}
)
```

把小羽当 **LangGraph 里的一个 sub-agent 节点**（不给模型选工具，代码直调）：

```python
async def xiaoyu_node(state):
    tools = {t.name: t for t in await client.get_tools()}
    first = await tools["xiaoyu"].ainvoke({"prompt": state["task"]})
    # first 是 structuredContent：{"session_id": ..., "text": ..., ...}
    return {"result": first["text"], "xiaoyu_session": first["session_id"]}
```

审批要接人就把 `waiting_for_approval` 映射到 LangGraph 的 `interrupt()`：
graph 挂起 → 人从 REST `/permissions` 放行 → 恢复。状态判定用 REST 的
`GET /session/{id}/status`（这正是两张脸共用会话层的意义）。

⚠️ 超时：adapters 底层是 httpx，默认读超时较短。长任务给 client 配大一点的
`timeout`，或让调用方带 `progressToken`（有 progress 帧就不算静默）。

## 协议范围（刻意做小）

- 方法只有五个：`initialize`、`notifications/*`（收下回 202）、`ping`、
  `tools/list`、`tools/call`。
- 能力只报 `tools`。sampling / roots / resources / elicitation 都不做——与
  client 侧（mcp.py）"不接反向使唤通道"同一纪律；审批走 REST 回路，不走
  elicitation（主流客户端不支持，做了没人能用）。
- 协议版本：2025-06-18 / 2025-03-26；JSON-RPC batch 不收（新 spec 已删除）。
- `GET /mcp` / `DELETE /mcp` 回 405：无状态 server，没有常驻推送通道，也没有
  可删的传输层会话。
- 鉴权与 REST 同一道门（`Authorization: Bearer <token>`），绑非回环地址必须
  给 token 的启动闸同样管着 `/mcp`。
- `/mcp` 不出现在 `/openapi.json` 里——那份 schema 是给 Dify / n8n 的 REST
  工具清单，别把 JSON-RPC 端点混进去。

## 会话回收

与 REST 同一条纪律：**会话不会自动过期**。编排流程收尾时调 `xiaoyu_close`
（或 REST 的 `DELETE /session/{id}`），否则消息历史与事件缓冲一直占着内存。
