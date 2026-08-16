"""`/resume` 进 REPL（cli.slash_resume / choose_session）的测试。

锁住四件事：
1. 切换语义：先 reset 再 restore——原对话被清空、所选会话完整接回，
   并走 replay_recent 回放（事件进 sink）；
2. 列表规则：只列当前工作区、剔除当前会话文件、最多 _SLASH_RESUME_LIMIT 个；
3. 选择通道：行内菜单（select 注入）→ 序号直选 → 编号输入回退 → 取消不动现场；
4. handle_slash 把 /resume 与 select 正确接给 slash_resume。
"""

from __future__ import annotations

import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from xiaoyu import session_log as session_log_module
from xiaoyu.cli import _SLASH_RESUME_LIMIT, choose_session, handle_slash, slash_resume
from xiaoyu.session_log import SESSION_FORMAT, SessionLog, list_sessions

from .test_agent_paths import AgentTestCase
from .test_render import RecordingSink


class SlashResumeTestCase(AgentTestCase):
    def setUp(self) -> None:
        super().setUp()
        config_dir = tempfile.TemporaryDirectory()
        self.addCleanup(config_dir.cleanup)
        self.config_home = Path(config_dir.name)
        #  session_log 用 from-import 绑定了 user_config_dir，patch 它自己的引用
        patcher = mock.patch.object(
            session_log_module, "user_config_dir", lambda: self.config_home
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def make_session(
        self, name: str, preview: str = "改一下 calc.py", answer: str = "好的，已完成。"
    ) -> Path:
        """手工落一个会话文件。文件名由调用方给：SessionLog.create 的
        时间戳+pid 命名在同秒内会撞名，测试里造多个会话必须自控。"""
        directory = self.config_home / "sessions" / session_log_module._workspace_slug(
            str(self.root)
        )
        log = SessionLog(directory / f"{name}.jsonl")
        log.event(
            "meta",
            format=SESSION_FORMAT,
            version="0",
            model="m",
            workspace=str(self.root),
            started_at="2026-08-09T10:00:00",
        )
        log.append({"role": "user", "content": preview})
        log.append({"role": "assistant", "content": answer})
        return log.path

    def run_slash(self, agent, rest: list[str], select=None) -> str:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            slash_resume(agent, rest, select)
        return buffer.getvalue()


class TestSlashResume(SlashResumeTestCase):
    def test_switch_resets_then_restores_and_replays(self) -> None:
        self.make_session("20260809-100000-1", preview="旧任务", answer="旧结论")
        sink = RecordingSink()
        agent = self.build([], sink=sink)
        #  现场有对话：切换必须先清掉，不能把两个会话拼在一起
        agent.messages.append({"role": "user", "content": "当前对话的话"})

        out = self.run_slash(agent, [], select=lambda title, options: options[0][0])

        self.assertIn("已切到", out)
        roles = [message["role"] for message in agent.messages]
        self.assertEqual(roles[0], "system")
        self.assertNotIn(
            "当前对话的话", [message.get("content") for message in agent.messages]
        )
        self.assertEqual(
            [message.get("content") for message in agent.messages[1:]],
            ["旧任务", "旧结论"],
        )
        #  回放走了 sink：header + 用户行 + 正文
        kinds = sink.kinds()
        self.assertIn("notice", kinds)
        self.assertIn("text.delta", kinds)
        header = sink.events[0].text
        self.assertIn("回放最近 1 轮", header)

    def test_direct_index_skips_menu(self) -> None:
        self.make_session("20260809-100000-1")

        def exploding_select(title, options):
            raise AssertionError("给了序号就不该弹菜单")

        agent = self.build([], sink=RecordingSink())
        out = self.run_slash(agent, ["1"], select=exploding_select)
        self.assertIn("已切到", out)

    def test_out_of_range_index_changes_nothing(self) -> None:
        self.make_session("20260809-100000-1")
        agent = self.build([], sink=RecordingSink())
        before = list(agent.messages)
        out = self.run_slash(agent, ["5"])
        self.assertIn("序号超出范围", out)
        self.assertEqual(agent.messages, before)

    def test_current_session_file_is_excluded(self) -> None:
        self.make_session("20260809-100000-1", preview="别的会话")
        current = SessionLog.create("m", str(self.root))
        agent = self.build([], sink=RecordingSink(), session_log=current)

        seen: list[list] = []

        def capture(title, options):
            seen.append(options)
            return None  # 只看列表，不切换

        self.run_slash(agent, [], select=capture)
        self.assertEqual(len(seen), 1)
        labels = [label for _value, label, _key in seen[0]]
        self.assertEqual(len(labels), 1, "当前会话文件不该出现在列表里")
        self.assertIn("别的会话", labels[0])

    def test_cancel_keeps_everything(self) -> None:
        self.make_session("20260809-100000-1")
        sink = RecordingSink()
        agent = self.build([], sink=sink)
        agent.messages.append({"role": "user", "content": "别丢了我"})
        self.run_slash(agent, [], select=lambda title, options: None)
        self.assertEqual(agent.messages[-1]["content"], "别丢了我")
        self.assertEqual(sink.events, [])

    def test_no_other_sessions_prints_hint(self) -> None:
        agent = self.build([], sink=RecordingSink())
        out = self.run_slash(agent, [])
        self.assertIn("没有其它会话", out)

    def test_list_is_capped(self) -> None:
        for index in range(_SLASH_RESUME_LIMIT + 3):
            self.make_session(f"20260809-1000{index:02d}-1")
        agent = self.build([], sink=RecordingSink())
        seen: list[list] = []
        self.run_slash(agent, [], select=lambda t, o: seen.append(o))
        self.assertEqual(len(seen[0]), _SLASH_RESUME_LIMIT)

    def test_handle_slash_routes_resume_with_select(self) -> None:
        agent = self.build([])
        marker = object()
        with mock.patch("xiaoyu.cli.slash_resume") as fake:
            quit_ = handle_slash(agent, "/resume 2", select=marker)
        self.assertFalse(quit_)
        fake.assert_called_once_with(agent, ["2"], marker)


class TestChooseSession(SlashResumeTestCase):
    def sessions(self):
        self.make_session("20260809-100002-1", preview="新")
        self.make_session("20260809-100001-1", preview="旧")
        return list_sessions(workspace=str(self.root))

    def test_menu_value_maps_to_session(self) -> None:
        sessions = self.sessions()
        chosen = choose_session(sessions, select=lambda title, options: 1)
        self.assertIs(chosen, sessions[1])

    def test_menu_cancel_returns_none_without_fallback(self) -> None:
        sessions = self.sessions()
        with mock.patch("builtins.input", side_effect=AssertionError("取消不该再问")):
            self.assertIsNone(choose_session(sessions, select=lambda t, o: None))

    def test_menu_amend_tuple_degrades_to_plain_choice(self) -> None:
        sessions = self.sessions()
        chosen = choose_session(sessions, select=lambda t, o: ("amend", 0))
        self.assertIs(chosen, sessions[0])

    def test_broken_menu_falls_back_to_numbered_input(self) -> None:
        sessions = self.sessions()

        def broken(title, options):
            raise RuntimeError("起不来")

        with mock.patch("builtins.input", return_value="2"):
            with contextlib.redirect_stdout(io.StringIO()):
                chosen = choose_session(sessions, select=broken)
        self.assertIs(chosen, sessions[1])

    def test_numbered_input_paths(self) -> None:
        sessions = self.sessions()
        for answer, expected in (("1", sessions[0]), ("", None), ("99", None), ("x", None)):
            with mock.patch("builtins.input", return_value=answer):
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertIs(choose_session(sessions), expected)


if __name__ == "__main__":
    unittest.main()
