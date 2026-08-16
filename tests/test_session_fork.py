"""session fork（按轮截断分叉）的测试。不打网络。

turn_starts 是纯函数直接测；CLI 的 --turns / --fork 用真子进程 + 手造会话
文件 + scripted 桩黑盒验证（复用 test_e2e_scripted 的隔离环境）。
"""

from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path

from xiaoyu.agent import EMPTY_REPLY_NUDGE, SYNTHETIC_USER_TEXTS
from xiaoyu.session_log import _workspace_slug, turn_starts

from .test_e2e_scripted import E2ECase


class TurnStartsTest(unittest.TestCase):
    def test_user_messages_are_turn_starts(self):
        messages = [
            {"role": "user", "content": "轮一"},
            {"role": "assistant", "content": "答一"},
            {"role": "user", "content": "轮二"},
            {"role": "assistant", "content": None, "tool_calls": [{}]},
            {"role": "tool", "content": "结果"},
            {"role": "assistant", "content": "答二"},
        ]
        self.assertEqual(turn_starts(messages), [0, 2])

    def test_synthetic_and_blank_user_messages_excluded(self):
        messages = [
            {"role": "user", "content": "真轮次"},
            {"role": "user", "content": EMPTY_REPLY_NUDGE},
            {"role": "user", "content": "   "},
            {"role": "user", "content": "又一轮"},
        ]
        self.assertEqual(turn_starts(messages, SYNTHETIC_USER_TEXTS), [0, 3])


class ForkE2ETest(E2ECase):
    """手造两轮会话文件 → resume --turns 列轮次 → --fork 1 只带前一轮分叉。"""

    def _plant_session(self) -> Path:
        sessions = (
            Path(self.tmp) / "config" / "xiaoyu" / "sessions"
            / _workspace_slug(str(self.workspace))
        )
        sessions.mkdir(parents=True)
        path = sessions / "20260809-000000-1.jsonl"
        records = [
            {"event": "meta", "format": 2, "model": "m", "workspace": str(self.workspace),
             "started_at": "2026-08-09T00:00:00"},
            {"role": "user", "content": "第一轮：查清结构"},
            {"role": "assistant", "content": "结构查清了"},
            {"role": "user", "content": "第二轮：动手改"},
            {"role": "assistant", "content": "改完了"},
        ]
        path.write_text(
            "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records),
            encoding="utf-8",
        )
        return path

    def _run_resume(self, script: str, argv: list[str]) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-m", "xiaoyu", "resume", *argv],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
            env=self.scripted_env(script),
            cwd=self.workspace,
        )

    def test_turns_lists_fork_points(self):
        self._plant_session()
        proc = self._run_resume("text: 不会用到\n", ["--last", "--turns"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("第一轮：查清结构", proc.stdout)
        self.assertIn("第二轮：动手改", proc.stdout)
        self.assertIn("--fork", proc.stdout)

    def test_fork_keeps_only_first_turn(self):
        self._plant_session()
        proc = self._run_resume(
            "text: 分叉后继续\n",
            ["--last", "--fork", "1", "--output-format", "stream-json", "接着第一轮做"],
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        result = json.loads(proc.stdout.splitlines()[-1])
        self.assertEqual(result["kind"], "result")
        self.assertEqual(result["result"], "分叉后继续")
        #  新会话文件只带第一轮 + 新指令；第二轮没有进来；原文件不动
        forked = Path(result["session_log"]).read_text(encoding="utf-8")
        self.assertIn("第一轮：查清结构", forked)
        self.assertNotIn("第二轮：动手改", forked)
        self.assertIn("接着第一轮做", forked)
        self.assertIn("resumed_from", forked)

    def test_fork_out_of_range_is_error(self):
        self._plant_session()
        proc = self._run_resume("text: x\n", ["--last", "--fork", "5", "指令", "--output-format", "text"])
        self.assertEqual(proc.returncode, 2, proc.stdout)
        self.assertIn("超出范围", proc.stderr)


if __name__ == "__main__":
    unittest.main()
