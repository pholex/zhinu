# Chrome DevTools MCP：前端调试与页面验证

适合开发网页时检查交互、控制台和网络请求。上游为
[ChromeDevTools/chrome-devtools-mcp](https://github.com/ChromeDevTools/chrome-devtools-mcp)，
许可证 Apache-2.0。通过 `mcp add` 接入，不需要安装技能包。

它和现有浏览器入口各有用途：

| 任务 | 入口 |
|---|---|
| 调试开发中的页面，检查控制台、请求、截图 | 本条推荐的独立 Chrome DevTools MCP |
| 操作用户已经登录的浏览器标签页 | [浏览器桥](../browser-bridge.md) |
| 只需要基础浏览器自动化，希望沿用 Python 环境 | 内置 `[browser]` 扩展 |

## 安装与使用

需要 Google Chrome、Node.js 和 npm/npx。本次验证使用 Node 24.19.0、Chrome
156.0.8078.80、macOS；固定包声明支持 Node `^20.19.0 || ^22.12.0 || >=23`，
使用时优先选择仍受维护的 Node LTS。首次 npx 下载需要联网。

```bash
xiaoyu mcp add chrome-devtools --scope user -- npx -y chrome-devtools-mcp@1.12.0 --headless --isolated --no-usage-statistics --no-performance-crux
```

若已添加同名服务，核对原配置后在 `--scope user` 后加 `--force` 覆盖。
在网页项目目录中启动小羽，用 `/mcp` 查看连接，然后可以说：
“打开我的本地开发地址，检查按钮点击后的内容变化、控制台错误和失败请求，保存截图。”
浏览器按需启动，单纯连接 MCP 不代表已打开浏览器。

上述命令启动无头 Chrome，`--isolated` 使用临时用户数据目录，不带日常浏览器的
登录态，也不连接已经打开的 Chrome。若需要看见浏览器窗口，可以去掉 `--headless`，
但有头模式不在本次验证范围。需要复用日常登录态时看浏览器桥，不要直接套用本条配置。

截图文件放在服务工作目录内；默认继承启动目录，需要固定路径时可在 MCP 添加参数中
指定 `--cwd /absolute/path/to/project`（放在 `-- npx` 前）。本次测试中，目录外的
截图路径被上游拒绝，在服务工作目录内保存成功。这不是对所有文件访问的沙箱保证。

## 真实验证与数据边界

通过小羽 MCP 客户端，在独立浏览器中打开临时本地测试网页，验证了：

- 页面快照可读，能从快照取得按钮 ID。
- 点击后文字从 `Ready` 变成 `Clicked`。
- 控制台能读到该次点击输出的 `review-click`。
- 网络列表包含点击触发的 `/probe` 请求，状态为 200。
- 截图保存为 PNG，并目视确认标题、按钮和点击后文字可见。
- 用户级 MCP 声明的添加与删除。

上游还提供性能分析工具，但本条没有验证性能 trace、CrUX、登录站点、复杂页面、
移动端模拟、有头模式、Windows/Linux 或模型自主完成任务的质量。

工具可读取网页内容、网络信息并执行交互，返回内容会进入模型上下文。页面中的点击
可能产生实际业务操作，仍应按小羽现有审批流程授权。临时配置文件不等于匿名联网或
网络沙箱：访问网站仍会向该站点发请求。

推荐命令关闭工具使用统计及向 CrUX 发送性能 URL；这两个开关不控制 Chrome 自身
的遥测。本次验证仅访问本地测试页，未使用用户账户，未测试全部网络行为。

## 更新和卸载

推荐固定 `1.12.0`，不使用 `@latest` 自动跟进未经验证的版本。升级时参考新验证记录，
用新的版本重新执行添加命令并加 `--force`。固定包版本不锁定全部传递依赖。

```bash
xiaoyu mcp remove chrome-devtools --scope user
```

结束当前小羽会话以关闭旧的 MCP 子进程及其浏览器；删除声明本身不会立即关闭已连接
的进程，也不会删除项目截图或 npm 缓存。完整记录见 [catalog.json](catalog.json)，
返回[推荐目录](README.md)。
