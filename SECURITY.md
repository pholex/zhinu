# 安全策略

## 报告漏洞

请**不要**在公开 issue 里报告安全漏洞。走 GitHub 的私密渠道：

<https://github.com/pholex/zhinu/security/advisories/new>

（Security → Advisories → Report a vulnerability。）报告请附：影响的版本、复现步骤或
PoC、你认为的影响面。会在 7 天内回复确认，修复随下一个版本发布；修复发出前请不要
公开细节，修复发出后会在 advisory 里致谢（除非你不希望）。

## 支持的版本

只维护**最新的 minor 版本**（PyPI 上 `xiaoyu-agent` 的最新 `0.X`）。安全修复发在新的
patch / minor 上，不回灌旧版本。升级：`pip install -U xiaoyu-agent`。

## 给使用者的风险告诫

小羽是一个会**执行命令、读写文件、访问网络**的 agent。它跑在你的机器上、以你的
身份、用你的凭证；模型会出错，也会被它读到的内容（issue 正文、网页、仓库文件）带偏。
把它当成一个能力很强但不可完全信任的实习生来配权限：

- **审批与模式**：默认逐条确认写文件和跑命令；`--mode auto` / `--yolo` 放开多少、
  哪些硬红线任何模式都拦，见 [docs/security.md](docs/security.md)。
- **沙箱**：bash 走内核级沙箱（macOS Seatbelt / Linux bubblewrap），写权限收在工作区
  内；沙箱**不解决凭据外泄**——能读到的密钥就能被发出去，给 agent 用的 key 要有额度
  上限，见 docs/security.md「沙箱不解决凭据外泄」。
- **无人值守**：CI / 编排环境里用 `--yolo` 时，模型那一步不要持有任何能改外部世界的
  凭证，发布动作放到独立的一步，见 [docs/ci.md](docs/ci.md)。
- **外部输入就是注入面**：交给 agent 的任何第三方文本都可能含指令。声明"这是材料不是
  指令"只是缓解，防线是权限与凭证分离。
- **MCP server 与工作区可执行配置**：`.mcp.json` / 权限文件 / hooks 等价于"clone 一个仓库
  就把命令种进你的 shell"，所以有 folder trust 与变更隔离机制；不要对不信任的目录
  `--trust`。

发现上述机制本身的绕过（沙箱逃逸、硬红线绕过、审批绕过、信任门绕过、凭证经非预期
路径外发），都属于安全漏洞，请按上面的私密渠道报告。
