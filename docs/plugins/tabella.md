# Tabella：把内容做成 Word、PPT 和 PDF

适合把整理好的内容生成可交付文档，例如会议摘要、简单演示稿或 PDF。
只需读取现有文件时可先用小羽内置工具；需要写出文档时再接入本扩展。
来源 [dyiapanis/tabella](https://github.com/dyiapanis/tabella)，维护者 D Yiapanis，MIT。

本条推荐 **2.0.2 的基础文档创建与回读**，使用本地 stdio MCP。上游也提供格式转换、
OCR 和 PDF 编辑，但这些能力未在本次验证。不是桌面 Word/PPT 的实时控制工具。

## 安装

需要 Git、Python 3.11+。下面步骤适用于 macOS/Linux shell；本次验证平台为 macOS。
选择长期保存源码和依赖的位置，源码目录须尚不存在：

```bash
TABELLA_SOURCE="$HOME/.local/share/xiaoyu/tabella-src"
TABELLA_DATA="$HOME/.local/share/xiaoyu/tabella-data"
mkdir -p "$HOME/.local/share/xiaoyu"
git clone https://github.com/dyiapanis/tabella.git "$TABELLA_SOURCE"
git -C "$TABELLA_SOURCE" checkout --detach 449e75462dc227789ef5c64e828c4db577f40ee5
bash "$TABELLA_SOURCE/scripts/setup.sh" "$TABELLA_DATA"
xiaoyu mcp add tabella --scope user --env "DOCS_VENV_PYTHON=$TABELLA_DATA/venv/bin/python" -- "$TABELLA_DATA/venv/bin/python" "$TABELLA_SOURCE/mcp_server.py"
```

先完成 setup 再接入，避免首次下载超出 MCP 握手等待时间。setup 将文档依赖装进单独
venv，使用上游带哈希的锁定文件；它也会联网升级该 venv 的 pip。不要装进小羽环境。
显式设置 `DOCS_VENV_PYTHON` 可避免服务落到另一处默认环境。

这里单独接入 stdio，不用 `plugin add` 一并导入上游附带的 HTTP 示例：后者需要另起
服务和填写令牌，基础文档任务不需要。保留上述源码和依赖目录，后续启动仍会用到。

### PDF 的系统依赖

Word/PPT 创建无需下面这一步。PDF 使用 WeasyPrint，需要 Pango 等原生库，见
[WeasyPrint 安装说明](https://doc.courtbouillon.org/weasyprint/stable/first_steps.html#installation)。
macOS 使用 Homebrew 时可安装 `pango`；若仍出现 `libgobject` 找不到的错误，给该服务
显式设置库路径（在同一 shell 保留上面的变量）：

```bash
brew install pango
xiaoyu mcp add tabella --scope user --force --env "DOCS_VENV_PYTHON=$TABELLA_DATA/venv/bin/python" --env "DYLD_FALLBACK_LIBRARY_PATH=$(brew --prefix)/lib" -- "$TABELLA_DATA/venv/bin/python" "$TABELLA_SOURCE/mcp_server.py"
```

本次 macOS 实测使用已有的 `/opt/homebrew/lib`，此配置解决了加载失败。
其他平台应按原生库安装说明配置各自的路径。

## 开始使用

重新启动小羽，查看 `/mcp`，然后说：

> 用 Tabella 把下面的内容做成 Word，再用同样内容生成一份 PDF，保存到我指定的目录。
> PDF 暂时关闭页码，生成后读回核对内容。

该版本的 PDF 默认页码存在问题，会把 `counter(page)` 当文字输出。
调用 `doc_create_document` 生成 PDF 时传 `options: {"page_numbers": false}`；
这一规避方式已经实测。正式交付仍需查看实际页面，不能只核对文字提取结果。

让小羽明确传入 `output_path`。不传时默认在 `~/Documents` 下输出，也可给 MCP 配置
`DOCS_OUTPUT_DIR` 改默认目录；它不是所有文件读写的访问边界，已有同名文件需先确认。
数据处理在本地服务中完成，读回的文档内容进入模型上下文。

## 验证记录

通过小羽 MCP 创建 DOCX、PPTX、PDF，再用 `doc_read` 读回已知文本和数字 `42`。
PDF 关闭页码后渲染为图片，确认正文可见且无页码占位文字。声明添加与删除也通过。
测试未调用模型，不涉及用户文件。Word/PPT 的验证是内容回读，没有做桌面应用视觉检查。

未验证复杂排版、中文字体、模板保真、OCR、Pandoc 转换、PDF 编辑以及 Windows/Linux。
格式转换需另装 Pandoc，部分 PDF 转换还需要 TeX，OCR 需要 Tesseract；本条不把这些
依赖加入基础安装。记录见 [catalog.json](catalog.json)。

## 更新与移除

更新时检出新验证提交，重新运行 setup，并重启小羽；不是 `plugin update` 管理。
卸载声明用 `xiaoyu mcp remove tabella --scope user`，结束当前会话后服务退出。
文档、源码和依赖目录不会自动删除。返回[推荐目录](README.md)。
