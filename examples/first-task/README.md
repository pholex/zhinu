# first-task：给小羽的第一个任务

一个两文件的小项目，带 3 个测试，其中 1 个是红的——`mathutil.clamp` 的上界写错了。
让小羽找到它、修实现、再跑一遍测试。五分钟看完它怎么干活：读文件、跑命令、改代码、
每一步要不要你点头。

| 文件 | 作用 |
|---|---|
| `setup.py` | 把 `project/` 拷到仓库外的目录（目标已存在则拒绝） |
| `project/mathutil.py` | 被测代码，`clamp` 有一处故意写错 |
| `project/tests/test_clamp.py` | 3 个 unittest 用例，当前 1 个失败 |
| `project/prompt.txt` | 第一句话：找失败的测试、修实现、跑测试 |

## 跑

```bash
python examples/first-task/setup.py ~/xiaoyu-first-task
cd ~/xiaoyu-first-task
python -m unittest -v                  # 先看：3 个用例，test_value_above_high_snaps_to_high 红
xiaoyu "$(cat prompt.txt)"             # 让小羽修（装法见仓库 README）
python -m unittest -v                  # 再看：应该全绿
```

## 预期会看到什么

1. 小羽先跑 `python -m unittest`（bash 工具）。出厂是 **auto** 档：沙箱内跑命令、工作区内
   改文件都不问你，每一步在屏幕上看得到；
2. 读 `mathutil.py` 与失败的那个用例，定位到 `clamp` 里 `value > high` 分支回了 `low`；
3. 用 `str_replace` 改成回 `high`，只动那一行——不会去改测试；
4. 再跑一次测试，3 个全过，收尾时说明改了什么。

改动落在 `~/xiaoyu-first-task/mathutil.py`，这不是 git 仓库，直接看文件。
想每一步都由你点头：`xiaoyu --mode default "$(cat prompt.txt)"`；想一步都不许它动、先看
计划：`--mode plan`。模式说明见仓库 README「模式：放手程度你定」。
