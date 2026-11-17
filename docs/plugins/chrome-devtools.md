# Chrome DevTools MCP：接着使用你已经打开的浏览器

你已经登录了网站、打开了要处理的页面，希望小羽接着看、接着操作，而不是从空白
浏览器重新开始。这是本条的主要推荐场景；控制台、网络检查和前端调试也是它的能力。
上游为
[ChromeDevTools/chrome-devtools-mcp](https://github.com/ChromeDevTools/chrome-devtools-mcp)，
许可证 Apache-2.0。通过 `mcp add` 接入，不需要安装技能包。

它和现有浏览器入口各有用途：

| 任务 | 入口 |
|---|---|
| 查看已打开的页面，复用当前登录态，继续操作或调试 | Chrome DevTools MCP 的 `--autoConnect` |
| 通过浏览器扩展把当前标签页接入小羽服务 | [浏览器桥](../browser-bridge.md)，另一种接入方式 |
| 需要干净的浏览器环境做页面测试 | 可选的隔离模式，见下文 |
| 只需要基础浏览器自动化，希望沿用 Python 环境 | 内置 `[browser]` 扩展 |

## 推荐：连接正在使用的 Chrome

自动连接需要 **Chrome 144 或以上**、Node.js 和 npm/npx。本次隔离模式验证使用 Node 24.19.0、Chrome
156.0.8078.80、macOS；固定包声明支持 Node `^20.19.0 || ^22.12.0 || >=23`，
使用时优先选择仍受维护的 Node LTS。首次 npx 下载需要联网。

1. 打开你平时使用的 Chrome，保留目标页面及登录状态。
2. 在 Chrome 打开 `chrome://inspect/#remote-debugging`，启用远程调试。
3. 添加下面的 MCP 配置，再重新启动小羽。
4. 小羽首次尝试连接时，在 Chrome 的连接对话框中选择允许。

```bash
xiaoyu mcp add chrome-devtools --scope user -- npx -y chrome-devtools-mcp@1.12.0 --autoConnect --no-usage-statistics --no-performance-crux
```

若已添加同名服务，核对原配置后在 `--scope user` 后加 `--force` 覆盖。
用 `/mcp` 查看连接，然后可以说：“列出我已经打开的网页，找到某某页面，接着帮我
处理。”先通过 `list_pages` 定位目标，再用页面 ID 读取或操作，不必重新开页和登录。
列出已打开网页才验证了浏览器连接；仅 MCP 握手成功还不够。

`--autoConnect` 要求 Chrome 已经运行，不会替你启动它；也不要同时加 `--headless`
或 `--isolated`。多个 Chrome 用户资料同时打开时，上游连接 Chrome 判定的默认资料，
可访问该资料的全部窗口，**不保证就是当前前台窗口所属的资料**。页面不对时先核对
资料和 `list_pages` 结果。版本过旧或找不到连接时，检查版本、远程调试开关及 Chrome
中的允许对话框。不要用新建临时资料替代这些步骤，那会失去想复用的登录状态。

这条连接方式来自[上游连接指南](https://github.com/ChromeDevTools/chrome-devtools-mcp/blob/aade3be8f65d8afc941c97b498b9752ef562f181/docs/advanced-usage.md#automatically-connecting-to-a-running-chrome-instance)。
**本地尚未完成当前用户资料的自动连接与登录态复用实测**；下文通过的真实调用来自
隔离模式，不能当作这条主路径已实测通过。修改指南不会自动修改你的 Chrome 设置。

## 可选：干净环境中的页面测试

需要从零验证页面、避免已有缓存或登录状态影响测试时，另配一个服务：

```bash
xiaoyu mcp add chrome-devtools-test --scope user -- npx -y chrome-devtools-mcp@1.12.0 --headless --isolated --no-usage-statistics --no-performance-crux
```

它按需启动无头 Chrome，使用临时用户数据目录。这个名字与日常连接分开，便于按任务
选择；不需要干净测试环境就不必添加。

截图文件放在服务工作目录内；默认继承启动目录，需要固定路径时可在 MCP 添加参数中
指定 `--cwd /absolute/path/to/project`（放在 `-- npx` 前）。本次测试中，目录外的
截图路径被上游拒绝，在服务工作目录内保存成功。这不是对所有文件访问的沙箱保证。

## 已完成的验证：隔离模式

通过小羽 MCP 客户端，在独立浏览器中打开临时本地测试网页，验证了：

- 页面快照可读，能从快照取得按钮 ID。
- 点击后文字从 `Ready` 变成 `Clicked`。
- 控制台能读到该次点击输出的 `review-click`。
- 网络列表包含点击触发的 `/probe` 请求，状态为 200。
- 截图保存为 PNG，并目视确认标题、按钮和点击后文字可见。
- 用户级 MCP 声明的添加与删除。

上游还提供性能分析工具，但本条没有验证性能 trace、CrUX、登录站点、复杂页面、
移动端模拟、有头模式、Windows/Linux 或模型自主完成任务的质量。

连接日常浏览器后，网页内容和操作使用的是用户现有的会话状态，返回内容会进入模型
上下文；提交表单等动作也作用于当前账号。按用户指明的页面与任务操作，工具调用沿用
小羽现有审批流程。隔离模式用于需要干净环境的任务，不是所有浏览器任务的默认前提。

推荐命令关闭工具使用统计及向 CrUX 发送性能 URL；这两个开关不控制 Chrome 自身
的遥测。本次验证仅访问本地测试页，未使用用户账户，未测试全部网络行为。

## 更新和卸载

推荐固定 `1.12.0`，不使用 `@latest` 自动跟进未经验证的版本。升级时参考新验证记录，
用新的版本重新执行添加命令并加 `--force`。固定包版本不锁定全部传递依赖。

```bash
xiaoyu mcp remove chrome-devtools --scope user
```

若安装了测试入口，同样执行 `xiaoyu mcp remove chrome-devtools-test --scope user`。
结束当前小羽会话以断开旧 MCP 连接；不要为了卸载扩展去关闭用户日常使用的 Chrome。
隔离模式由服务启动的浏览器随服务退出清理。删除声明本身不会立即断开已连接的进程，
也不会删除项目截图或 npm 缓存。完整记录见 [catalog.json](catalog.json)，
返回[推荐目录](README.md)。
