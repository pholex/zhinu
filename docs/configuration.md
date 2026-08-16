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

内置直连：deepseek / moonshot / qwen / zhipu / xai / openai / anthropic。**键名一律用厂商原生名**（别家工具已配过的直接复用）：

```ini
DEEPSEEK_API_KEY=<key>
MOONSHOT_API_KEY=<key>
OPENAI_API_KEY=<key>
ANTHROPIC_API_KEY=<key>
```

每家走哪种 wire 协议（chat completions / Responses / Anthropic Messages）、哪些型号能看图，都是按型号内置好的，不用管。

## 网关

任意 OpenAI 兼容端点（LiteLLM、vLLM、各家官方 API…）：

```ini
XIAOYU_BASE_URL=https://<你的网关>/v1
XIAOYU_MODEL=<你网关上的模型名>
XIAOYU_API_KEY=<key>
```

直连和网关至少配一个，两个都配见下方"多 provider"。

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
| `XIAOYU_ENV_FILE` | — | 指定 `.env` 路径，等价 `--env-file` |

### 上下文与压缩

| 变量 | 默认值 | 说明 |
|---|---|---|
| `XIAOYU_CONTEXT_LIMIT` | 按模型查表 | 上下文上限（token）覆写 |
| `XIAOYU_COMPACT_AT` | `0.7` | 用量占到这个比例时触发回收/压缩 |
| `XIAOYU_KEEP_RECENT` | `8` | 压缩时至少保留最近几条消息 |
| `XIAOYU_EXPLORE_ITERATIONS` | `12` | `explore` 子 agent 单次检索的工具调用轮数上限（1–100；主 agent 的 50 轮不受影响） |

### 功能开关（`0` = 关）

| 变量 | 说明 |
|---|---|
| `XIAOYU_ENABLE_EXPLORE` | `explore` 检索子 agent |
| `XIAOYU_ENABLE_SKILLS` | 扫描 `~/.agents/skills/` 与已装插件包下的 SKILL.md |
| `XIAOYU_ENABLE_WEB_SEARCH` | `web_search` 工具 |
| `XIAOYU_SEARCH_PROVIDER` | 搜索走哪家：`deepseek`（默认，便宜）/ `xai`（grok-4.6，更强更贵） |
| `XIAOYU_ENABLE_BROWSER` | `browser` 浏览器工具（依赖可选 `[browser]` extra 的 playwright，没装时本来就不出现） |
| `XIAOYU_ENABLE_PLUGINS` | entry point 组 `xiaoyu.tools` 的第三方工具**包**（代码级；和 `xiaoyu plugin` 装的**内容包**不是一回事，见下） |
| `XIAOYU_ENABLE_MCP` | MCP server 挂载 |
| `XIAOYU_MCP_OSV` / `_WATCHDOG` / `_CACHE` / `_RECONNECT` | MCP 的恶意包预检 / 孤儿进程回收 / schema 缓存 / 断线自动重连 |
| `XIAOYU_MCP_TOOL_SEARCH` | MCP 工具检索模式（默认开：工具不进 schema，`search_tool` 检索 + `use_tool` 调用；`0` = 回到全量注册） |
| `XIAOYU_FOLDER_TRUST` | 工作区信任门（默认开，见[安全](security.md)；只认真实环境变量与用户级 `.env`） |
| `XIAOYU_ENABLE_HOOKS` | 用户级 `hooks.toml` 生命周期钩子 |
| `XIAOYU_ENABLE_AGENTS` | 声明式 subagent（`agents/*.toml`） |
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

> `xiaoyu plugin` 装的是**内容包**（技能文本 + MCP 声明）。它和 `XIAOYU_ENABLE_PLUGINS`
> 管的**插件工具**（entry point 组 `xiaoyu.tools`，第三方 Python 包往进程里注册函数）
> 是两条互不相干的通道，只是恰好都叫 plugin。

## 接入未内置的厂商

`<NAME>` 自取，大写：

```ini
XIAOYU_PROVIDER_MINIMAX_BASE_URL=https://api.minimaxi.com/v1
XIAOYU_PROVIDER_MINIMAX_API_KEY=<key>
XIAOYU_PROVIDER_MINIMAX_MODELS=minimax-m2,minimax-m2-turbo   # 留空 = 通配
XIAOYU_PROVIDER_MINIMAX_PROTOCOL=responses                   # 默认 chat；可选 anthropic
XIAOYU_PROVIDER_MINIMAX_VISION=*                             # 声明视觉能力，默认不发图
```

`_PROTOCOL=anthropic` 也适用于 Bedrock Mantle 一类只挂 Claude 原生协议的端点。视觉是 fail-closed 的：**未声明即不发图**，模型会收到一行"有 N 张图但看不了"的说明而不是被静默丢弃。

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
