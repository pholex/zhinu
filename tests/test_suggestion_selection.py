"""编号选择的 keyless 前端回放：核对显示、实际请求与会话落盘。"""

from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
from unittest import mock

from xiaoyu.cli import _repl_loop
from xiaoyu.events import Suggestions
from xiaoyu.render import PlainSink
from xiaoyu.session_log import SessionLog

from .test_agent_paths import AgentTestCase, chunk
from .test_tui import HAS_TUI


class TestSuggestionSelection(AgentTestCase):
    def run_frontend(self, frontend, inputs, items, *, literal=False):
        output = io.StringIO()
        log = SessionLog(self.root / f"{frontend}.jsonl")
        self.addCleanup(log.release)
        agent = self.build([[chunk(content="完成")] for _ in inputs], session_log=log)
        request_users = []
        create = self.client.completions.create

        def record(**kwargs):
            users = [m["content"] for m in kwargs["messages"] if m["role"] == "user"]
            request_users.append(users[-1])
            return create(**kwargs)

        iterator = iter(inputs)
        first = True
        tui = None
        if frontend == "tui":
            if not HAS_TUI:
                self.skipTest("未安装 tui 可选依赖")
            from rich.console import Console
            from xiaoyu.tui import Tui

            tui = Tui(agent.permissions, console=Console(file=output, width=160))
            agent.sink = tui.sink
        else:
            agent.sink = PlainSink()

        def prompt(*args, **kwargs):
            nonlocal first
            if first:
                agent.sink.emit(Suggestions(items=items, turn=agent._turn_seq))
                first = False
                if tui is not None:
                    tui._literal_submit = literal
            try:
                return next(iterator)
            except StopIteration:
                raise EOFError from None

        with contextlib.ExitStack() as stack:
            stack.enter_context(contextlib.redirect_stdout(output))
            stack.enter_context(mock.patch.object(self.client.completions, "create", side_effect=record))
            if tui is None:
                stack.enter_context(mock.patch("builtins.input", side_effect=prompt))
                self.assertEqual(_repl_loop(agent), 0)
            else:
                session = mock.Mock()
                session.prompt.side_effect = prompt
                stack.enter_context(mock.patch.object(tui, "_input_session", return_value=session))
                stack.enter_context(mock.patch("xiaoyu.tui.SteerPoller"))
                self.assertEqual(tui._run_loop(agent), 0)
        records = [json.loads(line) for line in log.path.read_text(encoding="utf-8", errors="replace").splitlines()]
        return {
            "request_users": request_users,
            "logged_users": [r["content"] for r in records if r.get("role") == "user"],
            "visible_selection": [line.strip() for line in output.getvalue().splitlines() if line.strip().startswith("↳")],
        }

    def test_plain_selection_snapshot(self):
        self.check_snapshot("plain")

    def test_tui_selection_snapshot(self):
        self.check_snapshot("tui")

    def check_snapshot(self, frontend):
        fixture = json.loads((Path(__file__).parent / "fixtures" / "suggestion_selection.json").read_text(encoding="utf-8", errors="replace"))
        self.assertEqual(self.run_frontend(frontend, fixture["inputs"], fixture["suggestions"]), fixture["expected"])

    def test_plain_local_command_invalidates_suggestions(self):
        result = self.run_frontend("plain", ["/help", "2"], ["first", "second"])
        self.assertEqual(result["request_users"], ["2"])

    def test_tui_local_command_invalidates_suggestions(self):
        result = self.run_frontend("tui", ["/help", "2"], ["first", "second"])
        self.assertEqual(result["request_users"], ["2"])

    def test_tui_literal_submit_does_not_select(self):
        result = self.run_frontend("tui", ["2"], ["first", "second"], literal=True)
        self.assertEqual(result["request_users"], ["2"])

    def test_selected_text_is_not_reinterpreted_as_a_local_command(self):
        for frontend in ("plain", "tui"):
            with self.subTest(frontend=frontend):
                result = self.run_frontend(frontend, ["2"], ["first", "! echo example"])
                self.assertEqual(result["request_users"], ["! echo example"])
