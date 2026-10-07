# 配置

README 只给最小可跑配置，这里是全量。

## 配置文件与优先级

```bash
xiaoyu config             # 交互向导：直连 key / 网关端点 / 模型；落盘前先对主模型发一条最小请求验证
xiaoyu config --no-probe  # 向导不探测、直接写入（离线填配置、端点暂时不通时用）
xiaoyu config --show      # 看生效配置与每项来源（key 永不回显）
xiaoyu config --show --json   # 机器可读：默认模型、各模型路由（provider / 协议 / base_url / 有无 key / 视觉与工具能力 / 上下文上限 / effort 档位）、功能开关；密钥值不出现
xiaoyu config --path      # 打印用户级配置文件路径
xiaoyu config --set XIAOYU_MODEL=deepseek-flash   # 非交互写入，可重复
```

向导问完之后、写文件之前，默认按你刚填的配置对主模型发一条最小请求（与 `xiaoyu doctor --probe` 同一条路径，花一点点 token）：通过就显示耗时并保存；失败则显示分类后的原因（鉴权、端点不通、模型名不存在……）并问「仍要保存吗？[y/N]」，默认不保存。key 贴错一位、端点少个 `/v1`、模型名在网关上拼错，这些都在这一步被抓住，而不是等第一轮对话才炸。

用户级 `.env` 的位置：macOS / Linux 在 `~/.config/xiaoyu/.env`（跟随 `$XDG_CONFIG_HOME`），Windows 在 `%APPDATA%\xiaoyu\.env`。也可以手动在任意工作目录放 `.env`（零依赖自解析）。行格式 `KEY=值`，整行与行尾的 `# 注释` 都认（行尾注释要与值隔一个空白；值里紧挨着的 `#` 和引号里的 `#` 是内容）。

优先级：**真实环境变量 > 当前目录 `.env` > 项目根 `.env` > 用户级 `.env`**，所以临时覆盖很方便：

```bash
XIAOYU_MODEL=kimi-k3 xiaoyu
```

## 直连厂商

内置直连：deepseek / moonshot / qwen / zhipu / anthropic / gemini / openai / xai，以及 AWS Bedrock（只凭 AWS 凭证，见下方）。**键名一律用厂商原生名**（别家工具已配过的直接复用）：

```ini
DEEPSEEK_API_KEY=<key>
MOONSHOT_API_KEY=<key>
OPENAI_API_KEY=<key>
ANTHROPIC_API_KEY=<key>
GEMINI_API_KEY=<key>
```

每家走哪种 wire 协议（chat completions / Responses / Anthropic Messages）、哪些型号能看图，都是按型号内置好的，不用管。

### AWS Bedrock：AWS 凭证链或 Bedrock API key

Bedrock 直连走 bedrock-runtime 的原生 Messages 协议，两种鉴权任选：

```ini
# ① 只凭 AWS 凭证链（IAM 执行角色 / profile / SSO），不需要任何 key——AWS 容器里的自然形态
XIAOYU_BEDROCK_REGION=us-east-1        # 激活信号 = 区域；写 1 / default 等价于 us-east-1
# ② Bedrock API key（控制台生成的长期 key）：找到 key 即激活，区域缺省 us-east-1
AWS_BEARER_TOKEN_BEDROCK=<key>         # AWS 官方变量名
XIAOYU_MODEL=global.anthropic.claude-fable-5-1
```

```sh
pip install 'xiaoyu-agent[bedrock]'     # 仅 ① 需要：SigV4 签名要 botocore；缺包时首个请求前会提示
```

要点：

- 有 key 用 key，没 key 走凭证链。① **必须显式设区域**，不自动嗅探 AWS 凭证——机器上有 AWS profile 不等于想让模型请求走 Bedrock。
- 区域只认 `XIAOYU_BEDROCK_REGION`，刻意不读 aws cli 的 `AWS_REGION` / `AWS_DEFAULT_REGION`（几乎每台 AWS 机器都有，读了等于有 AWS 环境就自动换路由）。
- Bedrock 有 bedrock-runtime 与 Bedrock Mantle 两种端点，这里走的是 runtime。
- 内置型号只有 `global.anthropic.claude-fable-5-1`（anthropic 直连已有的 opus-5-5 / sonnet-5-5 不重复收）。其它推理 profile 或 ARN 用显式寻址：`/model bedrock/us.anthropic.claude-opus-5-5`。
- 模型 id 要用**推理 profile**（`global.` / `us.` / `eu.` 前缀）——裸 id `anthropic.claude-fable-5-1` 在 Bedrock 上不能按需调用，会 400。
- 没有凭证、模型未开通、区域不支持时如实报错（NoCredentials / 403 / 400），不会悄悄换到别的模型；换路由仍只按 `XIAOYU_FALLBACK_MODELS` 的显式降级链走。
- 本机验证：`AWS_PROFILE=<profile> XIAOYU_BEDROCK_REGION=us-east-1 xiaoyu -m global.anthropic.claude-fable-5-1`。
- 也认 `AWS_BEARER_TOKEN_BEDROCK`（Bedrock API key）——anthropic SDK 会优先用它代替 SigV4。

## 网关

任意 OpenAI 兼容端点（LiteLLM、vLLM、各家官方 API…）：

```ini
XIAOYU_BASE_URL=https://<你的网关>/v1
XIAOYU_MODEL=<你网关上的模型名>
XIAOYU_API_KEY=<key>
```

网关上的模型名含 `claude` / `anthropic` 时，发出的 chat 载荷会自动带 Anthropic 的缓存断点（`cache_control`：system、tools 末尾、尾部消息、往前约 20 条处的次锚点）——Anthropic 的 prompt cache 不是自动的，经网关走 chat 协议时没有断点就一个 token 都不缓存（对 LiteLLM 实测透传并命中）。网关不是 LiteLLM、拒收这个字段时设 `XIAOYU_GATEWAY_CACHE_CONTROL=0` 关掉。

直连和网关至少配一个，两个都配见下方"多 provider"。端点在**本机**（`localhost` / `127.0.0.1` / `::1` / `*.localhost`，如 vLLM、SGLang、Ollama）时 key 可以不填——远端地址缺 key 仍视为没配、不注册。

## 变量总表

### 模型与端点

