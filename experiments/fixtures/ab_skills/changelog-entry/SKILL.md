---
name: changelog-entry
description: 把 CHANGES/pending/ 下的变更片段汇成 CHANGELOG.md 的一个版本条目。用户说"写发版条目""整理变更记录"时使用。
---

# changelog-entry — 发版条目

把待发布的变更片段汇成 `CHANGELOG.md` 里的一个版本条目。

## 输入

- `CHANGES/pending/<编号>.md`：每个文件一条变更。第一行是 `type: feat`、`type: fix` 或
  `type: internal`，空一行后是一句话说明。文件名里的数字是变更编号。
- `RELEASE_DATE`：这次发版的日期。日期只从这里取，不要用今天的日期。
- 版本号由用户给。

## 步骤

1. 读完 `CHANGES/pending/` 下的所有片段。
2. 在 `CHANGELOG.md` 的标题行之后、上一个版本之前插入新条目，格式：

   ```
   ## <版本号> — <日期>

   - 新增：<说明>（#<编号>）
   - 修复：<说明>（#<编号>）
   ```

3. 删掉 `CHANGES/pending/` 下**所有**已读过的片段（包括没写进条目的）。

## 规则

1. `type: internal` 的片段不写进条目——用户看不到的改动不进变更记录。
2. 先列全部「新增」，再列全部「修复」；同一类里按编号从小到大。
3. 说明照抄片段原文，不改写、不合并、不加句号。
4. 不改以前版本的条目，不动 `CHANGELOG.md` 和 `CHANGES/pending/` 之外的任何文件，不提交。
