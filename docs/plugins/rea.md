# REA：理解现有软件如何工作

适合调查现有应用的实现和行为，例如 Electron 应用怎样导出数据、某个 JavaScript
入口调用了哪些文件 API，或配合反编译工具分析原生程序。它是软件分析工具集，
不是常规库文档查询工具，也不会自动证明分析结论正确。

来源 [morluto/rea](https://github.com/morluto/rea)，npm 包 `rea-agents`，MIT。
本条使用实际验证过的 **6.7.0**，直接接入原生 MCP，无需额外移植客户端插件。

## 安装与使用

需要 Node.js/npm。6.7.0 包声明支持 `^22.19.0 || ^24.11.0 || >=26.0.0`，本次使用
Node 24.19.0；新版本的要求可能不同。首次 npx 下载需要联网。

```bash
xiaoyu mcp add rea --scope user -- npx -y rea-agents@6.7.0 mcp
```

重新启动小羽，用 `/mcp` 查看连接。可以说：

> 用 REA 分析这个 JavaScript 应用目录，找出读取本地文件的入口，给出函数和文件证据。
> 这一步先做静态分析，不启动目标应用。

对于已有应用，明确给出文件或目录路径以及要回答的问题。REA 的工具返回结构化证据，
其中包含已知结果与分析限制；保留限制说明，不把静态推断写成运行时观察。
JavaScript 分析不要求先安装原生反编译器。分析原生二进制则按
[上游安装说明](https://rea.tools/get-started/)准备 Ghidra、Hopper 或 IDA 等对应后端；
不同目标需要不同依赖，不需要一次装齐全部工具。

## 实测范围

小羽 MCP 客户端成功发现 207 个工具，并真实调用 `analyze_javascript_application`。
样本是临时创建的 `package.json` 与 `main.js`，包含导出的 `readReport` 函数及
`fs.readFileSync` 调用；返回证据中包含这些标识及分析结果。没有执行样本程序。
用户级声明添加、移除也已验证。

本条只验证 JavaScript 目录静态分析，不代表全部 207 个工具已验证。
原生反编译、Android、运行时调试、浏览器附加、源码恢复、Windows/Linux 和模型自主
完成调查尚未验证。某些工具会启动程序、操作设备或写文件，按任务选择相应能力；
调用仍走小羽现有权限流程。分析读取的内容可能作为工具结果进入模型上下文。

## 更新与移除

按目录的新验证版本重新执行添加命令，在 `--scope user` 后加 `--force` 覆盖。
固定 npm 包版本不锁定所有后端工具和传递依赖。

```bash
xiaoyu mcp remove rea --scope user
```

结束当前小羽会话以退出旧服务。移除声明不卸载反编译器，也不删除分析产物或 npm
缓存。完整记录见 [catalog.json](catalog.json)，返回[推荐目录](README.md)。
