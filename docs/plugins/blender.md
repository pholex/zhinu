# Blender：接着编辑你的场景

适合让小羽查看已有 Blender 场景、调整对象、创建模型并保存工程。
使用 [Blender Lab 官方 MCP](https://projects.blender.org/lab/blender_mcp)：小羽启动
MCP 服务，再连接 Blender 应用内的扩展。源码许可证 GPL-3.0-or-later。

日常使用时连接用户已经打开的 Blender 工程；先读取当前场景，再按任务修改。
本次验证用临时后台场景，以免修改用户工程，不要求用户日常也使用后台模式。

## 准备 Blender

需要 Blender **5.1+**、Git 和 [uv](https://docs.astral.sh/uv/getting-started/installation/)。
本次使用 macOS 上已有的 Blender 5.2.2 LTS，扩展与 MCP 均为 **1.0.3**。

1. 在 Blender 中安装并启用 Blender Lab 的 MCP 扩展。固定版本安装包：
   [mcp-1.0.3.zip](https://projects.blender.org/lab/blender_mcp/releases/download/v1.0.3/mcp-1.0.3.zip)，
   在 Preferences 的扩展安装菜单选择 Install from Disk，选中 ZIP 并启用。
2. 按扩展要求打开 Allow Online Access。在 MCP 扩展设置中保持本地 Host 和 Port
   `9876`，启动 MCP Bridge Server；也可启用 Auto Start。
3. 保留你要工作的 Blender 窗口和场景。已有窗口时不必为了接入重开一个空场景。

安装方式详见 [Blender Lab 指南](https://www.blender.org/lab/mcp-server/)。MCP 服务能列
工具不代表它已经连上 Blender，接入后需要真实读取场景来确认。

## 接入小羽

先准备运行环境，避免第一次下载导致连接超时：

```bash
uv tool run --from 'git+https://projects.blender.org/lab/blender_mcp.git@2cea8d566dde07fbac28a61d698909d69724e853#subdirectory=mcp' blender-mcp --help
xiaoyu mcp add blender --scope user --env BLENDER_MCP_HOST=127.0.0.1 --env BLENDER_MCP_PORT=9876 -- uv tool run --from 'git+https://projects.blender.org/lab/blender_mcp.git@2cea8d566dde07fbac28a61d698909d69724e853#subdirectory=mcp' blender-mcp --transport stdio
```

重新启动小羽，检查 `/mcp`，然后说：

> 读取我当前 Blender 场景，先告诉我有哪些对象，再把我指定的对象移到指定位置，
> 读回坐标确认。保存时使用我指定的工程路径。

先调用 `get_objects_summary` 确认场景；连接拒绝时检查应用内扩展是否启动及端口是否
一致，而不是继续发送编辑命令。多个 Blender 实例需要不同端口并分别配置，不能根据
前台窗口猜测连接的是哪一个。

`execute_blender_code` 使用 Blender 的 Python API 执行操作，读取结果时让 `result`
为字典。它直接修改连接中的场景；保存、导出路径应来自用户任务。通过本地端口连接
不等于沙箱，扩展有 Blender 进程的权限，桥接端口保持本机访问即可。

## 实测范围

通过小羽 MCP 发现 26 个工具，在临时 Blender 5.2.2 后台场景中执行：读取对象列表 →
创建 `CatalogCube`，位置 `(1, 2, 3)` → 再读对象列表 → 保存 `.blend` → 用另一个
Blender 后台进程重开文件，断言对象存在且坐标一致。MCP 声明添加与删除通过。

测试从固定源码临时加载了同版本应用内桥接扩展，没有修改用户偏好设置。
图形界面安装扩展、当前用户工程、截图、渲染、动画、交互式延迟任务及 Windows/Linux
未在本轮验证；不能用后台创建对象通过替代这些验证。固定源码提交不锁定传递依赖。

## 更新与移除

更新时同时核对应用内扩展版本和 MCP 源码提交，再给添加命令加 `--force` 覆盖。

```bash
xiaoyu mcp remove blender --scope user
```

结束小羽会话后 MCP 连接退出，不需要关闭用户的 Blender 工程。应用内扩展及桥接服务
由 Blender 管理，可在其扩展设置中停止或禁用；工程文件和 uv 缓存不会被删除。
完整记录见 [catalog.json](catalog.json)，返回[推荐目录](README.md)。
