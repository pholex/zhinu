# Excel MCP：创建和编辑本地表格

小羽已有 `read_spreadsheet`，只读表格时优先使用内置工具。本条适合需要创建 XLSX、
写入单元格和简单公式的用户，不要求安装 Microsoft Excel。
上游为 [haris-musa/excel-mcp-server](https://github.com/haris-musa/excel-mcp-server)，
维护者 Haris Musa，许可证 MIT。接入形态是本地 MCP 服务，不是技能包。

## 安装与使用

需要 [uv / uvx](https://docs.astral.sh/uv/getting-started/installation/) 和 Python 3.11
或以上的可用运行环境。首次启动会下载 Python 包及依赖，需要联网。服务不安装进小羽
的 Python 环境，也不增加小羽的运行期依赖。

先创建专门的工作目录，再把下面的 `/absolute/path/to/workbooks` 替换为它的真实绝对
路径（Windows 使用对应的绝对路径；本次只验证 macOS）：

```bash
xiaoyu mcp add excel --scope user -- uvx excel-mcp-server==2.2.6 stdio --allow-dir /absolute/path/to/workbooks
```

路径含空格时用引号包住。保留 `--allow-dir`，它限制该服务可操作的目录；此范围是
服务自己的配置，不会自动跟随小羽当前工作区变化。

重新打开小羽，用 `/mcp` 查看状态。可以说：
“在 Excel 工作目录创建 demo.xlsx，Data 工作表写入两项金额 2、3，用 SUM 求和并读回。”
涉及已有文件时先备份；写入会改变磁盘文件，不是只在聊天中生成预览。

## 已验证的范围

通过小羽 MCP 客户端在临时目录中真实完成：

1. `create_workbook` 创建 `review.xlsx` 和 `Data` 工作表。
2. `write_range` 写入文本、数字和 `=SUM(B2:B3)`。
3. `read_range` 读回全部值，公式结果为 `5`；另检查 XLSX 内部 XML 确实保存了公式。
4. 在允许目录外创建文件被拒绝，确认目标文件没有生成。
5. 添加和删除用户级 MCP 声明。

这些结果只覆盖基础创建、写入、简单求和和一次目录限制检查。复杂公式、图表、透视表、
宏、样式保真、桌面 Excel 显示以及 Windows/Linux 均未验证。不把简单求和通过当作
完整 Excel 计算兼容性承诺。

服务是拥有本地文件访问能力的第三方进程，`--allow-dir` 不是操作系统沙箱。本次只向
本地 stdio 传入测试表格数据，没有使用远端表格服务；未做网络隔离或依赖安全审计。
目录中的文件可能被修改，工具调用仍经过小羽已有权限流程。推荐命令不启用 VBA 写入，
也不启动 HTTP 监听服务。

## 更新和卸载

此命令固定服务版本，不会自动升级，也不由 `plugin update` 管理。升级先看目录的新
验证记录，再用新的版本和同一工作目录重新执行 `mcp add`，加 `--force` 覆盖声明。
固定服务版本并不锁定全部传递依赖。

```bash
xiaoyu mcp remove excel --scope user
```

结束当前会话后再启动以退出旧子进程。移除声明不会删除已生成的表格或 uv 下载缓存。
完整验证记录见 [catalog.json](catalog.json)，返回[推荐目录](README.md)。
