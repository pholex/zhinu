"""流完成态、确定性拒绝及压缩/编辑诊断的回归。不访问网络。"""
from __future__ import annotations

import contextlib
import copy
import io
import json
import unittest
from types import SimpleNamespace as N
from unittest import mock

import httpx2
import openai

from xiaoyu import responses
from xiaoyu.compaction import Compactor
from xiaoyu.errors import StreamFailed, classify
from xiaoyu.session_inspect import inspect_session
from xiaoyu.session_log import SessionLog
from . import test_agent_paths, test_context, test_responses, test_tools


class ContentRejectionTest(test_agent_paths.AgentTestCase):
    def test_stream_rejection_never_retries_or_falls_back(self):
        self.config.fallback_models = ["backup"]
        agent = self.build([StreamFailed("data_inspection_failed: request rejected")])
        with mock.patch("xiaoyu.agent.Agent._sleep") as sleep, contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(StreamFailed):
                agent.send("hi")
        sleep.assert_not_called()
        self.assertEqual(len(self.client.completions.calls), 1)

    def test_structured_and_stream_codes(self):
        for code in ("data_inspection_failed", "content_filter", "ResponsibleAIPolicyViolation"):
            structured = openai.APIError("rejected", httpx2.Request("POST", "http://unused"),
                                         body={"error": {"code": code}})
            for exc in (structured, StreamFailed(f"{code}: rejected")):
                with self.subTest(code=code, kind=type(exc)):
                    verdict = classify(exc)
                    self.assertEqual(verdict.kind, "fatal")
                    self.assertFalse(verdict.retryable or verdict.should_compact)

    def test_configuration_and_transient_errors_are_not_refusals(self):
        for text in ("content_filter_results missing", "safety service overloaded", "policy config unavailable"):
            self.assertEqual(classify(StreamFailed(text)).kind, "transient")


class CompletedCallsTest(unittest.TestCase):
    collect = test_responses.TestStreamTranslation.collect

    @staticmethod
    def frame(kind, index=0, **fields):
        return N(type=kind, output_index=index, **fields)

    def added(self, index=0, call_id="", item_id="start"):
        return self.frame("response.output_item.added", index,
                          item=N(type="function_call", id=item_id, call_id=call_id, name="read_file"))

    def done(self, index=0, call_id="final", arguments='{"path":"a"}'):
        return self.frame("response.output_item.done", index,
                          item=N(type="function_call", id=f"end{index}", call_id=call_id,
                                 name="read_file", arguments=arguments))

    def test_late_id_and_missing_argument_suffix_are_completed_once(self):
        done = self.done()
        events = [self.added(), self.frame("response.function_call_arguments.delta", delta='{"pa'),
                  done, done, N(type="response.completed", response=N(output=[done.item], usage=None))]
        _, calls, _ = self.collect(events)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["id"], "final")
        self.assertEqual(calls[0]["function"]["arguments"], '{"path":"a"}')

    def test_interleaved_calls_and_reversed_done_keep_slots(self):
        _, calls, _ = self.collect([self.added(0, item_id="a"), self.added(1, item_id="b"),
                                   self.done(1, "second", '{"path":"b"}'), self.done(0, "first")])
        self.assertEqual([calls[i]["id"] for i in sorted(calls)], ["first", "second"])
        self.assertEqual(json.loads(calls[1]["function"]["arguments"]), {"path": "b"})

    def test_final_output_without_added_preserves_full_output_index(self):
        _, calls, _ = self.collect([N(type="response.completed", response=N(
            output=[N(type="reasoning"), self.done().item], usage=None))])
        self.assertEqual(list(calls), [1])
        self.assertEqual(calls[1]["id"], "final")

    def test_arguments_done_without_index_uses_item_identity(self):
        _, calls, _ = self.collect([self.added(call_id="final"),
            N(type="response.function_call_arguments.done", item_id="start", arguments='{"path":"a"}')])
        self.assertEqual(calls[0]["function"]["arguments"], '{"path":"a"}')

    def test_conflicting_completion_cannot_silently_change_action(self):
        for tail in (self.done(call_id="different"), self.done(arguments='{"path":"b"}')):
            with self.subTest(tail=tail), self.assertRaises(StreamFailed):
                self.collect([self.added(call_id="final"),
                              self.frame("response.function_call_arguments.delta", delta='{"path":"a"}'), tail])

    def test_same_call_id_cannot_create_two_actions(self):
        with self.assertRaises(StreamFailed):
            self.collect([self.added(0, "same", "a"), self.added(1, "same", "b")])


