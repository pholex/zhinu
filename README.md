<!-- 图标用 raw 绝对 URL：PyPI 项目页不解析仓库相对路径；GitHub 亮暗主题
     经 picture 双源切换（currentColor 版经 img 加载会落成黑色，暗色下隐身，
     所以这里用写死填充色的两个变体；registry 权威副本在 docs/acp-registry/） -->
# <picture><source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/pholex/zhinu/main/docs/assets/feather-dark.svg"><img src="https://raw.githubusercontent.com/pholex/zhinu/main/docs/assets/feather-light.svg" width="30" alt=""></picture> 小羽 · Xiaoyu

[![ci](https://github.com/pholex/zhinu/actions/workflows/ci.yml/badge.svg)](https://github.com/pholex/zhinu/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/xiaoyu-agent)](https://pypi.org/project/xiaoyu-agent/)

> **Zhinu Coding Agent | Token Weaver of the Universe**<br>
> Weaving code, connecting dots, and showing you the best harness architecture.
>
> 一个自研的 harness coding agent：<br>
> 终端里可交互、可无人值守，也可作为库嵌进您自己的程序，支持多会话并行。<br>
> 依赖极简，适配 Windows / macOS / Linux 全平台。
>
> 命名两层：**织女 Zhinu 是织坊，小羽 Xiaoyu 是梭子**——harness 本就是织机的提综装置，梭子带着 token 当纬线穿行。

各大模型厂商都在做自己的 coding agent、深度绑定自家模型；行业通用的那些又越做越重——订阅、账号体系，连模型也一并卖给你。小羽反着来：**harness 内核一样不少，商业外壳一概没有**——不订阅、不登录，key 是你自己的，模型你自己挑、随时换。

安全护栏也在往同一个方向加码：确认框越堆越多、越来越难关掉，体验被一点点吃掉。小羽把这个选择权交还给你——**`--yolo` 一档做满**，不问、不停、不降速，而沙箱与不可逆命令拦截在这一档下照样兜着。

再往前看一步：**每个行业、每个企业迟早都要有自己的 coding agent**——懂自己的代码库、自己的规范、自己的工具链和审批流程，这件事外人替你做不了。小羽想当的是那层内核：agent 该有的一整套（工具、沙箱、审批、上下文管理、MCP 与技能）现成给你，模型、工具、流程你自己往上接，而不是从零再写一遍 harness。

## 安装

```bash
pip install "xiaoyu-agent[tui]"   # [tui]：补全 / 历史 / 粘贴折叠 / 贴图 / diff 高亮
xiaoyu update                     # 升级
xiaoyu uninstall                  # 卸载；--purge 连配置目录一起删
```

## 配置

```bash
xiaoyu config             # 交互向导
xiaoyu config --show      # 看生效配置与来源（key 永不回显）
```

用户级 `.env`：macOS / Linux 在 `~/.config/xiaoyu/.env`，Windows 在 `%APPDATA%\xiaoyu\.env`。

最短路径是直连厂商，填一个 key 就能跑：

```ini
DEEPSEEK_API_KEY=<your-key>
```

内置直连 deepseek / moonshot / qwen / zhipu / anthropic / openai / xai，键名一律用厂商原生名。或者走 OpenAI 兼容网关：

```ini
XIAOYU_BASE_URL=https://<你的网关>/v1
XIAOYU_MODEL=<你网关上的模型名>
XIAOYU_API_KEY=<your-key>
```

直连和网关至少配一个；**两个都配时清单自动合并**，同名模型直连优先、网关兜底。两个都没配时启动会报 MissingConfig，运行 `xiaoyu config` 按向导填即可。

## 用

```bash
xiaoyu                                  # 交互（xy 是等价缩写）
xy "把 utils.py 里的类型注解补全"          # 一次性执行（-p/--prompt 是等价拼写，兼容抄来的脚本）
git diff | xy "写一条 commit message"     # 管道内容当材料
xy --output-format json "总结这个仓库"     # 或 stream-json（NDJSON 事件流）
xy resume --last "继续把测试修完"
xy -s nightly "跑一下回归"                # 命名会话：同名接着聊，脚本反复调用用它

xiaoyu sessions                         # 列出本机会话
xiaoyu send zhinu-1 "顺便把 lint 跑一下"  # 给另一个终端里的小羽递话

xiaoyu mcp add chrome-devtools --scope user npx -y chrome-devtools-mcp@latest
xiaoyu mcp list                         # 写的就是 .mcp.json / mcp.json
```

REPL 里：`/help` `/tools` `/skills` `/model` `/mode` `/usage` `/context` `/compact` `/clear` `/exit` `/tasks` `/plan` `/perm` `/allow` `/deny` `/resume` `/rewind` `/mcp` `/quit`

无人值守时没人按确认键：先用 `/allow` 配规则，或 `--mode auto`、`--yolo`。

## 模式：放手程度你定

默认 / auto / plan 用 Shift-Tab 循环切换，或 `/mode`、`--mode` 起手；`--yolo` 单独开。

| 档 | 会不会问你 |
|---|---|
| **默认** | 写文件、执行命令逐条确认 |
| **auto** | 工作区内改文件、沙箱内跑命令免确认；危险命令、提权、写到工作区外仍要问 |
| **plan** | 只读规划态：交计划后要你批准才执行 |
| **`--yolo`** | 全放行，一路跑到底 |

auto 档**放行的依据是沙箱，不是信任**——沙箱不可用时自动降级成只有改文件免确认。

## `--yolo`：自动化给满

商业化产品把"每步都要你确认"当成必选项；小羽把它当成一档——你可以选择完全不确认。`--yolo` 是做满的一档：不问、不停、不降速，无人值守、CI、容器里就该这么跑。

它也不是无政府，四条底线**在 `--yolo` 下照样生效**：

- bash 仍在内核级沙箱里（除非你人工批准一次升权），写不出工作区、临时目录和构建缓存
- `deny` 权限规则一条都不放行
- `rm -rf /`、fork bomb 一类不可逆命令任何模式下都不执行
- 计划批准（`exit_plan_mode`）仍要问；跨会话消息默认不收

所以 `--yolo` 的实际风险面是"沙箱内能做的一切 + 联网"——介意联网就 `--no-network`。

## 大致能做什么

- **工具组**：读 / grep / glob / 精确替换 / 写文件 / bash（Windows 换 PowerShell）/ 任务清单；`explore` 子 agent 把检索委托给便宜模型；`web_search` 联网
- **后台任务**：bash 加 `run_in_background` 立即返回接着干别的，完成自动通知（不用轮询）；`monitor` 盯 CI / tail 日志，事件逐行送达并自动限流；`/tasks` 查看、`kill_task` 终止
- **编辑不出岔子**：改前必须完整读过，替换目标不唯一或文件被外部改动即打回
- **沙箱**：bash 跑在内核级沙箱里（macOS Seatbelt / Linux bubblewrap），只能写工作区、临时目录和构建缓存；`--no-network` 可断网
- **长会话不断片**：上下文快满时分层回收，Ctrl-C 随时可继续，`xiaoyu resume` 恢复历史会话
- **出错自己扛**：限流 / 瞬时错误自动重试，配了降级链就换模型接着跑
- **多协议**：按型号自动选 chat completions / Responses / Anthropic Messages，推理状态回传与 prompt caching 都用得上
- **图片输入**：TUI 里 Ctrl-V 贴截图（Windows 上用 Alt-V——终端把 Ctrl-V 留给了文本粘贴）、拖文件进终端；MCP 工具返回的图也送到模型眼前
- **可扩展**：SKILL.md 技能、`pip install` 即挂载的工具包、MCP server（`mcpServers` 格式与主流 agent 客户端通用，含 OSV 检查与 rug-pull 隔离）；MCP 工具默认走检索模式——不塞满 schema，模型用 `search_tool` 按需检索、`use_tool` 调用，几百个工具也不吃上下文
- **插件包**：`xiaoyu plugin add aws/agent-toolkit-for-aws --name aws-core` 一行装齐技能 + MCP 声明，`plugin update` 拉新。认 [agent-plugins.org](https://agent-plugins.org) 的跨客户端 bundle 格式，社区已有的包直接能用；MCP 声明默认不装，摊出命令行问过才写
- **多会话并行**：一个终端跑长任务，另一个终端 `xiaoyu send <会话名> "..."` 递话，对方在下个步骤边界收进上下文；模型自己也会用——直接说"看看还有哪些会话""让 api-1 帮我查一下"，发信前问你一次
- **可嵌入**：`xiaoyu.embedding` 的 `AsyncAgent` 把 agent 当执行引擎嵌进你自己的进程（异步审批、事件流、会话复用）；跨语言用 `--wire` 的 stdio JSON-RPC
- **可编排**：`xiaoyu serve` 起 HTTP API，n8n / Dify / 自研调度直接驱动（异步提交 + 状态轮询 + 事件游标，需要放行的工具调用挂起等 HTTP 回决定）。OpenAPI schema 由代码生成，贴给 Dify 自定义工具即用——见 [docs/http-api.md](docs/http-api.md)
- **浏览器**：推荐挂 chrome-devtools MCP；内置 `[browser]` 是纯 pip 的兜底，`playwright install chromium` 后即用
