"""Result text is processed before persistence, without replaying tool actions."""
from __future__ import annotations

import asyncio
from dataclasses import FrozenInstanceError, replace
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "packages/xiaoyu-agent-sdk/src"))

from xiaoyu_agent_sdk import (
    AsyncSession, CloseTimeoutError, ConfigurationError, Hook, HookDecision,
    ResultTransform, ResultTransformError, Session, SessionBusyError,
    SQLiteSessionStore, Subagent, TaskSpec, Tool, ToolResult,
)
from tests.test_sdk_controls import options, call
from tests.test_agent_paths import chunk, call_fragment
from xiaoyu.tools import Tool as KernelTool
from xiaoyu.errors import Interrupted, attach_partial


SECRET = "private-result-318527"


def redact(output):
    return output.text.replace(SECRET, "[removed]")


def tool(handler):
    return Tool("lookup", "Lookup", {"type": "object"}, handler, requires_approval=False)


@unittest.skipUnless(importlib.util.find_spec("jsonschema"), "Install the sdk extra")
class TransformTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.workspace = Path(temporary.name).resolve()

    def test_order_filter_immutable_input_and_hooks_see_transformed_text(self):
        seen, hooks = [], []
        def first(output):
            seen.append(output)
            with self.assertRaises(FrozenInstanceError):
                output.text = "changed"
            return redact(output)
        transforms = [ResultTransform("redact", first),
                      ResultTransform("skip", lambda _: self.fail("wrong tool"), "other"),
                      ResultTransform("format", lambda o: "report: " + o.text, "lookup")]
        config = options(self.workspace, [[call("lookup", {})], [chunk("done")]],
                         tools=(tool(lambda: SECRET),), result_transforms=transforms,
                         hooks=(Hook("PostToolUse", lambda p: hooks.append(p) or HookDecision(False)),))
        with Session(config) as session:
            transforms.clear()  # The session owns its registration sequence.
            events = list(session.stream("lookup"))
            output = next(e.output for e in events if e.kind == "tool.completed")
            self.assertEqual(output, "report: [removed]")
            self.assertEqual(hooks[0]["output"], output)
            self.assertEqual(session._agent.trace[0]["output"], output)
            self.assertNotIn(SECRET, str(session.snapshot().history))
            self.assertEqual((seen[0].tool_name, seen[0].session_id, seen[0].tool_call_id),
                             ("lookup", session.session_id, "lookup"))
            self.assertTrue(seen[0].run_id)
            self.assertFalse(seen[0].is_error)
        self.assertNotIn(SECRET, str(config.model.client.completions.calls))

    def test_spill_and_recall_only_contain_processed_output(self):
        config = options(self.workspace, [[call("lookup", {})], [chunk("done")]],
                         builtin_tools=("recall",),
                         tools=(tool(lambda: (SECRET + "\n") * 5000),),
                         result_transforms=(ResultTransform("redact", redact),))
        with Session(config) as session:
            events = list(session.stream("read"))
            self.assertTrue(session._toolbox._spills)
            for path in session._toolbox._spill_dir.glob("*.txt"):
                text = path.read_text(encoding="utf-8", errors="replace")
                self.assertNotIn(SECRET, text)
                self.assertIn("[removed]", text)
            self.assertNotIn(SECRET, str(events))
            recalled = session._toolbox.run("recall", {"id": "1", "offset": 1, "limit": 2})
            self.assertNotIn(SECRET, recalled)
            self.assertIn("[removed]", recalled)

    def test_persistence_resume_and_fork_do_not_reprocess_old_results(self):
        seen = []
        def process(output):
            seen.append(output.tool_call_id)
            return redact(output)
        for backend in ("jsonl", "sqlite"):
            with self.subTest(backend=backend):
                store = SQLiteSessionStore(self.workspace / "results.sqlite")
                storage = {"session_dir": self.workspace / "logs"} if backend == "jsonl" else {"session_store": store}
                config = options(self.workspace, [[call("lookup", {})], [chunk("done")]],
                                 tools=(tool(lambda: SECRET),), result_transforms=(ResultTransform("redact", process),),
                                 **storage)
                with Session(config) as session:
                    session.run("lookup")
                    count = len(seen)
                    with session.fork() as fork:
                        self.assertNotIn(SECRET, str(fork.snapshot().history))
                    resume = {"resume_from": session.session_path} if backend == "jsonl" else {"resume_id": session.session_id}
                    ident, path = session.session_id, session.session_path
                with Session(config, **resume) as restored:
                    self.assertNotIn(SECRET, str(restored.snapshot().history))
                    self.assertEqual(len(seen), count)
                if path is not None:
                    records = [json.loads(line) for line in path.read_text(encoding="utf-8", errors="replace").splitlines()]
                else:
                    writer = store.open(ident, metadata={}, resume=True)
                    try:
                        records = writer.read()
                    finally:
                        writer.close()
                self.assertNotIn(SECRET, str(records))
                audit = [r for r in records if r.get("event") == "sdk.result_transform"]
                self.assertEqual([(r["name"], r["status"]) for r in audit], [("redact", "applied")])

    def test_failure_stops_pipeline_and_turn_without_repeating_side_effect(self):
        for kind in ("exception", "bad_type", "status_change"):
            with self.subTest(kind=kind):
                effects, events = [], []
                def fail(output):
                    if kind == "exception":
                        raise ValueError(SECRET)
                    return None if kind == "bad_type" else "ERROR: rewritten status"
                config = options(self.workspace, [[call("lookup", {})]],
                                 tools=(tool(lambda: effects.append(1) or SECRET),),
                                 result_transforms=(ResultTransform("fail", fail),
                                                    ResultTransform("later", lambda _: self.fail("must short circuit"))),
                                 session_dir=self.workspace / "logs")
                with Session(config) as session:
                    with self.assertRaises(ResultTransformError) as raised:
                        for event in session.stream("execute once"):
                            events.append(event)
                    self.assertNotIn(SECRET, str(raised.exception))
                    self.assertEqual(effects, [1])
                    self.assertEqual(len(config.model.client.completions.calls), 1)
                    self.assertEqual(len([e for e in events if e.kind == "tool.completed"]), 1)
                    self.assertNotIn(SECRET, str(events))
                    self.assertFalse(session._toolbox._spills)
                    history = str(session.snapshot().history)
                    self.assertIn("工具已执行", history)
                    self.assertNotIn(SECRET, history)
                    self.assertNotIn(SECRET, session.session_path.read_text(encoding="utf-8", errors="replace"))

    def test_execution_errors_remain_errors_after_redaction(self):
        config = options(self.workspace, [[call("lookup", {})], [chunk("done")]],
                         tools=(tool(lambda: ToolResult(SECRET, is_error=True)),),
                         result_transforms=(ResultTransform("hide", lambda _: "safe failure"),))
        with Session(config) as session:
            events = list(session.stream("try"))
        result = next(e for e in events if e.kind == "tool.completed")
        self.assertFalse(result.ok)
        self.assertEqual(result.output, "ERROR: safe failure")

    def test_handler_exceptions_are_processed_before_publication(self):
        def fail():
            raise RuntimeError(SECRET)
        config = options(self.workspace, [[call("native", {})], [chunk("done")]],
                         result_transforms=(ResultTransform("redact", redact),))
        with Session(config) as session:
            session._toolbox.register(KernelTool("native", "Native", {"type": "object"}, fail, False))
            events = list(session.stream("try"))
            self.assertNotIn(SECRET, str(events))
            self.assertIn("[removed]", str(events))

    def test_deny_and_pre_hook_do_not_run_transform_or_tool(self):
        for policy in ({"deny_rules": ("deny lookup",)},
                       {"hooks": (Hook("PreToolUse", lambda _: HookDecision(True, "blocked")),)}):
            config = options(self.workspace, [[call("lookup", {})], [chunk("done")]],
                             tools=(tool(lambda: self.fail("tool denied")),),
                             result_transforms=(ResultTransform("never", lambda _: self.fail("no result")),), **policy)
            with Session(config) as session:
                events = list(session.stream("blocked"))
            self.assertTrue(any(e.kind == "tool.denied" for e in events))

    def test_untrusted_wrapping_and_mcp_forwarder_name_matching_remain(self):
        config = options(self.workspace, [[call("use_tool", {"tool_name": "mcp__test__lookup"})], [chunk("done")]],
                         approver=lambda *_: True,
                         result_transforms=(ResultTransform("redact", redact, "mcp__test__lookup"),))
        with Session(config) as session:
            # A deterministic MCP transport view exercises the real use_tool route.
            session._toolbox._mcp = SimpleNamespace(
                ready_tools=lambda: [SimpleNamespace(name="mcp__test__lookup", check_fn=lambda: True, handler=lambda: SECRET)],
                take_media=lambda: [], server_states=lambda: {},
            )
            session._toolbox._mcp_search = True
            session._toolbox.notify_hook = None
            session._toolbox._register_mcp_search()
            events = list(session.stream("lookup"))
            self.assertNotIn(SECRET, str(events))
            history = str(session.snapshot().history)
            self.assertIn("untrusted_content", history)
            self.assertIn("[removed]", history)

    def test_child_tools_inherit_processing_before_child_model_and_archive(self):
        (self.workspace / "data.txt").write_text(SECRET, encoding="utf-8")
        config = options(self.workspace, [[call("read_file", {"path": "data.txt"})], [chunk("safe answer")]],
                         subagents=(Subagent("reader", "Read", "Read", ("read_file",)),),
                         result_transforms=(ResultTransform("redact", redact),),
                         session_dir=self.workspace / "logs")
        with Session(config) as session:
            task = session.tasks.submit((TaskSpec("job", "reader", "read"),))[0]
            self.assertEqual(task.wait(3).state, "succeeded")
            self.assertNotIn(SECRET, str(config.model.client.completions.calls))
        for path in (self.workspace / "logs").rglob("*.jsonl"):
            self.assertNotIn(SECRET, path.read_text(encoding="utf-8", errors="replace"))

    def test_model_delegation_keeps_parent_and_child_result_identity(self):
        seen = []
        (self.workspace / "data.txt").write_text(SECRET, encoding="utf-8")
        def process(output):
            seen.append((output.tool_name, output.tool_call_id))
            return redact(output)
        config = options(self.workspace, [[call("reader", {"task": "read"})],
                         [call("read_file", {"path": "data.txt"})], [chunk("safe answer")], [chunk("done")]],
                         subagents=(Subagent("reader", "Read", "Read", ("read_file",)),),
                         result_transforms=(ResultTransform("redact", process),))
        with Session(config) as session:
            session.run("delegate")
        self.assertEqual(seen, [("read_file", "read_file"), ("reader", "reader")])
        self.assertNotIn(SECRET, str(config.model.client.completions.calls))

    def test_failure_repairs_remaining_batch_calls_without_executing_them(self):
        effects = []
        batch = chunk(tool_calls=[call_fragment(i, str(i), "lookup", "{}") for i in range(2)])
        config = options(self.workspace, [[batch]], tools=(tool(lambda: effects.append(1) or SECRET),),
                         result_transforms=(ResultTransform("reject", lambda _: None),))
        with Session(config) as session:
            with self.assertRaises(ResultTransformError):
                session.run("batch")
            results = [m for m in session.snapshot().history if m.role == "tool"]
            self.assertEqual([m.tool_call_id for m in results], ["0", "1"])
            self.assertEqual(effects, [1])
            self.assertIn("not executed", results[1].text)

    def test_interrupted_partial_output_is_withheld(self):
        def stop():
            error = Interrupted()
            attach_partial(error, lambda: SECRET)
            raise error
        config = options(self.workspace, [[call("native", {})]], result_transforms=(ResultTransform("redact", redact),))
        with Session(config) as session:
            session._toolbox.register(KernelTool("native", "Native", {"type": "object"}, stop, False))
            result = session.run("read")
            self.assertTrue(result.interrupted)
            self.assertNotIn(SECRET, str(session.snapshot().history))

    def test_timeout_keeps_running_callback_owned_until_it_settles(self):
        entered, release = threading.Event(), threading.Event()
        def wait(output):
            entered.set()
            release.wait(3)
            return SECRET
        config = options(self.workspace, [[call("lookup", {})]], tools=(tool(lambda: SECRET),),
                         result_transforms=(ResultTransform("slow", wait),),
                         result_transform_timeout=0.03, close_timeout=0.03)
        session = Session(config)
        try:
            with self.assertRaises(ResultTransformError):
                session.run("lookup")
            self.assertTrue(entered.is_set())
            self.assertEqual(session.status(), "settling")
            with self.assertRaises(SessionBusyError):
                session.run("no second action")
            with self.assertRaises(CloseTimeoutError):
                session.close()
        finally:
            release.set()
            session.options = replace(session.options, close_timeout=3)
            session.close()

    def test_validation_and_sync_session_rejects_async_transform_before_execution(self):
        for setting in ({"result_transform_timeout": 0}, {"result_transform_timeout": True},
                        {"result_transform_timeout": float("nan")},
                        {"result_transforms": (ResultTransform("", redact),)},
                        {"result_transforms": (ResultTransform("x", redact), ResultTransform("x", redact))}):
            with self.assertRaises(ConfigurationError):
                Session(options(self.workspace, [], **setting))
        async def process(output):
            return output.text
        config = options(self.workspace, [], result_transforms=(ResultTransform("async", process),))
        with Session(config) as session:
            with self.assertRaises(ConfigurationError):
                session.run("no model request")
        self.assertEqual(config.model.client.completions.calls, [])


