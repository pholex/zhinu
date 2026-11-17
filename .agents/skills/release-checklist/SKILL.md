---
name: release-checklist
description: 发一版 xiaoyu-agent：scripts/release.py 一条命令（预检、bump、发版说明、ACP 副本、提交、tag）+ 一次 git push --atomic，之后无人值守；写提交时的「行为变化」footer 约定；nightly 自测
---

# 发版清单

发版 = **一条命令 + 一次 push**。`$R` 是仓库绝对路径，要在一个签出 main 的工作树里跑
（主目录在别的分支上就 `git worktree add /tmp/xiaoyu-release main` 去那边跑）。

```bash
cd $R   # 或签出 main 的 worktree
.venv/bin/python scripts/release.py 0.72.0 --dry-run   # 只看预检与将要做的事
.venv/bin/python scripts/release.py 0.72.0             # 或 --bump patch / minor
git push --atomic origin main v0.72.0                  # 推 main 与 tag 一次走；这一步由人做
```

推上去之后全自动，不用盯：ci.yml / sdk.yml 由 main 的 push 触发；release.yml 由 tag 触发，
先用 `scripts/ci_gate.py` 等同一提交的 CI 结论（不重跑），绿了就构建 → PyPI Trusted
Publishing → 装回冒烟 → 用 `docs/releases/<版本>.md` 建 GitHub Release。约 15 分钟后
`gh run list --workflow release.yml --limit 1` 看一眼即可。

## release.py 做了什么

1. **预检**（FAIL 拦住；WARN 只打出来）：在 main、工作树干净、HEAD == origin/main（先
   fetch）、目标版本号大于当前 `__version__` / 所有本地 tag / PyPI 已发布的每一版（烧毁的号
   不可复用）、tag 不存在、自上个 tag 有非 merge 提交、公开文档没有「尚未发布」、父提交最近
   一次 CI 绿（要 `gh`）、nightly 自测记录存在且不旧（只警告）。
2. 改 `xiaoyu/__init__.py` 的 `__version__`（pyproject 是 dynamic，别处不写）。
3. `scripts/release_notes.py` 生成 `docs/releases/<版本>.md`：一张平铺列表，一个提交一行「新增/修复/文档/工程/改动：subject（短 sha）」，不抄正文、不带 scope；
   **「行为变化（升级前看一眼）」从 commit 正文抽**（见下）；「本版验证」取最近一次 nightly
   自测记录。默认不调模型；要润色加 `--polish`。
4. 同步 `docs/acp-registry/agent.json` 的 `version` 与 `package==` 钉版本（registry 收录后会
   自动 bump，收录前副本跟着发版走）。`--acp-fork ~/Developer/acp-registry-fork/xiaoyu/agent.json`
   可一并改 fork 副本（不提交，自己 amend + force-with-lease 后推）。
5. 提交 `chore(release): X.Y.Z`，打带注释的 tag `vX.Y.Z`。**绝不 push。**

## 写提交时：行为变化 footer

升级后用户会感知到的不同，写进 commit 正文的 footer，一条一段，到空行为止：

```
fix(permissions): 运行器规则按内层命令判

说明为什么……

行为变化: 之前 allow 规则写 `npx *` 会放行所有 npx 子命令，现在按内层命令判，原来放行的可能要重新 /allow
```

`BREAKING CHANGE:` 也认；subject 带 `!`（`feat!: …`）而没写 footer 的，拿 subject 当那一条。
发版说明里这一节就从这些 footer 来，不再人工润色。

## nightly 自测

`tests_ai/self_test.md`（第一人称自测，真调模型）不再按版跑，由 `scripts/self_test.py`
每晚跑一遍，记录存到用户配置目录 `self-test/`（`latest.json` 指向最近一次），发版说明
「本版验证」一节引用它（带记录时间与跑的是哪份源码）。

```bash
.venv/bin/python scripts/self_test.py                   # 手动跑一遍（约 10–20 分钟，真花钱）
.venv/bin/python scripts/self_test.py --install-launchd # macOS：每天 03:30 跑；--at HH:MM 改时刻
launchctl kickstart gui/$(id -u)/com.pholex.xiaoyu-self-test   # 立即触发一次
```

记录太旧（> 7 天）或通过率 < 80% 时 `release.py` 预检给 WARN，不拦；fail 的项该修就修。

## 发后

- `gh run view <release run>` 偶发失败（PyPI 等待、网络）用 `gh run rerun --failed`，别重打 tag。
- 东京 serve 由服务器上的 `zhinu-autoupgrade.timer` 每小时查 PyPI 自动升级（无在跑会话才升，
  升完健康检查不过自动回滚），不用手动升；想立刻升就 `ssh tokyo-docker-04080903 sudo systemctl start zhinu-autoupgrade`。
- **旧 tag 与旧 Release 不删**。
- 红了：版本号烧掉，修完换个号再 `release.py`。

## 不要做的

- 不在 main 上直接提交发版以外的改动；不 `--no-verify` 跳过提交检查。
- 不手改 `docs/releases/` 里历史版本的文件（那是当时的事实）。
- 不手动 bump `__version__`、不手写发版说明——都是 `release.py` 的活。
