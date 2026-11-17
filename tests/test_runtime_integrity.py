"""路径、结果保真与流生命周期的故障回归；全部离线。"""

from __future__ import annotations

import contextlib
import io
import json
import os
import unittest
from unittest import mock

from xiaoyu import compaction, mcp, streaming
from xiaoyu.agent import _omitted_instruction_headings, collect_project_docs
from xiaoyu.config import Config
from xiaoyu.errors import StreamLifetimeExceeded, classify
from xiaoyu.tools import Tool, Toolbox
from .test_agent_paths import AgentTestCase, GOOD_SUMMARY, chunk, text_response
from . import test_builtin_extensions as patch_tests

change = patch_tests.change


class PatchLinkTest(unittest.TestCase):
    setUp = patch_tests.PatchTest.setUp
    file = patch_tests.PatchTest.file
    patch = patch_tests.PatchTest.patch
    @unittest.skipIf(os.name == "nt", "需要符号链接权限")
    def test_delete_link_keeps_target_and_entire_batch_untouched(self):
        target = self.file("target")
        link = self.root / "link"
        link.symlink_to("target")
        self.box.run("read_file", {"path": "link"})
        result = self.patch(change("new", "@@ -0,0 +1 @@\n+new\n", "create"),
                            change("link", operation="delete"))
        self.assertIn("符号链接", result)
        self.assertTrue(link.is_symlink())
        self.assertEqual(target.read_bytes(), b"old\n")
        self.assertFalse((self.root / "new").exists())

    @unittest.skipIf(os.name == "nt", "需要符号链接权限")
    def test_create_refuses_dangling_link_and_update_preserves_link(self):
        link = self.root / "link"
        link.symlink_to("missing")
        result = self.patch(change("link", "@@ -0,0 +1 @@\n+new\n", "create"))
        self.assertIn("符号链接", result)
        self.assertFalse((self.root / "missing").exists())
        self.file("missing")
        self.assertNotIn("ERROR", self.patch(change("link", "@@ -1 +1 @@\n-old\n+new\n")))
        self.assertTrue(link.is_symlink())
        self.assertEqual((self.root / "missing").read_bytes(), b"new\n")

    @unittest.skipIf(os.name == "nt", "需要符号链接权限")
    def test_entry_changed_after_validation_is_refused(self):
        target = self.file("target")
        self.file("first")
        original = self.box.rewind.written

        def replace_entry(path, content):
            original(path, content)
            if path.name == "first":
                target.rename(self.root / "saved")
                target.symlink_to("saved")

        with mock.patch.object(self.box.rewind, "written", side_effect=replace_entry):
            result = self.patch(change("first", operation="delete"), change("target", operation="delete"))
        self.assertIn("ERROR", result)
        self.assertEqual((self.root / "saved").read_bytes(), b"old\n")
        self.assertTrue(target.is_symlink())


class McpContentTest(unittest.TestCase):
    def test_structured_data_survives_summary_blank_and_nontext_parts(self):
        data = {"rows": [{"id": 42, "value": "结果"}]}
        for content in ([{"type": "text", "text": "查询成功"}],
                        [{"type": "text", "text": ""}],
                        [{"type": "audio"}], []):
            with self.subTest(content=content):
                text, _ = mcp._render_result({"content": content, "structuredContent": data})
                self.assertIn('"id": 42', text)

    def test_only_exact_json_data_is_deduplicated(self):
        data = {"x": 1}
        for text in ('{"x":1}', '{ "x" : 1 }'):
            result, _ = mcp._render_result({"content": [{"type": "text", "text": "结果"},
                                                       {"type": "text", "text": text}],
                                          "structuredContent": data})
            self.assertEqual(result, "结果\n" + text)
        result, _ = mcp._render_result({"content": [{"type": "text", "text": '{"x":2}'}],
                                      "structuredContent": data})
        self.assertIn('"x": 1', result)
        self.assertIn('"x":2', result)
        result, _ = mcp._render_result({"content": [{"type": "text", "text": '{"x":true}'}],
                                      "structuredContent": data})
        self.assertIn('"x": 1', result)
        self.assertIn('"x":true', result)

    def test_structured_error_keeps_error_classification(self):
        text, images = mcp._render_result({"isError": True, "content": [{"type": "text", "text": "失败"}],
                                          "structuredContent": {"reason": "missing argument"}})
        self.assertTrue(text.startswith("ERROR:"))
        self.assertIn("missing argument", text)
        self.assertEqual(images, [])


