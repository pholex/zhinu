"""wire 协议的单元测试（进程内、不打网络）。子进程 e2e 在 test_e2e_wire.py。"""

from __future__ import annotations

import unittest

from xiaoyu.agent import Allow, Deny
from xiaoyu.wire import verdict_from


class VerdictFromTest(unittest.TestCase):
    def test_allow_plain(self):
        verdict = verdict_from({"verdict": "allow"})
        self.assertIsInstance(verdict, Allow)
        self.assertEqual(verdict.note, "")
        self.assertIsNone(verdict.updated_args)

    def test_allow_with_note_and_updated_args(self):
        verdict = verdict_from(
            {"verdict": "allow", "note": "小心点", "updated_args": {"command": "ls"}}
        )
        self.assertIsInstance(verdict, Allow)
        self.assertEqual(verdict.note, "小心点")
        self.assertEqual(verdict.updated_args, {"command": "ls"})

    def test_deny_with_reason(self):
        verdict = verdict_from({"verdict": "deny", "reason": "换只读命令"})
        self.assertIsInstance(verdict, Deny)
        self.assertEqual(verdict.reason, "换只读命令")

    def test_fail_closed_on_garbage(self):
        for payload in (None, "yes", 42, [], {}, {"verdict": "maybe"},
                        {"verdict": "allow", "updated_args": "not-a-dict"}):
            with self.subTest(payload=payload):
                self.assertIsInstance(verdict_from(payload), Deny)


if __name__ == "__main__":
    unittest.main()
