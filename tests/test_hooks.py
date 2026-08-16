"""hooks（用户级生命周期钩子）的测试。不打网络；hook 命令是真子进程。

命令一律写成脚本文件再 `"python" "script.py"`，避免 shell=True 在
cmd.exe / sh 下的引号差异（CI 有 Windows）。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from xiaoyu.hooks import Decision, Hook, HookEngine, load_hooks

from .test_agent_paths import AgentTestCase, call_fragment, chunk


def _script_cmd(directory: Path, name: str, body: str) -> str:
    path = directory / name
    path.write_text(body, encoding="utf-8")
    return f'"{sys.executable}" "{path}"'


BLOCK_BODY = "import sys\nsys.stderr.write('不许这么干')\nsys.exit(2)\n"
ALLOW_BODY = "import sys\nsys.exit(0)\n"


class LoadTest(unittest.TestCase):
    def test_parse_and_skip_bad_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "hooks.toml"
            path.write_text(
                """
[[hooks]]
event = "PreToolUse"
matcher = "bash"
command = "echo ok"
timeout = 5

[[hooks]]
event = "NotAnEvent"
command = "echo bad"

[[hooks]]
event = "Stop"
command = ""

[[hooks]]
event = "PostToolUse"
matcher = "("
command = "echo bad-regex"
""",
                encoding="utf-8",
            )
            hooks, problems = load_hooks(path)
        self.assertEqual(len(hooks), 1)
        self.assertEqual(hooks[0].event, "PreToolUse")
        self.assertEqual(hooks[0].timeout, 5.0)
        self.assertEqual(len(problems), 3)

    def test_missing_file_is_empty(self):
        hooks, problems = load_hooks(Path("/nonexistent/hooks.toml"))
        self.assertEqual((hooks, problems), ([], []))


class EngineTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.notices: list[str] = []

    def tearDown(self):
        self._tmp.cleanup()

    def engine(self, hooks: list[Hook]) -> HookEngine:
        return HookEngine(hooks, self.tmp, notify=self.notices.append)

    def test_exit2_blocks_with_stderr_reason(self):
        cmd = _script_cmd(self.tmp, "block.py", BLOCK_BODY)
        engine = self.engine([Hook("PreToolUse", cmd)])
        decision = engine.fire("PreToolUse", {"tool": "bash"}, tool_name="bash")
        self.assertEqual(decision, Decision(blocked=True, reason="不许这么干"))

    def test_exit0_allows_and_payload_reaches_stdin(self):
        out = self.tmp / "payload.json"
        cmd = _script_cmd(
            self.tmp,
            "dump.py",
            f"import sys, pathlib\npathlib.Path({str(out)!r}).write_text(sys.stdin.read())\n",
        )
        engine = self.engine([Hook("PreToolUse", cmd, matcher="bash")])
        decision = engine.fire(
            "PreToolUse", {"tool": "bash", "args": {"command": "ls"}}, tool_name="bash"
        )
        self.assertFalse(decision.blocked)
        payload = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(payload["event"], "PreToolUse")
        self.assertEqual(payload["args"]["command"], "ls")
        self.assertEqual(payload["workspace"], str(self.tmp))

    def test_matcher_filters_by_tool_name(self):
        cmd = _script_cmd(self.tmp, "block.py", BLOCK_BODY)
        engine = self.engine([Hook("PreToolUse", cmd, matcher="^bash$")])
        self.assertFalse(engine.fire("PreToolUse", {}, tool_name="read_file").blocked)
        self.assertTrue(engine.fire("PreToolUse", {}, tool_name="bash").blocked)

    def test_other_exit_codes_fail_open_with_notice(self):
        cmd = _script_cmd(self.tmp, "crash.py", "import sys\nsys.exit(1)\n")
        engine = self.engine([Hook("Stop", cmd)])
        self.assertFalse(engine.fire("Stop", {}).blocked)
        self.assertTrue(any("退出码 1" in note for note in self.notices))

    def test_non_ascii_survives_both_directions_under_legacy_locale(self):
        """payload 进 hook、理由出 hook，两个方向的中文都得原样过。

        `PYTHONIOENCODING=iso8859-1` 把子进程的三个流按 Windows 默认（cp1252/
        GBK）那样降级——engine 不显式声明 UTF-8 的话：写 stdin 直接
        UnicodeEncodeError 把 fire 炸掉，stderr 的中文理由则被 backslashreplace
        成 `\\uXXXX` 字面量（不报错、值悄悄错，最阴的一种）。
        """
        out = self.tmp / "seen.json"
        cmd = _script_cmd(
            self.tmp,
            "echo_back.py",
            "import sys, pathlib\n"
            f"pathlib.Path({str(out)!r}).write_text(sys.stdin.read(), encoding='utf-8')\n"
            "sys.stderr.write('不许这么干')\nsys.exit(2)\n",
        )
        engine = self.engine([Hook("UserPromptSubmit", cmd)])
        with mock.patch.dict(os.environ, {"PYTHONIOENCODING": "iso8859-1"}):
            decision = engine.fire("UserPromptSubmit", {"prompt": "中文提示词"})
        self.assertEqual(decision, Decision(blocked=True, reason="不许这么干"))
        self.assertEqual(json.loads(out.read_text(encoding="utf-8"))["prompt"], "中文提示词")

    def test_timeout_fails_open_with_notice(self):
        cmd = _script_cmd(self.tmp, "slow.py", "import time\ntime.sleep(10)\n")
        engine = self.engine([Hook("Stop", cmd, timeout=1.0)])
        self.assertFalse(engine.fire("Stop", {}).blocked)
        self.assertTrue(any("超时" in note for note in self.notices))


def tool_turn(name: str, args: dict) -> list:
    return [chunk(tool_calls=[call_fragment(0, f"call_{name}", name, json.dumps(args))])]


def text_turn(text: str) -> list:
    return [chunk(content=text)]


class AgentIntegrationTest(AgentTestCase):
    def _engine(self, hooks: list[Hook]) -> HookEngine:
        return HookEngine(hooks, self.root, notify=lambda text: None)

    def _cmd(self, name: str, body: str) -> str:
        return _script_cmd(self.root, name, body)

    def test_pretooluse_block_stops_tool_and_feeds_reason(self):
        engine = self._engine([Hook("PreToolUse", self._cmd("b.py", BLOCK_BODY), matcher="bash")])
        agent = self.build(
            [tool_turn("bash", {"command": "echo x"}), text_turn("好")], hook_engine=engine
        )
        agent.send("干活")
        self.assertIn(
            "DENIED_BY_HOOK", [t["output"] for t in agent.trace if t["tool"] == "bash"]
        )
        tool_msgs = [m for m in agent.messages if m.get("role") == "tool"]
        self.assertIn("不许这么干", tool_msgs[-1]["content"])

    def test_posttooluse_feedback_appended_to_output(self):
        engine = self._engine([Hook("PostToolUse", self._cmd("b.py", BLOCK_BODY))])
        agent = self.build(
            [tool_turn("read_file", {"path": "calc.py"}), text_turn("好")],
            hook_engine=engine,
        )
        agent.send("看代码")
        tool_msgs = [m for m in agent.messages if m.get("role") == "tool"]
        self.assertIn("PostToolUse hook 反馈", tool_msgs[-1]["content"])
        self.assertIn("不许这么干", tool_msgs[-1]["content"])
        #  工具本身执行了（不是被拦）
        self.assertTrue(any(t["tool"] == "read_file" and t["ok"] for t in agent.trace))

    def test_userpromptsubmit_block_prevents_turn(self):
        engine = self._engine([Hook("UserPromptSubmit", self._cmd("b.py", BLOCK_BODY))])
        agent = self.build([], hook_engine=engine)
        before = len(agent.messages)
        agent.send("这句会被拦")
        self.assertEqual(len(agent.messages), before)  # 未入历史
        self.assertEqual(self.client.completions.calls, [])  # 未调模型

    def test_stop_block_forces_one_more_round_only(self):
        engine = self._engine([Hook("Stop", self._cmd("b.py", BLOCK_BODY))])
        agent = self.build(
            [text_turn("我做完了"), text_turn("补充完毕")], hook_engine=engine
        )
        agent.send("干活")
        #  第一次收尾被顶回（hook 反馈成 user 消息），第二次不再问 → 恰好 2 次调用
        self.assertEqual(len(self.client.completions.calls), 2)
        user_texts = [str(m.get("content")) for m in agent.messages if m.get("role") == "user"]
        self.assertTrue(any("hook 反馈" in text for text in user_texts))
        self.assertEqual(agent.last_assistant_text(), "补充完毕")


if __name__ == "__main__":
    unittest.main()
