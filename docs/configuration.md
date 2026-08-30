# 配置

README 只给最小可跑配置，这里是全量。

## 配置文件与优先级

```bash
xiaoyu config             # 交互向导：直连 key / 网关端点 / 模型
xiaoyu config --show      # 看生效配置与每项来源（key 永不回显）
xiaoyu config --path      # 打印用户级配置文件路径
xiaoyu config --set XIAOYU_MODEL=deepseek-v4-pro   # 非交互写入，可重复
```

用户级 `.env` 的位置：macOS / Linux 在 `~/.config/xiaoyu/.env`（跟随 `$XDG_CONFIG_HOME`），Windows 在 `%APPDATA%\xiaoyu\.env`。也可以手动在任意工作目录放 `.env`（零依赖自解析）。

优先级：**真实环境变量 > 当前目录 `.env` > 项目根 `.env` > 用户级 `.env`**，所以临时覆盖很方便：

```bash
XIAOYU_MODEL=deepseek-v4-flash xiaoyu
```

## 直连厂商

内置直连：deepseek / moonshot / qwen / zhipu / anthropic / gemini / openai / xai。**键名一律用厂商原生名**（别家工具已配过的直接复用）：

```ini
DEEPSEEK_API_KEY=<key>
MOONSHOT_API_KEY=<key>
OPENAI_API_KEY=<key>
ANTHROPIC_API_KEY=<key>
GEMINI_API_KEY=<key>
```

每家走哪种 wire 协议（chat completions / Responses / Anthropic Messages）、哪些型号能看图，都是按型号内置好的，不用管。

## 网关

任意 OpenAI 兼容端点（LiteLLM、vLLM、各家官方 API…）：

```ini
XIAOYU_BASE_URL=https://<你的网关>/v1
XIAOYU_MODEL=<你网关上的模型名>
XIAOYU_API_KEY=<key>
```

直连和网关至少配一个，两个都配见下方"多 provider"。端点在**本机**（`localhost` / `127.0.0.1` / `::1` / `*.localhost`，如 vLLM、SGLang、Ollama）时 key 可以不填——远端地址缺 key 仍视为没配、不注册。

## 变量总表

### 模型与端点

| 变量 | 默认值 | 说明 |
|---|---|---|
| `XIAOYU_MODEL` | `deepseek-v4-pro` | 主模型 |
| `XIAOYU_SUMMARY_MODEL` | `deepseek-v4-flash` | 压缩摘要用的便宜模型 |
| `XIAOYU_EXPLORE_MODEL` | `deepseek-v4-flash` | `explore` 子 agent 用的模型 |
| `XIAOYU_BASE_URL` | — | OpenAI 兼容网关端点 |
| `XIAOYU_API_KEY` | — | 网关 key（也认 `LITELLM_API_KEY`） |
| `XIAOYU_FALLBACK_MODELS` | —（不降级） | 备用模型链，逗号分隔，主模型重试耗尽后依次切 |
| `XIAOYU_PROVIDERS` | 直连 → 网关 | 覆盖 provider 优先级（如 `gateway,deepseek` = 临时全走网关） |
| `XIAOYU_VISION_MODELS` | — | 网关后面挂的视觉模型点名（`*` = 一律放行） |
| `XIAOYU_SIGNATURE_MODELS` | — | 网关后面挂的签名型号点名（Gemini 系，工具重放需带回 thought_signature；`*` = 一律） |
| `XIAOYU_VISION_FALLBACK` | —（不代读） | 代读模型：当前模型看不了图时，把图先交给它换成一段文字（见下方"图片代读"） |
| `XIAOYU_ENV_FILE` | — | 指定 `.env` 路径，等价 `--env-file` |

### 上下文与压缩