class StreamLimitTest(AgentTestCase):
    def test_read_timeout_after_total_deadline_does_not_retry(self):
        clock = [0.0]
        def source():
            clock[0] = 20.0
            raise TimeoutError("socket read timed out")
            yield  # 保持迭代器，异常来自 next 而非构造。
        with mock.patch("xiaoyu.streaming.time.monotonic", side_effect=lambda: clock[0]):
            with streaming.stream_scope(5):
                with self.assertRaises(StreamLifetimeExceeded):
                    next(streaming.iter_stream(source()))

    def test_native_protocol_adapters_check_ignored_events_and_release_source(self):
        from types import SimpleNamespace
        from .test_responses import FakeResponses, responses_transport
        from .test_messages import FakeMessagesAPI, anthropic_transport

        for protocol in ("responses", "messages"):
            with self.subTest(protocol=protocol):
                clock = [0.0]
                closed = []
                def raw():
                    try:
                        for at in (1.0, 4.0, 7.0):
                            clock[0] = at
                            yield SimpleNamespace(type="ping")
                    finally:
                        closed.append(True)
                api = FakeResponses() if protocol == "responses" else FakeMessagesAPI()
                api.create = mock.Mock(return_value=raw())
                client = responses_transport(api) if protocol == "responses" else anthropic_transport(api)[0]
                with mock.patch("xiaoyu.streaming.time.monotonic", side_effect=lambda: clock[0]):
                    with streaming.stream_scope(5):
                        stream = client.chat.completions.create(model="model", messages=[], stream=True)
                        with self.assertRaises(StreamLifetimeExceeded):
                            list(stream)
                self.assertEqual(closed, [True])

    def test_deadline_before_first_read_closes_already_opened_transport(self):
        source = mock.MagicMock()

        def convert(events):
            yield from events

        with mock.patch("xiaoyu.streaming.time.monotonic", side_effect=[0, 10]):
            with streaming.stream_scope(5):
                with self.assertRaises(StreamLifetimeExceeded):
                    next(streaming.iter_stream(streaming.translate_stream(source, convert)))
        source.close.assert_called_once_with()
        source.__iter__.assert_not_called()

    def test_partial_tool_call_is_not_executed_at_deadline(self):
        from .test_agent_paths import call_fragment

        clock = [0.0]
        def source():
            yield chunk(tool_calls=[call_fragment(0, "write", "write_file", '{"path":"unexpected",')])
            clock[0] = 9.0
            yield chunk(tool_calls=[call_fragment(0, None, None, '"content":"oops"}')])
        self.config.stream_max_lifetime = 5
        agent = self.build([source()])
        with mock.patch("xiaoyu.streaming.time.monotonic", side_effect=lambda: clock[0]), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(StreamLifetimeExceeded):
                agent.send("开始")
        self.assertFalse((self.root / "unexpected").exists())
        self.assertFalse(any(m.get("tool_calls") for m in agent.messages))

    def test_partial_text_is_saved_stream_closed_and_not_retried(self):
        clock = [10.0]
        closed = []

        def source():
            try:
                yield chunk(content="已经输出")
                clock[0] = 20.0
                yield chunk(content="不应输出")
            finally:
                closed.append(True)

        self.config.stream_max_lifetime = 5
        agent = self.build([source()])
        with mock.patch("xiaoyu.streaming.time.monotonic", side_effect=lambda: clock[0]), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(StreamLifetimeExceeded) as caught:
                agent.send("开始")
        self.assertEqual(closed, [True])
        self.assertEqual(len(self.client.completions.calls), 1)
        self.assertIn("已经输出", agent.last_assistant_text())
        self.assertNotIn("不应输出", agent.last_assistant_text())
        self.assertFalse(classify(caught.exception).retryable)

    def test_ignored_wire_events_share_deadline_and_close(self):
        clock = [0.0]
        closed = []

        def raw():
            try:
                for at in (1.0, 3.0, 6.0, 8.0):
                    clock[0] = at
                    yield {"type": "ping"}
            finally:
                closed.append(True)

        def ignore(events):
            for event in events:
                if event["type"] != "ping":
                    yield event

        with mock.patch("xiaoyu.streaming.time.monotonic", side_effect=lambda: clock[0]):
            with streaming.stream_scope(5):
                with self.assertRaises(StreamLifetimeExceeded):
                    list(streaming.translate_stream(raw(), ignore))
        self.assertEqual(closed, [True])
        self.assertEqual(clock[0], 6)

    def test_disabled_and_new_attempt_have_independent_clocks(self):
        with mock.patch("xiaoyu.streaming.time.monotonic", side_effect=[0, 100]):
            with streaming.stream_scope(0):
                self.assertEqual(list(streaming.iter_stream([1, 2])), [1, 2])
        for start in (0.0, 100.0):
            with mock.patch("xiaoyu.streaming.time.monotonic", return_value=start):
                with streaming.stream_scope(5):
                    self.assertEqual(list(streaming.iter_stream([1])), [1])

    def test_environment_defaults_and_disable(self):
        with mock.patch.dict(os.environ, {"XIAOYU_STREAM_MAX_LIFETIME": "7.5", "XIAOYU_SUMMARY_RETRY_COOLDOWN": "0"}):
            config = Config.from_env()
        self.assertEqual(config.stream_max_lifetime, 7.5)
        self.assertEqual(config.summary_retry_cooldown, 0)
        for value in ("-1", "nan", "inf"):
            with mock.patch.dict(os.environ, {"XIAOYU_STREAM_MAX_LIFETIME": value}):
                self.assertEqual(Config.from_env().stream_max_lifetime, 0)


