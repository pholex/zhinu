---
name: testing-workflow
description: 本仓库改了什么该跑哪些门（单文件 / 全量 / snapshot / wheel 冒烟 / tests_ai / 下游回归 / 多平台 CI 手动触发）的对照表与命令
---

# 改动类型 → 该跑哪些门

仓库刻意不装 pytest，一律用 venv 自带的 unittest；在仓库根目录跑，`-P` 不把 cwd 放进
sys.path，所以要显式给 `PYTHONPATH`。下面 `$R` 代表仓库绝对路径。

```bash
R=/abs/path/to/zhinu
cd "$R" && PYTHONPATH="$R" .venv/bin/python -P -m unittest tests.test_xxx -v        # 单文件
cd "$R" && PYTHONPATH="$R" .venv/bin/python -P -m unittest discover -s tests -t .   # 全量，约 4 分钟，不打网络
.venv/bin/python scripts/verify.py            # CI 同序：全量 → build → twine → wheel 冒烟 → pip-audit → 密钥扫描
.venv/bin/python scripts/verify.py --list     # 看本平台计划；--only <step> 只跑一步
```

| 改了什么 | 至少跑 | 还要注意 |
|---|---|---|
| 只改 `docs/*.md`、README | 可以不跑 | ci.yml 对只改文档的 push 不跑矩阵 |
| `xiaoyu/docs/*.md`（随包分发、路径进 system prompt） | `tests.test_e2e_snapshot` + wheel 冒烟 | 改前看 `tests/snapshot_support.py` 有无归一化影响；删了挪了 wheel_smoke 会拦 |
| 单个模块的小修 | 对应 `tests.test_<模块>` | 提交前仍建议全量；不确定对应哪个测试就 `grep -l "<模块名>" tests/*.py` |
| 会话 / 工具 / 权限 / 沙箱相关代码 | **全量** | AGENTS.md 的硬要求 |
| `permissions.py` 的规则推导、`banned_allow_reason`；`command_check.py` 的 wrapper / 运行器表 | 全量 + 下游回归 | 本机下游宿主仓 channels（不入库）：`cd ~/Developer/channels && .venv/bin/python -m pytest -q -p no:cacheprovider tests/test_agent_xiaoyu_sdk.py` |
| 模型可见的变更：wire 协议、provider、system prompt、工具 schema、人机可见输出 | `tests.test_e2e_snapshot`，并在同批改动里**新增或 refresh 一个 keyless 场景** | `XIAOYU_SNAPSHOT=refresh` 重写 golden，`record` 用真 API 录新场景；见 `docs/test_snapshot.md` |
| `pyproject.toml`、`MANIFEST.in`、package-data、新子包、按 `__file__` 读的数据文件 | `scripts/verify.py --only build --only twine --only wheel_smoke` | 新数据文件同时登记到 `tests/wheel_smoke.py` 的 `RUNTIME_DATA` |
| 运行期依赖增减 | 同上 + `RUNTIME_DEPENDENCY_COUNT` 同步 | pip-audit 在 CI 里对装好的 wheel 跑（失败只黄标） |
| `packages/xiaoyu-agent-sdk/`、`examples/sdk/`、`examples/serve/`、`xiaoyu/questions.py` 等 SDK 面 | `tests.test_sdk*`、`tests.test_public_api`、`tests.test_embedding_smoke`，各 `examples/sdk/*.py --demo` | 完整清单与 mypy 在 `.github/workflows/sdk.yml`；它对所有分支的这些路径 push 自动跑 |
| 顶层 `xiaoyu.__all__` / `docs/embedding.md` | `tests.test_public_api` | 双向对账：漏文档或多出没承诺的名字都失败 |
| `xiaoyu/tui.py`、按键表 `keys.py` | `tests.test_tui`、`tests.test_keys` | CI 三平台都装了 `[tui]`，这些用例不会被跳过；行编辑类改动再开真终端手测 |
| 跨平台敏感：路径、子进程、文件锁、编码、信号 | 推分支后**手动触发**三平台 CI（见下） | ci.yml 只对 main 的 push 自动跑 |
| import 分层、外部字节解码这类架构约束 | `tests_ai/run.py`（真调模型，不进 CI） | `.venv/bin/python tests_ai/run.py [layering|encoding]`，吃仓库 `.env` 的模型 key |
| 发版前 | `tests_ai/self_test.md` + `scripts/verify.py` + `scripts/release.py --dry-run` | 见 release-checklist 技能 |

## 多平台 CI 手动触发

feature 分支 push 不会自动跑 ci.yml（只有 main 跑），要三平台结果就手动起：

```bash
gh workflow run ci.yml --ref <分支>            # 三平台 × 3.11/3.14
gh workflow run sdk.yml --ref <分支>           # SDK 契约 + mypy + 示例 demo
gh run list --workflow ci.yml --branch <分支> --limit 3
gh run view <run-id>                           # 判结果看这个，别信 `gh run watch` 的退出码
gh run view <run-id> --log-failed              # 运行器级失败再看 check-runs annotations
```

代价是 Actions 里永久留记录，所以日常走本地全量 + verify.py，只有平台敏感的改动才上去跑。

## 写测试的几条硬纪律

- 假密钥用拼接写法（`"sk-" + "…"`），成串字面量会被提交检查拒绝，也会被 gitleaks 抓。
- 绝不用无限输出的命令（`yes`、`seq` 无上限）当夹具——CI 磁盘写满过一次。
- 新加 `XIAOYU_ENABLE_*` 开关要在测试隔离里关掉（`tests/test_e2e_scripted.py` 的 `env_for`、
  `tests/wheel_smoke.py` 的 `clean_env`）。
- 断言要能 fail：先喂错答案确认它红，再喂对的（eval case 固化成 `tests/test_eval_assertions.py`）。
- 并行 worktree 里不用 `git stash`（全仓共享，会互相 pop）；全量在自己的 worktree 上跑。
