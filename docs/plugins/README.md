# 官方推荐插件与 MCP 扩展

推荐从实际任务出发：让小羽接着使用用户已有的文件、页面和工作状态，减少重复准备。
需要干净环境时提供独立的测试路径，由任务决定使用方式。

这里收录具备真实验证记录的扩展，具体接入模式分别标明“已实测”和“按上游说明，待实测”。
插件由各自的上游维护；推荐不代表由小羽开发，也不保证上游后续版本的行为。
当前目录是人工维护的安装指南，不会自动安装或更新扩展。技能包走 `plugin add`，
独立 MCP 服务走 `mcp add`，两种接入方式在各条指南中分别说明。

| 分类 | 推荐项 / 接入方式 | 适合做什么 | 已验证范围 | 上游维护者 |
|---|---|---|---|---|
| 浏览器协作 | [Chrome DevTools](chrome-devtools.md) / MCP | 连接当前 Chrome、复用登录态，也可调试页面 | 隔离模式已实测；当前资料自动连接待实测 | ChromeDevTools |
| 文档生成 | [Tabella](tabella.md) / MCP | 生成 Word、PPT、PDF | 基础创建与内容回读；PDF 有已知页码问题及规避步骤 | D Yiapanis |
| 表格编辑 | [Excel MCP](excel.md) / MCP | 创建 XLSX、写入数据与简单公式 | 创建、写入、求和回读、目录限制、声明装卸 | Haris Musa |
| 3D 创作 | [Blender](blender.md) / MCP | 读取、编辑场景并保存工程 | 后台场景创建、保存及重开核验；图形界面待实测 | Blender Lab |
| 软件分析 | [REA](rea.md) / MCP | 分析现有应用的实现与行为 | JavaScript 目录静态分析；其他后端待实测 | REA 项目 |
| 开发文档 | [Context7](context7.md) / MCP | 按库与版本查询 API 文档 | Python 指定版本查询、声明装卸 | Upstash |
| 云平台 | [aws-core](#aws-core) / 技能包 + MCP | AWS 开发指导、查询公开 AWS 文档 | 技能安装与加载、匿名文档搜索、卸载 | Amazon Web Services |

只有满足真实调用和维护要求的扩展才加入，不以条目数量为目标。
验证版本、日期、环境和结果统一记录在 [catalog.json](catalog.json)。

只需读取 PDF、DOCX 或 XLSX 时，先使用小羽的
[内置文档工具](../builtin-tools.md)，按需安装 `xiaoyu-agent[pdf,docx,spreadsheet]`。
浏览器也已有内置工具与桥接入口，本目录优先补足能力缺口。

### 已评估但暂未推荐

以下结论只针对记录中的提交和安装路径，不代表对项目全部能力的判断：

| 候选 | 本次结果 |
|---|---|
| [LSP 桥接](lsp-evaluation.md) | Python 定义、类型查询与修改后诊断刷新通过；引用查询遗漏别名调用，暂缓推荐 |
| [document-SKILLs](https://github.com/appautomaton/document-SKILLs) | 技能路径放在市场清单中，固定提交的根目录缺少小羽能识别的包级技能声明；直接安装无法识别，不要求用户手改上游包凑合安装 |
| [OfficeKit documents](https://github.com/w31r4/OfficeKit) | 单独安装 documents 包可加载技能，但其引用的相邻 office-kit 公共指引没有随包安装；完整运行时与文档创建流程未验证，暂缓推荐 |

Word/PDF 的基础创建可使用上面的 Tabella；复杂编辑和版式保真仍需按具体任务验证。
候选提交和具体失败点保留在 `catalog.json` 中，供以后复验。

## aws-core

适合需要编写 AWS 基础设施代码、理解服务用法的开发者。技能提供任务指导；
可选的 MCP 服务用于获取在线资料。来源为
[AWS 的公开仓库](https://github.com/aws/agent-toolkit-for-aws)，许可证 Apache-2.0。

### 安装验证过的版本

需要 Git 和 xiaoyu。仅安装技能无需云账户；启用 MCP 另需
[uv（包含 uvx）](https://docs.astral.sh/uv/getting-started/installation/)。
首次启动 MCP 会下载代理程序及其依赖，需要联网。

在准备长期存放插件来源的目录中执行（目标目录须不存在）：

```bash
git clone https://github.com/aws/agent-toolkit-for-aws.git xiaoyu-aws-plugins
git -C xiaoyu-aws-plugins checkout --detach 1761e96813ebcbd43ed81d19e837219033f0afe5
xiaoyu plugin add ./xiaoyu-aws-plugins --name aws-core
```

安装时会展示 MCP 启动命令并询问是否安装声明。只需技能时回车即可；需要在线
文档搜索时检查命令再回答 `y`。验证过的声明为：

```text
uvx mcp-proxy-for-aws-cli==1.7.0 https://aws-mcp.us-east-1.api.aws/mcp --skip-auth --metadata INSTALL_SOURCE=agent-toolkit-core
```

非交互安装默认只装技能；确实需要 MCP 时可在安装命令后加 `--accept-mcp`。
已经装了技能，再补 MCP 时需要同一命令加 `--force`，确认后覆盖安装。

安装后重新打开小羽，用 `/skills` 找 `aws-core:` 前缀的技能，用 `/mcp` 查看服务状态。
可以试着说：“加载 aws-core:aws-cdk 技能，然后搜索 AWS 官方文档，解释 construct
是什么，给出来源链接。”仅装技能时省去搜索要求。

### 权限和验证边界

- 已验证 25 个技能的发现和正文加载，以及通过小羽 MCP 客户端真实调用
  `aws___search_documentation`，查询 `AWS CDK constructs` 并收到官方文档内容与链接。
  没有验证模型在所有 AWS 任务中的回答质量。
- 该固定版本的声明带 `--skip-auth`；本次只验证匿名公开文档搜索，没有使用云账户
  凭据，没有调用部署、资源管理或远端脚本执行功能。工具列表可能仍列出这些能力，
  不代表匿名模式可以执行它们。
- 搜索词会发送给 AWS 服务；不要把密钥或业务数据放入文档搜索词。后续若按上游
  指南配置账户访问，权限取决于使用的身份，可能产生资源变更和费用，超出本条验证范围。
- 技能是供模型读取的指引，可能引导它使用 shell 或云工具；后续工具调用仍走小羽
  现有权限流程。上游随包附带的 hooks 不会安装，不能依赖那些 hooks 提供保护。
- 固定仓库提交和代理版本不能固定远端服务的实现；在线结果可能随服务更新变化。

### 更新和卸载

以上安装记录的是本地来源。请保留 `xiaoyu-aws-plugins` 目录；`plugin update`
只会重新读取这个本地目录，不会替它拉取 Git 更新。需要升级时，先查看本目录是否有
新的验证记录，再把来源目录检出到对应提交，执行：

```bash
xiaoyu plugin update aws-core
xiaoyu plugin list
```

也可以直接 `xiaoyu plugin add aws/agent-toolkit-for-aws --name aws-core` 跟随上游默认
分支，但拿到的版本可能超出本次验证范围。`--ref` 目前接受分支或 tag，不能用提交 SHA
代替；要精确复现请用前面的本地检出步骤。

不再需要时：

```bash
xiaoyu plugin remove aws-core
```

这会删除已安装的技能并摘掉该插件安装的 MCP 声明，不会删除来源仓库、uv 的下载缓存
或用户自行配置的凭据。结束当前小羽会话后再启动，可确保旧的 MCP 连接已退出。

## 收录和维护规则

推荐范围必须对应具体用户场景。新增条目时，在同一份变更中提供：

1. 上游仓库、维护者、许可证、固定提交或发行包版本，以及必需的软件和账户条件。
   从源码安装时记录包内目录；远端服务不能固定实现时，明确记录核验日期与地址。
2. 可复制的安装和卸载命令，联网去向、凭据使用和可能写入的资源。
3. 在临时用户配置目录中的安装、技能加载或工具发现、一次真实能力调用、卸载结果。
   MCP 插件还需确认声明能安装和摘除。仅解析成功不能标为真实调用通过。
4. 日期、小羽版本、验证平台、未覆盖的能力，以及失败时可复现的现象。

推荐信息由小羽维护者随 PR 审核。每次更换推荐提交、上游改变启动命令或凭据方式、
用户报告兼容问题时重新验证；超过 90 天未复验的条目移到“待复验”，不继续作为
当前推荐。失效、来源转移或缺少维护响应时撤下推荐并保留原因记录。
未来更新目录时同步修改本页命令和 `catalog.json`，核对提交及启动命令一致。

复验应使用临时 `XDG_CONFIG_HOME`（Windows 为临时 `APPDATA`），不要覆盖日常插件
配置；只做推荐范围内的调用，不用部署生产资源来证明连接成功。在线验证按需执行，
不放进离线单元测试。当前记录只覆盖所列平台，不推断其他平台已通过。

完整的安装器行为见[插件包配置说明](../configuration.md#插件包skills--mcp-一起装)。
