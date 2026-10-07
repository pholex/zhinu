"""轮末「接着问」建议（suggest.py）与 Agent 的接线。"""

from __future__ import annotations

import contextlib
import io
import unittest

from xiaoyu import suggest
from xiaoyu.events import Suggestions, UIEvent

from .test_agent_paths import AgentTestCase, chunk, text_response


class TestParse(unittest.TestCase):
    def test_json_array_is_the_main_path(self) -> None:
        self.assertEqual(suggest.parse('["跑一遍测试", "推到远端"]'), ["跑一遍测试", "推到远端"])

    def test_tolerates_code_fence_and_wrapper_object(self) -> None:
        self.assertEqual(suggest.parse('```json\n["a", "b"]\n```'), ["a", "b"])
        self.assertEqual(suggest.parse('{"suggestions": ["x"]}'), ["x"])

    def test_falls_back_to_bullet_lines(self) -> None:
        text = "- 跑测试\n2. 写发版说明\n① 推 main"
        self.assertEqual(suggest.parse(text), ["跑测试", "写发版说明", "推 main"])

    def test_caps_count_length_and_dedupes(self) -> None:
        items = suggest.parse('["a", "a", "b", "c", "d"]')
        self.assertEqual(items, ["a", "b", "c"])
        long = "x" * 100
        self.assertEqual(len(suggest.parse(f'["{long}"]')[0]), suggest.MAX_CHARS)

    def test_strips_controls_quotes_and_empties(self) -> None:
        self.assertEqual(suggest.parse('["\\u001b[31m红", "“引号”", "   "]'), ["[31m红", "引号"])

    def test_garbage_yields_nothing(self) -> None:
        self.assertEqual(suggest.parse(""), [])
        self.assertEqual(suggest.parse("[]"), [])
        self.assertEqual(suggest.parse("null"), [])


class TestBuildMessages(unittest.TestCase):
    def test_single_user_message_with_both_sides_clipped(self) -> None:
        messages = suggest.build_messages("问" * 5000, "答" * 9000)
        self.assertEqual(len(messages), 1)
        content = messages[0]["content"]
        self.assertIn("【用户说】", content)
        self.assertIn("【助手回答】", content)
        self.assertIn("（中略）", content)
        self.assertLess(len(content), 6000)

    def test_render_line(self) -> None:
        self.assertEqual(suggest.render_line(["a", "b"]), "接着问：① a  ② b")


class Recorder:
    def __init__(self) -> None:
        self.events: list[UIEvent] = []

    def emit(self, event: UIEvent) -> None:
        self.events.append(event)


class TestAgentWiring(AgentTestCase):
    def _join(self, agent) -> None:
        thread = agent._suggest_thread
        if thread is not None:
            thread.join(timeout=10)
            self.assertFalse(thread.is_alive(), "建议线程没在限时内回来")

    def test_suggest_next_uses_cheap_route_and_records_usage(self) -> None:
        agent = self.build([[chunk(content="改好了")], text_response('["跑测试", "提交"]', 300, 12)])
        with contextlib.redirect_stdout(io.StringIO()):
            agent.send("把 bug 修了")
        self.assertEqual(agent.suggest_next(), ["跑测试", "提交"])
        request = self.client.completions.calls[-1]
        self.assertEqual(request["model"], "cheap-model")
        self.assertNotIn("tools", request)
        self.assertIn("把 bug 修了", request["messages"][0]["content"])
        self.assertIn("改好了", request["messages"][0]["content"])
        by_model = agent.usage.to_dict()["by_model"]
        self.assertTrue(any(key.endswith("/cheap-model") for key in by_model), by_model)

    def test_suggest_next_swallows_failures(self) -> None:
        agent = self.build([[chunk(content="好")], RuntimeError("down"), RuntimeError("down")])
        with contextlib.redirect_stdout(io.StringIO()):
            agent.send("嗨")
        self.assertEqual(agent.suggest_next(), [])

    def test_disabled_by_default_makes_no_extra_request(self) -> None:
        #  脚本里只有主轮的那一项：多发一次请求就会把假 client 的脚本用完而报错
        agent = self.build([[chunk(content="好")]], sink=Recorder())
        with contextlib.redirect_stdout(io.StringIO()):
            agent.send("嗨")
        self.assertIsNone(agent._suggest_thread)
        self.assertFalse(any(isinstance(e, Suggestions) for e in agent.sink.events))

    def test_enabled_emits_event_from_background_thread(self) -> None:
        sink = Recorder()
        agent = self.build([[chunk(content="好")], text_response('["继续"]')], sink=sink)
        agent.suggestions_enabled = True
        with contextlib.redirect_stdout(io.StringIO()):
            agent.send("嗨")
        self._join(agent)
        events = [e for e in sink.events if isinstance(e, Suggestions)]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].items, ["继续"])
        self.assertEqual(events[0].turn, agent._turn_seq)
        self.assertEqual(events[0].to_dict()["kind"], "turn.suggestions")

    def test_late_result_for_an_old_turn_is_dropped(self) -> None:
        sink = Recorder()
        agent = self.build([[chunk(content="好")], text_response('["过时"]')], sink=sink)
        agent.suggestions_enabled = True
        #  线程还没跑之前就把轮序号推进：模拟用户已经开始下一轮
        original = agent.suggest_next

        def slow_then_stale():
            agent._turn_seq += 1
            return original()

        agent.suggest_next = slow_then_stale  # type: ignore[method-assign]
        with contextlib.redirect_stdout(io.StringIO()):
            agent.send("嗨")
        self._join(agent)
        self.assertFalse(any(isinstance(e, Suggestions) for e in sink.events))

    def test_failed_turn_gets_no_suggestions(self) -> None:
        sink = Recorder()
        agent = self.build([RuntimeError("boom")], sink=sink)
        agent.suggestions_enabled = True
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(Exception):
            agent.send("嗨")
        self.assertIsNone(agent._suggest_thread)


if __name__ == "__main__":
    unittest.main()
