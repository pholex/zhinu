"""命令逃逸语料库的参数化测试。

对 tests/fixtures/command_corpus/*.jsonl 里的每一条样本，断言它被声明的护栏
抓住（expect=block → 返回非空原因）或放行（expect=allow → 返回 None）。

设计意图：**加一个逃逸点子 = 往某个 .jsonl 加一行，零测试代码改动**。
loader glob 整个目录，所以新逃逸家族可以单开文件。一条 block 样本被"放松
检查让红测变绿"的改动放行时这里立刻变红——这正是它存在的意义：绝不为让
红测变绿而放松护栏。

与各护栏模块自己的单测（test_command_check / test_mcp_guard）是互补关系：
那些测边界与实现细节，这里测"逃逸覆盖面"这一个横切不变量，且可被非改码
的人扩充。
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from xiaoyu import command_check, mcp_guard

_CORPUS_DIR = Path(__file__).resolve().parent / "fixtures" / "command_corpus"

#  guard 名 → 判定函数（命中返回非空原因，放行返回 None）。
#  admission / endpoint 的调用签名不同，各自适配。
_GUARDS = {
    "injection": lambda case: command_check.injection_risk(case["cmd"]),
    "dangerous": lambda case: command_check.dangerous_command(case["cmd"]),
    "privileged": lambda case: command_check.privileged_command(case["cmd"]),
    "admission": lambda case: mcp_guard.admission_violation(
        case["cmd"], case.get("args", []), case.get("env", {})
    ),
    "endpoint": lambda case: mcp_guard.endpoint_violation(case["url"]),
}


def _load_corpus() -> list[tuple[str, int, dict]]:
    cases: list[tuple[str, int, dict]] = []
    for path in sorted(_CORPUS_DIR.glob("*.jsonl")):
        for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            raw = raw.strip()
            if not raw:
                continue
            cases.append((path.name, lineno, json.loads(raw)))
    return cases


class TestCommandCorpus(unittest.TestCase):
    def test_corpus_is_not_empty(self) -> None:
        #  glob 打空（目录挪了、扩展名写错）不能表现为"全部通过"
        self.assertGreater(len(_load_corpus()), 0, "语料库为空——检查目录/文件名")

    def test_every_sample_matches_its_verdict(self) -> None:
        for filename, lineno, case in _load_corpus():
            guard_name = case["guard"]
            with self.subTest(file=filename, line=lineno, note=case.get("note", "")):
                self.assertIn(guard_name, _GUARDS, f"未知 guard：{guard_name}")
                verdict = _GUARDS[guard_name](case)
                target = case.get("cmd") or case.get("url")
                if case["expect"] == "block":
                    self.assertIsNotNone(
                        verdict, f"[{guard_name}] 该拦未拦：{target}（{case.get('note', '')}）"
                    )
                elif case["expect"] == "allow":
                    self.assertIsNone(
                        verdict, f"[{guard_name}] 该放误拦：{target}（{case.get('note', '')}）"
                    )
                else:
                    self.fail(f"expect 只能是 block/allow，得到 {case['expect']!r}")


if __name__ == "__main__":
    unittest.main()