class SummaryCooldownTest(AgentTestCase):
    def test_failed_route_is_skipped_until_recovery_probe(self):
        agent = self.build([RuntimeError("unavailable"), text_response(GOOD_SUMMARY),
                            text_response(GOOD_SUMMARY), text_response(GOOD_SUMMARY)])
        with contextlib.redirect_stdout(io.StringIO()), mock.patch("xiaoyu.agent.time.monotonic", return_value=100):
            agent._summarize("history")
            agent._summarize("history")
        with contextlib.redirect_stdout(io.StringIO()), mock.patch("xiaoyu.agent.time.monotonic", return_value=161):
            agent._summarize("history")
        self.assertEqual([c["model"] for c in self.client.completions.calls],
                         ["cheap-model", "main-model", "main-model", "cheap-model"])
        self.assertFalse(agent._summary_retry_at)
        self.assertEqual(agent.compactor.state.failures, 0)

    def test_total_failure_still_allows_smaller_transcript_retry(self):
        agent = self.build([RuntimeError("bad"), RuntimeError("bad"), text_response(GOOD_SUMMARY)])
        with contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(RuntimeError):
                agent._summarize("too large")
            self.assertEqual(agent._summarize("smaller"), GOOD_SUMMARY)
        self.assertEqual(self.client.completions.calls[-1]["model"], "cheap-model")

    def test_manual_compaction_clears_cooldown(self):
        agent = self.build([])
        agent._summary_retry_at[("gateway", "cheap-model")] = float("inf")
        with mock.patch.object(agent.compactor, "compact", return_value=(agent.messages, "no history")), contextlib.redirect_stdout(io.StringIO()):
            agent.maybe_compact(force=True)
        self.assertFalse(agent._summary_retry_at)


