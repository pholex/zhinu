# 持续运行与记忆验证

不需要真实业务数据。`scripts/continuity_check.py` 自带隔离任务，默认离线，
只写显式指定的新结果目录，不安装定时任务、不改变用户记忆库。验证分三层，
离线结果不能当成真实模型的学习效果，短跑不能当成 72 小时稳定性结论。

在仓库根目录用 `.venv/bin/python -P` 运行；脚本自动定位本仓库 SDK，无新增依赖。
结果目录必须不存在，避免覆盖旧证据；只有中断的 soak 支持 `--resume`。

## 进程故障矩阵

```bash
.venv/bin/python -P scripts/continuity_check.py faults --out /tmp/xiaoyu-faults
```

11 项检查覆盖：可重复／不可重复动作在副作用前后被杀、等待期间反复重启、
网络不可用的模拟异常与退避、验收失败后的有限修复、期限、调用预算和卡死回调。
等待子进程写出定位标记后才强制终止，不用随机 sleep 猜故障窗口。
业务账本使用另一份 SQLite，独立核对实际动作、尝试次数和幂等键。

卡死测试由**验证脚本的宿主监督器**终止子进程，随后确认不安全动作进入 uncertain；
这不表示 SDK 自己已经能够抢占同步回调。故障矩阵的逻辑时钟是模拟时间，
不是长期在线时长。所有故障只施加到脚本创建的子进程。

## 跨会话对照

```bash
.venv/bin/python -P scripts/continuity_check.py memory --out /tmp/xiaoyu-memory-offline
```

每轮 20 组对照，覆盖事实、偏好、经验应用、更正、遗忘、过期、待审、项目隔离、
身份隔离和无关记忆。事实值随机生成，经验应用使用金额舍入契约。
每组分别以新进程、新 Session 运行“关闭／开启记忆”，交替运行顺序。
另外检查一次从合成会话提取候选、出处校验、待审不可检索、重复提取不再调用模型。

离线模型只按收到的上下文产生确定输出，用于验证接线。真实模型评测显式开启：

```bash
# 先在环境中配置 SDK_MODEL、SDK_API_KEY；可选 SDK_BASE_URL、SDK_PROTOCOL。
# 不要把凭据写进命令参数或报告。与 examples/sdk 使用相同的配置约定。
.venv/bin/python -P scripts/continuity_check.py memory --live --repeats 3 \
  --max-model-requests 121 --out /tmp/xiaoyu-memory-live
```

每轮至多 40 次回答请求，另有一次候选提取请求；每个 Session 最多一次请求，
不为格式错误追加模型修复。报告分别保留两组结果、严格答案判定、召回 key、
token 使用量与耗时。服务未报告用量时记 `null`，请求次数另从请求账本统计；
成本缺少价格表时记 `null`，不是零费用。
超时／失败保留在分母中；不开记忆组答不出隐藏事实是预期基线，不要求它全对。

对照组的记忆是测试程序显式审核的合成夹具；真实模型提取的候选始终保持 pending。
因此这能测召回、简单规则迁移和候选来源，尚不能证明真实项目中的自主学习收益，
也不能把 fixture-oracle 审核搬到生产环境自动批准任意候选。

## 连续运行

```bash
# 先做短跑，检查环境与报告。
.venv/bin/python -P scripts/continuity_check.py soak --seconds 60 --interval 1 \
  --out /tmp/xiaoyu-soak-short

# 72 小时离线实进程运行，不产生模型费用。
.venv/bin/python -P scripts/continuity_check.py soak --seconds 259200 --interval 60 \
  --max-cycles 2000 --out /tmp/xiaoyu-soak-72h

# Ctrl-C 或进程退出后，保持源码不变，从原目录继续。
.venv/bin/python -P scripts/continuity_check.py soak --resume --out /tmp/xiaoyu-soak-72h
```

任务是修复发票舍入 JSON 配置 → 等待测试服务 → 在独立 Python 进程跑 unittest →
验收失败后修复 → 再验收。生成内容只作为数据读取，不执行生成的代码。
离线第一次故意使用错误规则，确保验收与修复路径实际被走到。
首次在产物提交后杀掉执行进程；重启复用同一幂等键的产物，之后每次 tick 都换进程。
测试服务以本地信号文件模拟，不代表真实远程 CI 可用性。

`--live` 可让真实模型生成配置，须同时配置 SDK 环境；`--max-model-requests`
限制整个 campaign 的请求预留次数，先落盘再请求，重启不重置，失败也不退还。
预算不足或达到 `--max-cycles` 会提前停止并报告，不能冒充完成指定时长。
单回调进程超时会终止本次实验并留下证据，不自动无穷重试。

`progress.json` 持续更新，分别记录监督器累计在线时间与中断空档；恢复时不把停机时间
计入目标时长。源码摘要变化会拒绝继续，避免把多个实现混成一次实验。
需要系统确保进程持续存活时，由宿主自己的进程管理器启动这条前台命令。

每种运行产生 `report.json` 与简明 `report.md`，附提交、工作区是否有改动、模式、
结果与证据；运行中的 soak 可先查看 `progress.json`。报告和会话数据不应提交入库。
先看有没有越界执行、任务丢失、错误记忆泄漏，再比较效果和成本；没有达到运行时长
或未实际调用模型的部分必须保持“未验证”。