| 变量 | 默认值 | 说明 |
|---|---|---|
| `XIAOYU_EFFORT` | 不传 | 推理深度 `low / medium / high / xhigh / max`（OpenAI 线另有 `none / minimal`）。同一个名字出内核，按协议翻译成 `reasoning_effort` / `reasoning.effort` / `output_config.effort`；上游不认的取值会 400。命令行 `--effort`，会话里 `/effort`，子 agent 可在 spec 里单独声明 |
| `XIAOYU_CONTEXT_LIMIT` | 按模型查表 | 上下文上限（token）覆写 |
| `XIAOYU_COMPACT_AT` | `0.7` | 用量占到这个比例时触发回收/压缩 |
| `XIAOYU_BUDGET_TOKENS` | 不限 | 本会话 token 软预算（prompt+completion 累计，≥5000 才生效）：模型按 50/80/95% 收到倒计时（operator 通道），到线前一步优雅收尾交代现场，而不是被硬闸中途砍断；直连支持型号（Opus 5/4.8/4.7/Fable/Mythos/Sonnet 5）另附 Anthropic 原生 `task_budget`（服务端倒计时）。命令行 `--budget-tokens` |
| `XIAOYU_TURN_EXTENSION` | `1.0` | 撞 `max_iterations` 时允许模型调 `extend_turns` 申请追加轮数，总追加量 ≤ `max_iterations ×` 此系数；`0` = 不许延期（撞顶即收尾）。理由展示给用户、可审计 |
| `XIAOYU_SERVER_COMPACTION` | `1` | 直连 Claude（opus-4.6+/sonnet-4.6+/5 系）时把压缩交给服务端（模型自己写摘要，`compaction` 块下轮回传，服务端忽略块前历史）；本地摘要压缩降为兜底。设 `0` 回纯本地压缩 |
| `XIAOYU_KEEP_RECENT` | `8` | 压缩时至少保留最近几条消息 |
| `XIAOYU_EXPLORE_ITERATIONS` | `12` | `explore` 子 agent 单次检索的工具调用轮数上限（1–100；主 agent 的 50 轮不受影响） |
| `XIAOYU_QIXIANG_CONCURRENCY` | `4` | 七襄批量委托的并发上限（1–16） |
| `XIAOYU_QIXIANG_TIMEOUT` | `0` | 七襄单项任务墙钟超时（秒，从实际启动起算；`0` = 不限时） |
| `XIAOYU_CHENSHU_MAX_WORKERS` | `4` | 宸枢同时在跑的成员上限（worker + reviewer，1–16） |

### 功能开关（`0` = 关）

| 变量 | 说明 |
|---|---|
| `XIAOYU_ENABLE_EXPLORE` | `explore` 检索子 agent |
| `XIAOYU_ENABLE_SKILLS` | 扫描 `~/.agents/skills/` 与已装插件包下的 SKILL.md |
| `XIAOYU_SKILLS_DIR` | 覆盖技能扫描目录（`os.pathsep` 分隔）：给了就只认它、不混默认目录（宿主指定技能库 / 测试隔离用） |
| `XIAOYU_ENABLE_WEB_SEARCH` | `web_search` 工具 |
| `XIAOYU_SEARCH_PROVIDER` | 搜索走哪家：`deepseek`（默认，便宜）/ `xai`（grok-4.6，更强更贵） |
| `XIAOYU_ENABLE_BROWSER` | `browser` 浏览器工具（依赖可选 `[browser]` extra 的 playwright，没装时本来就不出现） |
| `XIAOYU_ENABLE_PLUGINS` | entry point 组 `xiaoyu.tools` 的第三方工具**包**（代码级；和 `xiaoyu plugin` 装的**内容包**不是一回事，见下） |
| `XIAOYU_ENABLE_MCP` | MCP server 挂载 |
| `XIAOYU_MCP_OSV` / `_WATCHDOG` / `_CACHE` / `_RECONNECT` | MCP 的恶意包预检 / 孤儿进程回收 / schema 缓存 / 断线自动重连 |
| `XIAOYU_MCP_TOOL_SEARCH` | MCP 工具检索模式（默认开：工具不进 schema，`search_tool` 检索 + `use_tool` 调用；`0` = 回到全量注册） |
| `XIAOYU_FOLDER_TRUST` | 工作区信任门（默认开，见[安全](security.md)；只认真实环境变量与用户级 `.env`） |
| `XIAOYU_ENABLE_HOOKS` | 用户级 `hooks.toml` 生命周期钩子 |
| `XIAOYU_ENABLE_AGENTS` | 声明式 subagent（`agents/*.toml`）与七襄并行织造模式（见[多 agent 协同](multi-agent.md)） |
| `XIAOYU_ENABLE_CHENSHU` | 宸枢统筹织造模式（见[多 agent 协同](multi-agent.md)） |
| `XIAOYU_SUBAGENT_MAX_DEPTH` | 子 agent 嵌套深度上限（默认 `1` = 不套娃）；设 2/3 显式放开有界嵌套 |
| `XIAOYU_ENABLE_PEERS` | 跨会话消息（`--yolo` 下默认关，见[安全](security.md)） |

### 沙箱与界面

| 变量 | 默认值 | 说明 |
|---|---|---|
| `XIAOYU_MODE` | `default` | 个人默认交互模式：`default`（逐条确认）/ `auto`（工作区内改文件与沙箱内命令免确认）/ `plan`（只读规划态）。命令行 `--mode` 优先；会话里 Shift+Tab / `/mode` 随时切 |
| `XIAOYU_SANDBOX` | 开 | bash 的内核级沙箱（macOS Seatbelt / Linux bubblewrap） |
| `XIAOYU_SANDBOX_NETWORK` | 开 | 沙箱内是否允许联网（`0` = 断网） |
| `XIAOYU_SANDBOX_WRITABLE` | — | 追加可写根目录，冒号分隔 |
| `XIAOYU_THEME` | `auto` | `dark` / `light` 跳过终端背景色探测 |
| `XIAOYU_BROWSER_CDP` | — | 接管以 `--remote-debugging-port` 起的本机 Chrome（要登录态时用） |
| `XIAOYU_BROWSER_HEADED` | 无头 | 有头模式启动浏览器 |

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

