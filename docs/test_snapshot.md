# snapshot 套件：录制回放 + golden 对比

录制回放 + golden 对比的 test-support 范式，按小羽体量裁剪。
与 `tests/test_e2e_scripted.py` 的分工：
那边手写脚本 + 手写期望，锁**单点行为**、期望短到能表达"为什么该是这个序列"；
这边 fixture + golden，锁**完整可见表面**——归一化后的整条 stream-json 事件流、
整份会话 JSONL、pinned header。任何可见面的改动都会以 diff 的形式被摆到眼前。

## 核心设计决策（为什么）

**fixture 格式 = scripted DSL 本身，而不是扩 session_log。**
另一条常见路线是拿会话日志当 fixture（chunk 级事件随日志天然留存），但
小羽的会话文件是消息级、供 resume 用，为测试塞 chunk 级高频事件是污染生产格式。
录制器（`xiaoyu/snapshot.py`）把真实流量直接序列化成 `scripted.py` 的 DSL 脚本，
于是：

- **回放侧零新代码**——`XIAOYU_SCRIPTED_SCRIPTS` 走的就是久经考验的 scripted 桩；
- **fixture 人可读、可手改**——"chunk 前就 throw 的 401"和错误时序，
  在脚本里写一行 `error:` 即可，无需旁车 override 文件那类机制；
- "模型须回显请求里随机 id"的占位符场景暂无对应需求，不做；
  哪天需要了在 scripted DSL 上加即可。

**不做 per-session 绑定。** 小羽的 scripted 队列本来就是进程级 FIFO
（主循环/摘要/子 agent 共用），录制器也按同一全局调用顺序追加——两边天然
对齐。并发子 agent 出现之前不需要按 sessionId 绑定多份 fixture 这类更强的键。

**录的是"内核读的面"，不是原始 SSE。** 录制点在 `wrap_transport` 外侧，
Responses / Anthropic 协议差异已被传输层抹平，fixture 天然协议无关；
序列化只覆盖 `_consume_stream` 实际消费的属性（`delta.content`、tool_call
分片、usage、reasoning）。round-trip 性质有单测锁着
（`tests/test_snapshot.py`）："录出来的一定能回放"不是口头承诺。

## 三模式（`XIAOYU_SNAPSHOT` 环境变量）

```sh
# replay（缺省）：keyless，随全量套件并跑
.venv/bin/python -m unittest tests.test_e2e_snapshot

# refresh：keyless 重写 golden 与 pin sidecar；跑完再跑一遍缺省模式验证，
# diff 审阅后提交
XIAOYU_SNAPSHOT=refresh .venv/bin/python -m unittest tests.test_e2e_snapshot

# record：真 API 重录 model.txt（花真钱；只覆盖 recorded=True 的场景；
# 用真机配置与密钥，XIAOYU_SNAPSHOT_MODEL 可指定模型），录完自动 refresh——
# 录出的 fixture 当场证明能回放
XIAOYU_SNAPSHOT=record .venv/bin/python -m unittest tests.test_e2e_snapshot
```

错误时序场景（`recorded=False`）永远手写——真 API 录不出确定的 429/致命错。

## 场景目录（`tests/snapshots/<scenario>/`）

| 文件 | 内容 |
|---|---|
| `model.txt` | scripted DSL fixture（一轮 = 一次 LLM 调用） |
| `stdout.expected.jsonl` | 归一化后的完整 stream-json 事件流 golden |
| `session.expected.jsonl` | 归一化后的完整会话 JSONL golden |
| `system-prompt.expected.md` | 仅 pin 场景：钉住的 system prompt |
| `tool-schemas.expected.json` | 仅 pin 场景：钉住的 tool schemas |

**pin 纪律**：恰好一个场景（text-turn）钉完整请求头，
其余场景只核对与 pin 一致——改一次 prompt 只 churn 一处。机器相关段
（工作区路径、平台串、shell 注记、环境画像）由归一化器换成 token
（`tests/snapshot_support.py`，token 值在测试进程内等值重算），sidecar 锁的
是小羽自己控制的模板内容。Windows 上跳过 pin 对比（shell 注记/工具面随平台
不同），stdout 与会话 golden 照常全平台比。

**归一化器**是纯函数：耗时→0、时间戳/版本→token、临时目录→token、
tool_call id→首见序号（重录制不 churn golden）、重试退避秒数（带 jitter）→
token、CRLF→LF。`format` 字段刻意保留字面值——SESSION_FORMAT 升版**应当**
churn golden，格式变更就该在 diff 里被看见。

**assertConsumed 双向响亮**：CLI 比 fixture 多调一次模型 →
`ScriptedExhausted` 浮出为结构化错误；少调 → `XIAOYU_SCRIPTED_STRICT=1` 让
子进程退出时向 stderr 打 `ScriptedUnconsumed`，套件断言 stderr 干净。
两个方向都有"证明会红"的用例。

**fixture 守卫**：孤儿场景目录、缺 golden、多 pin、录制残留
（`request-*.json`）入库、DSL 解析不过，全部拒绝。

## 纪律

**每个非平凡的"模型可见 / 协议可见 / 人机可见"变更，同一批改动内要加或
refresh 一个 keyless 场景；套件不够用就扩套件。** 配套哲学：
verify the world, not the self-report——断言落在可见表面（事件流、落盘文件、
请求头），不落在内部状态；guard 要证明会红（引回归 → 见红 → revert），
本套件落地时两条 guard 都验过红。