| 变量 | 默认值 | 说明 |
|---|---|---|
| `XIAOYU_MODEL` | `deepseek-flash` | 主模型 |
| `XIAOYU_SUMMARY_MODEL` | `deepseek-flash` | 压缩摘要用的便宜模型 |
| `XIAOYU_EXPLORE_MODEL` | `deepseek-flash` | `explore` 子 agent 用的模型 |
| `XIAOYU_BASE_URL` | — | OpenAI 兼容网关端点 |
| `XIAOYU_API_KEY` | — | 网关 key（也认 `LITELLM_API_KEY`） |
| `XIAOYU_BEDROCK_REGION` | —（有 key 时 `us-east-1`） | AWS Bedrock 区域，设了即激活 IAM 路线（`1` / `default` = `us-east-1`）；见上方"AWS Bedrock" |
| `AWS_BEARER_TOKEN_BEDROCK` | — | Bedrock API key（AWS 官方变量名），找到即激活 Bedrock 直连 |
| `XIAOYU_FALLBACK_MODELS` | —（不降级） | 备用模型链，逗号分隔，主模型重试耗尽后依次切。委托出去的子 agent（含 explore、七襄、斗巧、宸枢成员）沿用同一条链，即使那次委托另外点名了模型 |
| `XIAOYU_PROVIDERS` | 直连 → 网关 | 覆盖 provider 优先级（如 `gateway,deepseek` = 临时全走网关） |
| `XIAOYU_VISION_MODELS` | — | 网关后面挂的视觉模型点名（`*` = 一律放行） |
| `XIAOYU_SIGNATURE_MODELS` | — | 网关后面挂的签名型号点名（Gemini 系，工具重放需带回 thought_signature；`*` = 一律） |
| `XIAOYU_VISION_FALLBACK` | —（不代读） | 代读模型：当前模型看不了图时，把图先交给它换成一段文字（见下方"图片代读"） |
| `XIAOYU_ENV_FILE` | — | 指定 `.env` 路径，等价 `--env-file` |
| `XIAOYU_TERM_SESSION` | — | 终端集成的会话 id，由 `eval "$(xiaoyu term init <shell>)"` 导出（随机 `term-<8 位>`，或 `--name` 指定的 `term-<名字>`）；`@x` / `xiaoyu term run` 按它续写会话、`xiaoyu term log|info` 按它找 pending 文件。配套的 `XIAOYU_TERM_PENDING` 是钩子追加命令的文件路径，同样由脚本导出，不必手设。见[终端集成](terminal-integration.md) |

### 上下文与压缩

| 变量 | 默认值 | 说明 |
|---|---|---|
| `XIAOYU_EFFORT` | 不传 | 推理深度 `low / medium / high / xhigh / max`（OpenAI 线另有 `none / minimal`）。同一个名字出内核，按协议翻译成 `reasoning_effort` / `reasoning.effort` / `output_config.effort`；你给自己点名的模型配的取值原样发，上游不认会 400；换到降级链上的模型、或由子 agent 继承过去时，对实测过档位范围的型号就近换成它认的一档并提示（没实测过的型号不改）。命令行 `--effort`，会话里 `/effort`，子 agent 可在 spec 里单独声明 |
| `XIAOYU_CONTEXT_LIMIT` | 按模型查表 | 上下文上限（token）覆写 |
| `XIAOYU_MAX_OUTPUT_TOKENS` | 不传（Claude 原生协议用内置常量：流式 64000 / 同步 16000） | 单次请求输出 token 上限覆写（正整数）。本地小模型、中转站常限输出上限，超了直接 400。按协议翻译：chat `max_tokens`、Responses `max_output_tokens`、Anthropic `max_tokens`；摘要/收尾这类同步请求取 min(设值, 16000) |
| `XIAOYU_COMPACT_AT` | `0.7` | 用量占到这个比例时触发回收/压缩；取 0.05~1 的比例，写成 `70` 这类整数会被忽略并在启动时提示。用量到压缩阈值的 50% / 80% 时模型各收到一次余量提示（operator 通道，不碰 system prompt），让它在压缩前合并读取、先把结论落下来；压缩/回滚后按现状重定基线 |
| `XIAOYU_BUDGET_TOKENS` | 不限 | 本会话 token 软预算（prompt+completion 累计，≥5000 才生效）：模型按 50/80/95% 收到倒计时（operator 通道），到线前一步优雅收尾交代现场，而不是被硬闸中途砍断；直连支持型号（Opus 5/4.8/4.7/Fable/Mythos/Sonnet 5）另附 Anthropic 原生 `task_budget`（服务端倒计时）。命令行 `--budget-tokens` |
| `XIAOYU_TURN_EXTENSION` | `1.0` | 撞 `max_iterations` 时允许模型调 `extend_turns` 申请追加轮数，总追加量 ≤ `max_iterations ×` 此系数；`0` = 不许延期（撞顶即收尾）。理由展示给用户、可审计。轮数用到上限的 50% / 80% 时模型各收到一次「轮数 N/M」提示（每轮各一次） |
| `XIAOYU_SERVER_COMPACTION` | `1` | 直连 Claude（opus-4.6+/sonnet-4.6+/5 系）时把压缩交给服务端（模型自己写摘要，`compaction` 块下轮回传，服务端忽略块前历史）；本地摘要压缩降为兜底。设 `0` 回纯本地压缩 |
| `XIAOYU_KEEP_RECENT` | `8` | 压缩时至少保留最近几条消息 |
| `XIAOYU_FIRST_CHUNK_TIMEOUT` | `300` | 首 chunk 看门狗（秒）：请求发出后等第一个流事件超过它就中止本次尝试、按瞬时错误走既有重试/降级链，报错点名"首 chunk 等待超过 300s（XIAOYU_FIRST_CHUNK_TIMEOUT）"；`0` = 关。实现是收紧这次请求的读超时（等首 token 时进程阻塞在一次 socket 读上，不起线程就只有它能打断），所以看门狗生效时流内两个 chunk 之间的等待上限也是这个数；比单次请求超时（600s）长时不生效 |
| `XIAOYU_MAX_IMAGES_PER_REQUEST` | `20` | 每次请求最多发出去几张图（`0` = 不限）：只投影发出去的副本——更早的图换成一行"[图片已省略：第 N 张…]"占位，历史与会话文件照旧带图，`/compact` 或用户重贴随时能回来。内置厂商若声明了更小的张数上限，按较小的算。用户贴的图刻意不老化，没有这一层，贴图多的长会话会撞端点的张数上限（400 且每次重发同样 400） |
| `XIAOYU_EXPLORE_ITERATIONS` | `12` | `explore` 子 agent 单次检索的工具调用轮数上限（1–100；主 agent 的 50 轮不受影响） |
| `XIAOYU_QIXIANG_CONCURRENCY` | `4` | 七襄批量委托的并发上限（1–16） |
| `XIAOYU_QIXIANG_TIMEOUT` | `0` | 七襄单项任务墙钟超时（秒，从实际启动起算；`0` = 不限时） |
| `XIAOYU_CHENSHU_MAX_WORKERS` | `4` | 宸枢同时在跑的成员上限（worker + reviewer，1–16） |

### 功能开关（`0` = 关）

