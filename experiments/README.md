# experiments — 真实调模型的验收与测量脚本

`tests/` 是本地单元测试（130 个，不打网络）。这里是**真实调模型**的端到端验收，
以及 README 里那些百分比的出处。跑之前先 `pip install -e .` 并配好 `.env`。

## ⚠️ 前提

所有脚本都用 `--yolo`（无人值守、不逐条确认），工作区在 `/tmp` 下的一次性目录。
**`bash` 工具在 `--yolo` 下没有路径边界**——它能写你有权限的任何地方。
已经踩过一次：eval 期间某个模型 `pip install pytest` 装进了系统 Python。
所以这些脚本只在可丢弃目录里用，别指着真实仓库跑。

## macOS 后台运行

脚本动辄跑几分钟，别在前台阻塞：

```bash
nohup bash -c "trap '' HUP; exec ./experiments/compaction_e2e.sh" >/tmp/run.log 2>&1 &
```

两个坑都踩过：

- **macOS 没有 `setsid`**，别照抄 Linux 写法。
- **只用 `nohup` 不够**：它只让直接子进程忽略 SIGHUP，`bash -l` 起的孙进程会把
  SIGHUP 恢复成默认处理，终端一断就被杀（`exit_status: 129`）。所以要 `trap '' HUP`。
- 长任务输出重定向到文件时 Python 默认全缓冲，**日志会一直是空的、看着像挂了**。
  eval runner 里已经 `sys.stdout.reconfigure(line_buffering=True)` 修掉了。

## 脚本

| 脚本 | 测什么 |
|---|---|
| `explore_ab.sh off\|on\|forced\|compare` | explore 子 agent 到底省不省 |
| `compaction_e2e.sh` | 上下文压缩 + 便宜模型摘要，6 条显式判定 |
| `edit_precision_e2e.sh` | 130 行文件定点改，用 diff 抓整文件重写 |
| `fixtures/multihop_repo.py` | 4 层间接跳转 + 诱饵常量的仓库 |
| `fixtures/versioned_modules.py` | 5 个大模块，各带一个 VERSION 常量 |

## 已记录的结果

### explore（2026-08-03，主模型 deepseek-v4-pro / 子 agent deepseek-v4-flash）

多跳链路追踪任务：

| 组 | 主模型 in tok | 总成本 | explore |
|---|---|---|---|
| off | 24165 | $0.01168 | — |
| on（弱引导） | 25398 (+5%) | $0.01238 (+6%) | 没用 |
| **forced** | **11925 (-51%)** | **$0.00957 (-18%)** | 用了 |
| on（强引导） | 29097 (+20%) | $0.01640 (+40%) | 用了，但又重读了 8 个文件 |
| on（修好证据行+offset 后） | 25804 (+7%) | $0.01237 (+6%) | 没用 |

结论见根 README「explore 与实测数据」。要点：用了确实省一半上下文，但
**prompt 引导的采用率只有 1/3**，所以改成 harness 层面强制（连续读 3 次提示、5 次拦截）。

### 压缩（2026-08-03）

`6866 → 1255 tok（省 5611）`，摘要由 `deepseek-v4-flash` 生成，
5 个版本号全部活过压缩，产物可运行，无 API 错误。

## 写新实验的三条教训

1. **必须显式判定「被测功能真的执行了」**，不能只判「最终结果对」。
   压缩验收前两版都因为这个假通过——一个从不触发的功能，在「结果正确」的测试下永远绿灯。
2. **断言要判行为，不判字面**。用 `file_contains("ZeroDivisionError")` 判「处理了除零」，
   模型抛 `ValueError` 就被误判；用 `unittest discover` 判「测试跑通」，
   模型写 pytest 风格就被误判成 NO TESTS RAN（12 个模型里误杀 9 个）。
3. **生成失败要中止**。有一版脚本用 `sed` 从别的脚本里抽函数，抽到 Python heredoc 里
   顶格的 `}` 就截断了，仓库没生成，结果算出「成本 -100%」——看着像省了全部开销，
   实际压根没运行。假数据比报错危险。
