"""验收用例（题面里不出现）：request 行摘要带 token 数；没有这两个字段的旧记录不受影响。"""
import json
import tempfile
from pathlib import Path
from unittest import TestCase

from xiaoyu.session_inspect import inspect_session


class RequestTokensTest(TestCase):
    def rows(self, *records: dict) -> list[dict]:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        path = Path(temp.name) / "session.jsonl"
        path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
        return inspect_session(path, kinds=("request",))["rows"]

    def test_tokens_shown_after_existing_fields(self) -> None:
        summary = self.rows({"event": "request", "provider": "p", "model": "m", "outcome": "ok",
                             "attempt": 1, "total_ms": 20, "prompt_tokens": 21137,
                             "completion_tokens": 89})[0]["summary"]
        self.assertIn("prompt_tokens=21137", summary)
        self.assertIn("completion_tokens=89", summary)
        self.assertLess(summary.index("total_ms=20"), summary.index("prompt_tokens=21137"))

    def test_old_records_without_tokens_are_unchanged(self) -> None:
        summary = self.rows({"event": "request", "provider": "p", "model": "m", "outcome": "ok",
                             "attempt": 1, "total_ms": 20})[0]["summary"]
        self.assertEqual(summary, "p/m ok attempt=1 total_ms=20")