| 变量 | 说明 |
|---|---|
| `XIAOYU_ENABLE_EXPLORE` | `explore` 检索子 agent |
| `XIAOYU_ENABLE_SKILLS` | 扫描 `~/.agents/skills/`、工作区自带的 `.xiaoyu/skills/` 与 `.agents/skills/`（按 git 根 → 工作区逐层找，越靠近工作区优先；工作区不在 git 仓里只看它自己；与仓库级 `.mcp.json` 同受信任门）、已装插件包下的 SKILL.md |
| `XIAOYU_SKILLS_DISABLED` | 停用清单（不是开关）：逗号分隔的技能名，可通配，如 `lark-*,remotion-*,aws-core:*`。按带插件前缀的全名或目录名匹配。技能库是几家客户端共用的，要给索引腾预算时在这里点名，不必去删文件；`/skills` 会列出被停用的 |
| `XIAOYU_SKILLS_DIR` | 覆盖技能扫描目录（`os.pathsep` 分隔）：给了就只认它、不混默认目录，工作区自带的也不扫（宿主指定技能库 / 测试隔离用） |
| `XIAOYU_ENABLE_WEB_SEARCH` | `web_search` 工具 |
| `XIAOYU_ENABLE_X_SEARCH` | `x_search` 工具（默认开启，需 `XAI_API_KEY`；`0` 关闭），独立于网页搜索后端 |
| `XIAOYU_ENABLE_DEEP_RESEARCH` | Gemini 后台研究工具（默认开启，需 Gemini key；`0` 关闭），支持提交、查询报告与取消 |
| `XIAOYU_SEARCH_PROVIDER` | 搜索走哪家：`deepseek`（默认，deepseek-flash，需 `DEEPSEEK_API_KEY`）、`xai`（grok-4.7，需 `XAI_API_KEY`）或 `bedrock`（openai.gpt-5.6-luna，走 Mantle Responses）。DeepSeek 搜索单独走 Anthropic 兼容接口，主对话协议不变；其 Responses 接口不支持内置搜索。所选 provider 未注册时不挂载工具；Bedrock 鉴权与权限见下方 |
| `XIAOYU_ENABLE_BROWSER` | `browser` 浏览器工具（依赖可选 `[browser]` extra 的 playwright，没装时本来就不出现） |
| `XIAOYU_ENABLE_PLUGINS` | entry point 组 `xiaoyu.tools` 的第三方工具**包**（代码级；和 `xiaoyu plugin` 装的**内容包**不是一回事，见下） |
| `XIAOYU_ENABLE_MCP` | MCP server 挂载 |
| `XIAOYU_MCP_OSV` / `_WATCHDOG` / `_CACHE` / `_RECONNECT` | MCP 的恶意包预检 / 孤儿进程回收 / schema 缓存 / 断线自动重连 |
| `XIAOYU_MCP_TRUST_CHANGES` | **默认关**，`1` = 开：所有 MCP server 的工具描述/schema 变更自动接受、不再隔离等 `/mcp approve`（逐 server 版是声明里的 `trustToolChanges`；见[安全](security.md)） |
| `XIAOYU_MCP_TOOL_SEARCH` | MCP 工具检索模式（默认开：工具不进 schema，`search_tool` 检索 + `use_tool` 调用；`0` = 回到全量注册） |
| `XIAOYU_GATEWAY_CACHE_CONTROL` | 网关下型号名含 `claude` / `anthropic` 的后端：chat 载荷自动打 Anthropic 缓存断点（默认开，见上方"网关"）；`0` = 关 |
| `XIAOYU_UPDATE_CHECK` | 新版本提示（默认开）：交互式启动时每 24 小时至多查一次 PyPI，有新版在横幅后提一行；`-p`、`--wire`、serve、ACP、嵌入宿主不查。请求只带版本号，没有身份标识；同一个新版本每 24 小时至多提一次。`0` = 关 |
| `XIAOYU_FOLDER_TRUST` | 工作区信任门（默认开，见[安全](security.md)；只认真实环境变量与用户级 `.env`） |
| `XIAOYU_HARDLINE` | bash 硬红线（`rm -rf /`、`mkfs`、`dd of=/dev/…`，默认开、任何模式都拦）；`0` = 关，给隔离环境里的镜像烧录 / 格式化用（见[安全](security.md)） |
| `XIAOYU_SEARCH_SENSITIVE` | 搜索工具的敏感文件过滤（默认开）：`grep` / `list_files` 不把 `.env`、私钥、`.ssh/`、`.aws/credentials` 等读进上下文，起点是这类路径直接拒绝；`0` = 关，给隔离环境里确实要在凭据目录里搜的任务用（见[安全](security.md)） |
| `XIAOYU_UNATTENDED` | **默认关**，`1` = 开：`--yolo` 下仍必问的三项（`exit_plan_mode`、沙箱升权、写可执行配置）也不再问；等价命令行 `--unattended` |
| `XIAOYU_UNGUARDED` | `--unguarded` 无护栏预设的**环境同意**：只认真实环境变量、不读 `.env`，由容器 / VM 编排脚本注入；没有它 `--unguarded` 报错退出（见[安全](security.md)） |
| `XIAOYU_ENABLE_HOOKS` | 用户级 `hooks.toml` 生命周期钩子（工具前后 / 用户输入 / 收尾 / 会话起止 / 子 agent 起止 / 压缩前后，见下文事件表；退出码 2 = 拦截，其它失败 fail-open 放行）。样本：[examples/hooks/adversary](../examples/hooks/adversary/)——bash 命令交给另一次 `xiaoyu -p` 做二审 |
| `XIAOYU_ENABLE_AGENTS` | 声明式 subagent（`agents/*.toml`）与七襄并行织造模式（见[多 agent 协同](multi-agent.md)） |
| `XIAOYU_ENABLE_CHENSHU` | 宸枢统筹织造模式（见[多 agent 协同](multi-agent.md)） |
| `XIAOYU_SUBAGENT_MAX_DEPTH` | 子 agent 嵌套深度上限（默认 `1` = 不套娃）；设 2/3 显式放开有界嵌套 |
| `XIAOYU_ENABLE_PEERS` | 跨会话消息（`--yolo` 下默认关，见[安全](security.md)） |

会话中可用 `/search` 查看当前搜索后端及各后端的配置状态，
用 `/search deepseek`、`/search xai` 或 `/search bedrock` 切换。
切换从下一次搜索开始生效，只影响当前会话，不修改 `.env`；新进程仍按环境配置选择。
未配置的后端不能切换，主对话模型不受影响。TUI 支持后端名 Tab 补全，ACP 也提供此命令。

### X 平台搜索

配置 `XAI_API_KEY` 后，`x_search` 可搜索 X 帖子、用户与讨论串，使用 `grok-4.7`
的 Responses 内置搜索，无需额外 SDK。它与 `web_search` 同时可用，
不受 `XIAOYU_SEARCH_PROVIDER` 或 `/search` 切换影响。
例如直接问「查一下 @某账号 最近一周关于某话题的帖子，附原帖链接」。

