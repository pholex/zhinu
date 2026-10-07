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

    def test_misspelled_table_name_is_reported(self):
        """[[hooks]] 写成 [[hook]]：一条都不加载，但不能连个声都没有。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "hooks.toml"
            path.write_text('[[hook]]\nevent = "PreToolUse"\ncommand = "exit 2"\n', encoding="utf-8")
            hooks, problems = load_hooks(path)
        self.assertEqual(hooks, [])
        self.assertEqual(len(problems), 1)
        self.assertIn("'hook'", problems[0])
        self.assertIn("[[hooks]]", problems[0])

    def test_unknown_entry_keys_and_bad_timeout_are_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "hooks.toml"
            path.write_text(
                '[[hooks]]\nevent = "PreToolUse"\nmatch = "bash"\ncommand = "true"\ntimeout = "soon"\n',
                encoding="utf-8",
            )
            hooks, problems = load_hooks(path)
        #  照常加载（matcher 空 = 全匹配，比不加载更严），但两处都点了名
        self.assertEqual(len(hooks), 1)
        self.assertEqual(hooks[0].matcher, "")
        self.assertTrue(any("match" in item for item in problems), problems)
        self.assertTrue(any("timeout" in item for item in problems), problems)

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

    def test_for_tools_keeps_only_tool_hooks_in_the_new_workspace(self):
        elsewhere = self.tmp / "worktree"
        engine = self.engine(
            [Hook("PreToolUse", "true", matcher="bash"), Hook("PostToolUse", "true"),
             Hook("Stop", "true"), Hook("UserPromptSubmit", "true")]
        )
        scoped = engine.for_tools(elsewhere)
        self.assertEqual([hook.event for hook in scoped.hooks], ["PreToolUse", "PostToolUse"])
        self.assertEqual(scoped.workspace, elsewhere)
        self.assertIsNone(self.engine([Hook("Stop", "true")]).for_tools(elsewhere))

    def test_matcher_also_accepts_the_forwarded_tool_name(self):
        cmd = _script_cmd(self.tmp, "block.py", BLOCK_BODY)
        engine = self.engine([Hook("PreToolUse", cmd, matcher="^mcp__gh__delete$")])
        self.assertFalse(engine.fire("PreToolUse", {}, tool_name="use_tool").blocked)
        self.assertTrue(
            engine.fire("PreToolUse", {}, tool_name="use_tool", also="mcp__gh__delete").blocked
        )
        #  写给转发器本身的 matcher 照旧匹配
        engine = self.engine([Hook("PreToolUse", cmd, matcher="^use_tool$")])
        self.assertTrue(
            engine.fire("PreToolUse", {}, tool_name="use_tool", also="mcp__gh__list").blocked
        )

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

    def test_stop_feedback_is_neither_a_user_turn_nor_an_operator_message(self):
        """hook 打印的东西可能来自仓库文件：不当用户原话，也不进权威通道。"""
        from xiaoyu import media
        from xiaoyu.responses import OPERATOR_KEY
        from xiaoyu.session_log import turn_starts

        engine = self._engine([Hook("Stop", self._cmd("b.py", BLOCK_BODY))])
        agent = self.build(
            [text_turn("我做完了"), text_turn("补充完毕")], hook_engine=engine
        )
        agent.send("干活")
        feedback = next(m for m in agent.messages if "hook 反馈" in str(m.get("content")))
        self.assertTrue(feedback.get(media.INJECTED_KEY))
        self.assertFalse(feedback.get(OPERATOR_KEY))
        self.assertTrue(media.is_injected_message(feedback))
        starts = turn_starts(agent.messages)
        self.assertEqual([agent.messages[i]["content"] for i in starts], ["干活"])
        #  私有标记止于内核边界（出网口统一摘下划线键）
        from xiaoyu.responses import strip_private

        self.assertNotIn(media.INJECTED_KEY, strip_private([feedback])[0])

    def test_long_hook_output_is_clipped_with_both_ends_kept(self):
        body = (
            "import sys\n"
            "sys.stderr.write('FIRST-ERROR\\n' + 'x' * 300000 + '\\nSUMMARY: 3 failed')\n"
            "sys.exit(2)\n"
        )
        engine = self._engine([Hook("Stop", self._cmd("long.py", body))])
        decision = engine.fire("Stop", {"last_text": "done"})
        self.assertTrue(decision.blocked)
        self.assertLess(len(decision.reason), 4_200)
        self.assertIn("FIRST-ERROR", decision.reason)
        self.assertIn("SUMMARY: 3 failed", decision.reason)
        self.assertIn("省略", decision.reason)


class OnFailureAndNewEventsTest(unittest.TestCase):
    """on_failure 开关、三个新事件、放行钩子的 stdout 首行。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.notices: list[str] = []

    def tearDown(self):
        self._tmp.cleanup()

    def engine(self, hooks: list[Hook]) -> HookEngine:
        return HookEngine(hooks, self.tmp, notify=self.notices.append)

    def test_load_on_failure_and_new_events(self):
        path = self.tmp / "hooks.toml"
        path.write_text(
            '[[hooks]]\nevent = "PreToolUse"\ncommand = "true"\non_failure = "block"\n'
            '[[hooks]]\nevent = "SessionStart"\ncommand = "true"\n'
            '[[hooks]]\nevent = "SessionEnd"\ncommand = "true"\n'
            '[[hooks]]\nevent = "ToolFailed"\ncommand = "true"\n'
            #  非 PreToolUse 上的 block：照常加载但归零并点名
            '[[hooks]]\nevent = "Stop"\ncommand = "true"\non_failure = "block"\n'
            #  不认识的取值：按 allow 并点名
            '[[hooks]]\nevent = "PreToolUse"\ncommand = "true"\non_failure = "maybe"\n'
            #  子 agent 与压缩两对事件：内核早就在触发，hooks.toml 也得挂得上
            '[[hooks]]\nevent = "SubagentStart"\ncommand = "true"\n'
            '[[hooks]]\nevent = "SubagentEnd"\ncommand = "true"\n'
            '[[hooks]]\nevent = "BeforeCompact"\ncommand = "true"\n'
            '[[hooks]]\nevent = "AfterCompact"\ncommand = "true"\n',
            encoding="utf-8",
        )
        hooks, problems = load_hooks(path)
        self.assertEqual(
            [(h.event, h.on_failure) for h in hooks],
            [("PreToolUse", "block"), ("SessionStart", "allow"), ("SessionEnd", "allow"),
             ("ToolFailed", "allow"), ("Stop", "allow"), ("PreToolUse", "allow"),
             ("SubagentStart", "allow"), ("SubagentEnd", "allow"),
             ("BeforeCompact", "allow"), ("AfterCompact", "allow")],
        )
        self.assertEqual(len(problems), 2, problems)
        self.assertTrue(any("只对 PreToolUse 生效" in p for p in problems), problems)
        self.assertTrue(any("maybe" in p for p in problems), problems)

    def test_events_table_covers_every_event_the_kernel_fires(self):
        """EVENTS 是 hooks.toml 的准入表：内核 `fire("X", …)` 的每个字面量都得在表里，
        否则文档承诺的事件用户挂不上（曾漏掉 Subagent*/​*Compact 四个）。"""
        import re

        from xiaoyu import hooks as hooks_module

        package = Path(hooks_module.__file__).parent
        fired: set[str] = set()
        for source in package.glob("*.py"):
            text = source.read_text(encoding="utf-8", errors="replace")
            fired.update(re.findall(r'\.fire\(\s*"([A-Za-z]+)"', text))
        self.assertTrue(fired, "没扫到任何 fire 调用——正则或目录不对")
        self.assertEqual(sorted(fired - set(hooks_module.EVENTS)), [], f"内核触发但 EVENTS 没列：{fired}")
        for name in ("SubagentStart", "SubagentEnd", "BeforeCompact", "AfterCompact"):
            self.assertIn(name, hooks_module.EVENTS)

    def test_on_failure_block_turns_crash_and_timeout_into_denial(self):
        crash = _script_cmd(self.tmp, "crash.py", "import sys\nsys.exit(1)\n")
        engine = self.engine([Hook("PreToolUse", crash, on_failure="block")])
        decision = engine.fire("PreToolUse", {"tool": "bash"}, tool_name="bash")
        self.assertTrue(decision.blocked)
        self.assertIn("钩子失败", decision.reason)
        self.assertIn("退出码 1", decision.reason)
        self.assertTrue(any("拦截" in note for note in self.notices), self.notices)

        slow = _script_cmd(self.tmp, "slow.py", "import time\ntime.sleep(10)\n")
        engine = self.engine([Hook("PreToolUse", slow, timeout=1.0, on_failure="block")])
        decision = engine.fire("PreToolUse", {"tool": "bash"}, tool_name="bash")
        self.assertTrue(decision.blocked)
        self.assertIn("超时", decision.reason)

    def test_on_failure_allow_keeps_fail_open_with_visible_warning(self):
        crash = _script_cmd(self.tmp, "crash.py", "import sys\nsys.exit(1)\n")
        engine = self.engine([Hook("PreToolUse", crash)])
        self.assertFalse(engine.fire("PreToolUse", {"tool": "bash"}, tool_name="bash").blocked)
        self.assertTrue(any("放行" in note and "退出码 1" in note for note in self.notices), self.notices)

    def test_allowed_hook_stdout_first_line_is_reported(self):
        say = _script_cmd(
            self.tmp, "say.py",
            "import sys\nsys.stdout.write('\\n  branch: main  \\nsecond line\\n')\nsys.exit(0)\n",
        )
        quiet = _script_cmd(self.tmp, "quiet.py", ALLOW_BODY)
        engine = self.engine([Hook("SessionStart", say), Hook("SessionStart", quiet)])
        decision = engine.fire("SessionStart", {"model": "m"})
        self.assertEqual(decision, Decision(blocked=False, reason="", output="branch: main"))

    def test_first_line_is_capped(self):
        from xiaoyu.hooks import OUTPUT_LINE_CAP, first_line

        self.assertEqual(first_line(""), "")
        self.assertEqual(first_line("\n\n"), "")
        clipped = first_line("x" * (OUTPUT_LINE_CAP + 50))
        self.assertLess(len(clipped), OUTPUT_LINE_CAP + 20)
        self.assertTrue(clipped.endswith("（已截断）"))

    def test_for_tools_also_carries_toolfailed(self):
        engine = self.engine(
            [Hook("ToolFailed", "true"), Hook("SessionStart", "true"), Hook("SessionEnd", "true")]
        )
        scoped = engine.for_tools(self.tmp / "w")
        self.assertEqual([hook.event for hook in scoped.hooks], ["ToolFailed"])


