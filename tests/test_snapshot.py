"""snapshot 录制器（xiaoyu/snapshot.py）的单元测试。

核心是 round-trip 性质：真实 chunk 流 → chunk_lines 序列化成 DSL →
parse_scripts → ScriptedClient 回放，_consume_stream 视角读到的面
（正文分片顺序、tool_call 分片、usage、reasoning）必须与原始流一致。
这条性质成立，"录出来的 fixture 一定能回放"才不是口头承诺。
"""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from types import SimpleNamespace

from xiaoyu.scripted import ScriptedClient, parse_scripts
from xiaoyu.snapshot import (
    Recorder,
    RecordingClient,
    chunk_lines,
    dump_request_header,
    maybe_record,
    response_lines,
)


def delta_chunk(content=None, tool_calls=None, usage=None, reasoning=None):
    chunk = SimpleNamespace(
        usage=usage,
        choices=[SimpleNamespace(delta=SimpleNamespace(content=content, tool_calls=tool_calls))],
    )
    if reasoning is not None:
        chunk.reasoning = reasoning
    return chunk


def fragment(index=0, id=None, name=None, arguments=None):
    return SimpleNamespace(
        index=index, id=id, function=SimpleNamespace(name=name, arguments=arguments)
    )


def usage_obj(p, c):
    return SimpleNamespace(prompt_tokens=p, completion_tokens=c)


def consume(stream):
    """镜像 agent._consume_stream 的累加逻辑，返回 (正文, tool_calls, usage, reasoning)。"""
    parts, pending, usage, reasoning = [], {}, None, []
    for chunk in stream:
        if getattr(chunk, "usage", None):
            usage = (chunk.usage.prompt_tokens, chunk.usage.completion_tokens)
        if items := getattr(chunk, "reasoning", None):
            reasoning.extend(items)
        if not chunk.choices:
            continue
        delta = chunk.choices[0].delta
        if delta.content:
            parts.append(delta.content)
        for frag in delta.tool_calls or []:
            slot = pending.setdefault(
                frag.index, {"id": "", "name": "", "arguments": ""}
            )
            if frag.id:
                slot["id"] = frag.id
            if frag.function.name and not slot["name"]:
                slot["name"] = frag.function.name
            if frag.function.arguments:
                slot["arguments"] += frag.function.arguments
    return "".join(parts), pending, usage, reasoning


class RoundTripTest(unittest.TestCase):
    def test_stream_round_trips_through_dsl(self) -> None:
        original = [
            delta_chunk(reasoning=[{"summary": "想一想"}]),
            delta_chunk(content="第一段\n"),
            delta_chunk(content="第二段，含 --- 与 # 井号"),
            delta_chunk(tool_calls=[fragment(0, id="call_x9", name="bash")]),
            delta_chunk(tool_calls=[fragment(0, arguments='{"command": "ec')]),
            delta_chunk(tool_calls=[fragment(0, arguments='ho hi"}')]),
            delta_chunk(usage=usage_obj(42, 7)),
        ]
        want = consume(original)

        lines: list[str] = []
        for chunk in original:
            lines.extend(chunk_lines(chunk))
        turns = parse_scripts("\n".join(lines))
        self.assertEqual(len(turns), 1)
        client = ScriptedClient(turns)
        got = consume(client.chat.completions.create(stream=True, messages=[]))
        self.assertEqual(got, want)

    def test_non_stream_round_trips(self) -> None:
        response = SimpleNamespace(
            usage=usage_obj(10, 3),
            choices=[SimpleNamespace(message=SimpleNamespace(content="摘要正文"))],
        )
        turns = parse_scripts("\n".join(response_lines(response)))
        client = ScriptedClient(turns)
        replayed = client.chat.completions.create(stream=False, messages=[])
        self.assertEqual(replayed.choices[0].message.content, "摘要正文")
        self.assertEqual(replayed.usage.prompt_tokens, 10)
        self.assertEqual(replayed.usage.completion_tokens, 3)


class FakeInner:
    """假的真 client：按预置行为响应 create。"""

    def __init__(self, behavior) -> None:
        self._behavior = behavior
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))
        self.models = "透传属性"

    def _create(self, **request):
        return self._behavior(request)


class RecordingClientTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._tmp.cleanup)
        self.directory = Path(self._tmp.name)
        self.recorder = Recorder(self.directory)

    def _request(self, stream=True):
        return {
            "model": "m1",
            "messages": [{"role": "system", "content": "系统提示"},
                         {"role": "user", "content": "问题"}],
            "tools": [{"type": "function", "function": {"name": "bash"}}],
            "stream": stream,
        }

    def test_stream_recorded_as_turns_with_headers(self) -> None:
        chunks = [delta_chunk(content="你好"), delta_chunk(usage=usage_obj(5, 2))]
        client = RecordingClient(FakeInner(lambda req: iter(chunks)), self.recorder)
        list(client.chat.completions.create(**self._request()))
        list(client.chat.completions.create(**self._request()))

        fixture = (self.directory / "model.txt").read_text(encoding="utf-8")
        turns = parse_scripts(fixture)
        self.assertEqual(len(turns), 2, fixture)
        self.assertEqual(turns[0][0], ("text", "你好"))
        header = json.loads((self.directory / "request-1.json").read_text(encoding="utf-8"))
        self.assertEqual(header["model"], "m1")
        self.assertEqual(header["system"], "系统提示")
        self.assertEqual(header["tools"][0]["function"]["name"], "bash")
        self.assertTrue((self.directory / "request-2.json").is_file())

    def test_mid_stream_error_recorded_as_error_line(self) -> None:
        def broken(req):
            yield delta_chunk(content="半截")
            raise RuntimeError("rate limit 429")

        client = RecordingClient(FakeInner(lambda req: broken(req)), self.recorder)
        with self.assertRaises(RuntimeError):
            list(client.chat.completions.create(**self._request()))
        turns = parse_scripts((self.directory / "model.txt").read_text(encoding="utf-8"))
        self.assertEqual(turns[0], [("text", "半截"), ("error", "rate limit 429")])

    def test_pre_chunk_throw_recorded_as_error_turn(self) -> None:
        def explode(req):
            raise RuntimeError("401 unauthorized")

        client = RecordingClient(FakeInner(explode), self.recorder)
        with self.assertRaises(RuntimeError):
            client.chat.completions.create(**self._request())
        turns = parse_scripts((self.directory / "model.txt").read_text(encoding="utf-8"))
        self.assertEqual(turns[0], [("error", "401 unauthorized")])

    def test_non_stream_recorded(self) -> None:
        response = SimpleNamespace(
            usage=usage_obj(9, 4),
            choices=[SimpleNamespace(message=SimpleNamespace(content="摘要"))],
        )
        client = RecordingClient(FakeInner(lambda req: response), self.recorder)
        got = client.chat.completions.create(**self._request(stream=False))
        self.assertIs(got, response, "非流式响应必须原样透传")
        turns = parse_scripts((self.directory / "model.txt").read_text(encoding="utf-8"))
        self.assertEqual(turns[0], [("text", "摘要"), ("usage", {"prompt_tokens": 9, "completion_tokens": 4})])

    def test_passthrough_attributes(self) -> None:
        client = RecordingClient(FakeInner(lambda req: None), self.recorder)
        self.assertEqual(client.models, "透传属性")

    def test_maybe_record_is_identity_without_env(self) -> None:
        import os

        self.assertNotIn("XIAOYU_SNAPSHOT_RECORD", os.environ)
        sentinel = object()
        self.assertIs(maybe_record(sentinel), sentinel)


class DumpRequestHeaderTest(unittest.TestCase):
    def test_missing_fields_degrade_gracefully(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            dump_request_header(Path(tmp), 3, {"model": "m", "messages": [], "stream": True})
            data = json.loads((Path(tmp) / "request-3.json").read_text(encoding="utf-8"))
            self.assertEqual(data, {"model": "m", "system": "", "tools": None})


class ScriptedAdditionsTest(unittest.TestCase):
    def test_reasoning_kind_parses_and_replays(self) -> None:
        turns = parse_scripts('reasoning: [{"summary": "想"}]\ntext: 答')
        client = ScriptedClient(turns)
        chunks = list(client.chat.completions.create(stream=True, messages=[]))
        self.assertEqual(chunks[0].reasoning, [{"summary": "想"}])
        self.assertEqual(chunks[1].choices[0].delta.content, "答")

    def test_reasoning_rejects_non_array(self) -> None:
        from xiaoyu.scripted import ScriptError

        with self.assertRaises(ScriptError):
            parse_scripts('reasoning: {"summary": "想"}')

    def test_complain_unconsumed_prints_leftover(self) -> None:
        client = ScriptedClient([[("text", "a")], [("text", "b")]])
        list(client.chat.completions.create(stream=True, messages=[]))
        buffer = io.StringIO()
        with redirect_stderr(buffer):
            client._complain_unconsumed()
        self.assertIn("ScriptedUnconsumed", buffer.getvalue())
        self.assertIn("剩 1 轮", buffer.getvalue())

    def test_complain_quiet_when_drained(self) -> None:
        client = ScriptedClient([[("text", "a")]])
        list(client.chat.completions.create(stream=True, messages=[]))
        buffer = io.StringIO()
        with redirect_stderr(buffer):
            client._complain_unconsumed()
        self.assertEqual(buffer.getvalue(), "")


class WindowsPathNormalizerTest(unittest.TestCase):
    """归一化器对 Windows 形态路径的输出必须与 macOS 录制的 golden 同形。

    纯函数直喂 Windows 形态输入，本机即可验证（不用等 Windows CI 首跑挂了
    才知道）：tmp 前缀 token 化后，token 起始路径段的反斜杠一律压成正斜杠。
    """

    TMP = r"C:\Users\ci\AppData\Local\Temp\tmpabc123"

    def test_session_record_workspace_uses_forward_slashes(self):
        from .snapshot_support import CallIdMap, normalize_session_record

        record = {"event": "meta", "workspace": self.TMP + r"\ws", "ts": "x"}
        out = normalize_session_record(record, self.TMP, CallIdMap())
        self.assertEqual(out["workspace"], "<tmp>/ws")

    def test_event_deep_path_flattened(self):
        from .snapshot_support import normalize_event

        event = {"text": f"写入 {self.TMP}\\ws\\sub\\a.txt 完成"}
        out = normalize_event(event, self.TMP)
        self.assertEqual(out["text"], "写入 <tmp>/ws/sub/a.txt 完成")

    def test_backslashes_outside_path_tokens_untouched(self):
        from .snapshot_support import normalize_event

        event = {"text": r"正则 \d+ 与换行 a\nb 不该被动"}
        out = normalize_event(event, self.TMP)
        self.assertEqual(out["text"], r"正则 \d+ 与换行 a\nb 不该被动")


if __name__ == "__main__":
    unittest.main()
