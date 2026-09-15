# ci-github-actions：issue 打标签，小羽修，workflow 开 PR

一个无人值守的最小样本：给 issue 打上 `ai-fix` 标签，GitHub Actions 起一台用完即弃的
runner，小羽读 issue、改代码、跑测试，按 JSON Schema 交回结论；workflow 据此开 PR，
或者在 issue 下回帖说明为什么没修。

| 文件 | 作用 |
|---|---|
| `ai-fix.yml` | 整条 workflow，拷到 `.github/workflows/` 即可用 |

```bash
mkdir -p .github/workflows
cp examples/ci-github-actions/ai-fix.yml .github/workflows/
#  再去仓库 Settings → Secrets 配 DEEPSEEK_API_KEY（换模型见 docs/configuration.md）
gh label create ai-fix
#  新仓库默认不许 Actions 开 PR，打开它（组织仓库可能要管理员在组织级放开）
gh api -X PUT repos/<owner>/<repo>/actions/permissions/workflow \
  -f default_workflow_permissions=read -F can_approve_pull_request_reviews=true
```

三个值得照抄的结构（原因见 [docs/ci.md](../../docs/ci.md)）：

1. **模型改文件，workflow 发布**——小羽那一步只拿模型 key，`persist-credentials: false`
   让它碰不到仓库写凭证；push 和开 PR 是后面独立一步。
2. **外部输入走环境变量 + 管道**——issue 标题正文从 `env` 进、从 stdin 喂，
   不拼进 `run:`。
3. **按结论分支，不按退出码**——`--output-schema` 让模型交 `{fixed, summary}`，
   开 PR 还是回帖由 `fixed` 决定。
