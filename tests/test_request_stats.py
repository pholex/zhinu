"""请求级计时：request.ended 带 duration/ttft/usage，轮级 TurnStats 汇总，`--stats` 的一行。
不打网络（假 client 注入）。"""

from __future__ import annotations

import contextlib
import io
import types
import unittest

from xiaoyu.agent import TurnStats
from xiaoyu.cli import turn_stats_line
from xiaoyu.events import RequestEnded, UIEvent

from .test_agent_paths import AgentTestCase, chunk


def usage_chunk_with_cache(prompt: int, completion: int, cached: int):
    usage = types.SimpleNamespace(
        prompt_tokens=prompt,
        completion_tokens=completion,
        prompt_tokens_details=types.SimpleNamespace(cached_tokens=cached),
    )
    return types.SimpleNamespace(choices=[], usage=usage)


class _Collect:
    def __init__(self) -> None:
        self.events: list[UIEvent] = []

    def emit(self, event: UIEvent) -> None:
        self.events.append(event)


class RequestEndedFieldsTest(AgentTestCase):
    def test_ended_carries_timing_and_usage(self) -> None:
        sink = _Collect()
        agent = self.build(
            [[chunk("好"), usage_chunk_with_cache(120, 7, 40)]], sink=sink
        )
        with contextlib.redirect_stdout(io.StringIO()):
            agent.send("干活")
        ended = [e for e in sink.events if isinstance(e, RequestEnded)]
        self.assertEqual(len(ended), 1)
        event = ended[0]
        self.assertGreaterEqual(event.duration_ms, 0)
        self.assertIsNotNone(event.ttft_ms)
        self.assertLessEqual(event.ttft_ms, event.duration_ms)
        self.assertEqual(
            event.usage, {"prompt_tokens": 120, "completion_tokens": 7, "cached_tokens": 40}
        )
        #  线上形态：老字段不变、新字段只是多出来的键
        payload = event.to_dict()
        self.assertEqual(payload["kind"], "request.ended")
        self.assertEqual(payload["usage"]["cached_tokens"], 40)

    def test_no_usage_means_empty_dict_not_fabricated(self) -> None:
        sink = _Collect()
        agent = self.build([[chunk("好")]], sink=sink)
        with contextlib.redirect_stdout(io.StringIO()):
            agent.send("干活")
        event = next(e for e in sink.events if isinstance(e, RequestEnded))
        self.assertEqual(event.usage, {})

    def test_turn_stats_reset_each_turn(self) -> None:
        agent = self.build(
            [[chunk("一"), usage_chunk_with_cache(10, 3, 0)], [chunk("二"), usage_chunk_with_cache(12, 4, 0)]]
        )
        with contextlib.redirect_stdout(io.StringIO()):
            agent.send("第一轮")
            first = agent.turn_stats
            agent.send("第二轮")
        self.assertEqual(first.requests, 1)
        self.assertEqual(first.completion_tokens, 3)
        self.assertEqual(agent.turn_stats.requests, 1)
        self.assertEqual(agent.turn_stats.completion_tokens, 4)

    def test_stats_line_only_when_enabled(self) -> None:
        agent = self.build([[chunk("好"), usage_chunk_with_cache(10, 3, 0)]])
        with contextlib.redirect_stdout(io.StringIO()):
            agent.send("干活")
        self.assertEqual(turn_stats_line(agent), "")
        agent.show_stats = True
        line = turn_stats_line(agent)
        self.assertIn("耗时", line)
        self.assertIn("首 token", line)


class TurnStatsTest(unittest.TestCase):
    def test_summary_shape(self) -> None:
        stats = TurnStats()
        self.assertEqual(stats.summary(), "")
        stats.record(duration_ms=2500, ttft_ms=500, completion_tokens=100)
        stats.record(duration_ms=1000, ttft_ms=200, completion_tokens=40)
        #  ttft 取第一次请求的；tok/s 的分母是扣掉首 token 等待后的吐字时长
        self.assertEqual(stats.ttft_ms, 500)
        self.assertEqual(stats.generation_ms, 2000 + 800)
        summary = stats.summary()
        self.assertIn("耗时 3.5s", summary)
        self.assertIn("首 token 0.5s", summary)
        self.assertIn("输出 50 tok/s", summary)
        self.assertIn("2 次请求", summary)

    def test_default_event_still_constructs(self) -> None:
        """老消费方 / 老测试 `RequestEnded()` 不带参数照常可用。"""
        event = RequestEnded()
        self.assertEqual((event.duration_ms, event.ttft_ms, event.usage), (0, None, {}))


if __name__ == "__main__":
    unittest.main()
