# 双包发布操作

发行包 `xiaoyu-agent` 和 `xiaoyu-agent-sdk` 同号，后者精确依赖前者的 sdk extra。
唯一版本源为 `xiaoyu/__init__.py`；SDK 构建生成自己的版本文件。

## 本地准备

完成 [验收记录](sdk-validation.md) 的门禁后，从同一提交构建：

```sh
python -m pip install build twine
python scripts/build_sdk.py --outdir /tmp/xiaoyu-release-candidate
python scripts/sdk_release.py verify /tmp/xiaoyu-release-candidate
python -m twine check /tmp/xiaoyu-release-candidate/kernel/* /tmp/xiaoyu-release-candidate/sdk/*
```

输出目录必须为空。构建同时产生两个 wheel、两个 sdist 和 release-manifest.json，
记录版本、提交、工作区是否有未提交修改及每个制品的 SHA-256。wheel 从 sdist 重建。
清单验证用于完整性和来源追溯；不承诺重新构建产生逐字节相同的压缩包。
正式发布要求清单来自准确的、无未提交修改的标签提交。

## 标签工作流

`.github/workflows/release.yml` 仍是唯一标签发布入口。推送 `v<版本>` 后执行：

1. 完整内核 CI 与 SDK 跨平台测试、类型和干净安装检查。
2. 构建和验证双包，保存不可变的 Actions artifact（保留 90 天）。
3. `pypi` environment 发布 job 下载同一制品，核对提交、版本和哈希。
4. 比较 PyPI 上内核同版本文件，仅上传缺失文件；已存在文件必须哈希相同。
5. 等待内核在索引可读且哈希一致，然后对 SDK 做同样处理。
6. 从 PyPI 新建环境只安装 SDK，执行 pip check 和安装制品冒烟。

开始前分别为两个 PyPI 项目配置 Trusted Publishing，workflow 名为 `release.yml`，
environment 为 `pypi`，并确认名称与发布权限。既有内核发布权限不会自动授权 SDK。
这次工作只修改流程定义，没有推送标签、注册项目或上传制品。
内核发布原先只上传 wheel；成对发布起会同时上传已验证的 sdist。

## 部分发布与重试

SDK 上传失败后，内核可能已经公开。保留 Actions 制品、清单与日志，使用同一次
运行的 **Re-run failed jobs**，复用成功构建 job 的制品。脚本按索引实际内容决定
跳过已成功上传的文件，不使用忽略冲突的 skip-existing。

若哈希不一致、制品过期或无法确认来源，停止该版本的续发，排查后使用新的补丁版本。
不要重跑所有 job 后拿重新构建的同版本文件覆盖已上传制品。索引暂时不可见会使等待
步骤失败（默认 180 秒，单次请求超时 20 秒）；确认服务恢复后仍可重跑失败 job。
403、网络错误等不会当作“版本不存在”。

离线可用 `verify` 核对保存的制品；`stage`/`wait` 会读取 PyPI，但本身不上传：

```sh
python scripts/sdk_release.py stage /tmp/xiaoyu-release-candidate --package kernel --destination /tmp/kernel-upload
python scripts/sdk_release.py wait /tmp/xiaoyu-release-candidate --package kernel
```

发布失败分支测试覆盖制品篡改、提交/版本不符、部分上传、同名文件哈希冲突、404 与
权限错误区分。真实 OIDC、平台矩阵和远端上传流程必须在实际发版时单独验收。
