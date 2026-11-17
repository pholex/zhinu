# Context7：按库版本查 API 文档

适合编程时确认某个库的 API、参数和示例。它提供库 ID 和版本定位，补充小羽已有的
通用网页搜索；不保证每个库或版本都已收录。

上游为 [Upstash / Context7](https://github.com/upstash/context7)，源码许可证 MIT。
本条推荐的是 **MCP 服务接入**，不是 `plugin add` 安装的技能包。

## 安装与使用

无需 Node.js 或本地服务程序，需联网：

```bash
xiaoyu mcp add context7 --scope user --url https://mcp.context7.com/mcp
```

重新打开小羽，用 `/mcp` 查看连接，再提问：
“用 Context7 查 Python 3.13.9 的 pathlib.Path.read_text 参数，给出文档来源。”
先用 `resolve-library-id` 找库和已收录版本，再用 `query-docs` 查询，不要猜库 ID。

本次无凭据调用通过，但匿名额度和可用性由服务方控制。需要更多额度时按
[上游配置说明](https://github.com/upstash/context7#readme)申请 API key，使用小羽的
静态 HTTP header 配置；账户登录、额度和带凭据调用未在本条验证。

**不要把地址替换为 `/mcp/oauth`。** 本次检查的上游标准插件包使用该地址，
在小羽中返回 HTTP 401，要求 OAuth 登录；小羽当前不完成这种登录流程。
普通 `/mcp` 地址已实际验证，不需要先安装那个技能包。

## 数据与验证边界

查询词和库名发送给 Upstash 服务，不要包含项目私有源码或凭据。此次通过小羽 MCP
客户端真实完成：查找 Python → 获取 `/python/cpython` 及版本列表 → 查询
`/python/cpython/v3.13.9` 的 `Path.read_text`，收到参数解释与固定版本源码文档链接。
声明添加和删除也已在临时配置目录中验证。

服务实现在线更新，无法用 Git 提交固定远端行为。验证记录中的提交仅标识本次
检查过的上游说明和插件声明，不是远端服务版本。未验证其他库覆盖率、付费额度、
Windows/Linux 或模型回答质量。没有把检索片段作为正确性担保，使用时仍核对来源。

## 卸载

```bash
xiaoyu mcp remove context7 --scope user
```

结束当前会话再启动以关闭旧连接；此命令不会撤销服务方账户或 API key。
完整记录见 [catalog.json](catalog.json)，返回[推荐目录](README.md)。
