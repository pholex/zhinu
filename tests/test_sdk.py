"""Host contracts exercised against the real kernel with a scripted transport."""
from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import os
import sys
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "packages/xiaoyu-agent-sdk/src"))

from xiaoyu_agent_sdk import (
    Allow, AsyncSession, CloseTimeoutError, ExecutionError, Hook, HookDecision,
    ModelOptions, OutputSchemaError, OutputSpec, RunCompleted, Session,
    SessionBusyError, SessionClosedError, SessionLockedError, SessionOptions,
    Tool, ToolResult, McpServer, Subagent, SessionStorageError, Plugin, ConfigurationError,
)
from tests.test_agent_paths import FakeClient, chunk, call_fragment, usage_chunk


def call(name, arguments, ident="call"):
    return chunk(tool_calls=[call_fragment(0, ident, name, arguments)])


@unittest.skipUnless(importlib.util.find_spec("jsonschema"), "Install the sdk extra for SDK tests")
class SDKTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.workspace = Path(self.tmp.name).resolve()

    def options(self, script, **kwargs):
        client = FakeClient(script)
        return SessionOptions(ModelOptions("test-model", client=client), self.workspace,
                              builtin_tools=(), **kwargs)

    def test_sync_multiturn_close_borrowed_client(self):
        options = self.options([[chunk("one")], [chunk("two")]])
        closed = []
        options.model.client.close = lambda: closed.append(True)
        with Session(options) as session:
            self.assertEqual(session.run("first").text, "one")
            self.assertEqual(session.run("second").text, "two")
            messages = options.model.client.completions.calls[-1]["messages"]
            self.assertTrue(any(m.get("content") == "first" for m in messages))
        session.close()
        self.assertEqual(closed, [])
        with self.assertRaises(SessionClosedError):
            session.run("third")

    def test_null_is_valid_and_next_turn_has_no_output_tool(self):
        options = self.options([[call("structured_output", '{"value":null}')], [chunk("done")]])
        with Session(options) as session:
            result = session.run("null", output=OutputSpec({"type": "null"}))
            self.assertIsNone(result.output)
            self.assertEqual(result.output_status, "valid")
            self.assertEqual(session.run("text").output_status, "not_requested")
            names = [t["function"]["name"] for t in options.model.client.completions.calls[-1].get("tools", [])]
            self.assertNotIn("structured_output", names)

    def test_validation_repair_counts_usage_without_replaying_side_effect(self):
        effects = []
        tool = Tool("charge", "Charge once", {"type": "object", "properties": {}},
                    lambda: effects.append("charged") or {"id": 4}, requires_approval=False)
        options = self.options([
            [call("charge", "{}"), usage_chunk(10, 1)],
            [call("structured_output", '{"value":"4"}'), usage_chunk(20, 2)],
            [call("structured_output", '{"value":4}'), usage_chunk(30, 3)],
        ], tools=(tool,))
        with Session(options) as session:
            result = session.run("charge", output=OutputSpec({"type": "integer", "minimum": 1}))
        self.assertEqual(effects, ["charged"])
        self.assertEqual((result.output, result.output_status, result.output_retries), (4, "valid", 1))
        self.assertEqual(result.usage["prompt_tokens"], 60)
        self.assertEqual(result.usage["turns"], 3)

    def test_output_failure_statuses(self):
        for retries, script, status in [
            (0, [[call("structured_output", '{"value":"bad"}')]], "invalid"),
            (1, [[call("structured_output", '{"value":0}')]] * 2, "retries_exhausted"),
            (1, [[chunk("no output")]] * 2, "missing"),
            (0, [[chunk()]], "missing"),
        ]:
            with self.subTest(status=status), Session(self.options(script)) as session:
                result = session.run("return", output=OutputSpec({"type": "integer", "minimum": 1}, retries))
                self.assertEqual(result.output_status, status)

    def test_budget_during_repair_never_wraps_up_or_reports_success(self):
        with Session(self.options([[call("structured_output", '{"value":"bad"}')]], max_iterations=1)) as session:
            result = session.run("return", output=OutputSpec({"type": "integer"}))
            self.assertEqual((result.stopped, result.output_status), ("turn_cap", "budget_exhausted"))

    def test_token_budget_stops_repair_without_an_extra_request(self):
        options = self.options([[call("structured_output", '{"value":"bad"}'), usage_chunk(6000, 1)]],
                               budget_tokens=5000)
        with Session(options) as session:
            result = session.run("return", output=OutputSpec({"type": "integer"}))
        self.assertEqual((result.stopped, result.output_status), ("budget", "budget_exhausted"))
        self.assertEqual(len(options.model.client.completions.calls), 1)

    def test_unsupported_schema_rejected_before_model_or_history(self):
        options = self.options([])
        with Session(options) as session:
            for schema in [{"type": "string", "format": "email"}, {"$ref": "https://example.com"}, {"type": "wat"}]:
                with self.subTest(schema=schema), self.assertRaises(OutputSchemaError):
                    session.run("never sent", output=OutputSpec(schema))
            self.assertEqual(len(session._agent.messages), 1)
        self.assertEqual(options.model.client.completions.calls, [])

    def test_constraints_and_errors_do_not_echo_value(self):
        schema = {"type": "object", "properties": {"secret": {"type": "string", "pattern": "^ok$"}},
                  "required": ["secret"], "additionalProperties": False}
        with Session(self.options([[call("structured_output", '{"secret":"private-token"}')]])) as session:
            result = session.run("return", output=OutputSpec(schema, 0))
        self.assertEqual(result.output_status, "invalid")
        self.assertNotIn("private-token", str(result.output_errors))

    def test_default_approval_denies_business_tool(self):
        effects = []
        tool = Tool("mutate", "mutate", {"type": "object"}, lambda: effects.append(1))
        with Session(self.options([[call("mutate", "{}")], [chunk("declined")]], tools=(tool,))) as session:
            events = list(session.stream("mutate"))
        self.assertEqual(effects, [])
        self.assertTrue(any(e.kind == "tool.denied" for e in events))
        self.assertIsInstance(events[-1], RunCompleted)

    def test_rewritten_arguments_recheck_deny(self):
        effects = []
        tool = Tool("save", "save", {"type": "object", "properties": {"path": {"type": "string"}}},
                    lambda path: effects.append(path))
        options = self.options([[call("save", '{"path":"safe"}')], [chunk("denied")]], tools=(tool,),
                               approver=lambda *_: Allow(updated_args={"path": "blocked"}),
                               deny_rules=("deny save(blocked)",))
        with Session(options) as session:
            session.run("save")
        self.assertEqual(effects, [])

    def test_tool_exception_is_redacted_and_model_can_recover(self):
        def broken():
            raise RuntimeError("api-key-private")
        tool = Tool("broken", "broken", {"type": "object"}, broken, requires_approval=False)
        with Session(self.options([[call("broken", "{}")], [chunk("failed")]], tools=(tool,))) as session:
            events = list(session.stream("try"))
        completed = next(e for e in events if e.kind == "tool.completed")
        self.assertFalse(completed.ok)
        self.assertNotIn("api-key-private", completed.output)

    def test_callback_timeout_exception_is_not_mistaken_for_wait_timeout(self):
        def broken():
            raise TimeoutError("business deadline")
        tool = Tool("broken", "broken", {"type": "object"}, broken, requires_approval=False)
        with Session(self.options([[call("broken", "{}")], [chunk("recovered")]], tools=(tool,))) as session:
            self.assertEqual(session.run("try").text, "recovered")

    def test_malformed_structured_call_uses_repair_budget(self):
        malformed = call("structured_output", "not-json")
        malformed.choices[0].finish_reason = "tool_calls"
        with Session(self.options([[malformed]])) as session:
            result = session.run("return", output=OutputSpec({"type": "integer"}, 0))
        self.assertEqual(result.output_status, "invalid")

    def test_subagent_inherits_explicit_configuration_and_approval(self):
        (self.workspace / "AGENTS.md").write_text("ambient-secret")
        options = self.options([
            [call("reviewer", '{"task":"answer briefly"}')],
            [chunk("child answer")], [chunk("parent answer")],
        ], subagents=(Subagent("reviewer", "Review", "Review carefully", ("read_file",)),))
        with Session(options) as session:
            self.assertEqual(session.run("review").text, "parent answer")
        self.assertEqual(len(options.model.client.completions.calls), 3)
        child_messages = options.model.client.completions.calls[1]["messages"]
        self.assertNotIn("ambient-secret", str(child_messages))

    def test_static_mcp_is_owned_and_no_ambient_discovery(self):
        with patch("xiaoyu.mcp.McpManager", autospec=True) as factory, \
             patch("xiaoyu.mcp.launch", side_effect=AssertionError("ambient MCP")):
            manager = factory.return_value
            manager.ready_tools.return_value = []
            manager.loading.return_value = False
            manager.shutdown_pending.return_value = ()
            manager.server_states.return_value = {"own": "ready"}
            options = self.options([[chunk("done")]], mcp_servers=(McpServer("own", command="test-server"),))
            with Session(options) as session:
                self.assertEqual([(s.name, s.state) for s in session.mcp_status()], [("own", "ready")])
                self.assertEqual(session.run("hello").text, "done")
            manager.start.assert_called_once()
            manager.close.assert_called_once()
            self.assertEqual(factory.call_args.args[0][0].command, "test-server")

    def test_rewind_conflict_preserves_external_edit_and_conversation(self):
        options = replace(self.options([
            [call("write_file", '{"path":"report.txt","content":"written"}')], [chunk("done")],
        ], approver=lambda *_: True), builtin_tools=("write_file",))
        with Session(options) as session:
            session.run("write")
            path = self.workspace / "report.txt"
            self.assertEqual(path.read_text(), "written")
            before = len(session._agent.messages)
            path.write_text("external edit")
            conflict = session.rewind(1)
            self.assertEqual(conflict.status, "conflict")
            self.assertEqual(conflict.conflicts, (str(path),))
            self.assertFalse(conflict.conversation_rewound)
            self.assertEqual(path.read_text(), "external edit")
            self.assertEqual(len(session._agent.messages), before)
            path.write_text("written")
            result = session.rewind(1)
            self.assertEqual(result.status, "completed")
            self.assertEqual(result.removed_files, (str(path),))
            self.assertTrue(result.conversation_rewound)
            self.assertFalse(path.exists())
            self.assertEqual(len(session._agent.messages), 1)

    def test_rewind_reports_partial_and_unavailable(self):
        with Session(self.options([[chunk("done")]])) as session:
            self.assertEqual(session.rewind(99).status, "unavailable")
            session.run("first")
            session._agent.messages = session._agent.messages[:1]  # Compaction removed this prompt.
            result = session.rewind(1)
            self.assertEqual(result.status, "partial")
            self.assertFalse(result.conversation_rewound)
            self.assertTrue(result.files_rewound)

    def test_selected_plugins_load_only_matching_distribution_and_entry(self):
        from xiaoyu.tools import Tool as KernelTool
        loaded = []
        def entry(distribution):
            def factory(config):
                loaded.append((distribution, config.workspace))
                return KernelTool("selected", "selected", {"type": "object"}, lambda: "ok", False)
            return SimpleNamespace(name="business", dist=SimpleNamespace(name=distribution), load=lambda: factory)
        with patch("importlib.metadata.entry_points", return_value=[entry("wanted-plugin"), entry("unwanted")]):
            options = self.options([[call("selected", "{}")], [chunk("done")]],
                                   plugins=(Plugin("business", "wanted_plugin"),))
            with Session(options) as session:
                self.assertEqual(session.run("go").text, "done")
        self.assertEqual(loaded, [("wanted-plugin", self.workspace)])
        with patch("importlib.metadata.entry_points", return_value=[]):
            with self.assertRaises(ConfigurationError):
                Session(options)

    def test_worktree_isolation_cannot_be_downgraded_or_silently_fallback(self):
        from xiaoyu.worktree import WorktreeError
        options = self.options([[call("reviewer", '{"task":"review","isolation":"none"}')], [chunk("not executed")]],
            subagents=(Subagent("reviewer", "Review", "Review", ("read_file",), isolation="worktree"),))
        with patch("xiaoyu.worktree.create", side_effect=WorktreeError("unavailable")) as create:
            with Session(options) as session:
                events = list(session.stream("go"))
            create.assert_called_once()
        self.assertTrue(any(e.kind == "tool.completed" and not e.ok for e in events))
        self.assertEqual(len(options.model.client.completions.calls), 2)

    def test_mcp_state_directories_are_private_to_each_session(self):
        from xiaoyu.mcp import McpManager
        with patch("xiaoyu.mcp.McpManager", autospec=True) as factory:
            factory.return_value.ready_tools.return_value = []
            factory.return_value.shutdown_pending.return_value = ()
            options = self.options([], mcp_servers=(McpServer("same", command="unused"),))
            first, second = Session(options), Session(options)
            self.addCleanup(first.close)
            self.addCleanup(second.close)
            left, right = (call.kwargs["state_dir"] for call in factory.call_args_list)
            self.assertNotEqual(left, right)
            with patch("xiaoyu.mcp.user_config_dir", side_effect=AssertionError("ambient MCP state")):
                manager = McpManager([], state_dir=left)
                self.assertEqual(manager._baseline_path.parent, left)
                self.assertEqual(manager._cache_path.parent.parent, left)
                self.assertEqual(manager._log_path("same", left).parent.parent, left)
                manager.close()
            first.close()
            self.assertFalse(left.exists())
            self.assertTrue(right.exists())
            second.close()
            self.assertFalse(right.exists())

    def test_mcp_pending_shutdown_keeps_log_locked_until_retry(self):
        with patch("xiaoyu.mcp.McpManager", autospec=True) as factory:
            manager = factory.return_value
            manager.ready_tools.return_value = []
            manager.shutdown_pending.side_effect = [("server-reader",), ()]
            options = self.options([], session_dir=self.workspace / "logs", mcp_servers=(McpServer("own", command="test"),))
            session = Session(options)
            mcp_dir = factory.call_args.kwargs["state_dir"]
            with self.assertRaises(CloseTimeoutError):
                session.close()
            self.assertFalse(session.closed)
            self.assertTrue(mcp_dir.is_dir())
            self.assertTrue(session._log.locked)
            session.close()
            self.assertTrue(session.closed)
            self.assertFalse(mcp_dir.exists())
            self.assertFalse(session._log.locked)

    def test_blocked_client_cleanup_has_one_worker_and_is_retryable(self):
        entered, release = threading.Event(), threading.Event()
        session = Session(self.options([], close_timeout=0.02))
        calls = []
        def close():
            calls.append(1)
            entered.set()
            release.wait(3)
        session._owned_clients.append(SimpleNamespace(close=close))
        try:
            with self.assertRaises(CloseTimeoutError):
                session.close()
            self.assertTrue(entered.is_set())
            self.assertFalse(session.closed)
            with self.assertRaises(CloseTimeoutError):
                session.close()
            self.assertEqual(calls, [1])
        finally:
            release.set()
            session.close()
        self.assertTrue(session.closed)

    def test_model_error_preserves_cause_and_session_can_continue(self):
        options = self.options([ValueError("private credential"), [chunk("recovered")]])
        with Session(options) as session:
            with self.assertRaises(ExecutionError) as error:
                session.run("fail")
            self.assertIsInstance(error.exception.__cause__, ValueError)
            self.assertNotIn("private credential", str(error.exception))
            self.assertEqual(session.run("continue").text, "recovered")

    def test_explicit_config_does_not_discover_ambient_extensions(self):
        (self.workspace / "AGENTS.md").write_text("ambient-secret-instruction")
        before = os.getcwd(), dict(os.environ)
        with patch("xiaoyu.providers.build", side_effect=AssertionError("ambient provider")), \
             patch("xiaoyu.skills.skill_sources", side_effect=AssertionError("ambient skills")), \
             Session(self.options([[chunk("done")]])) as session:
            self.assertNotIn("ambient-secret-instruction", session._agent.messages[0]["content"])
            session.run("hello")
        self.assertEqual(before, (os.getcwd(), dict(os.environ)))

    def test_hooks_and_explicit_skills(self):
        skills = self.workspace / "skills"
        (skills / "business").mkdir(parents=True)
        (skills / "business/SKILL.md").write_text("---\nname: business\ndescription: Our business skill\n---\nDo it")
        options = self.options([], hooks=(Hook("UserPromptSubmit", lambda _: HookDecision(True, "blocked")),),
                               skill_directories=(skills,))
        with Session(options) as session:
            self.assertEqual([s.name for s in session._agent.skills], ["business"])
            session.run("blocked")
        self.assertEqual(options.model.client.completions.calls, [])

    def test_resume_lock_fork_and_restart_snapshot_limit(self):
        options = self.options([[chunk("first")]], session_dir=self.workspace / "logs")
        with Session(options) as session:
            session.run("remember")
            path = session.session_path
            with self.assertRaises(SessionLockedError):
                Session(options, resume_from=path)
            with session.fork(options=self.options([[chunk("forked")]])) as child:
                self.assertEqual(child.run("fork").text, "forked")
                self.assertTrue(any(m.get("content") == "remember" for m in child._agent.messages))
        with Session(self.options([[chunk("resumed")]]), resume_from=path) as restored:
            self.assertEqual(restored.checkpoints(), ())
            self.assertEqual(restored.run("continue").text, "resumed")
            self.assertTrue(any(m.get("content") == "remember" for m in restored._agent.messages))

    def test_slow_callback_close_reports_timeout_then_finishes(self):
        entered, release = threading.Event(), threading.Event()
        def slow():
            entered.set()
            release.wait(5)
            return "done"
        tool = Tool("slow", "slow", {"type": "object"}, slow, requires_approval=False)
        session = Session(self.options([[call("slow", "{}")]], tools=(tool,), close_timeout=0.05))
        self.addCleanup(release.set)
        future = session._start("slow", None)
        self.assertTrue(entered.wait(2))
        with self.assertRaises(SessionBusyError):
            session.run("overlap")
        with self.assertRaises(CloseTimeoutError):
            session.close()
        self.assertFalse(session.closed)
        self.assertTrue(future.result(timeout=2).interrupted)
        release.set()
        session.close()
        self.assertTrue(session.closed)