支持 `allowed_x_handles` / `excluded_x_handles`（二选一，最多 20 个账号）、
`from_date` / `to_date`（UTC 的 `YYYY-MM-DD` 日期，包含首尾当天）以及图片、视频分析
`enable_image_understanding` / `enable_video_understanding`（默认关闭，按需开启）。
返回内容标注外部来源，帖子中的说法不代表已核实事实。未配置 xAI 时不暴露工具，
可用 `XIAOYU_ENABLE_X_SEARCH=0` 关闭；检索子 agent 与评估任务默认不启用。
X Search 除模型 token 外另收搜索费用，按抓取的帖子和用户资料计费，见
[官方文档](https://docs.x.ai/developers/tools/x-search)。

### Gemini 深度研究

配置 `GEMINI_API_KEY`（或 `GOOGLE_API_KEY`）后，内置的 `deep_research` 可提交
Gemini 后台研究任务，`deep_research_status` 查询进度并获取完整报告，
`deep_research_cancel` 取消任务（按现有权限规则确认）。例如：
「用 Gemini Deep Research 研究某主题，返回带来源的中文报告」，
随后「查询刚才研究任务的进度」。任务 ID 会返回到会话，可在新会话中凭 ID 查询。

默认 `tier=standard` 使用 `deep-research-preview-04-2026`；
要求更全面的研究时可选 `tier=max`（`deep-research-max-preview-04-2026`）。
`previous_interaction_id` 可继续旧研究。研究经官方 Interactions API 后台执行，
通常需要数分钟；本地不会常驻轮询或自动推送，稍后主动查询即可。
报告保留正文与来源，长输出沿用 `recall` 查看全文。

此能力独立于主对话模型、`web_search` 和 `/search`；默认开启，未配置 Gemini key
时不暴露工具，`XIAOYU_ENABLE_DEEP_RESEARCH=0` 可关闭。检索子 agent 与评估默认关闭。
不增加 SDK 依赖，沿用现有代理配置；提交失败不会自动重试，避免重复付费任务。
任务单独计费，取消不退还已产生用量。本进程提交的任务在查询到终态后计入模型用量
（思考 token 归入输出），同一工具实例重复查询不重复记账；恢复旧任务只展示服务端用量。
API 当前为预览，权限、配额及费用见
[官方说明](https://ai.google.dev/gemini-api/docs/deep-research)。

### Bedrock 联网搜索

```dotenv
XIAOYU_SEARCH_PROVIDER=bedrock
XIAOYU_BEDROCK_REGION=us-east-1
```

搜索使用 `bedrock-mantle.<区域>.api.aws/openai/v1` 的 Responses API，模型为
`openai.gpt-5.6-luna`；现有 Bedrock 主对话仍走自己的模型与协议。
鉴权复用 `AWS_BEARER_TOKEN_BEDROCK`；没配置 token 时使用 AWS 默认凭证链
（支持 `AWS_PROFILE`），这条 IAM 路径需要可选依赖 `pip install 'xiaoyu-agent[bedrock]'`。

调用身份需要 `bedrock-mantle:CreateInference` 与 `bedrock-websearch:InvokeSearch`、
`bedrock-websearch:InvokeFetch`；使用 bearer token 时还需 `bedrock-mantle:CallWithBearerToken`。
工具固定发送 `external_web_access=false`，
只查询 AWS 索引与缓存，不要求 `bedrock-websearch:ExternalWebAccess`。
未注册 Bedrock 时不挂载工具；缺凭证、权限不足或未返回搜索证据时明确报错，
不会自动改走其它厂商。

区域和模型支持范围以 [AWS Web Search 文档](https://docs.aws.amazon.com/bedrock/latest/userguide/web-search.html)
为准；官方发布文章列出的区域为 `us-east-1`、`us-east-2`、`us-west-2`。
这项服务适合检索最新公开知识，缓存与索引可能有延迟，不能保证实时天气或行情。

### 沙箱与界面

| 变量 | 默认值 | 说明 |
|---|---|---|
| `XIAOYU_MODE` | `auto` | 个人默认交互模式：`auto`（工作区内改文件与沙箱内命令免确认）/ `default`（确认档，逐条确认）/ `plan`（只读规划态）。命令行 `--mode` 优先；会话里 Shift+Tab / `/mode` 随时切 |
| `XIAOYU_SANDBOX` | 开 | bash 的内核级沙箱（macOS Seatbelt / Linux bubblewrap） |
| `XIAOYU_SANDBOX_NETWORK` | 开 | 沙箱内是否允许联网（`0` = 断网） |
| `XIAOYU_SANDBOX_WRITABLE` | — | 追加可写根目录，冒号分隔 |
| `XIAOYU_THEME` | `auto` | `dark` / `light` 跳过终端背景色探测 |
| `XIAOYU_TURN_SUMMARY` | 开 | 交互模式每轮结束打一行简版耗时（耗时 · 输出 tok/s），只在该轮耗时 ≥ 5s 时打；`0` = 关。`--stats` 的全版（含首 token、请求数）不受此影响，`-p` 与 json 输出也不打 |
| `XIAOYU_BELL` | 关 | 一轮结束 / 等审批时往终端写通知。`1` / `bel` = 响铃（BEL），终端翻译成提示音、Dock 弹跳或标签高亮；`osc9`（iTerm2 / WezTerm / ghostty）、`osc777`（rxvt 一路）、`osc99`（kitty）= 带文案的桌面通知转义序列，文案含状态与会话名/目录名；`auto` 按 `TERM_PROGRAM` / `KITTY_WINDOW_ID` 挑一种，认不出退回 BEL。tmux 里自动用 DCS 透传。只对真终端写，管道里不写 |
| `XIAOYU_TITLE` | 开 | 交互模式把窗口标题设成「<状态> · <会话名或目录名> · xiaoyu」，状态随会话走（就绪 / 运行中 / 等审批 / 等输入），具名会话（`--session-id`、`term-…`）用名字；退出时还原（认标题栈的终端精确还原，其余清空）；`0` = 关 |
| `XIAOYU_STATUS_HOOK` | — | 状态变成"等人"时后台跑的命令，状态串作最后一个参数（`waiting_input` / `waiting_approval`），也放进环境变量 `XIAOYU_STATUS`。给系统通知用，如 macOS：`osascript -e 'display notification "小羽在等你"'`；超时（10s）与失败静默。只在 TUI / 明文 REPL 生效 |
| `XIAOYU_BROWSER_CDP` | — | 接管以 `--remote-debugging-port` 起的本机 Chrome（要登录态时用） |
| `XIAOYU_BROWSER_HEADED` | 无头 | 有头模式启动浏览器 |

### 可观测性（OpenTelemetry，标准 `OTEL_*` 变量）

设了 `OTEL_EXPORTER_OTLP_ENDPOINT`（或 `OTEL_TRACES_EXPORTER=console`）就把每轮 / 每次模型调用 /
每次工具调用按 GenAI 语义约定打成 span 推到你自己的 collector；没设就一个字节不发、包都不加载。
需要可选 extra `pip install "xiaoyu-agent[otel]"`。变量表、span 树与属性清单、内容采集开关见
[可观测性](observability.md)。

## 自定义 system prompt

小羽出厂是编码 agent。要让它换一种工作身份（写作助手、客服话术、某类专项工作的人设），把提示词写进文件、启动时指过去：

```bash
xiaoyu --system-prompt-file ~/prompts/writer.md
```

| 旗标 | 作用 |
|---|---|
| `--system-prompt-file PATH` | 文件内容**顶替**内置的"身份"与"回答风格"两段 |
| `--system-prompt TEXT` | 同上，直接给文本；与上一个只能给一个 |
| `--append-system-prompt TEXT` | 在身份之后**追加**一段，内置身份不变（宿主嵌入时注入人格用） |
| `--append-system-prompt-file PATH` | 同上，内容从文件读；与上一个只能给一个 |

顶替的只是身份与风格。下面这些照常保留，不受自定义提示词影响：

- **运行纪律**：工具怎么用（explore / str_replace / bash 验证）、计划怎么记、`<untrusted_content>` 里的指令不照做、工作区与系统信息。这是 harness 正常且安全运转的前提，不属于人格；
- 环境画像、项目指令（`AGENTS.md` 等）、技能索引；`--append-system-prompt` 给的内容仍追加在后。

项目指令按 git 根 → 工作区逐层收集进 system prompt；工作区**下层**目录（monorepo 的子包）里的 `AGENTS.md` / `XIAOYU.md` / `CLAUDE.md` 只在 system prompt 里留指针，正文在模型第一次读写该目录（`read_file` / `write_file` / `str_replace` 的路径、bash 命令里的路径词）时随工具结果附上——从工作区到那个目录的每一层各载一次，只向下、不出工作区、依赖目录不算，总量与 system prompt 里的项目指令同一上限（超了只给指针）。

几点约定：

- 文件按 UTF-8 读，内容原样使用——里面的花括号（`{{占位符}}`、代码示例）不会被当模板处理。文件读不了或是空的，启动时直接报错退出；
- **注释**：块级 HTML 注释不发给模型，用来写给维护者看的说明（这份提示词怎么改、占位符填什么）。会话里记的、`/context` 里算的都是剥掉注释之后的那份：

  ```markdown
  <!-- 维护说明：
  语气相关的改 Voice 一行；示例保持简短。
  -->

  你是炉匠 Cinder……
  ```

  边界刻意收得窄，宁可少剥不错剥：`<!--` 要在行首（前面只许空白）、`-->` 之后到行尾只许空白才算；行内夹着的 `a <!-- b --> c` 不动；代码围栏（` ``` ` / `~~~`）里的不动，那是给模型看的示例。`<!--` 没闭合时**不剥**并在启动时警告行号——否则漏写一个 `-->`，后半份提示词就悄悄没了。只有两个 `-file` 旗标认注释，`--system-prompt` / `--append-system-prompt` 给的文本原样使用；
- 正文里还留着 `{{…}}` 形态的占位符时，启动会提醒一句（多半是模板没填完）；只提醒，不改内容；
- 自定义提示词**全文记进会话文件**：`xiaoyu resume` 和 `--session-id` 续写时不必再给旗标，沿用原来那份；重新给了就以新给的为准。模型与交互模式同一纪律：`xiaoyu resume` 默认跟随旧会话最后生效的模型与模式（`/model`、降级链、plan 进出都有留痕；与 ACP `session/load`、`@x` 接回同口径），`--model` / `--mode` 显式给了才覆盖。存全文而不是路径，是为了文件挪走、改过之后旧会话仍能原样接回；
- 提示词常驻每一轮请求，长度直接计入上下文与费用；`/context` 里它单列为"自定义身份"一行；
- 库层嵌入对应 `Config(system_prompt=...)`，ACP（`xiaoyu acp`）同样认这组旗标。`xiaoyu serve` 的 agent 对象目前只有 `append_system_prompt`。

## 插件包（skills + MCP 一起装）

认的是 [agent-plugins.org](https://agent-plugins.org) 那套中立的 bundle 格式——
AWS 的 [agent-toolkit-for-aws](https://github.com/aws/agent-toolkit-for-aws) 就按它分发，
主流 agent 客户端各自认领同一个包。

```bash
xiaoyu plugin add aws/agent-toolkit-for-aws --name aws-core   # owner/repo、URL 或本地目录
xiaoyu plugin list                                            # 已装的包、版本、来源
xiaoyu plugin update [名字…]                                   # 按记下的来源拉新
xiaoyu plugin remove aws-core                                 # 删目录 + 摘掉它装的 MCP 声明
```

包装在 `~/.config/xiaoyu/plugins/<包名>/`（**不写** `~/.agents/skills`——那是跨客户端
共享的规范库，写进去会和别家客户端自己的插件版形成双份漂移）。装进去以后：

- **技能**带包名前缀，如 `aws-core:aws-cdk`。两家插件各带一个同名技能不会互相顶掉，
  `/skills` 里也标得出哪些是装来的。
- **MCP server 声明**合进用户级 `mcp.json`，名字加命名空间（`aws-core__aws-mcp`），
  条目里留 `"_plugin"` 记号。写的就是 `mcp.json` 本身，`xiaoyu mcp list` 直接看得见，
  也随时可以手改手删。
- **hooks 不装**（中立规范里没有 hooks，那是各家私有扩展），但发现了会报出来。
  非 stdio 的 server 同理——只报不装，不静默丢。

### MCP 那道门

包是从网上拉来的、里面就带着会被 spawn 的命令行，所以 MCP 声明**默认不装**：

- 交互终端下会把完整命令行摊出来问一次，回车 = 不装；
- 非交互（管道 / CI）下一律不装，技能照装，提示加 `--accept-mcp` 重来；
- `update` 时命令行只要有变化就重新问一次并打出 diff（首装人畜无害、第 N 次更新
  悄悄换掉 command 正是 MCP 生态最现实的攻击），不确认就不动已装的那份；
- 写盘前还要过一遍 [MCP 准入规则](security.md)，和 `xiaoyu mcp add` 同一道门。

MCP 子进程的环境是**纯白名单**（定位类变量，加上代理与自定义 CA 这几个出网设置：
`HTTP_PROXY` / `HTTPS_PROXY` / `ALL_PROXY` / `NO_PROXY`、`SSL_CERT_FILE` / `SSL_CERT_DIR` /
`REQUESTS_CA_BUNDLE` / `CURL_CA_BUNDLE` / `NODE_EXTRA_CA_CERTS`——不想让某个 server 走代理，
在它的 `env` 块里把对应变量设成空串），所以像 aws-mcp 这类要 SigV4 凭证的 server，
装完得自己在 `mcp.json` 的 `env` 块里用 `${env:AWS_PROFILE}` 之类显式点名——
不点名只会得到一个莫名其妙的 401/403。macOS 上令牌不必明文进 `.env`：用户级 `mcp.json`
里的 `${GITHUB_TOKEN}` 在环境变量里找不到时，会按同名去 Keychain 取
（`security add-generic-password -U -s GITHUB_TOKEN -a "$USER" -w`）。这条回落只对你亲手写在
用户级 `mcp.json` 里的声明生效；工作区 `.mcp.json` 与插件包装进来的条目不触发。远端 server 被 401/403 拒绝会整代停用、不自动重试；
把 `mcp.json` 里的 headers / env 改好后 `/mcp reconnect <name>` 热恢复，不必重启会话（不给名字 =
全部失败的；对在线的 server 就是干净重启一次）。它重读的是配置文件：在别的终端 export 的变量、
会话启动后才改的 `.env`，本进程都看不到，那两种仍得重启会话。

除了各家通用的 `command` / `args` / `env` / `url` / `headers` / `timeout` / `disabled`，声明里还认几个
小羽自己的字段（`xiaoyu mcp add` 有对应选项）：

```json
{
  "mcpServers": {
    "fs": {
      "command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "."],
      "cwd": "${env:HOME}/projects/site",
      "tools": ["read_file", "list_directory"],
      "inheritEnv": ["MYAPP_*"]
    }
  }
}
```

- `cwd`：stdio server 的工作目录（文件系统类 server 把它当根用；宿主由编辑器或 systemd 拉起时
  当前目录是哪儿全看运气）。与 `url` / `headers` 同一套 `${env:VAR}` 展开；目录不存在在启动前就报
  清楚，不会变成一句看起来像"命令找不到"的 `FileNotFoundError`。远端 server 没有这个字段。
- `tools`：只暴露名单内的工具。几十个工具的 server 往往只用两三个，其余的既占检索结果又是多余的
  攻击面——名单外的工具模型看不见（不注册、不进 `search_tool`），拿着全限定名直接点名也会被
  server 调用层再拦一次。名单里写了 server 没提供的名字，启动时告警一次并列出它实际提供的；
  rug-pull 基线只对名单内的工具比对，名单外工具的描述怎么变都不会把整代隔离。
- `inheritEnv`：从父环境透传给 server 的变量名或前缀（子进程环境是白名单，见下）。

server 启动失败时（`/mcp` 的状态、工具调用的报错、重连耗尽的提示）会直接附上它 stderr 日志的
末几行（脱敏、去控制序列、每行截断），多数时候不必再去翻 `日志：` 那个路径。server 经
`notifications/message` 发来的日志按级别分流：warning 及以上一行上屏，其余只进同一份
`mcp-<name>.log`；长调用期间 server 的 `notifications/progress` 会显示在活区那一行（`3/10（30%） · 正在下载`），
`--output-format stream-json` 下是 `tool.progress` 事件。

排障不必经模型：`xiaoyu mcp probe <server名>` 直接握手、打印 serverInfo / 协议版本 / 能力 /
instructions / 工具表（不在 `tools` 名单里的标 ✗）；也可以 `xiaoyu mcp probe -- npx -y 某包` 探一条
还没写进配置的命令行，或 `--url` 探远端。`--script steps.json` 按脚本顺序执行
`[{"op":"listTools"}, {"op":"callTool","name":"echo","args":{"text":"hi"}}]`，每步一行 JSON（耗时、
错误分类 `timeout` / `rpc` / `dropped`…、结果形状），任一步失败退出码 1。走的是会话里同一个客户端：
准入规则、OSV 预检、`cwd` 校验、超时都一样，"probe 通了会话里却不通"不会发生在这一层；探测的日志
单独写 `mcp-probe-<name>.log`，不碰正在跑的会话。

找启动命令（`npx` / `uvx` …）时先看该 server 的 `env` 块里声明的 `PATH`，再看小羽自己的
`PATH`。小羽由编辑器或 systemd 拉起时自己的 `PATH` 往往很短，在 `env` 里补一行
`"PATH": "/opt/homebrew/bin:${env:PATH}"` 即可，不必把 `command` 写成绝对路径。
`xiaoyu doctor` 用同一套找法，并会指出 `args` / `env` 里已经不存在的路径。

server 的工具描述 / schema 一变（多半是 `npx xxx@latest` 拉到了新版）就会被整代隔离，
启动时直接摊出变了什么（描述逐行 diff、参数增删改），`/mcp diff <name>` 看全部，核对后
`/mcp approve <name>`（不给名字 = 全部批准）。信得过来源、不想每次上游发版都重批的，
在该 server 的声明里加 `"trustToolChanges": true`（或全局 `XIAOYU_MCP_TRUST_CHANGES=1`），
变更自动接受并刷新基线（stderr 记一行）；更稳的做法仍是钉死版本号，让基线只在你主动
升级时才需要重批。

server 的**结果**默认包进 `<untrusted_content>` 回灌（里面的指令只当数据）。来源就是自己人的
内部 server（内网 runbook、工单系统——你就是想让模型照它说的做）在声明里加
`"trustContent": true`，该 server 的结果按可信内容回灌。与 `trustToolChanges` 是两条轴：那个信
的是工具声明的变更，这个信的是工具返回的内容。

> `xiaoyu plugin` 装的是**内容包**（技能文本 + MCP 声明）。它和 `XIAOYU_ENABLE_PLUGINS`
> 管的**插件工具**（entry point 组 `xiaoyu.tools`，第三方 Python 包往进程里注册函数）
> 是两条互不相干的通道，只是恰好都叫 plugin。

## 技能（SKILL.md）：参数、斜杠调用、支持文件

技能是 `<目录>/SKILL.md`（frontmatter `name` / `description` + markdown 正文），扫描位置见上表
`XIAOYU_ENABLE_SKILLS` 一行。索引（名字 + 一句话描述）进 system prompt，正文由模型用 `skill`
工具按需加载。加载时除正文外还带三样东西：

- **技能目录**：正文里的相对路径以它为基准；
- **支持文件清单**：技能目录下除 `SKILL.md` 外的文件，按「相对路径 → 绝对路径」列出（隐藏文件、
  `__pycache__` 不算，不跟符号链接；最多 40 条，超了提示用 `list_files` 看）。模型引用脚本 /
  参考文档时用绝对路径，不会在工作区里瞎找；
- **参数占位**：正文里的 `$ARGUMENTS`（全部参数原文）、`$ARGUMENTS[i]` / `$i`（按空白切的第 i 个，
  从 1 起）、`$名字`（frontmatter 声明的具名参数，按位置对应）由调用时给的参数填充。声明写法：

  ```yaml
  ---
  name: deploy
  description: 部署到指定环境
  arguments: [env, version]      # 顶层行内列表；块列表（- env）或 metadata.arguments 也认
  ---
  把 $version 部署到 $env（完整参数：$ARGUMENTS）
  ```

  没给到的 `$ARGUMENTS` / 声明过的 `$名字` **原样保留**，并在头部点名缺了哪些（模型按上下文推断，
  推不出就问你）。裸 `$1` 缺参不报：技能正文里 `awk '{print $1}'` 这类 shell 片段太常见。别的
  `$xxx`（`$PATH`、`$HOME`）不是占位，原样不动。

交互前端里 **`/<技能名> 参数…`** 直接把技能展开成本轮提示（模型看到的就是 `skill` 工具加载的那份，
外加一句"用户点名要执行"）。斜杠名字空间里**内建命令优先**：技能叫 `help` 也遮不住 `/help`，
要写 `/skill:help`；`/skills` 列表会标出撞名的技能，TUI 补全里技能也列在内建命令之后
（撞名的以 `/skill:` 形态出现）。`/skill:` 前缀下找不到技能报错，不回落到内建命令。
ACP 客户端（Zed 等）建会话时收到的命令菜单里也列出技能（描述带 `[Skill]` 标记，撞名的同样是
`skill:<名>`），选中后附参数即按同一规则展开；装了新技能重开会话即可见。

## 生命周期钩子（hooks.toml）

用户级 `<配置目录>/hooks.toml`（刻意不读工作区级：hook 是任意代码执行，clone 一个仓库不该把命令
种进你的 shell）。`XIAOYU_ENABLE_HOOKS=0` 一键关。

```toml
[[hooks]]
event = "PreToolUse"        # 见下表
matcher = "bash"            # 正则匹配工具名，只对工具类事件有意义；MCP 工具按真名匹配
command = "python ~/bin/check.py"
timeout = 10                # 秒，缺省 30，上限 600
on_failure = "allow"        # allow | block，只对 PreToolUse 生效
```

钩子从 stdin 收一个 JSON 对象（`event`、`workspace` 必有，其余按事件），**退出码 2 = 拦截**
（stderr 作为理由），0 = 放行；超时、起不来、其它退出码默认 **fail-open 放行并告警**——
hook 是辅助护栏，deny 规则才是硬闸。`on_failure = "block"` 反过来：钩子坏了也按拦截处理，
理由里标明「钩子失败」，给"这道闸必须跑过才能动手"的场景；只对 PreToolUse 生效，写在别的
事件上会提示并忽略。同一事件多个钩子顺序执行，任一拦截即拦截。

| 事件 | 时机与拦截语义 | payload 额外字段 |
|---|---|---|
| `PreToolUse` | 审批之后、执行之前；拦截 → 不执行，理由回灌模型 | `tool`、`args`、`call_id` |
| `PostToolUse` | 工具已执行；拦截 → 理由作为附注拼进结果（不撤销副作用） | `tool`、`args`、`ok`、`output`、`call_id` |
| `ToolFailed` | 工具结果判成失败（`ERROR:`）之后的通知，拦截无意义 | `tool`、`args`、`ok=false`、`output`、`call_id` |
| `UserPromptSubmit` | 用户输入入历史之前；拦截 → 本轮不发 | `prompt` |
| `Stop` | 模型想收尾时；拦截 → 理由作为消息顶回去续跑一步（每轮一次） | `last_text` |
| `SessionStart` | 会话首轮之前一次（接回历史之后；子 agent 不触发）；拦截 → 拒绝启动 | `model`、`session` |
| `SessionEnd` | 会话正常收尾一次，结果不影响退出 | `model`、`session` |
| `SubagentStart` | 主会话委托子 agent 之前；拦截 → 这次委托不执行，模型收到委托失败自行改道 | `agent` |
| `SubagentEnd` | 子 agent 收工之后的通知；拦截只在委托结果里留一条附注 | `agent`、`failed` |
| `BeforeCompact` | 要压缩上下文之前；拦截 → 不压缩、本次压缩以异常中止（上下文已超窗时这一轮无法继续，只给「压缩前必须先归档」之类的硬需求用） | `context_tokens`、`forced` |
| `AfterCompact` | 压缩完成之后的通知 | `changed`、`method`（`microcompact` / `summary`） |

同一次工具调用的 `PreToolUse` / `PostToolUse` / `ToolFailed` 带**同一个 `call_id`**，外部钩子
靠它把"要跑什么"和"跑出了什么"对上（并行工具调用下光靠工具名对不上）。

放行钩子的 stdout 有两种去处：

- **纯文本**：首个非空行（上限 2000 字符）作为一次性消息注入历史。只有两个事件消费它——
  `SessionStart` 注入在首轮之前（给宿主注入环境说明：当前分支、值班提示……），
  `UserPromptSubmit` 紧跟本轮用户输入之后（按这一轮输入查个工单号、附上当前 git 状态……）；
  其它事件的 stdout 没去处。它走的是"harness 放进来、内容不可信"的通道，不是权威指令。
- **一个 JSON 对象且含 `systemMessage` 键**：那段文本**只**作为提示显示给用户，不进历史、
  不进模型，所有事件都认；其余键忽略。stdout 整体是 JSON 对象时首行规则不再生效——
  钩子想对用户说一句「已记录到审计日志」，不该同时把这句喂给模型。

挂在工具事件上的钩子在子 agent 里照样触发（工作目录换成它的）；会话类事件不带下去。
事件名与 payload 形状和 SDK 进程内 hook（见 [sdk-platform](sdk-platform.md)）一致。

## 接入未内置的厂商

`<NAME>` 自取，大写：

```ini
XIAOYU_PROVIDER_MINIMAX_BASE_URL=https://api.minimaxi.com/v1
XIAOYU_PROVIDER_MINIMAX_API_KEY=<key>
XIAOYU_PROVIDER_MINIMAX_MODELS=minimax-m2,minimax-m2-turbo   # 留空 = 通配；auto = 本机端点自动发现
XIAOYU_PROVIDER_MINIMAX_PROTOCOL=responses                   # 默认 chat；可选 anthropic
XIAOYU_PROVIDER_MINIMAX_VISION=*                             # 声明视觉能力，默认不发图
XIAOYU_PROVIDER_MINIMAX_TOOLS=text                           # 默认 native；端点不会 function calling 时设 text
XIAOYU_PROVIDER_MINIMAX_SIGNATURES=*                         # 工具调用重放需带回 thought_signature 的型号（Gemini 系端点用；仅 chat 协议生效，配上 PROTOCOL=responses/anthropic 会出声忽略）
XIAOYU_PROVIDER_MINIMAX_HEADERS=X-Title=xiaoyu;Authorization=Bearer ${env:RELAY_TOKEN}   # 随每个请求附带的自定义 header，分号分隔
```

`_HEADERS`：中转站要求的额外头（站点标识、`Authorization: Bearer` 这类与 SDK 默认鉴权形态不同的头……）。格式 `Name=value;Name2=value2`，分号分隔，值里允许再出现 `=`；值可写 `${env:VAR}`（也认 `${VAR}`）引用环境变量或 macOS Keychain 同名条目，令牌不必明文进配置——引用没兑现的那个 header 会被出声丢掉，不会把 `${env:…}` 字面量发上游。三条协议（chat / responses / anthropic）的 client 都带上，SDK 把它们合并在自家鉴权头之后，所以能盖过默认的 `x-api-key` 形态。`config --show` 与 `doctor` 只显示 header 的**名字**，值永不出现。

本机端点免 key 的规则同网关：`_BASE_URL` 指向 `localhost` 时 `_API_KEY` 可省略（显式给了则以给的为准）。

`_MODELS=auto`：启动时探一次端点 `/v1/models`，把它当前 serve 的 model id 自动注册进来——本机端点换了 model 不用改配置，重启 xiaoyu 即自动跟上（新 id 会出现在 `config --show` 与 `/model` 补全里）。**只对本机 `localhost` 端点生效**（远端仍守「启动不探测」，请显式列模型名）；探测失败或返回空则该 provider 本次不注册（**不会**退化成通配去劫持网关路由）。默认路径依然从不探测 `/v1/models`。

`_PROTOCOL=anthropic` 也适用于只挂 Claude 原生协议、用 key 鉴权的自建端点（AWS Bedrock 本身已内置，见上方，不必走这里）。视觉是 fail-closed 的：**未声明即不发图**，模型会收到一行"有 N 张图但看不了"的说明而不是被静默丢弃。

`_TOOLS=text` 是给**不支持 function calling** 的端点（本地 vLLM / Ollama 上的小模型、带 `tools` 就 400 或静默忽略的老服务）准备的逃生舱：工具说明改为写进 system prompt，模型用 ```` ```tool_call ```` 代码块（也认 `<tool_call>` 标签）发起调用，结果以 `<tool_result>` 文本回灌。翻译只发生在出网那一刻，会话历史仍是标准形态，随时 `/model` 切回原生工具调用的模型。它与 `_PROTOCOL` 正交，可同时设置。原生 function calling 能用就别开它——文本解析天生更脆。

解析器认三种发起格式：我们教的 ```` ```tool_call ```` / `<tool_call>` 里放 `{"name":…,"arguments":…}`，以及训练过原生工具调用的开源模型（Qwen3 等）会自发使用的原生方言 `<tool_call><function=名字>…</function></tool_call>`（名字在壳上，参数用块内 JSON 或 `<parameter=键>值` 子标签）——后者无需你做任何配置，自动认。

## 出网代理

小羽自己的网络请求（模型调用、`/v1/models` 探测、MCP HTTP 传输、MCP OSV 预检）认标准代理变量 `HTTP_PROXY` / `HTTPS_PROXY` / `ALL_PROXY` / `NO_PROXY`（大小写都认，小写优先）；环境里一个都没有时沿用系统代理设置（macOS 网络偏好 / Windows 注册表）。在此之上有三条规则：

- **本机地址一律直连**：`localhost` / `*.localhost` / `127.0.0.0/8` / `::1` 不走代理，不用为本机模型端点（vLLM、Ollama…）另配 `NO_PROXY`。
- **用不了的代理变量降级、不崩**：`socks4://`（HTTP 客户端不支持）、`socks5://` 但没装 socksio（修复：`pip install "httpx[socks]"`）、写法解析不了——启动时在 stderr 说一次是哪个变量、为什么、怎么修，然后小羽自身按该变量未设置处理（还有别的代理变量就用别的，都没有就直连）。
- **子进程拿到的是原值**：bash 工具、MCP stdio server 继承你设的代理变量，小羽不改写它们。

`SOCKS5` 代理只作用于模型请求；MCP HTTP 传输与 OSV 预检基于标准库，只会用 http(s) 代理。`xiaoyu doctor` 的 `proxy` 一项列出解析结果（代理地址里的账号密码会脱敏）。

## macOS Keychain

key 可以不落盘。`.env` 里留空即会自动回退去读，service 名就是变量名本身（`.env` / 环境变量 / Keychain 三处同名）：

```bash
security add-generic-password -a "$USER" -s "DEEPSEEK_API_KEY" -U -w
security add-generic-password -a "$USER" -s "XIAOYU_API_KEY" -U -w
```

Windows 上用 `.env` 或环境变量。

## 多 provider：直连优先，网关兜底

直连和网关同时配时，两边的模型清单会**合并**：

- **同名模型直连赢**——少一跳、不加价、key 不过第三方。
- **网关那份不消失，降级为兜底**——直连限流 / 5xx / key 失效时自动切到网关同名模型，会话原样继续。网关从此不是单点。
- 网关**通配**：任何没被直连认领的名字照旧转发过去。

`/model` 无参看合并后的清单与来源：

```
  deepseek-flash     ← 直连 deepseek（同名可兜底：网关）
  其余任意模型名     ← 网关（转发，不枚举）
降级链：deepseek/deepseek-flash → gateway/deepseek-flash → …
```

`provider/model` 是显式寻址，用来点名走哪一家：`/model gateway/deepseek-flash`。点名之后不再自动兜底——既然指定了，就不该被偷偷换掉。

## 图片代读（当前模型看不了图时）

视觉能力是 **fail-closed** 的：模型没有实测声明过收得下图，图片就不会发出去
（见"接入未内置的厂商"）。默认行为是不静默丢弃、也不猜——告诉模型"有 N 张图但我
看不了"，告诉用户"`/model` 换个支持视觉的再重发"。

`XIAOYU_VISION_FALLBACK` 给这条降级路径加一手**代读**：图片先单独交给一个视觉模型
换成一段文字，再作为文本进历史。主模型、工具、消息配对、记账全都不动。

```ini
XIAOYU_VISION_FALLBACK=deepseek-flash
```

- **默认空**。不预置默认值是刻意的：代读是**有损**的（截图里的像素位置、字体细节、
  没被转写下来的角落都会丢），默认打开等于让用户以为主模型看了图，而它看的是一段
  转述——与 fail-closed 同一条诚实纪律。何况默认值只能点名某一家的型号，还得是用户
  恰好配了 key 的那家。
- 代读模型**自己必须收得下图**（同一套 fail-closed 校验），否则视为没配、照旧降级为
  文字说明，并提醒一次。网关后面的视觉模型仍用 `XIAOYU_VISION_MODELS` 点名放行。
- 代读**不是"这一轮换个模型跑"**：那会把 system prompt、工具 schema、`tool_calls`
  配对一起拖下水，而收得下图的往往正是工具能力最弱的那一档型号。
- 转写用本轮的用户原话当取景框（同一张截图，"这报错怎么回事"和"这配色好看吗"该被
  写下来的细节完全不同），产物带明确抬头进历史，主模型据此知道自己看的是转述。
- 代读失败（超时、限流、空回复）只降级回文字说明，绝不打断本轮。花掉的 token 按
  路由记进 `/usage`。
- 四个面都吃这个开关：TUI 贴图、ACP 附图、`--image/--paste` 一次性模式、以及 MCP
  工具在一轮中途返回的图。**最后一个收益最大**——图在一轮中途产出，人不在环里，
  没有"换个模型重发"这一手。
- `/model` 无参会印出当前的代读状态（配了才印）。

要原图保真，路只有一条：`/model` 换用支持视觉的模型。代读是够不着那条路时的兜底。
