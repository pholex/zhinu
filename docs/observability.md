# 可观测性：OpenTelemetry 导出

小羽能把每一轮、每一次模型调用、每一次工具调用按 [OpenTelemetry GenAI 语义约定](https://opentelemetry.io/docs/specs/semconv/gen-ai/)
打成 span，推到**你自己的** collector（Jaeger / Grafana Tempo / Datadog / 任何吃 OTLP 的后端）。
一个 `-p` 跑了 40 秒，慢在排队等首 token、慢在吐字、还是慢在某个工具上，看瀑布图一眼分清；
serve 多会话、七襄并发子 agent 的调用关系也串在同一棵 trace 里。

**这不是遥测。** 目的地只能是你用标准 `OTEL_*` 变量指定的地址；一个变量都没设就一个字节不发，
连 `opentelemetry` 包都不 import（[安全模型](security.md) 里"无遥测"的口径见那边）。

## 开法（五分钟本地验证）

```bash
pip install "xiaoyu-agent[otel]"

# 本地起一个 Jaeger（自带 OTLP 接收与 UI）
docker run --rm -d --name jaeger -p 16686:16686 -p 4318:4318 jaegertracing/jaeger:latest

export OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:4318
xiaoyu -p "看一下这个仓库的 README 讲了什么"
# 打开 http://127.0.0.1:16686，Service 选 xiaoyu
```

不想起 collector、只想看 span 长什么样：

```bash
OTEL_TRACES_EXPORTER=console xiaoyu -p "回复 ok"   # span 以 JSON 打到 stderr
```

这些变量放在 `.env` 里同样生效（[配置](configuration.md)）。所有前端共用一套接线——TUI、
明文 REPL、`-p`、`xiaoyu serve`、ACP、[嵌入 SDK](embedding.md)——因为它们都经过同一个事件出口。

## 变量

全部是 OpenTelemetry 的标准变量，没有小羽自造的开关：

| 变量 | 说明 |
|---|---|
| `OTEL_EXPORTER_OTLP_ENDPOINT` | OTLP 基地址，自动拼 `/v1/traces`。设了就激活 |
| `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` | traces 专用地址，原样使用，优先于上一条 |
| `OTEL_TRACES_EXPORTER` | `otlp`（默认，要有 endpoint）/ `console`（stderr，调试用）/ `none`（关）。其它值不支持，按关处理 |
| `OTEL_SDK_DISABLED` | `true` = 总关，优先级最高 |
| `OTEL_EXPORTER_OTLP_PROTOCOL` | 只支持 `http/protobuf`（默认）；设成 `grpc` 会在 stderr 提示一次并跳过导出 |
| `OTEL_SERVICE_NAME` | 资源里的 `service.name`，默认 `xiaoyu`；`service.version` 自动带小羽版本 |
| `OTEL_EXPORTER_OTLP_HEADERS` / `OTEL_RESOURCE_ATTRIBUTES` / `OTEL_EXPORTER_OTLP_TIMEOUT` 等 | 由 OTel SDK 自己认，照官方文档用 |
| `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT` | `true` = 把用户提示、工具参数、工具结果写进属性（见下文隐私一节）。默认不采 |

配了 endpoint 但没装包：stderr 一行 `pip install "xiaoyu-agent[otel]"` 提示，照常运行。

## span 树

一轮对话（一次 `send()`）就是一棵子树：

```
invoke_agent xiaoyu                       一轮
├── chat deepseek-flash                   第 1 次模型请求（重试各算一次）
├── execute_tool read_file                工具调用（审批等待也算在内）
├── execute_tool explore                  触发子 agent 的那次工具调用
│   └── invoke_agent explore              子 agent 的轮挂在它下面
│       ├── chat deepseek-flash
│       └── execute_tool grep
└── chat deepseek-flash                   第 2 次模型请求
```

- 每轮一个 `invoke_agent <agent 名>`，主 agent 叫 `xiaoyu`，子 agent 用它的名字（`explore` /
  委托的 agent 名 / 宸枢成员名）。serve 多会话各自成树，靠 `session.id` 区分。
- 子 agent 的轮以**触发它的工具调用 span** 为父——explore、单发委托在父级线程里构造，
  挂接精确；七襄 / 宸枢把子 agent 构造在工作线程里，按父子共享的用量账本找父级正在跑的
  工具 span（同深度兄弟并发时孙辈可能挂错，默认深度下没有孙辈）；无论挂没挂上，
  `xiaoyu.parent_session.id` 总是准的。
- 被 deny 规则或用户拒绝的工具调用也是一个 span（status=ERROR，`error.type=denied_by_rule|denied_by_user`）。
- 失败的模型请求 status=ERROR，`error.type` 是小羽的错误分类（`rate_limit` / `transient` /
  `context_overflow` / `quota` / `auth` / `fatal` / `interrupted`）；重试后成功的那次是另一个 span。

## 属性清单

`gen_ai.*` 严格按约定现版；小羽特有的信息用 `xiaoyu.` 前缀。

**invoke_agent（轮）**

| 属性 | 值 |
|---|---|
| `gen_ai.operation.name` | `invoke_agent` |
| `gen_ai.agent.name` | `xiaoyu` / 子 agent 名 |
| `gen_ai.provider.name` | 本轮用到的 provider（跨多家时取字典序第一家，全集在 `xiaoyu.providers`） |
| `gen_ai.request.model` | 会话配置的模型 |
| `gen_ai.conversation.id` / `session.id` | 会话日志名（`--session-id` 起的名字或文件名） |
| `gen_ai.usage.input_tokens` / `output_tokens` / `cache_read.input_tokens` | 轮内各请求合计 |
| `xiaoyu.mode` | 本轮的模式（auto / default / plan …） |
| `xiaoyu.subagent.depth` | 0 = 主 agent |
| `xiaoyu.parent_session.id` | 子 agent 才有：父会话 |
| `xiaoyu.turn.requests` / `xiaoyu.turn.tool_calls` | 轮内请求数 / 工具调用数 |
| `xiaoyu.models` / `xiaoyu.providers` | 轮内用到的模型 / provider 全集 |
| `gen_ai.input.messages` | 仅开了内容采集：用户输入 |

**chat（每次模型请求）**

| 属性 | 值 |
|---|---|
| `gen_ai.operation.name` | `chat` |
| `gen_ai.provider.name` | 约定里的已知名（`anthropic` / `openai` / `x_ai` / `deepseek` / `gcp.gemini` / `aws.bedrock` / `moonshot_ai`），网关与其它厂商用小羽里的 provider 名 |
| `gen_ai.request.model` / `gen_ai.response.model` | 请求的模型 / 响应里报的模型（网关改写时两者不同） |
| `gen_ai.request.stream` | `true` |
| `gen_ai.response.id` | 上游响应编号 |
| `gen_ai.response.finish_reasons` | `["stop"]` / `["tool_calls"]` / `["length"]` … |
| `gen_ai.response.time_to_first_chunk` | 首 chunk 延迟（秒）；同时打一个 `gen_ai.first_chunk` 事件 |
| `gen_ai.usage.input_tokens` / `output_tokens` / `cache_read.input_tokens` | 这次请求的用量；上游没回 usage 就不写 |
| `error.type` | 失败分类（见上） |

**execute_tool（每次工具调用）**

| 属性 | 值 |
|---|---|
| `gen_ai.operation.name` | `execute_tool` |
| `gen_ai.tool.name` / `gen_ai.tool.call.id` / `gen_ai.tool.type` | 工具名 / 调用 id / `function` |
| `xiaoyu.tool.outcome` | `ok` / `error` / `denied` / `abandoned`（轮异常结束时还没收尾的） |
| `xiaoyu.tool.denied_by` | `rule` / `user` |
| `xiaoyu.tool.seconds` | 真正执行的耗时（span 本身从收到调用起算，含审批等待；两者之差就是等人的时间） |
| `error.type` | `tool_error` / `denied_by_rule` / `denied_by_user` |
| `gen_ai.tool.call.arguments` / `gen_ai.tool.call.result` | 仅开了内容采集 |

span 上还有一个 `running` 事件：通过全部关卡、即将真正执行的那一刻。

## 内容采集与隐私

默认**只有结构，没有内容**：模型名、token 数、耗时、工具名、成败——没有用户说了什么、
工具读了什么。这与约定一致：内容属性是 Opt-In。

`OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=true` 后，用户输入、工具参数、
工具结果会写进属性，每项先过小羽现有的凭据脱敏（`sk-…` / Bearer / URL 账号密码 /
`api_key=…` 等换成 `[REDACTED]`）再截到 4KB。脱敏是按模式匹配的兜底，不是保证——
collector 那条线从此是数据出口之一，开之前想清楚它通向哪里。

## 运行期行为

- 导出走 BatchSpanProcessor：发出 span 只是入队，主循环永远不等网络。
- 会话收尾（`end_session` / 进程退出）把队列推出去，**封顶 2 秒**；collector 不在时
  退出不会挂住，没发出去的 span 丢掉。
- 导出失败在 stderr 提示一次，之后静默——它不影响任何功能，不值得刷屏。
- 这套接线与 `xiaoyu-agent-sdk` 的 `TelemetryOptions`（[SDK 平台能力](sdk-platform.md)）
  是两条路：SDK 那条由宿主自带 provider、按宿主的会话模型导出；这条由内核按标准变量
  自装 provider，所有前端通用。两条同时开会各导各的。

## 与「无遥测」的区别

小羽没有任何形式的上报：不统计、不打点、没有默认目的地。OpenTelemetry 导出只在你
显式写了 `OTEL_EXPORTER_OTLP_ENDPOINT`（或 `OTEL_TRACES_EXPORTER=console`）时才存在，
发往的也只是那个地址。没设就等于这个功能不存在——包都不会加载。