class LifecycleIntegrationTest(AgentTestCase):
    """call_id 贯穿 Pre / Post / ToolFailed；SessionStart / SessionEnd 从 Agent 入口触发。"""

    def _engine(self, hooks: list[Hook]) -> HookEngine:
        return HookEngine(hooks, self.root, notify=lambda text: None)

    def _dump_cmd(self, name: str) -> str:
        out = self.root / f"{name}.json"
        return _script_cmd(
            self.root, f"{name}.py",
            f"import sys, pathlib\npathlib.Path({str(out)!r}).write_text(sys.stdin.read(), encoding='utf-8')\n",
        )

    def _payload(self, name: str) -> dict:
        return json.loads((self.root / f"{name}.json").read_text(encoding="utf-8"))

    def test_pre_and_post_share_call_id(self):
        engine = self._engine(
            [Hook("PreToolUse", self._dump_cmd("pre")), Hook("PostToolUse", self._dump_cmd("post"))]
        )
        agent = self.build([tool_turn("read_file", {"path": "calc.py"}), text_turn("好")], hook_engine=engine)
        agent.send("看代码")
        pre, post = self._payload("pre"), self._payload("post")
        self.assertEqual(pre["call_id"], "call_read_file")
        self.assertEqual(post["call_id"], pre["call_id"])
        self.assertEqual(post["tool"], "read_file")
        self.assertTrue(post["ok"])

    def test_toolfailed_fires_with_call_id_and_output(self):
        engine = self._engine([Hook("ToolFailed", self._dump_cmd("failed"))])
        agent = self.build([tool_turn("read_file", {"path": "missing.txt"}), text_turn("好")], hook_engine=engine)
        agent.send("看代码")
        failed = self._payload("failed")
        self.assertEqual(failed["event"], "ToolFailed")
        self.assertEqual(failed["call_id"], "call_read_file")
        self.assertFalse(failed["ok"])
        self.assertEqual(failed["args"], {"path": "missing.txt"})
        self.assertTrue(failed["output"].startswith("ERROR:"))

    def test_session_start_injects_first_line_once(self):
        from xiaoyu import media

        say = _script_cmd(
            self.root, "say.py", "import sys\nsys.stdout.write('当前分支 main，别碰 prod\\n细节\\n')\n"
        )
        engine = self._engine([Hook("SessionStart", say)])
        agent = self.build([], hook_engine=engine)
        before = len(agent.messages)
        decision = agent.begin_session()
        self.assertFalse(decision.blocked)
        injected = [m for m in agent.messages[before:] if m.get(media.INJECTED_KEY)]
        self.assertEqual(len(injected), 1)
        self.assertEqual(injected[0]["content"], "[SessionStart hook] 当前分支 main，别碰 prod")
        #  再调不再触发、不再注入
        self.assertIsNone(agent.begin_session())
        self.assertEqual(len(agent.messages), before + 1)

    def test_session_start_payload_and_block(self):
        engine = self._engine([Hook("SessionStart", self._dump_cmd("start"))])
        agent = self.build([], hook_engine=engine)
        agent.begin_session()
        start = self._payload("start")
        self.assertEqual(start["event"], "SessionStart")
        self.assertEqual(start["model"], agent.config.model)
        self.assertIn("session", start)

        engine = self._engine([Hook("SessionStart", _script_cmd(self.root, "b.py", BLOCK_BODY))])
        agent = self.build([], hook_engine=engine)
        before = len(agent.messages)
        decision = agent.begin_session()
        self.assertTrue(decision.blocked)
        self.assertEqual(decision.reason, "不许这么干")
        self.assertEqual(len(agent.messages), before)
        #  没启动成功就不算开始：收尾钩子不跑，再次 begin 还会重试
        self.assertFalse(agent._session_started)

    def test_session_end_fires_once_after_start(self):
        engine = self._engine([Hook("SessionEnd", self._dump_cmd("end"))])
        agent = self.build([], hook_engine=engine)
        agent.end_session()  # 没 begin 过：不触发
        self.assertFalse((self.root / "end.json").exists())
        agent.begin_session()
        agent.end_session()
        self.assertEqual(self._payload("end")["event"], "SessionEnd")
        (self.root / "end.json").unlink()
        agent.end_session()  # 幂等
        self.assertFalse((self.root / "end.json").exists())

    def test_no_engine_still_marks_session_started(self):
        agent = self.build([])
        self.assertIsNone(agent.begin_session())
        self.assertTrue(agent._session_started)
        agent.end_session()  # 不炸

    def test_run_once_refuses_when_session_start_blocks(self):
        from types import SimpleNamespace

        from xiaoyu.cli import run_once

        sent: list[str] = []
        agent = SimpleNamespace(
            begin_session=lambda: Decision(blocked=True, reason="环境没就绪"),
            end_session=lambda: sent.append("end"),
            send=lambda text: sent.append(text),
        )
        with mock.patch("sys.stderr", new_callable=lambda: __import__("io").StringIO()) as err:
            self.assertEqual(run_once(agent, "干活"), 2)
        self.assertIn("环境没就绪", err.getvalue())
        self.assertEqual(sent, [])


if __name__ == "__main__":
    unittest.main()
