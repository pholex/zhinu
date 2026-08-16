# 编码纪律不变量

中文 Windows（GBK/OEM 代码页）与损坏文件是真实用户环境。
`UnicodeDecodeError` 一旦抛到模型面前，它只会盲猜和空转（真实会话里绕过十几轮）。

## Case 1: 外部字节解码永不裸抛 UnicodeDecodeError

**Scope**: `xiaoyu/tools.py`

**Requirements**:
- 所有把**外部来源字节**（读文件、子进程 stdout/stderr）解码成文本的路径，
  最终都必须有 `errors="replace"` 兜底——允许"先严格 UTF-8、再本地代码页、
  最后 replace"的阶梯（严格尝试被 try/except 包住即可），
  但不允许存在"解码失败会把 UnicodeDecodeError 抛出函数"的路径。
- 写文件（编码方向）不需要 replace，不在本审计范围内。

<examples>
正例：`data.decode("utf-8")` 在 try 里、except UnicodeDecodeError 后续走
兜底链、链尾是 `decode("utf-8", errors="replace")`。
反例：某个读取路径直接 `path.read_text(encoding="utf-8")` 且无 try 包裹、
无 errors 参数，异常一路上抛。
</examples>

## Case 2: 会话/配置文件读取容错

**Scope**: `xiaoyu/session_log.py`、`xiaoyu/config.py`、`xiaoyu/skills.py`

**Requirements**:
- 读用户机器上的文本文件（会话 JSONL、.env、SKILL.md）时，
  `read_text` / `open` 必须带 `errors="replace"`（或等价的解码容错），
  一个字节坏掉不该让整个功能（resume / 配置加载 / 技能扫描）失效。