MCP 子进程的环境是**纯白名单**，所以像 aws-mcp 这类要 SigV4 凭证的 server，
装完得自己在 `mcp.json` 的 `env` 块里用 `${env:AWS_PROFILE}` 之类显式点名——
不点名只会得到一个莫名其妙的 401/403。

server 的工具描述 / schema 一变（多半是 `npx xxx@latest` 拉到了新版）就会被整代隔离、
等 `/mcp approve <name>`。信得过来源、不想每次上游发版都重批的，在该 server 的声明里加
`"trustToolChanges": true`，变更自动接受并刷新基线（stderr 记一行）；更稳的做法仍是钉死
版本号，让基线只在你主动升级时才需要重批。

> `xiaoyu plugin` 装的是**内容包**（技能文本 + MCP 声明）。它和 `XIAOYU_ENABLE_PLUGINS`
> 管的**插件工具**（entry point 组 `xiaoyu.tools`，第三方 Python 包往进程里注册函数）
> 是两条互不相干的通道，只是恰好都叫 plugin。

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
```

本机端点免 key 的规则同网关：`_BASE_URL` 指向 `localhost` 时 `_API_KEY` 可省略（显式给了则以给的为准）。

`_MODELS=auto`：启动时探一次端点 `/v1/models`，把它当前 serve 的 model id 自动注册进来——本机端点换了 model 不用改配置，重启 xiaoyu 即自动跟上（新 id 会出现在 `config --show` 与 `/model` 补全里）。**只对本机 `localhost` 端点生效**（远端仍守「启动不探测」，请显式列模型名）；探测失败或返回空则该 provider 本次不注册（**不会**退化成通配去劫持网关路由）。默认路径依然从不探测 `/v1/models`。

`_PROTOCOL=anthropic` 也适用于 Bedrock Mantle 一类只挂 Claude 原生协议的端点。视觉是 fail-closed 的：**未声明即不发图**，模型会收到一行"有 N 张图但看不了"的说明而不是被静默丢弃。

`_TOOLS=text` 是给**不支持 function calling** 的端点（本地 vLLM / Ollama 上的小模型、带 `tools` 就 400 或静默忽略的老服务）准备的逃生舱：工具说明改为写进 system prompt，模型用 ```` ```tool_call ```` 代码块（也认 `<tool_call>` 标签）发起调用，结果以 `<tool_result>` 文本回灌。翻译只发生在出网那一刻，会话历史仍是标准形态，随时 `/model` 切回原生工具调用的模型。它与 `_PROTOCOL` 正交，可同时设置。原生 function calling 能用就别开它——文本解析天生更脆。

解析器认三种发起格式：我们教的 ```` ```tool_call ```` / `<tool_call>` 里放 `{"name":…,"arguments":…}`，以及训练过原生工具调用的开源模型（Qwen3 等）会自发使用的原生方言 `<tool_call><function=名字>…</function></tool_call>`（名字在壳上，参数用块内 JSON 或 `<parameter=键>值` 子标签）——后者无需你做任何配置，自动认。

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
  deepseek-v4-pro    ← 直连 deepseek（同名可兜底：网关）
  deepseek-v4-flash  ← 直连 deepseek（同名可兜底：网关）
  其余任意模型名     ← 网关（转发，不枚举）
降级链：deepseek/deepseek-v4-pro → gateway/deepseek-v4-pro → …
```

`provider/model` 是显式寻址，用来点名走哪一家：`/model gateway/deepseek-v4-pro`。点名之后不再自动兜底——既然指定了，就不该被偷偷换掉。

## 图片代读（当前模型看不了图时）

视觉能力是 **fail-closed** 的：模型没有实测声明过收得下图，图片就不会发出去
（见"接入未内置的厂商"）。默认行为是不静默丢弃、也不猜——告诉模型"有 N 张图但我
看不了"，告诉用户"`/model` 换个支持视觉的再重发"。

`XIAOYU_VISION_FALLBACK` 给这条降级路径加一手**代读**：图片先单独交给一个视觉模型
换成一段文字，再作为文本进历史。主模型、工具、消息配对、记账全都不动。

```ini
XIAOYU_VISION_FALLBACK=deepseek-v4-flash-vision-exp
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