class ContextCoverageTest(AgentTestCase):
    def test_compaction_journal_records_transcript_coverage_and_route(self):
        from xiaoyu.session_log import SessionLog

        agent = self.build([text_response(GOOD_SUMMARY)])
        agent.compactor.transcript_cap = 1000
        for index in range(20):
            agent.messages.append({"role": "user", "content": f"记录 {index} " + "历史事实" * 500})
        log = SessionLog.create(str(self.root), self.config.model, directory=self.root / "logs")
        self.addCleanup(log.release)
        agent.session_log = log
        with contextlib.redirect_stdout(io.StringIO()):
            agent.maybe_compact(force=True)
        records = [json.loads(line) for line in log.path.read_text(encoding="utf-8", errors="replace").splitlines()]
        record = next(row for row in records if row.get("event") == "compact_end")
        self.assertEqual(record["summary_input_mode"], "transcript")
        self.assertEqual(record["summary_route"], "gateway/cheap-model")
        self.assertGreater(record["transcript_omitted_records"], 0)
        self.assertEqual(record["transcript_chars"], record["transcript_sampled_chars"] + record["transcript_omitted_chars"])

    def test_omitted_headings_exclude_fenced_code_and_point_to_file(self):
        path = self.root / "AGENTS.md"
        path.write_text("intro\n```python\n# fake\n```\n## 重要规则\ntext\n", encoding="utf-8")
        with mock.patch("xiaoyu.agent.project_levels", return_value=[self.root]):
            docs = collect_project_docs(self.root, ("AGENTS.md",), cap=6)
        self.assertIn("重要规则", docs[0][1])
        self.assertNotIn("fake", docs[0][1])
        self.assertIn("read_file：AGENTS.md", docs[0][1])

    def test_coverage_counts_dropped_and_partial_records_without_content(self):
        records = ["甲" * 100, "乙" * 100, "丙" * 100]
        report = compaction.transcript_coverage(records, 80)
        self.assertEqual(report["transcript_chars"], 302)
        self.assertEqual(report["transcript_sampled_records"], 2)
        self.assertEqual(report["transcript_partial_records"], 2)
        self.assertEqual(report["transcript_omitted_records"], 1)
        self.assertEqual(report["transcript_sampled_chars"] + report["transcript_omitted_chars"], 302)
        self.assertNotIn("甲", json.dumps(report))
        for cap in [*range(100), 400]:
            self.assertLessEqual(len(compaction.clamp("\n".join(records), cap)), cap)


class PluginAdvertisementTest(AgentTestCase):
    def test_probe_cache_starts_when_slow_probe_returns(self):
        clock = [100.0]
        calls = []
        def probe():
            calls.append(True)
            clock[0] += 10
            return False
        tool = Tool("slow", "external", {}, lambda: "unused", check_fn=probe)
        self.config.enable_plugins = True
        with mock.patch("xiaoyu.tools.load_plugin_tools", return_value=[tool]):
            box = Toolbox(self.config)
        self.addCleanup(box.tasks.shutdown)
        with mock.patch("xiaoyu.tools.time.monotonic", side_effect=lambda: clock[0]):
            box.schemas()
            clock[0] += 1
            box.schemas()
        self.assertEqual(len(calls), 1)

    def test_plugin_flaps_leave_prefix_stable_but_execution_fails_closed(self):
        ready = [False]
        calls = []
        handler = mock.Mock(return_value="ran")

        def probe():
            calls.append(True)
            return ready[0]

        tool = Tool("external", "external dependency", {}, handler, check_fn=probe)
        self.config.enable_plugins = True
        with mock.patch("xiaoyu.tools.load_plugin_tools", return_value=[tool]):
            box = Toolbox(self.config)
        self.addCleanup(box.tasks.shutdown)
        with mock.patch("xiaoyu.tools.time.monotonic", return_value=100):
            self.assertNotIn("external", [s["function"]["name"] for s in box.schemas()])
            box.schemas()
        self.assertEqual(len(calls), 1)
        ready[0] = True
        with mock.patch("xiaoyu.tools.time.monotonic", return_value=106):
            before = box.schemas()
        ready[0] = False
        self.assertEqual(before, box.schemas())
        self.assertIn("ERROR", box.run("external", {}))
        handler.assert_not_called()

    def test_builtin_dynamic_check_stays_live(self):
        ready = [False]
        box = Toolbox(self.config, only=["read_file"])
        box.register(Tool("dynamic", "dynamic", {}, lambda: "ok", check_fn=lambda: ready[0]))
        self.assertNotIn("dynamic", [s["function"]["name"] for s in box.schemas()])
        ready[0] = True
        self.assertIn("dynamic", [s["function"]["name"] for s in box.schemas()])
        ready[0] = False
        self.assertNotIn("dynamic", [s["function"]["name"] for s in box.schemas()])