class CompressionDiagnosticsTest(test_agent_paths.AgentTestCase):
    def test_outcome_is_per_attempt_and_failed_candidates_keep_history(self):
        compactor = Compactor(1000, .7, 4, lambda _t, _p: "摘要", user_voice_tokens=0)
        original = test_context.single_turn_conversation(rounds=8)
        original[3]["content"] *= 100
        baseline = copy.deepcopy(original)
        result, _ = compactor.compact(original)
        self.assertIsNot(result, original)
        self.assertEqual(compactor.last_attempt["outcome"], "committed")
        self.assertGreater(compactor.last_attempt["tokens_reclaimed"], 0)
        for summary, outcome, reason in (("x" * 200000, "skipped", "would_grow"),
                                         ("", "failed", "empty_summary")):
            compactor.summarizer = lambda _t, _p: summary
            result, _ = compactor.compact(original)
            self.assertIs(result, original)
            self.assertEqual(original, baseline)
            self.assertEqual(compactor.last_attempt["outcome"], outcome)
            self.assertEqual(compactor.last_attempt["reason"], reason)
            self.assertEqual(compactor.last_attempt["tokens_reclaimed"], 0)
        compactor.compact([{"role": "system", "content": "s"}])
        self.assertEqual(compactor.last_attempt["reason"], "no_history")
        self.assertNotIn("summary", compactor.last_attempt)

    def test_inspector_filters_skips_but_keeps_real_and_legacy_failures(self):
        path = self.root / "events.jsonl"
        records = [{"event": "compact_end", "ok": False, "outcome": "skipped"},
                   {"event": "compact_end", "ok": False, "outcome": "failed"},
                   {"event": "compact_end", "ok": False}]
        path.write_text("\n".join(json.dumps(r) for r in records), encoding="utf-8")
        rows = inspect_session(path, errors_only=True)["rows"]
        self.assertEqual([r["line"] for r in rows], [2, 3])

    def test_agent_persists_structured_attempt(self):
        path = self.root / "events.jsonl"
        log = SessionLog(path)
        self.addCleanup(log.release)
        agent = self.build([], session_log=log)
        with contextlib.redirect_stdout(io.StringIO()):
            agent.maybe_compact(force=True)
        records = [json.loads(line) for line in path.read_text(encoding="utf-8", errors="replace").splitlines()]
        end = next(r for r in records if r.get("event") == "compact_end")
        self.assertEqual(end["outcome"], "skipped")
        self.assertEqual(end["token_count_method"], "estimate")
        self.assertGreaterEqual(end["duration_ms"], 0)


class EscapeDiagnosticsTest(test_tools.ToolboxTestCase):
    def test_suspected_escaping_reports_hint_without_writing(self):
        for original, anchor in [(r'path = "C:\tmp"', r'path = "C:\\tmp"'),
                                 ('say("hello")', r'say(\"hello\")')]:
            with self.subTest(anchor=anchor):
                self.sample.write_text(original, encoding="utf-8")
                self.box.run("read_file", {"path": "calc.py"})
                result = self.box.run("str_replace", {"path": "calc.py", "old_str": anchor,
                                                       "new_str": r'keep = "\\n"'})
                self.assertIn("转义诊断", result)
                self.assertTrue(result.startswith("ERROR:"))
                self.assertEqual(self.sample.read_text(encoding="utf-8"), original)

    def test_exact_backslashes_are_preserved(self):
        original = r'pattern = "\\d+"'
        replacement = r'pattern = "\\w+"'
        self.sample.write_text(original, encoding="utf-8")
        self.box.run("read_file", {"path": "calc.py"})
        result = self.box.run("str_replace", {"path": "calc.py", "old_str": original, "new_str": replacement})
        self.assertNotIn("转义诊断", result)
        self.assertEqual(self.sample.read_text(encoding="utf-8"), replacement)