@unittest.skipUnless(importlib.util.find_spec("jsonschema"), "Install the sdk extra")
class AsyncTransformTests(unittest.IsolatedAsyncioTestCase):
    async def test_async_transform_runs_on_host_loop(self):
        loop = asyncio.get_running_loop()
        async def process(output):
            self.assertIs(asyncio.get_running_loop(), loop)
            await asyncio.sleep(0)
            return redact(output)
        with tempfile.TemporaryDirectory() as temporary:
            config = options(Path(temporary).resolve(), [[call("lookup", {})], [chunk("done")]],
                             tools=(tool(lambda: SECRET),), result_transforms=(ResultTransform("async", process),))
            async with AsyncSession(config) as session:
                events = [e async for e in session.stream("lookup")]
                self.assertNotIn(SECRET, str(events))
                self.assertNotIn(SECRET, str((await session.snapshot()).history))

    async def test_cancellation_withholds_result_and_waits_for_callback_cleanup(self):
        entered, settled = asyncio.Event(), asyncio.Event()
        async def process(output):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                settled.set()
        with tempfile.TemporaryDirectory() as temporary:
            config = options(Path(temporary).resolve(), [[call("lookup", {})]],
                             tools=(tool(lambda: SECRET),), result_transforms=(ResultTransform("async", process),))
            async with AsyncSession(config) as session:
                active = asyncio.create_task(session.run("lookup"))
                await asyncio.wait_for(entered.wait(), 2)
                active.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await active
                self.assertTrue(settled.is_set())
                self.assertNotIn(SECRET, str((await session.snapshot()).history))
