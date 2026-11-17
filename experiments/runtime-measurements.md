# 提示词成本与推理回放测量

状态（2026-10-14）：两项测量入口保留备用，已移出当前待办，不安排真实模型实验。
目前没有证据表明提示词成本是主要瓶颈；推理回放的收益依赖具体模型和端点，
也没有已确认的问题需要调整生产策略。今后出现明确的成本瓶颈或回放故障时再评估。
已完成离线验证，尚无真实模型收益结论；下文是按需使用说明，不是待执行计划。

这两份脚本不属于普通单测。提示词捕获和推理探测的默认计划模式不调用模型；
`benchmark` 与 `--live` 会调用真实端点。产物放在仓库之外，不提交原始响应或凭据。

## 提示词：先比较实际请求，再比较任务结果

用同一个虚拟环境解释器，捕获两个 checkout 的生产 CLI 请求。
捕获复用现有 keyless 回放及环境隔离：固定模型桩、临时工作区、禁用本机技能和插件。
分别覆盖 default、auto、plan，归一化机器路径；产物记录 HEAD 与是否有未提交改动。

```bash
python experiments/prompt_surface.py capture --repo /path/to/baseline --output /tmp/prompt-before.json
python experiments/prompt_surface.py capture --repo /path/to/candidate --output /tmp/prompt-after.json
python experiments/prompt_surface.py diff /tmp/prompt-before.json /tmp/prompt-after.json
```

报告列出 system 是否变化、增删和变化的工具、估算 token 差值。估算不等于厂商账单，
当前矩阵也不覆盖真实 provider 的出网翻译、Windows 或用户安装的工具。

缩短提示词之后，再用相同型号、端点和环境对两边执行固定行为集：修复并测试、
定点编辑、只读问答、多文件重命名。底层复用既有 eval 判据和 token 记账，避免复制一套评分。
先通过环境变量配置模型 key/端点；运行目录独立，脚本不会自动加载 checkout 的 `.env`。
每个输出目录必须尚不存在，防止两组结果混写。

```bash
python experiments/prompt_surface.py benchmark --repo /path/to/baseline --output-dir /tmp/eval-before --model provider/model --repeat 3
python experiments/prompt_surface.py benchmark --repo /path/to/candidate --output-dir /tmp/eval-after --model provider/model --repeat 3
```

对比输出目录内的 eval 报告，先看成功率和失败任务，再看完整同组任务的 token 和耗时。
调用错误、工具未发现、提前失败都留在失败数中，不能靠剔除失败任务得到“省 token”的结论。
记录了未提交改动的运行要额外保存对应 diff，HEAD 本身不能完整复现实验。

## 推理回放：接受、计入输入与任务收益分开记录

脚本支持两种协议，各测同一工具循环内和跨用户轮两种位置：

- chat：只在 assistant 的 `reasoning_content` 放入随机校验码，基线去掉该字段。
  校验码仅放在推理字段，普通对话里不包含它；判据是模型能否返回该码。
- responses：先用真实端点生成带加密推理的工具调用，再成对提交“去掉推理块”和
  “保留原始推理块”的历史。工具结果由固定夹具给出，不执行模型提出的任何工具。
  种子未产生加密块或工具调用时记为不可测，不伪造载荷。

```bash
python experiments/reasoning_replay_probe.py --protocol responses --model your-model
python experiments/reasoning_replay_probe.py --protocol chat --model your-model --base-url https://your-endpoint/v1 --key-env MODEL_API_KEY --live --output /tmp/replay-chat.json
python experiments/reasoning_replay_probe.py --protocol responses --model your-model --base-url https://your-endpoint/v1 --key-env MODEL_API_KEY --live --output /tmp/replay-responses.json
```

默认每个位置一对请求；responses 每对多一次种子请求，总上限 6 次；chat 总上限 4 次。
`--repeat` 最多 10，计划超过 `--max-requests` 会在联网前拒绝。
`--max-output-tokens` 限制每次输出；这不是金额预算。每次请求超时 60 秒，无自动重试。
多次重复会交替先发基线/回放；仍不能排除端点后端路由变化，正式测量应固定路由。

输出只存接受/错误/截断状态、输入输出 token、预期答案是否出现、耗时和成对差值；
不写 key、原始思考、加密块或错误响应正文。输出文件必须不存在，逐对刷新以便中断后查账。
失败或截断的一臂使该对不可比较，缺 usage 不推算成零；退出码 1 表示有不可比较的对。

HTTP 成功只说明接受；输入 token 增长只说明计数发生变化；chat 的校验码命中提供
内容被使用的证据，但不表示真实任务能力提升。responses 的算术答案本来就在工具调用里，
正确率只检查载荷对账，不证明加密推理改善推理质量。
这份脚本直接测端点，不经过小羽的 resume、压缩与跨 provider 路由；这些生产路径仍由
`tests.test_responses`、`tests.test_messages` 和会话测试守住。不能用这份结果自动改生产回放策略。
