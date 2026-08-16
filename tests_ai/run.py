#!/usr/bin/env python3
"""markdown 不变量测试 runner（按小羽体量收敛的极简版）。

规约是自然语言 markdown（`#` 测试名、`##` case、**Scope**/**Requirements**），
审计者是 xiaoyu 自己：每个 md 起一次一次性执行，让它读 Scope 指定的文件、
对照 Requirements 逐 case 裁决，最后输出 JSON 裁决数组。本脚本解析裁决，
打成 pytest 风格报告。

定位与 experiments/ 相同：**真调模型、不进 CI**，守护的是静态工具管不了的
架构约束（import 层级、编码纪律这类"读得懂代码才判得了"的不变量）。

用法（在仓库根目录）：
    .venv/bin/python tests_ai/run.py              # 全部 test_*.md
    .venv/bin/python tests_ai/run.py layering     # 名字含 layering 的
    XIAOYU_MODEL=deepseek-v4-pro .venv/bin/python tests_ai/run.py

要求环境已配好模型 key（吃仓库 .env，与日常跑 xiaoyu 相同）。
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TESTS_DIR = Path(__file__).resolve().parent

AUDIT_PROMPT = """你是代码审计员。下面是一份"架构不变量"测试规约（markdown）：
`##` 开头的每一节是一个 case，含 **Scope**（要检查的文件）与 **Requirements**（必须成立的约束）。

任务：逐 case 只读审计。用 read_file / grep 检查 Scope 指定的文件（不要发散到
Scope 之外），判断 Requirements 是否全部成立。你只审计，绝不修改任何文件。

裁决标准：
- 所有 Requirements 都成立 → passed=true；
- 任何一条被违反 → passed=false，evidence 里给出违例的 路径:行号 + 原文行；
- 拿不准按 false 处理并说明疑点（漏报比误报便宜：误判通过会掩盖真实违例）。

输出格式（严格遵守）：最后一条回复以 ```json 围栏收尾，内容是数组，每个 case 一项：
[{"case": "case 标题原文", "passed": true, "evidence": "一句话依据（含关键 路径:行号）"}]
围栏外不要再解释。

=== 规约开始 ===
{spec}
"""


def discover(patterns: list[str]) -> list[Path]:
    files = sorted(TESTS_DIR.glob("test_*.md"))
    if patterns:
        files = [f for f in files if any(p in f.name for p in patterns)]
    return files


def case_titles(spec: str) -> list[str]:
    return [line.lstrip("# ").strip() for line in spec.splitlines() if line.startswith("## ")]


def audit(md: Path) -> tuple[list[dict], str]:
    """跑一份规约，返回 (裁决列表, 错误信息)。"""
    spec = md.read_text(encoding="utf-8", errors="replace")
    prompt = AUDIT_PROMPT.replace("{spec}", spec)
    proc = subprocess.run(
        [
            sys.executable, "-m", "xiaoyu", prompt,
            "--output-format", "json",
            "--workspace", str(REPO),
            "--yolo",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=900,
        cwd=REPO,
    )
    if proc.returncode != 0:
        return [], f"xiaoyu 退出码 {proc.returncode}：{proc.stderr.strip()[:300]}"
    try:
        result = json.loads(proc.stdout.strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError) as exc:
        return [], f"输出不是 JSON：{exc}"
    answer = str(result.get("result", ""))
    fence = re.findall(r"```json\s*(.*?)```", answer, re.DOTALL)
    blob = fence[-1] if fence else answer
    try:
        verdicts = json.loads(blob)
    except json.JSONDecodeError:
        return [], f"裁决不是合法 JSON（回答开头：{answer[:200]!r}）"
    if not isinstance(verdicts, list):
        return [], "裁决不是数组"
    return [v for v in verdicts if isinstance(v, dict)], ""


def main(argv: list[str]) -> int:
    files = discover(argv)
    if not files:
        print("没有匹配的 test_*.md")
        return 2
    started = time.monotonic()
    passed = failed = errored = 0
    for md in files:
        expected = case_titles(md.read_text(encoding="utf-8", errors="replace"))
        verdicts, error = audit(md)
        if error:
            errored += len(expected) or 1
            print(f"\x1b[31mERROR\x1b[0m  {md.name} — {error}")
            continue
        judged = {str(v.get("case", "")): v for v in verdicts}
        for title in expected:
            verdict = judged.get(title)
            if verdict is None:
                #  容错：模型可能改写了标题，按顺序兜底配对
                verdict = verdicts[expected.index(title)] if expected.index(title) < len(verdicts) else None
            if verdict is None:
                errored += 1
                print(f"\x1b[31mERROR\x1b[0m  {md.name}::{title} — 裁决缺失")
                continue
            evidence = str(verdict.get("evidence", ""))[:200]
            if verdict.get("passed") is True:
                passed += 1
                print(f"\x1b[32mPASSED\x1b[0m {md.name}::{title}")
            else:
                failed += 1
                print(f"\x1b[31mFAILED\x1b[0m {md.name}::{title} — {evidence}")
    seconds = time.monotonic() - started
    total = passed + failed + errored
    color = "\x1b[32m" if failed == errored == 0 else "\x1b[31m"
    summary = f" {passed} passed"
    if failed:
        summary += f", {failed} failed"
    if errored:
        summary += f", {errored} errored"
    print(f"{color}{'=' * 20}{summary} in {seconds:.1f}s {'=' * 20}\x1b[0m")
    return 0 if failed == errored == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
