# 浏览器桥（`xiaoyu serve` 的 `/session/{id}/browser`）

让 agent 反向操作用户**正在用的那个浏览器**：读页、点击、输入、截图、开标签页。
这是 serve 的又一条通道，契约在这里定义；[xiaoyu-chrome](https://github.com/pholex/xiaoyu-chrome)
是它的第一个实现，别的浏览器扩展照这份契约实现即可。

```
xiaoyu serve                                       浏览器扩展
────────────                                       ──────────
/session/{id}/browser  ◀── WebSocket（扩展主动连）──  hello: 我支持这些工具
  · 把扩展声明支持的工具注册进该会话的 Toolbox         ◀── call {id, tool, args}
  · 模型调用 = 经 socket 发 call、阻塞等 result         ──▶ result {id, ok, content|error}
  · 写类工具默认走审批回路；断线即注销
```

方向是定死的：**扩展不能监听端口**，只能扩展连 serve。桥**按会话**绑定——一台机器
可能同时开着几个侧栏 / 几个 Chrome profile，会话私有才不会串（与 agent 自带 MCP server
「会话私有、关会话即收」同一纪律）。

## 连接

- URL：`ws://127.0.0.1:8420/session/{session_id}/browser`
- 鉴权：浏览器的 WebSocket API 发不了自定义头，所以 token 放**第一帧** `hello` 里，
  不放 URL（会进日志）。`hello` 之前的任何其它帧 → 关闭码 `4401`
- 一个会话同一时刻只接一条桥连接；后来的把先来的顶掉（用户重开侧栏时旧连接可能还没死）
- 会话关闭 → 服务端发 `bye` 后关闭；扩展断线 → 该会话的浏览器工具立即注销、在途调用报错
- 心跳：服务端每 20s 发 WebSocket ping（uvicorn 默认），扩展照标准 pong 即可；20s 没 pong 视为断线
- 关闭码（应用段）：`4401` 未鉴权 / 第一帧不是 hello / 10s 内没发 hello；`4404` 未知会话；
  `4409` 被更新的连接顶掉；`4410` 会话已关闭。后两种前面都有 `bye`——扩展收到 `bye` 或
  `error` 就别再重连，其它关闭才按断线重连

## 报文（JSON 文本帧）

扩展 → 服务端：

```jsonc
{"type": "hello", "token": "…", "client": "xiaoyu-chrome/0.1.0",
 "supports": ["browser_tabs", "browser_read_page", "browser_click", …]}   // 支持的工具名子集
{"type": "result", "id": "c1", "ok": true,  "content": "…文本…"}
{"type": "result", "id": "c1", "ok": true,  "content": "…", "image": {"media_type": "image/png", "data": "<base64>"}}
{"type": "result", "id": "c1", "ok": false, "error": "没有该站点的权限"}
```

服务端 → 扩展：

```jsonc
{"type": "hello.ok", "session_id": "…", "registered": ["browser_tabs", …],   // 实际注册上的，顺序按服务端清单
 "ignored": ["…"], "timeout": 60}                                             // 未知名字；服务端等 result 的秒数
{"type": "error", "message": "token 不对"}                                   // 随后关闭
{"type": "call", "id": "c1", "tool": "browser_click", "args": {"ref": "e12"}, "timeout": 60}
{"type": "bye", "reason": "session closed"}                                   // 或 "replaced by a newer connection"
```

- `id` 由服务端生成，`result` 原样带回；一个 `call` 恰好对应一个 `result`
- `timeout`（秒）是服务端等 `result` 的上限（`--browser-timeout`，默认 60），超时按工具错误
  回给模型；扩展应尽量在此之前回 `ok:false`，而不是让它超时。别把它调到 30 以下：
  `browser_navigate` / `browser_open` 要等页面 load，扩展侧的等待上限就有 20s
- `content` 是给模型看的文本；`image` 可选，仅 `browser_screenshot` 用

## 工具清单（v1，名字与 schema 由服务端定义）

| 工具 | 参数 | 返回 | 审批 |
|---|---|---|---|
| `browser_tabs` | — | 每行 `tab_id · 标题 · URL`，当前标签页标 `*` | 否 |
| `browser_open` | `url`（必填），`active`=true | `tab_id` | **是** |
| `browser_navigate` | `url`（必填），`tab_id` | 加载完成后的标题 | **是** |
| `browser_read_page` | `tab_id`，`mode`=`text`\|`interactive`，`max_chars`=12000 | 见下 | 否 |
| `browser_click` | `ref`（必填），`tab_id` | 点击后页面标题 / URL 是否变化 | **是** |
| `browser_type` | `ref`、`text`（必填），`submit`=false，`tab_id` | 同上 | **是** |
| `browser_screenshot` | `tab_id` | `image` + 一行说明 | 否 |

- `tab_id` 缺省 = 扩展侧栏当前所在的标签页（扩展决定，不由模型猜）
- `browser_read_page`：`text` 模式返回标题、URL、正文（`main/article` 优先，超长截断并注明）；
  `interactive` 模式在正文之外列出可交互元素，每个带 **ref**：`[e12] button "提交"`、
  `[e13] input[type=email] placeholder="邮箱"`。ref 由扩展在页面里编号并记住（同一 tab 内
  导航前稳定），`click` / `type` 只认 ref，不认选择器——选择器让模型猜，猜错就点错地方
- 只读三件（tabs / read_page / screenshot）默认免审批；四件写类默认 `requires_approval`，
  走既有 `/permissions` 回路。用户想免确认，`--mode auto` 配 `/allow browser_click` 之类规则
- 扩展对 `chrome://` / 扩展页 / 无权限站点一律回 `ok:false`，说明原因；不要静默做半截
- 站点权限要提前拿：`permissions.request` 需要用户手势，agent 调用时没有手势可用。
  扩展应在用户打开「浏览器桥」开关那一下把 `<all_urls>`（或用户选的站点）申请下来

## 服务端行为

- `GET /session/{id}/status` 的 `browser` 字段：`null` = 没连桥；否则
  `{"connected": true, "client": "xiaoyu-chrome/0.1.0", "tools": [...], "in_flight": 0}`
- 事件流多两种：`browser.connected`（带 `client` / `tools`）与 `browser.disconnected`

- 扩展 `hello` 后，`supports` ∩ 清单 才注册进**该会话**的 Toolbox；模型看到的工具描述、
  schema 全部来自服务端，扩展只声明"我做得了哪些"
- 调用 = 经 socket 发 `call` → 工作线程阻塞等 `result`（与审批同一等法）→ 文本（及图片）
  作为工具输出回模型；`image` 走多模态工具结果（同 TUI 贴图）
- 断线：在途调用立即以错误收场；之后模型再调 → 工具错误「浏览器未连接」（工具已注销，
  正常情况下模型看不到它们）
- 事件流照旧：`tool.pending / permission.requested / tool.running / tool.completed`，
  侧栏不需要为浏览器工具另画一套

## 刻意不做

- **不做 CDP 直连**（`--remote-debugging-port` / chrome-devtools-mcp）作为正式通道：
  拿不到用户的登录态，且要用户改启动参数。验证想法可以先挂它当 MCP server
- **不做「录制回放」/ 宏**：那是另一类产品
- **不做跨会话共享的浏览器**：会话私有是纪律
- **不做按工具分别的超时**：一个 `timeout` 覆盖全部；扩展知道每个工具自己要等多久，
  在上限之前自己回 `ok:false` 即可
