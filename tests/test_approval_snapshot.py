"""审批文本的 keyless 可见面快照：普通会话授权与单次升权不能混淆。"""

from __future__ import annotations

import contextlib
import io
import os
import unittest
from pathlib import Path
from unittest import mock

from xiaoyu import ui
from xiaoyu.cli import make_confirm
from xiaoyu.permissions import Permissions


class ApprovalSnapshotTest(unittest.TestCase):
    def test_confirmation_scope_snapshot(self):
        output = io.StringIO()
        for tier, answer in (("", "a"), ("danger-full-access", "y"), ("danger-full-access", "a")):
            permissions = Permissions(Path.cwd())
            args = {"command": "ps -ax"}
            if tier:
                args.update(sandbox_permissions=tier, justification="查询进程")

            def respond(prompt):
                print(prompt + answer)
                return answer

            with (
                contextlib.redirect_stdout(output),
                mock.patch.object(ui, "_ENABLED", False),
                mock.patch("builtins.input", side_effect=respond),
            ):
                print(f"--- {tier or 'ordinary'} / {answer} ---")
                self.assertTrue(make_confirm(permissions)("bash", args))
            self.assertEqual(permissions.session_commands, set() if tier else {"ps"})
        actual = output.getvalue()
        golden = Path(__file__).parent / "fixtures" / "approval_scope.expected.txt"
        if os.environ.get("XIAOYU_SNAPSHOT") == "refresh":
            golden.write_text(actual, encoding="utf-8")
        self.assertEqual(actual, golden.read_text(encoding="utf-8", errors="replace"))
