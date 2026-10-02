# 给在本仓库里工作的 agent

你在 `xiaoyu`（PyPI 包名 `xiaoyu-agent`，导入名 `xiaoyu`）的源码仓里。开工前读这几处：

- **开发者文档**：`docs/internal/DEVELOPMENT.md`（本机内部文档，不入库；没有它就看
  `docs/` 下各篇与本文件）。
- **安全模型**：[docs/security.md](docs/security.md)——改审批 / 沙箱 / 权限推导前必读。
- **公开 API 冻结面**：[docs/embedding.md](docs/embedding.md)。顶层 `xiaoyu.__all__`
  的名字受 semver 承诺保护，加公开名必须同步那份清单（有测试拦）。

## 测试

```bash
.venv/bin/python -P -m unittest discover -s tests -t .      # 全量，约 2–3 分钟，不打网络
.venv/bin/python -P -m unittest tests.test_xxx -v             # 单个文件
```

- 用 venv 自带的 unittest，**仓库刻意不装 pytest**，别引入。
- 在仓库根目录跑，并带 `PYTHONPATH=<仓库绝对路径>`（`-P` 不把 cwd 放进 sys.path）。
- `tests_ai/`（markdown 不变量审计、第一人称自测清单）和 `experiments/` **真调模型，
  不进 CI**；只在本机按需跑。
- `tests/wheel_smoke.py` 不是 unittest 用例：要先 `python -m build`，CI 的 build job 调它。
- 改了会话 / 工具 / 权限相关代码，跑一遍全量；只改文档可以不跑。

## 提交纪律

- **不在 main 上直接提交**：开 feature 分支，完成后由人合回。
- commit message 中文，`type(scope): 说明` 风格（feat / fix / docs / chore / refactor /
  test）；正文说清"为什么"，不复述 diff。
- **不加 `Co-Authored-By` 之类的 trailer。**
- **不在代码、注释、commit、文档里出现任何同类产品的名字**：外来的思路写成自己的
  设计理由。pre-commit hook 有词表，命中会拒绝提交——被拒就改措辞，不要跳过检查。
- 不用 `git stash`（worktree 之间共享）；不 push，push 由人做。
- 生成产物、`docs/internal/`、`.env`、本机数据不入库（见 `.gitignore`）。

## 风格与边界

- 单进程、不并发、零非必要依赖：运行期核心依赖只有 pyproject `dependencies` 里那几个，
  加依赖要有不得不加的理由并同步 `tests/wheel_smoke.py` 的计数。
- 工具输出、配置、会话文件的解码一律有 `errors="replace"` 兜底；外部字节不裸抛
  `UnicodeDecodeError`（`tests_ai/test_encoding.md` 守着）。
- 核心模块不 import `rich` / `prompt_toolkit`，TUI 只住在 `xiaoyu/tui.py`
  （`tests_ai/test_layering.md` 守着）。
- 新护栏先问能不能关、怎么关（`xiaoyu/guardrails.py` 一张表）；硬红线那层刻意无开关。
- 改了 `.github/workflows/`：action 一律钉 commit SHA 并注释版本号。

## 发版

见 `docs/internal/DEVELOPMENT.md`「发版」。要点：改 `xiaoyu/__init__.py` 的
`__version__` → 跑 `tests_ai/self_test.md` 与 `scripts/release_notes.py` → 推 main 等 CI
绿 → 打 `vX.Y.Z` tag，其余自动。
