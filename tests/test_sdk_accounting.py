from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
import importlib.util
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "packages/xiaoyu-agent-sdk/src"))
from xiaoyu_agent_sdk import (BudgetExceededError, BudgetOptions, CloseTimeoutError, Hook, HookDecision,
    ModelOptions, ModelPrice, OutputSpec, Session, SessionOptions, SQLiteSessionStore, Subagent, TaskSpec, TelemetryOptions, Tool)
from tests.test_agent_paths import FakeClient, chunk, usage_chunk, text_response
from tests.test_sdk import call


class AccountingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.price = ModelPrice(1, 2, .1, "host-test-price-v1")

    def options(self, script, **kwargs):
        return SessionOptions(ModelOptions("test", client=FakeClient(script)), self.root, builtin_tools=(), **kwargs)

    def test_dollar_gate_and_cached_usage(self):
        usage = usage_chunk(1000, 500)
        usage.usage.prompt_tokens_details = SimpleNamespace(cached_tokens=200)
        opts = self.options([[chunk("done"), usage]], budget=BudgetOptions({"test": self.price}, max_usd=.001))
        with Session(opts) as session:
            session.run("once")
            self.assertEqual(session.cost.known_usd, "0.00182")
            self.assertEqual(session.cost.entries[0].cached_tokens, 200)
            with self.assertRaises(BudgetExceededError):
                session.run("over budget")
            self.assertEqual(len(opts.model.client.completions.calls), 1)

    def test_unknown_price_and_missing_usage_are_not_zero(self):
        with Session(self.options([], budget=BudgetOptions(max_usd=1))) as session:
            with self.assertRaises(BudgetExceededError):
                session.run("unknown price")
            self.assertEqual(session.cost.requests, 0)
        with Session(self.options([[chunk("no usage")]], budget=BudgetOptions({"test": self.price}, max_usd=1))) as session:
            session.run("missing")
            self.assertEqual(session.cost.unknown_requests, 1)
            self.assertIsNone(session.cost.entries[0].usd)
            with self.assertRaises(BudgetExceededError):
                session.run("cannot price next")

    def test_protocol_missing_usage_stays_unknown_after_normalization(self):
        from xiaoyu import messages, responses
        missing = (responses._usage(SimpleNamespace()), messages._usage(SimpleNamespace()),
                   list(messages.stream_chunks(iter([SimpleNamespace(type="message_stop")])))[-1].usage)
        for raw in missing:
            with self.subTest(usage=raw), Session(self.options([
                    [chunk("done"), SimpleNamespace(choices=[], usage=raw)]],
                    budget=BudgetOptions({"test": self.price}, max_usd=1))) as session:
                self.assertEqual(session.run("missing usage").text, "done")
                self.assertEqual(session.cost.unknown_requests, 1)
                self.assertIsNone(session.cost.entries[0].input_tokens)
                self.assertIsNone(session.cost.entries[0].usd)
                with self.assertRaises(BudgetExceededError):
                    session.run("no new request with unknown prior cost")

    def test_cache_creation_requires_explicit_price_and_counts_separately(self):
        from xiaoyu import messages
        raw = messages._usage(SimpleNamespace(input_tokens=5, output_tokens=1,
                    cache_read_input_tokens=2, cache_creation_input_tokens=3))
        for price, expected in ((self.price, None), (replace(self.price, cache_creation_per_million=3), "0.0000162")):
            with self.subTest(price=price), Session(self.options([
                    [chunk("done"), SimpleNamespace(choices=[], usage=raw)]],
                    budget=BudgetOptions({"test": price}, max_usd=1))) as session:
                session.run("cache accounting")
                entry = session.cost.entries[0]
                self.assertEqual((entry.input_tokens, entry.cached_tokens, entry.cache_creation_tokens), (10, 2, 3))
                self.assertEqual(entry.usd, expected)
                if expected is None:
                    with self.assertRaises(BudgetExceededError):
                        session.run("write-cache price unknown")

    def test_request_gate_is_shared_with_children_and_persisted(self):
        store = SQLiteSessionStore(self.root / "cost.sqlite")
        budget = BudgetOptions({"test": self.price}, max_requests=2)
        opts = self.options([[chunk("child"), usage_chunk(10, 2)], [chunk("parent"), usage_chunk(20, 3)]],
            budget=budget, session_store=store, subagents=(Subagent("worker", "work", "work", ()),))
        with Session(opts) as session:
            handle = session.tasks.submit((TaskSpec("child", "worker", "work"),))[0]
            self.assertEqual(handle.wait(5).state, "succeeded")
            session.run("parent")
            self.assertEqual(session.cost.requests, 2)
            self.assertEqual(session.cost.entries[0].task_id, handle.task_id)
            key = session.session_id
        with Session(replace(opts, model=ModelOptions("test", client=FakeClient([]))), resume_id=key) as session:
            with self.assertRaises(BudgetExceededError):
                session.run("restored gate")
        with Session(replace(opts, budget=replace(budget, history="reset"),
                             model=ModelOptions("test", client=FakeClient([[chunk("new")]]))), resume_id=key) as session:
            session.run("explicit new budget")
            self.assertEqual(session.cost.requests, 1)

    def test_concurrent_children_do_not_exceed_request_limit(self):
        opts = self.options([[chunk("one"), usage_chunk(10, 1)]], budget=BudgetOptions(max_requests=1),
            subagents=(Subagent("worker", "work", "work", ()),))
        with Session(opts) as session:
            handles = session.tasks.submit(tuple(TaskSpec(str(i), "worker", "work") for i in range(4)))
            states = [h.wait(5).state for h in handles]
            self.assertEqual(states.count("succeeded"), 1)
            self.assertEqual(len(opts.model.client.completions.calls), 1)

    def test_trace_is_content_free_and_spans_close_on_tool_failure(self):
        traces = []
        tool = Tool("broken", "test", {"type": "object"}, lambda: 1/0)
        opts = self.options([[call("broken", "{}")], [chunk("private-answer")]], tools=(tool,),
            approver=lambda *_: True, telemetry=TelemetryOptions(traces.append))
        with Session(opts) as session:
            session.run("private-prompt")
        self.assertEqual(len(traces), 1)
        spans = traces[0].spans
        self.assertTrue({"agent.run", "model.request", "tool.call", "tool.approval"} <= {s.name for s in spans})
        self.assertTrue(all(s.end_ns >= s.start_ns for s in spans))
        self.assertTrue(any(s.name == "tool.call" and s.failed for s in spans))
        self.assertNotIn("private-prompt", repr(traces))
        self.assertNotIn("private-answer", repr(traces))
        ids = {s.span_id for s in spans}
        self.assertTrue(all(not s.parent_id or s.parent_id in ids for s in spans))

    def test_stream_cost_and_trace_correlate_requests_and_tool_calls(self):
        traces = []
        tool = Tool("echo", "test", {"type": "object"}, lambda: "ok")
        with Session(self.options([[call("echo", "{}"), usage_chunk(10, 2)], [chunk("done"), usage_chunk(20, 3)]],
                tools=(tool,), approver=lambda *_: True, telemetry=TelemetryOptions(traces.append))) as session:
            events = list(session.stream("go"))
            entries = session.cost.entries
            key = session.session_id
        requests = [e for e in events if e.kind == "request.started"]
        self.assertEqual({e.request_id for e in requests}, {e.request_id for e in entries})
        self.assertTrue(all(e.session_id == key and e.run_id == requests[0].run_id for e in events))
        tool_events = [e for e in events if e.kind in {"tool.pending", "tool.running", "tool.completed"}]
        self.assertTrue(tool_events)
        self.assertTrue(all(e.tool_call_id and e.tool_call_id == tool_events[0].tool_call_id for e in tool_events))
        self.assertEqual(tool_events[0].to_dict()["tool_call_id"], tool_events[0].tool_call_id)
        spans = traces[0].spans
        self.assertEqual({s.attributes["request_id"] for s in spans if s.name == "model.request"}, {e.request_id for e in entries})
        self.assertEqual(next(s.attributes["tool_call_id"] for s in spans if s.name == "tool.call"), tool_events[0].tool_call_id)

    def test_fork_includes_cost_unless_host_explicitly_resets(self):
        opts = self.options([[chunk("done"), usage_chunk(10, 2)]], budget=BudgetOptions(max_requests=1))
        with Session(opts) as parent:
            parent.run("initial")
            with parent.fork() as child:
                self.assertEqual(child.cost.entries, parent.cost.entries)
                with self.assertRaises(BudgetExceededError):
                    child.run("blocked")
            reset = replace(opts, budget=BudgetOptions(max_requests=1, history="reset"),
                            model=ModelOptions("test", client=FakeClient([[chunk("new")]])))
            with parent.fork(options=reset) as child:
                self.assertEqual(child.cost.requests, 0)
                self.assertEqual(child.run("new budget").text, "new")

    def test_summary_and_output_repair_share_request_gate_and_accounting(self):
        seen = []
        opts = self.options([text_response("FACT-42 " * 50),
            [call("structured_output", '{"value":"bad"}'), usage_chunk(10, 2)],
            [call("structured_output", '{"value":42}'), usage_chunk(20, 3)]],
            budget=BudgetOptions({"test": self.price}, max_requests=3),
            hooks=tuple(Hook(name, lambda event: seen.append(event) or HookDecision(False))
                        for name in ("BeforeCompact", "AfterCompact")))
        with Session(opts) as session:
            session._agent.messages.extend(
                {"role": role, "content": "FACT-42 " * 500}
                for _ in range(20) for role in ("user", "assistant"))
            session._executor.submit(session._agent.maybe_compact, True).result()
            self.assertEqual(session.cost.requests, 1)
            self.assertEqual([e["event"] for e in seen], ["BeforeCompact", "AfterCompact"])
            self.assertTrue(seen[-1]["changed"])
            result = session.run("Return 42", output=OutputSpec({"type": "integer"}))
            self.assertEqual((result.output, result.output_retries), (42, 1))
            self.assertEqual(session.cost.requests, 3)
            self.assertEqual(sum(e.input_tokens for e in session.cost.entries), 130)
            with self.assertRaises(BudgetExceededError):
                session.run("No fourth request")

    def test_compaction_pre_hook_failure_preserves_history_and_skips_model(self):
        def failed(event):
            raise ValueError("private hook error")
        with Session(self.options([], hooks=(Hook("BeforeCompact", failed),))) as session:
            original = list(session._agent.messages)
            with self.assertRaises(RuntimeError):
                session._executor.submit(session._agent.maybe_compact, True).result()
            self.assertEqual(session._agent.messages, original)
            self.assertEqual(session.options.model.client.completions.calls, [])

    def test_failure_notification_exceptions_do_not_hide_tool_result(self):
        def failed(event):
            raise ValueError("private hook error")
        tool = Tool("broken", "test", {"type": "object"}, lambda: 1/0)
        with Session(self.options([[call("broken", "{}")], [chunk("handled")]], tools=(tool,),
                approver=lambda *_: True, hooks=(Hook("ToolFailed", failed),))) as session:
            self.assertEqual(session.run("go").text, "handled")
            self.assertIn("ToolFailed", session.hook_errors)

    def test_slow_exporter_backpressure_and_close_retry(self):
        entered, release = threading.Event(), threading.Event()
        def export(record):
            entered.set()
            release.wait(5)
        session = Session(self.options([[chunk("done")]] * 5, telemetry=TelemetryOptions(export, queue_size=1), close_timeout=.02))
        try:
            session.run("first")
            self.assertTrue(entered.wait(5))
            for _ in range(4):
                session.run("continues")
            self.assertGreater(session.telemetry_status["dropped"], 0)
            with self.assertRaises(CloseTimeoutError):
                session.close()
            self.assertFalse(session.closed)
        finally:
            release.set()
            session.options = replace(session.options, close_timeout=5)
            session.close()

    def test_exporter_failure_never_breaks_execution(self):
        def fail(record):
            raise RuntimeError("exporter secret")
        with Session(self.options([[chunk("done")]], telemetry=TelemetryOptions(fail))) as session:
            self.assertEqual(session.run("go").text, "done")
        self.assertEqual(session.telemetry_status["failures"], 1)

    @unittest.skipUnless(importlib.util.find_spec("opentelemetry"), "Install SDK telemetry extra and opentelemetry-sdk")
    def test_real_opentelemetry_parent_spans_and_borrowed_provider(self):
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import SimpleSpanProcessor
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
        from opentelemetry import trace
        from xiaoyu_agent_sdk import OpenTelemetryExporter
        global_before = trace.get_tracer_provider()
        provider = TracerProvider()
        exporter = InMemorySpanExporter()
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        self.addCleanup(provider.shutdown)
        with Session(self.options([[chunk("done")]], telemetry=TelemetryOptions(OpenTelemetryExporter(provider)))) as session:
            session.run("private")
        spans = exporter.get_finished_spans()
        root = next(s for s in spans if s.name == "agent.run")
        request = next(s for s in spans if s.name == "model.request")
        self.assertEqual(request.parent.span_id, root.context.span_id)
        self.assertEqual(request.context.trace_id, root.context.trace_id)
        self.assertIs(trace.get_tracer_provider(), global_before)
        with provider.get_tracer("host").start_as_current_span("still-owned-by-host"):
            pass
        self.assertEqual(len(exporter.get_finished_spans()), len(spans) + 1)

    def test_extended_hooks_have_order_identity_and_fail_closed_start(self):
        seen = []
        def hook(event):
            seen.append(event)
            return HookDecision(False)
        names = ("SessionStart", "SubagentStart", "SubagentEnd", "SessionEnd")
        with Session(self.options([[chunk("done")]], subagents=(Subagent("worker", "work", "work", ()),),
             hooks=tuple(Hook(name, hook) for name in names))) as session:
            handle = session.tasks.submit((TaskSpec("a", "worker", "work"),))[0]
            handle.wait(5)
            key = session.session_id
        self.assertEqual([e["event"] for e in seen], list(names))
        self.assertTrue(all(e["session_id"] == key for e in seen))
        opts = self.options([], hooks=(Hook("SessionStart", lambda _: HookDecision(True)),))
        with Session(opts) as session:
            with self.assertRaises(Exception):
                session.run("blocked")
            self.assertEqual(opts.model.client.completions.calls, [])
