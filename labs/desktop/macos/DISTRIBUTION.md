# 羽案 macOS 本地发布

产品名「羽案」，Bundle ID `click.openapi.xiaoyu`。版本来自 `build.py` 的
`APP_VERSION`。保留原生 AppKit/WKWebView 外壳，Python、标准库、后端及桌面依赖
均放入应用包。使用包内相对路径定位解释器；不需要用户安装 Python。

当前流程构建本机架构，macOS 最低版本 13。Apple Silicon 构建不能标为通用安装包；
Intel 需要匹配架构的独立 Python、依赖和构建环境，并另行验证。

## 准备

- 构建环境安装仓库的核心依赖、`serve`、`sdk`、`tui` extras，以及 `httpx`、`packaging`。
- 准备明确版本的 python-build-standalone 运行时。Python 主次版本和架构必须与构建环境一致。
- 钥匙串中有含私钥的 Developer ID Application 身份。
- 公证凭据预先通过 `xcrun notarytool store-credentials` 保存在钥匙串；命令只传 profile 名称。

运行时来源说明：[独立 Python 分发](https://gregoryszorc.com/docs/python-build-standalone/main/distributions.html)。
发布服务说明：[Apple 公证流程](https://developer.apple.com/documentation/security/customizing-the-notarization-workflow)。

## 构建与验证

在仓库根目录执行。以下路径、签名身份与 profile 由本机配置提供：

```bash
python labs/desktop/macos/release.py build \
  --directory labs/desktop/local-results/release-0.13.0 \
  --standalone-python /path/to/standalone-python
python labs/desktop/macos/verify_bundle.py \
  'labs/desktop/local-results/release-0.13.0/羽案.app'
```

依赖按 pyproject 声明取构建环境中已安装的递归依赖，校验版本约束，并记录具体版本。
按发行包文件清单复制，保留许可证，排除开发环境的入口脚本和字节码；不复制整个 venv。
不接受运行时链接到包外的符号链接，也不接受未经审查的 `.pth` 初始化文件。
构建检查原生二进制动态库引用，拒绝对开发机框架或包管理器路径的依赖。

`build-manifest.json` 记录签名前文件 SHA-256、依赖版本、架构与产品版本，
以及构建开始时的源码 commit 和 dirty 标识（含未跟踪、非忽略文件）。签名、公证阶段
保留该记录，不用签名时的源码状态覆盖构建来源。正式交付应从已提交的干净工作区构建。
`verify_bundle.py` 实际复制应用到新路径，用临时 HOME 和最小 PATH 检查导入、
离线示例后端、鉴权 API、页面与 EOF 退出，最后核验签名未被运行过程改变。
它不调用真实模型，也不读取用户已有会话。

## 正式签名和公证

```bash
python labs/desktop/macos/release.py sign \
  --directory labs/desktop/local-results/release-0.13.0 \
  --identity 'Developer ID Application: YOUR NAME (TEAMID)'
python labs/desktop/macos/verify_bundle.py \
  'labs/desktop/local-results/release-0.13.0/羽案.app'
python labs/desktop/macos/release.py notarize \
  --directory labs/desktop/local-results/release-0.13.0 \
  --keychain-profile YOUR_PROFILE
python labs/desktop/macos/release.py dmg \
  --directory labs/desktop/local-results/release-0.13.0 \
  --identity 'Developer ID Application: YOUR NAME (TEAMID)'
python labs/desktop/macos/release.py notarize-dmg \
  --directory labs/desktop/local-results/release-0.13.0 \
  --keychain-profile YOUR_PROFILE
```

先对包内原生代码逐个签名，再签外层应用；使用 Hardened Runtime 和时间戳。
公证先处理应用并附加票据，再制作带 Applications 快捷方式的 DMG；随后公证 DMG，
附加票据并核验 Gatekeeper。公证结果必须为 Accepted，失败不会继续下一步。
最终 `SHA256SUMS.txt` 对应附加票据之后的 DMG。

## 分发边界

仅上传已公证的 DMG、校验文件和发行说明；不上传源码、运行时暂存目录或钥匙串文件。
构建与发布脚本不执行 git push，不自动上传下载站，不导出私钥。
每次使用新的构建目录，保留旧版本供回滚。切换正式标识时会话存储目录保持不变，
原生偏好只在首次启动迁移，不覆盖正式版已有选择。

发布前还需在其他 Mac 上验证下载后的首次启动和真实模型配置。当前没有自动更新器，
也没有内置 Node.js、Git、第三方 CLI 或浏览器自动化内核；需要这些工具的功能仍取决于
用户安装的环境。内置 Python 仅保证桌面后端及其声明依赖能独立运行。

原生应用为后端保留独立 Python 入口，并提供系统目录、继承的绝对 PATH 目录、
两种 Homebrew 安装目录、用户 `.local/bin`、Volta、asdf 与 mise 的常用入口。
不会执行 shell 初始化文件；其他安装位置可在连接器中填写绝对命令或显式 PATH。
连接前检查启动命令是否可用，缺少运行环境时不会挂载连接器。
