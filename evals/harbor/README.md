# 小羽 × harbor：terminal-bench 横评

把小羽放到 [harbor](https://github.com/laude-institute/harbor) 的任务容器里跑
terminal-bench 系数据集（默认 `terminal-bench/terminal-bench-2`，89 题），得到与其它
agent **同数据集、同模型**下可直接对表的通过率 / 耗时 / token / 费用。
这是「自研 eval 无区分度」的解法：拿外部基准，不是再造 case。

目录里的东西：

| 文件 | 作用 |
|---|---|
| `cmd.py` | 入口（PEP 723 脚本，`uv` 自动装依赖）：`run` / `list` / `show` / `task` / `compare` / `rm` |
| `agent.py` | harbor 适配器 `XiaoyuWheelAgent`：装 wheel、跑 `xiaoyu -p`、解析用量与轨迹 |
| `install.sh` | 容器内安装脚本：uv → 系统 python3 两条退路 |
| `runner.py` / `reporter.py` | `run` 的配置生成与拉起；结果表 |
| `config_template.yaml` | 注入容器的小羽环境变量、无人值守提示语、解释器版本约束 |
| `prices.json` | 单价表（估费用用；本机 `xiaoyu/evals/models.local.json` 会覆盖它） |
| `runs/` | 跑分产物（`.gitignore`，不入库） |

## 准备

宿主要有：`uv`、Docker（daemon 在跑）；`rsync` 只有在远端跑完往本机同步结果时才用。

```bash
uv --version && docker info >/dev/null && echo ok
```

密钥与模型放 `.env`（`run` 依次找当前目录、本目录、仓库根，或 `--env-file` 指定），
只往进程环境里补缺、不覆盖已有的真实环境变量，**值不会写进任何生成的配置文件**：

```ini
# 走 OpenAI 兼容网关（默认路线）
XIAOYU_BASE_URL=https://<网关>/v1
XIAOYU_API_KEY=<key>
XIAOYU_MODEL=deepseek-flash       # 没给 --model 时的默认模型

# 直连厂商时按小羽认的原生键名给（任选其一即可）
DEEPSEEK_API_KEY=... / ANTHROPIC_API_KEY=... / OPENAI_API_KEY=... / GEMINI_API_KEY=... / XAI_API_KEY=...
# Bedrock：XIAOYU_BEDROCK_REGION + AWS_BEARER_TOKEN_BEDROCK 或 AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY，并加 --extras bedrock
```

模型写成 `provider/model`：`gateway/deepseek-flash`、`anthropic/claude-sonnet-5-5`、
`deepseek/deepseek-flash`、`bedrock/global.anthropic.claude-fable-5-1`……
容器里只注册这一家 provider（`XIAOYU_PROVIDERS=<provider>`），摘要与 explore 子 agent
也用同一个模型，不降级——横评要的是单一模型的成绩。

## 跑

```bash
# 冒烟：3 题，默认模型（gateway/<XIAOYU_MODEL>），wheel 现场从仓库根 uv build
./evals/harbor/cmd.py run --n-tasks 3 --job-name smoke

# 指定 wheel（比如要测某个历史版本）、换模型
./evals/harbor/cmd.py run --wheel dist/xiaoyu_agent-0.60.0-py3-none-any.whl \
  --model anthropic/claude-sonnet-5-5 --job-name sonnet55-full

# 点名任务（任务名见 harbor 注册表 / show 的 task 列）
./evals/harbor/cmd.py run --tasks build-pov-ray,circuit-fibsqrt --job-name two

# 全量 89 题
./evals/harbor/cmd.py run --job-name xiaoyu-0.60.0-deepseek-flash

# 给超时的任务加倍重跑；对 API 抖动重试
./evals/harbor/cmd.py run --tasks oom,compile-vim --timeout-multiplier 2 --max-retries 2 --job-name retry

# 改"参赛的小羽"：--env 覆盖 config_template.yaml 里的开关，跑成两个 job 再 compare
./evals/harbor/cmd.py run --env XIAOYU_ENABLE_EXPLORE=0 --job-name no-explore
```

其它旗标：`--trials N`（每题跑几次）、`--concurrency`（默认 4）、`--budget-tokens` /
`--effort`（原样传给小羽）、`--allow-host`（任务用 allowlist 网络策略时额外放行；
模型端点已按 provider 自动加）、`--prices 文件`（额外单价表）、`--dry-run`（只打印配置）。

`run` 做的事：读 `.env` → 没给 `--wheel` 就在仓库根 `uv build --wheel` →
生成 `runs/<job>/_generated_config.json`（不含密钥）→ `harbor run`。每个任务容器里：
上传 wheel 与 `install.sh`，以 root 装进 `/installed-agent/venv`（有 uv 用 uv，没有就
curl 装一个，再不行退回系统 python3 ≥ 3.11 + venv），然后以任务用户执行

```
xiaoyu -p --yolo --unattended --no-sandbox --output-format stream-json \
  --append-system-prompt "<无人值守说明>" < /installed-agent/instruction.txt \
  2> /logs/agent/xiaoyu.stderr.txt | tee /logs/agent/xiaoyu.jsonl
```

`XDG_CONFIG_HOME` 指到 `/logs/agent/config`，会话日志跟着 harbor 的日志一起下载——
任务超时被杀时收尾对象没了，用量还能从会话日志的 `request` 事件累加出来。

## 看结果

```bash
./evals/harbor/cmd.py list                      # 每个 job 一行
./evals/harbor/cmd.py show <job>                # 逐任务
./evals/harbor/cmd.py show <job> --status error # 只看某种结局
./evals/harbor/cmd.py task <job> <task> --tail 30   # 判分输出、stderr、事件流尾部
./evals/harbor/cmd.py compare <job_a> <job_b> -v    # 逐任务对比：都过 / 只 A 过 / 只 B 过
./evals/harbor/cmd.py rm <job> [-y]
```

`list` 的列：

| 列 | 含义 |
|---|---|
| `rate` | 通过率 = reward ≥ 1 的 trial / 全部 trial |
| `agent` | 各 trial **agent 执行**时长之和（不含建容器、装 agent、判分） |
| `compute` | 各 trial 全程时长之和（含建容器 + 装 + 跑 + 判分） |
| `in` / `out` | prompt / completion token 总量（含 explore、七襄等子 agent） |
| `turns` | 模型调用次数 |
| `cost` | 按单价表估算；没单价的模型记 `-`，`task` 子命令的 `Meta` 里会点名 |
| `pass/fail/err/tout` | 通过 / 判分没过 / 异常 / 超时 |

两个时长都把并发展开（4 并发跑 1 小时记 ≈ 4 小时），所以与宿主并发数无关，可跨机器比。
**判分优先于异常**：agent 超时或退出码非零时 harbor 照样跑 verifier，分到手就算过。

每个 trial 目录（`runs/<job>/<task>__<id>/`）里：`agent/xiaoyu.jsonl`（事件流）、
`agent/xiaoyu.stderr.txt`、`agent/config/xiaoyu/sessions/**.jsonl`（会话日志，每次工具
调用与输出都在）、`agent/trajectory.json`（ATIF 轨迹，`harbor view` 可看）、
`verifier/`（判分输出）、`result.json`。

## 费用与耗时预估

以 `gateway/deepseek-flash`、小羽 0.60.0 的 3 题冒烟（见下：3.4 M prompt token / 题、
$0.52 / 题、agent 时长 13 分钟 / 题）线性外推，全量 89 题约
**200–300 M prompt token、5–6 M completion token**，deepseek-flash 档位约 **$30–50 / 全量**。
撞到 100 轮上限的题（下面的 make-mips-interpreter：102 轮、8.1 M token、$1.23）是花钱大户，
真实全量里这类题占比决定上下限。换 Claude Sonnet 档模型按 20 倍上下估，Opus 档再翻几倍
——先 `--n-tasks 3` 看一眼 `list` 的 token 列再决定。

耗时：单题上限由任务自带的超时决定（多数 15–60 分钟），全量 89 题 4 并发约 **5–8 小时墙钟**；
`agent` 列（并发展开）约 20 小时、`compute` 列再多 15% 左右（建容器与判分）。
镜像首次拉取另算，几十个镜像合计几 GB。

## 已知限制（解读表时要知道的）

- **缓存 token 不分列**：小羽的用量账本只记 prompt / completion，`cost` 把 prompt 全按
  输入价算，是费用**上界**；有缓存折扣的模型实际更便宜。
- **轮数上限不可从命令行改**：`max_iterations` 出厂 50，`XIAOYU_TURN_EXTENSION=1.0`
  允许模型再申请最多 50 轮，合计约 100 轮——与同类横评常用的 100 轮上限对齐。
- **沙箱关着**：容器里没有 bubblewrap，`--no-sandbox` + `--yolo --unattended`，与其它
  agent 在容器里的跑法一致；`XIAOYU_HARDLINE` 等四条底线仍在。
- **退出码**：小羽 `-p` 出错（模型 / 网络异常、服务端拒答）退出码 1，harbor 记成
  error 并按输出文本归类（限流 → `ApiRateLimitError` 等，可配 `--max-retries` 重试）；
  模型正常跑完但没做对是 fail，不是 error。
- 每个容器都要联网装依赖（PyPI、必要时 astral.sh 取 uv / 下载解释器）；离线镜像
  会卡在安装，`task` 子命令看 `Error` 一行即知。

## 冒烟结果（2026-10-02，xiaoyu 0.60.0，gateway/deepseek-flash，--n-tasks 3）

```
job_name   version  model           rate   agent  compute      in     out  turns     cost pass/fail/err/tout
smoke3     0.60.0   deepseek-flash 66.7%   38.6m    45.4m   10.2M    202k    158    $1.57            2/1/0/0

task                    status  reward   agent      in     out turns     cost
build-pov-ray           pass      1.00    6.3m    1.4M     21k    39    $0.22
circuit-fibsqrt         pass      1.00   10.3m    709k     47k    17    $0.12
make-mips-interpreter   fail      0.00   22.0m    8.1M    134k   102    $1.23
```

三题都在容器里装上、跑完并进了 verifier 判分；make-mips-interpreter 跑满 102 轮
（50 + 延期 50 + 收尾）没做完，是 fail 不是 error。这组数只说明链路通，样本太小不构成成绩。
