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
- 提交前想把 CI 那一套在本地过一遍：`.venv/bin/python scripts/verify.py`（与 ci.yml 同序：
  全量单测 → build → twine → wheel 冒烟 → pip-audit → 密钥扫描；`--list` 看计划、
  `--only <step>` 只跑一步，缺工具的步骤 SKIP 并告诉你怎么装）。

## 分析用户的会话

用户给出会话 id（开场和退出时那行 `接回本会话：xiaoyu resume <id>` 里的 id，即会话文件名）
时，用它取这场会话的完整记录，别去猜目录：

```bash
xiaoyu sessions export <id> --format json   # 对话正文 + 工具调用摘要，不含 system 提示
xiaoyu sessions inspect <id> --errors       # 执行时间线：请求、工具错误、拒绝、压缩；--raw 展开记录、--json 结构化
```

id 精确到文件，在哪个目录跑都找得到；记录可能含用户隐私，只用于分析问题。

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

## 单一事实源

下面这些东西各只在一处声明，别处都从它渲染或与它对账；改之前先找到那一处，
再看最后一列有没有测试替你拦漂移（写「无」的要靠自己核对）。

| 关注点 | 声明在哪 | 谁消费 | 哪个测试拦 |
|---|---|---|---|
| 护栏开关层（`--unguarded` 能关哪几层、怎么单独关） | `xiaoyu/guardrails.py` 的 `LAYERS` / `KEPT` | `cli.py` 解析与横幅（`guardrails.notice`）；`docs/security.md`「放开护栏」 | `tests/test_guardrails.py::TableTest`（每层是 `Config` 字段且关值≠出厂值、横幅列全 `KEPT`）；文档一节无对账 |
| 公开 API | `xiaoyu/__init__.py` 的 `_EXPORTS` / `__all__` ↔ `docs/embedding.md`「公开面清单」 | 嵌入宿主、SDK | `tests/test_public_api.py::TestDocContract`（**双向**：漏写文档或表里多出没承诺的名字都失败） |
| 运行期依赖数、wheel 体积上限 | `pyproject.toml` 的 `dependencies` ↔ `tests/wheel_smoke.py` 的 `RUNTIME_DEPENDENCY_COUNT` / `WHEEL_SIZE_CAP` | `pip install xiaoyu-agent` 的用户 | `tests/wheel_smoke.py` 第 0 步（CI build job 调，本地 `scripts/verify.py`） |
| 命令 wrapper 与透传运行器名单 | `xiaoyu/command_check.py` 的 `WRAPPER_NAMES` / `RUNNER_SUBCOMMANDS` / `RUNNER_HEADS` | `permissions.py`（授权范围、allow 规则按内层命令判、会话授权探针） | `tests/test_command_check.py`（运行器剥开、注入看穿）、`tests/test_permissions.py`（runner 规则按内层命令判）；本机下游 channels 仓另有回归 |
| 按键表 | `xiaoyu/keys.py` 的 `BINDINGS` | `tui.py` 按表注册按键、首屏速览 `hint_line`、轮播 `tips`、菜单提示；`cli.py` 的 `/keys` 打 `help_text` | `tests/test_keys.py`（注册只能来自表、`/keys` 覆盖每个 `show` 项、速览行覆盖前缀） |
| hooks 事件表 | `xiaoyu/hooks.py` 的 `EVENTS`（带工具名的子集 `TOOL_EVENTS`） | `hooks.toml` 的准入校验；`docs/configuration.md`「生命周期钩子」事件表 | `tests/test_hooks.py::test_events_table_covers_every_event_the_kernel_fires`（扫源码里每个 `fire("X")` 字面量）；文档表无对账 |
| 斜杠命令 | `xiaoyu/cli.py` 的 `SLASH_COMMANDS` | `/help`、TUI 补全、ACP `available_commands_update`（`acp.py` 只列名字、描述从这里取，缺键直接 KeyError） | `tests/test_goal.py`（登记即出现在 /help 与补全）、`tests/test_e2e_acp.py`（广告载荷） |
| 环境变量与开关 | `docs/configuration.md`「变量总表」；读取点散在 `xiaoyu/config.py` 等处 | 用户、`xiaoyu doctor` 的提示 | **无**——加变量时自己对一遍表，并在测试隔离（`tests/test_e2e_scripted.py` 的 `env_for`、`tests/wheel_smoke.py` 的 `clean_env`）里把新开关关掉 |
| 发版说明 | `docs/releases/<版本>.md`，由 `scripts/release_notes.py` 生成 | `release.yml` 的 `gh release create`（文件不存在退回自动生成） | `tests/test_release_notes.py`（分组与退化路径）；`scripts/release.py --dry-run` 查文件在不在 |

常见改动的食谱：

- **加一个 env 开关**：在 `config.py` 照现有 `XIAOYU_ENABLE_*` 的读法加读取 →
  `docs/configuration.md` 变量总表加一行 → 若它是一层护栏，登记到 `guardrails.LAYERS`
  → 测试隔离里关掉它（上表「环境变量」一行）。
- **加一个公开名**：`xiaoyu/__init__.py` 的 `_EXPORTS` 加一项（懒导出，别在顶层急切
  import）→ `docs/embedding.md`「公开面清单」加同名一行 → 跑 `tests.test_public_api`。
- **加一个运行期依赖**：先问能不能不加；要加就在 `pyproject.toml` 精确锁版本 →
  `tests/wheel_smoke.py` 的 `RUNTIME_DEPENDENCY_COUNT` 同步 +1 → commit 正文写为什么
  不得不加。可选能力走 `optional-dependencies` 的 extra，不动计数。
- **加一个 hook 事件**：`hooks.py` 的 `EVENTS` 加名字（带工具名就同时进 `TOOL_EVENTS`）
  → 内核里 `engine.fire("新事件", payload)` → `docs/configuration.md` 事件表加一行
  （时机、拦截语义、payload 字段）→ `tests/test_hooks.py` 补用例。
- **加一个斜杠命令**：`cli.py` 的 `SLASH_COMMANDS` 加键值 → `handle_slash` 实现 →
  要给 ACP 客户端用就进 `acp.py` 的 `_ACP_COMMANDS` → README「用」一节的 REPL 行补上。

## 发版

见 `docs/internal/DEVELOPMENT.md`「发版」。要点：改 `xiaoyu/__init__.py` 的
`__version__` → 跑 `tests_ai/self_test.md` 与 `scripts/release_notes.py` → 推 main 等 CI
绿 → 打 `vX.Y.Z` tag，其余自动。打 tag 前跑 `scripts/release.py --dry-run`：在 main、
工作树干净、HEAD 已推、版本号大于所有本地 tag 与 PyPI 已发布版本、发版说明文件存在、
公开文档没有「尚未发布」、main 最近一次 CI 绿；全过后不带 `--dry-run` 即在本地打带注释
的 tag（它绝不 push，推 tag 由人做）。