@unittest.skipUnless(importlib.util.find_spec("jsonschema"), "Install the sdk extra for SDK tests")
class AsyncSDKTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.workspace = Path(self.tmp.name).resolve()

    def options(self, script, **kwargs):
        return SessionOptions(ModelOptions("test-model", client=FakeClient(script)), self.workspace,
                              builtin_tools=(), **kwargs)

    async def test_async_approval_and_tool_share_host_loop(self):
        loop = asyncio.get_running_loop()
        async def approve(*_):
            self.assertIs(asyncio.get_running_loop(), loop)
            return True
        async def business():
            self.assertIs(asyncio.get_running_loop(), loop)
            return ToolResult({"ok": True})
        tool = Tool("business", "business", {"type": "object"}, business)
        async with AsyncSession(self.options([[call("business", "{}")], [chunk("done")]],
                                            tools=(tool,), approver=approve)) as session:
            events = [e async for e in session.stream("go")]
            self.assertEqual(events[-1].result.text, "done")
            self.assertTrue(next(e for e in events if e.kind == "tool.completed").ok)

    async def test_cancel_pending_approval_then_continue(self):
        entered, cleaned = asyncio.Event(), asyncio.Event()
        async def approve(*_):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                await asyncio.sleep(0.01)
                cleaned.set()
        tool = Tool("mutate", "mutate", {"type": "object"}, lambda: self.fail("must not execute"))
        async with AsyncSession(self.options([[call("mutate", "{}")], [chunk("continued")]],
                                            tools=(tool,), approver=approve)) as session:
            task = asyncio.create_task(session.run("go"))
            await asyncio.wait_for(entered.wait(), 2)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            await asyncio.wait_for(cleaned.wait(), 2)
            self.assertEqual((await session.run("continue")).text, "continued")

    async def test_approval_timeout_fails_closed(self):
        async def approve(*_):
            await asyncio.sleep(30)
            return True
        tool = Tool("mutate", "mutate", {"type": "object"}, lambda: self.fail("must not execute"))
        async with AsyncSession(self.options([[call("mutate", "{}")], [chunk("denied")]],
                                            tools=(tool,), approver=approve, approval_timeout=0.02)) as session:
            events = [e async for e in session.stream("go")]
            self.assertTrue(any(e.kind == "tool.denied" for e in events))

    async def test_interrupt_during_repair_is_a_result_then_session_can_continue(self):
        entered = asyncio.Event()
        async def wait():
            entered.set()
            await asyncio.Event().wait()
        tool = Tool("wait", "Wait for business data", {"type": "object"}, wait, requires_approval=False)
        options = self.options([[call("structured_output", '{"value":"bad"}')],
                                [call("wait", "{}")], [chunk("continued")]], tools=(tool,))
        async with AsyncSession(options) as session:
            task = asyncio.create_task(session.run("return", output=OutputSpec({"type": "integer"})))
            await asyncio.wait_for(entered.wait(), 2)
            session.interrupt()
            result = await task
            self.assertTrue(result.interrupted)
            self.assertEqual(result.output_status, "interrupted")
            self.assertEqual((await session.run("continue")).text, "continued")

    async def test_stream_early_close_releases_backpressure_and_can_continue(self):
        options = self.options([[chunk("x") for _ in range(1000)], [chunk("again")]], event_buffer_size=1)
        async with AsyncSession(options) as session:
            async with contextlib.aclosing(session.stream("long")) as events:
                async for event in events:
                    if event.kind == "text.delta":
                        break
            self.assertEqual((await session.run("next")).text, "again")

    async def test_independent_sessions_do_not_share_state(self):
        async with AsyncSession(self.options([[chunk("one")]])) as first, \
                   AsyncSession(self.options([[chunk("two")]])) as second:
            results = await asyncio.gather(first.run("a"), second.run("b"))
            self.assertEqual([r.text for r in results], ["one", "two"])
            self.assertIsNot(first._session._agent.usage, second._session._agent.usage)

    async def test_parallel_workspaces_approvals_tools_events_and_usage_are_isolated(self):
        approvals = []
        both = asyncio.Event()
        def make(label, allowed, tokens):
            workspace = self.workspace / label
            workspace.mkdir()
            async def approve(name, arguments):
                approvals.append((label, arguments["value"]))
                if len(approvals) == 2:
                    both.set()
                await asyncio.wait_for(both.wait(), 2)
                return allowed
            def save(value):
                (workspace / "output").write_text(value)
                return label
            return replace(self.options([
                [call("save", '{"value":"' + label + '"}'), usage_chunk(tokens, 1)],
                [chunk(label), usage_chunk(tokens, 1)],
            ], approver=approve, tools=(Tool("save", "save", {"type": "object", "properties": {"value": {"type": "string"}}}, save),)),
                workspace=workspace, system_prompt="Host " + label, tool_env={"TENANT": label})
        async def collect(session):
            return [e async for e in session.stream("execute")]
        async with AsyncSession(make("a", True, 10)) as a, AsyncSession(make("b", False, 20)) as b:
            first, second = await asyncio.gather(collect(a), collect(b))
            self.assertEqual(first[-1].result.usage["prompt_tokens"], 20)
            self.assertEqual(second[-1].result.usage["prompt_tokens"], 40)
            self.assertTrue(any(e.kind == "tool.completed" for e in first))
            self.assertTrue(any(e.kind == "tool.denied" for e in second))
            self.assertEqual(a._session._agent.config.extra_env, {"TENANT": "a"})
            self.assertEqual(b._session._agent.config.extra_env, {"TENANT": "b"})
        self.assertEqual(sorted(approvals), [("a", "a"), ("b", "b")])
        self.assertEqual((self.workspace / "a/output").read_text(), "a")
        self.assertFalse((self.workspace / "b/output").exists())


if __name__ == "__main__":
    unittest.main()
