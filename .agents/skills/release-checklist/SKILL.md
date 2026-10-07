---
name: release-checklist
description: 发一版 xiaoyu-agent 的步骤清单：bump 版本、自测、发版说明、推 main 等 CI、release.py 预检打 tag、推 tag 后核对
---

# 发版清单

发版 = 改 `__version__` → 自测与发版说明一起提交 → 推 main 等 CI 绿 → 本地打带注释的
tag → 推 tag。推 tag 之后全自动：release.yml 跑完整 CI 与 SDK 契约 → 构建校验 →
PyPI Trusted Publishing（OIDC，无长期 token）→ 装回冒烟 → 用 `docs/releases/<版本>.md`
建 GitHub Release。下面每一步都有脚本或命令，按序走；`$R` 是仓库绝对路径。

1. **起点干净**：在 main 上、工作树干净、本地 main == origin/main。要发的改动已经合进 main
   （feature 分支由人合，不在 main 上直接开发）。
2. **bump 版本**：只改 `xiaoyu/__init__.py` 的 `__version__`（pyproject 是 dynamic，别处不写）。
   烧毁的版本号不可复用——PyPI 上发过又删的也不行，第 7 步的预检会查索引。
3. **全量门禁**：`.venv/bin/python scripts/verify.py`（与 CI 同序）。bump 之后必须再跑一遍，
   不能拿 bump 前的结果顶。pre-push 钩子还会再跑一次全量，中途别再改 `__version__`。
4. **第一人称自测**（真调模型）：按 `tests_ai/self_test.md` 顶部的命令在空临时目录里跑，
   `jq -e '.output.rate >= 0.8'` 过了才继续；密钥在用户级 `.env` 时带 `XIAOYU_ENV_FILE`。
   再按那里的 jq 一行补上 `platform` 字段，得到 `self_test.record.json`。
5. **发版说明**：`python scripts/release_notes.py --validation <self_test.record.json>`
   → `docs/releases/<版本>.md`。润色稿逐条对事实，人工补一节「行为变化（升级前看一眼）」；
   文末的「本版验证」由脚本按记录生成，不要手改。顺手 grep 公开文档的「尚未发布」字样
   并清掉（`scripts/release.py --dry-run` 也会拦）。
6. **提交并推 main**：版本号与发版说明一个提交，`chore(release): X.Y.Z`。push 慢是 pre-push
   在跑全量（约 2–4 分钟），别去查网络。然后等 CI：
   ```bash
   gh run list --workflow ci.yml --branch main --limit 1
   gh run view <run-id>          # 判结果看这个，别信 `gh run watch` 的退出码
   ```
7. **预检 + 打 tag**：
   ```bash
   .venv/bin/python scripts/release.py --dry-run   # 分支 / 树 / 已推 / 版本号 vs tag 与 PyPI / 说明文件 / 「尚未发布」/ CI
   .venv/bin/python scripts/release.py             # 全过才在本地打带注释的 tag vX.Y.Z，绝不 push
   ```
   WARN（没网查不到 PyPI、fetch 失败）不拦，自己判断；FAIL 必须先修。
8. **推 tag**：`git push origin vX.Y.Z`（只推 tag 时 pre-push 跳过测试）。这一步触发发布，
   由人做。
9. **盯 release workflow**：`gh run list --workflow release.yml --limit 1` → `gh run view`。
   偶发失败（PyPI 等待、网络）用 `gh run rerun --failed <run-id>`，别重打 tag。
10. **发后核对**：
    - PyPI 看 `https://pypi.org/simple/xiaoyu-agent/` 索引（别信 JSON API 的缓存）；
    - `gh release view vX.Y.Z` 说明来自 `docs/releases/<版本>.md`，不是自动生成的；
    - 干净 venv `pip install xiaoyu-agent==X.Y.Z && xiaoyu --version`；
    - 旧 tag / 旧 Release 删不删先问人，别自作主张；
    - 有自管部署或外部注册表钉着版本的，按内部文档逐个升。

## 不要做的

- 不在 main 上直接提交发版以外的改动；不 `--no-verify` 跳过提交检查。
- 不手改 `docs/releases/` 里历史版本的文件（那是当时的事实）。
- 不在 CI 没绿之前打 tag：release.yml 会再跑一遍 CI，红了不发，但 tag 已经占了号。
