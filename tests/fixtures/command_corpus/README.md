# 命令逃逸语料库

这个目录是 xiaoyu 三个命令护栏（`command_check` 的 injection/dangerous/privileged、
`mcp_guard` 的 admission/endpoint）的**恶意样本库**。`tests/test_command_corpus.py`
对整个目录参数化：每一条样本都断言它被声明的护栏抓住（`expect: block`）或放行
（`expect: allow`）。

**加一个逃逸点子 = 往某个 `.jsonl` 里加一行**，零测试代码改动。一个新的逃逸
家族值得单开一个 `.jsonl`。loader glob 整个目录，文件名只是分类，不影响判定。

每行一个 JSON 对象：

```json
{"cmd": "sudo rm -rf /tmp/x", "guard": "dangerous", "expect": "block", "note": "sudo 包裹"}
```

字段：
- `cmd`：要判定的命令串（endpoint 例外，见下）。
- `guard`：`injection` | `dangerous` | `privileged` | `admission` | `endpoint`。
- `expect`：`block`（护栏必须返回非空原因）| `allow`（必须返回 None）。
- `args` / `env`：仅 `admission` 用——`cmd` 当 argv[0]，`args` 是其余参数，
  `env` 是环境变量字典（省略则为空）。
- `url`：仅 `endpoint` 用，替代 `cmd`。
- `note`：给人看的说明，不参与判定。

一条 `block` 样本一旦被某次"放松检查让红测变绿"改动放行，这里就会变红——
这正是它的用途：**绝不为了让红测变绿而放松护栏**。
