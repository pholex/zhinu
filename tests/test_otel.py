"""OpenTelemetry 导出（xiaoyu/otel.py）：激活规则、span 树与属性、内容采集开关、
缺包提示、collector 不可达时的收尾上限、子 agent 挂父。

span 用 InMemorySpanExporter 收，不打网络；没装 opentelemetry 包的环境整文件跳过
（除激活规则与"不激活就不 import"这两组——它们恰恰要在没包时也成立）。
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import socket
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from xiaoyu import otel

from .test_agent_paths import AgentTestCase, call_fragment, chunk, usage_chunk

try:
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
except ImportError:  # pragma: no cover - 没装可选 extra 的环境
    TracerProvider = None  # type: ignore[assignment]

ROOT = Path(__file__).resolve().parent.parent
ENDPOINT_ENV = {"OTEL_EXPORTER_OTLP_ENDPOINT": "http://127.0.0.1:4318"}


class DecideTest(unittest.TestCase):
    """激活规则是纯函数：逐条锁定。"""

    def test_nothing_set_is_off(self) -> None:
        self.assertEqual(otel.decide({}), ("off", ""))

    def test_generic_endpoint_gets_traces_path(self) -> None:
        self.assertEqual(
            otel.decide({"OTEL_EXPORTER_OTLP_ENDPOINT": "http://c:4318/"}),
            ("otlp", "http://c:4318/v1/traces"),
        )

    def test_traces_endpoint_wins_and_is_used_verbatim(self) -> None:
        env = {
            "OTEL_EXPORTER_OTLP_ENDPOINT": "http://c:4318",
            "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT": "http://t:9999/custom",
        }
        self.assertEqual(otel.decide(env), ("otlp", "http://t:9999/custom"))

    def test_sdk_disabled_overrides_everything(self) -> None:
        env = {**ENDPOINT_ENV, "OTEL_SDK_DISABLED": "true"}
        self.assertEqual(otel.decide(env), ("off", ""))

    def test_exporter_none_is_off_even_with_endpoint(self) -> None:
        env = {**ENDPOINT_ENV, "OTEL_TRACES_EXPORTER": "none"}
        self.assertEqual(otel.decide(env), ("off", ""))

    def test_console_needs_no_endpoint(self) -> None:
        self.assertEqual(otel.decide({"OTEL_TRACES_EXPORTER": "console"}), ("console", ""))

    def test_otlp_without_endpoint_is_off(self) -> None:
        self.assertEqual(otel.decide({"OTEL_TRACES_EXPORTER": "otlp"}), ("off", ""))

    def test_unknown_exporter_is_off(self) -> None:
        self.assertEqual(otel.decide({**ENDPOINT_ENV, "OTEL_TRACES_EXPORTER": "zipkin"}), ("off", ""))


class NoActivationNoImportTest(unittest.TestCase):
    """不激活的三种情形：attach 返回 None，且进程里没有任何 opentelemetry 模块被
    import——这段成本不该落在没开它的人头上。必须在子进程里验：本进程可能已经
    被别的用例 import 过。"""

    SCRIPT = (
        "import sys\n"
        "import xiaoyu.agent\n"
        "from xiaoyu import otel\n"
        "assert otel.attach(object()) is None\n"
        "loaded = sorted(m for m in sys.modules if m.split('.')[0] == 'opentelemetry')\n"
        "assert not loaded, loaded\n"
        "print('clean')\n"
    )

    def _run(self, extra: dict[str, str]) -> None:
        env = {
            key: value for key, value in os.environ.items() if not key.startswith("OTEL_")
        }
        env.update(extra)
        env["XIAOYU_ENV_FILE"] = str(ROOT / "nonexistent.env")
        env["PYTHONPATH"] = str(ROOT)
        proc = subprocess.run(
            [sys.executable, "-P", "-c", self.SCRIPT],
            env=env, capture_output=True, text=True, encoding="utf-8", timeout=120, cwd=str(ROOT),
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("clean", proc.stdout)

    def test_no_endpoint(self) -> None:
        self._run({})

    def test_sdk_disabled(self) -> None:
        self._run({**ENDPOINT_ENV, "OTEL_SDK_DISABLED": "true"})

    def test_exporter_none(self) -> None:
        self._run({**ENDPOINT_ENV, "OTEL_TRACES_EXPORTER": "none"})


class MissingPackageTest(unittest.TestCase):
    def setUp(self) -> None:
        otel._reset_for_tests()
        self.addCleanup(otel._reset_for_tests)

    def test_hint_once_and_agent_keeps_running(self) -> None:
        """配了 endpoint 但没装包：stderr 一行怎么装，attach 给 None，不抛。"""
        blocked = {name: None for name in ("opentelemetry", "opentelemetry.sdk", "opentelemetry.sdk.trace")}
        err = io.StringIO()
        with mock.patch.dict(os.environ, ENDPOINT_ENV), mock.patch.dict(sys.modules, blocked), \
                contextlib.redirect_stderr(err):
            self.assertIsNone(otel.attach(object()))
            self.assertIsNone(otel.attach(object()))
        self.assertIn('pip install "xiaoyu-agent[otel]"', err.getvalue())
        self.assertEqual(err.getvalue().count("[otel] "), 1, err.getvalue())


@unittest.skipIf(TracerProvider is None, "没装 opentelemetry-sdk（可选 extra [otel]）")
class _SpanTestCase(AgentTestCase):
    """预置内存 exporter 的 provider；激活靠标准变量，与真实路径只差 exporter。"""

    def setUp(self) -> None:
        super().setUp()
        self.exporter = InMemorySpanExporter()
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(self.exporter))
        otel._reset_for_tests(preset_provider=provider)
        self.addCleanup(otel._reset_for_tests)
        self.env = mock.patch.dict(os.environ, {**ENDPOINT_ENV, "XIAOYU_ENV_FILE": str(ROOT / "nonexistent.env")})
        self.env.start()
        self.addCleanup(self.env.stop)

    def spans(self) -> dict[str, list]:
        by_name: dict[str, list] = {}
        for span in self.exporter.get_finished_spans():
            by_name.setdefault(span.name, []).append(span)
        return by_name

    def send(self, agent, text: str) -> None:
        with contextlib.redirect_stdout(io.StringIO()):
            agent.send(text)


class SpanTreeTest(_SpanTestCase):
    def test_turn_with_chat_and_tool_children(self) -> None:
        first = [
            chunk(tool_calls=[call_fragment(0, "call_1", "read_file", json.dumps({"path": "calc.py"}))]),
            usage_chunk(500, 30),
        ]
        second = [chunk(content="文件里有 add 函数"), usage_chunk(700, 40)]
        agent = self.build([first, second])
        self.assertIsNotNone(agent._otel)
        self.send(agent, "看一下 calc.py")

        spans = self.spans()
        self.assertEqual(sorted(spans), ["chat main-model", "execute_tool read_file", "invoke_agent xiaoyu"])
        turn = spans["invoke_agent xiaoyu"][0]
        chats = spans["chat main-model"]
        tool = spans["execute_tool read_file"][0]
        self.assertEqual(len(chats), 2)
        #  父子：chat 与 execute_tool 都挂在这一轮下
        for child in chats + [tool]:
            self.assertEqual(child.parent.span_id, turn.context.span_id)
        self.assertIsNone(turn.parent)

        attrs = dict(turn.attributes)
        self.assertEqual(attrs["gen_ai.operation.name"], "invoke_agent")
        self.assertEqual(attrs["gen_ai.agent.name"], "xiaoyu")
        self.assertEqual(attrs["gen_ai.request.model"], "main-model")
        self.assertEqual(attrs["xiaoyu.mode"], "default")
        self.assertTrue(attrs["gen_ai.conversation.id"])
        self.assertEqual(attrs["session.id"], attrs["gen_ai.conversation.id"])
        #  轮内 token 合计 = 两次请求之和
        self.assertEqual(attrs["gen_ai.usage.input_tokens"], 1200)
        self.assertEqual(attrs["gen_ai.usage.output_tokens"], 70)
        self.assertEqual(attrs["xiaoyu.turn.requests"], 2)
        self.assertEqual(attrs["xiaoyu.turn.tool_calls"], 1)
        #  内容默认不采
        self.assertNotIn("gen_ai.input.messages", attrs)

        chat = dict(chats[0].attributes)
        self.assertEqual(chat["gen_ai.operation.name"], "chat")
        self.assertEqual(chat["gen_ai.provider.name"], "gateway")
        self.assertEqual(chat["gen_ai.request.model"], "main-model")
        self.assertTrue(chat["gen_ai.request.stream"])
        self.assertEqual(chat["gen_ai.usage.input_tokens"], 500)
        self.assertEqual(chat["gen_ai.usage.output_tokens"], 30)
        self.assertEqual(chat["gen_ai.usage.cache_read.input_tokens"], 0)
        self.assertIn("gen_ai.response.time_to_first_chunk", chat)
        self.assertEqual([e.name for e in chats[0].events], ["gen_ai.first_chunk"])
        self.assertEqual(chats[0].status.status_code.name, "OK")

        tool_attrs = dict(tool.attributes)
        self.assertEqual(tool_attrs["gen_ai.operation.name"], "execute_tool")
        self.assertEqual(tool_attrs["gen_ai.tool.name"], "read_file")
        self.assertEqual(tool_attrs["gen_ai.tool.call.id"], "call_1")
        self.assertEqual(tool_attrs["gen_ai.tool.type"], "function")
        self.assertEqual(tool_attrs["xiaoyu.tool.outcome"], "ok")
        self.assertNotIn("gen_ai.tool.call.arguments", tool_attrs)
        self.assertNotIn("gen_ai.tool.call.result", tool_attrs)
        self.assertEqual(tool.status.status_code.name, "OK")
        self.assertEqual([e.name for e in tool.events], ["running"])
        #  工具 span 从 pending 起算，必须在 chat 结束之后、第二次 chat 之前
        self.assertGreaterEqual(tool.start_time, chats[0].end_time)
        self.assertLessEqual(tool.end_time, chats[1].start_time)

    def test_failed_request_is_error_with_kind(self) -> None:
        import httpx2
        import openai

        response = httpx2.Response(429, request=httpx2.Request("POST", "http://unused"))
        limited = openai.RateLimitError("slow down", response=response, body=None)
        agent = self.build([limited, [chunk(content="恢复"), usage_chunk(10, 2)]])
        with mock.patch("xiaoyu.agent.Agent._sleep"):
            self.send(agent, "干活")
        chats = self.spans()["chat main-model"]
        self.assertEqual(len(chats), 2)
        failed, ok = chats
        self.assertEqual(failed.status.status_code.name, "ERROR")
        self.assertEqual(dict(failed.attributes)["error.type"], "rate_limit")
        self.assertEqual(ok.status.status_code.name, "OK")
        turn = self.spans()["invoke_agent xiaoyu"][0]
        self.assertEqual(turn.status.status_code.name, "OK")

    def test_denied_tool_is_a_span_with_error_status(self) -> None:
        from xiaoyu.permissions import Permissions, parse_rule

        first = [chunk(tool_calls=[call_fragment(0, "c1", "read_file", json.dumps({"path": "calc.py"}))])]
        second = [chunk(content="被拒了")]
        agent = self.build(
            [first, second],
            permissions=Permissions(self.root, [parse_rule("deny read_file(calc.py)")]),
        )
        self.send(agent, "读")
        tool = self.spans()["execute_tool read_file"][0]
        attrs = dict(tool.attributes)
        self.assertEqual(tool.status.status_code.name, "ERROR")
        self.assertEqual(attrs["error.type"], "denied_by_rule")
        self.assertEqual(attrs["xiaoyu.tool.outcome"], "denied")
        self.assertEqual(attrs["xiaoyu.tool.denied_by"], "rule")

    def test_tool_error_marks_span(self) -> None:
        first = [chunk(tool_calls=[call_fragment(0, "c1", "read_file", json.dumps({"path": "missing.py"}))])]
        agent = self.build([first, [chunk(content="没有这个文件")]])
        self.send(agent, "读")
        tool = self.spans()["execute_tool read_file"][0]
        self.assertEqual(tool.status.status_code.name, "ERROR")
        self.assertEqual(dict(tool.attributes)["error.type"], "tool_error")
        self.assertEqual(dict(tool.attributes)["xiaoyu.tool.outcome"], "error")

    def test_two_turns_two_root_spans(self) -> None:
        agent = self.build([[chunk(content="一")], [chunk(content="二")]])
        self.send(agent, "第一轮")
        self.send(agent, "第二轮")
        turns = self.spans()["invoke_agent xiaoyu"]
        self.assertEqual(len(turns), 2)
        self.assertEqual({t.parent for t in turns}, {None})

    def test_end_session_flushes_without_error(self) -> None:
        agent = self.build([[chunk(content="一")]])
        self.send(agent, "第一轮")
        agent.end_session()
        self.assertEqual(len(self.spans()["invoke_agent xiaoyu"]), 1)


class ContentCaptureTest(_SpanTestCase):
    def test_opt_in_captures_redacted_and_truncated(self) -> None:
        big = ("x" * 100 + "\n") * 60  # > 4KB
        (self.root / "big.txt").write_text(big, encoding="utf-8")
        first = [chunk(tool_calls=[call_fragment(0, "c1", "read_file", json.dumps({"path": "big.txt"}))])]
        agent_env = {"OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT": "true"}
        with mock.patch.dict(os.environ, agent_env):
            agent = self.build([first, [chunk(content="读完了")]])
            #  凭据样本运行期拼出来：仓库的提交前扫描按字面认令牌形状，写死会被当真凭据拦下
            secret = "sk-" + "k" * 24
            self.send(agent, f"读 big.txt，顺带 Bearer {secret}")
        turn = self.spans()["invoke_agent xiaoyu"][0]
        prompt = dict(turn.attributes)["gen_ai.input.messages"]
        self.assertIn("读 big.txt", prompt)
        self.assertNotIn(secret, prompt)
        self.assertIn("[REDACTED]", prompt)
        tool = dict(self.spans()["execute_tool read_file"][0].attributes)
        self.assertEqual(json.loads(tool["gen_ai.tool.call.arguments"]), {"path": "big.txt"})
        result = tool["gen_ai.tool.call.result"]
        self.assertTrue(result.startswith("x" * 100))
        self.assertLess(len(result), otel.CONTENT_LIMIT + 64)
        self.assertIn("截断", result)

    def test_default_captures_nothing(self) -> None:
        first = [chunk(tool_calls=[call_fragment(0, "c1", "read_file", json.dumps({"path": "calc.py"}))])]
        agent = self.build([first, [chunk(content="好")]])
        self.send(agent, "读")
        for spans in self.spans().values():
            for span in spans:
                for key in span.attributes:
                    self.assertNotIn(key, (
                        "gen_ai.input.messages", "gen_ai.output.messages",
                        "gen_ai.tool.call.arguments", "gen_ai.tool.call.result",
                    ))


class SubagentParentTest(_SpanTestCase):
    def test_explore_turn_hangs_under_parent_tool_span(self) -> None:
        script = [
            [chunk(tool_calls=[call_fragment(0, "c1", "explore", json.dumps({"question": "add 在哪"}))])],
            [chunk(tool_calls=[call_fragment(0, "s1", "read_file", json.dumps({"path": "calc.py"}))])],
            [chunk(content="子：在 calc.py")],
            [chunk(content="主：收到")],
        ]
        agent = self.build(script)
        self.send(agent, "查一下")
        spans = self.spans()
        parent_turn = spans["invoke_agent xiaoyu"][0]
        explore_tool = spans["execute_tool explore"][0]
        child_turn = spans["invoke_agent explore"][0]
        child_tool = spans["execute_tool read_file"][0]
        self.assertEqual(explore_tool.parent.span_id, parent_turn.context.span_id)
        self.assertEqual(child_turn.parent.span_id, explore_tool.context.span_id)
        self.assertEqual(child_tool.parent.span_id, child_turn.context.span_id)
        attrs = dict(child_turn.attributes)
        self.assertEqual(attrs["gen_ai.agent.name"], "explore")
        self.assertEqual(attrs["xiaoyu.parent_session.id"], dict(parent_turn.attributes)["session.id"])
        #  同一 trace
        self.assertEqual(child_tool.context.trace_id, parent_turn.context.trace_id)


@unittest.skipIf(TracerProvider is None, "没装 opentelemetry-sdk（可选 extra [otel]）")
class RealExporterTest(unittest.TestCase):
    def setUp(self) -> None:
        otel._reset_for_tests()
        self.addCleanup(otel._reset_for_tests)

    def test_console_exporter_prints_spans_to_stderr(self) -> None:
        err = io.StringIO()
        env = {"OTEL_TRACES_EXPORTER": "console", "XIAOYU_ENV_FILE": str(ROOT / "nonexistent.env")}
        with mock.patch.dict(os.environ, env), contextlib.redirect_stderr(err):
            tracer = otel._get_tracer()
            self.assertIsNotNone(tracer)
            tracer.start_span("invoke_agent probe").end()
            otel.flush()
        self.assertIn("invoke_agent probe", err.getvalue())

    def test_grpc_protocol_declines_with_one_warning(self) -> None:
        err = io.StringIO()
        env = {**ENDPOINT_ENV, "OTEL_EXPORTER_OTLP_PROTOCOL": "grpc"}
        with mock.patch.dict(os.environ, env), contextlib.redirect_stderr(err):
            self.assertIsNone(otel.attach(object()))
            self.assertIsNone(otel.attach(object()))
        self.assertIn("grpc", err.getvalue())
        self.assertEqual(err.getvalue().count("[otel] "), 1, err.getvalue())

    def test_shutdown_returns_within_budget_when_collector_hangs(self) -> None:
        """collector 收下连接却永不应答：exporter 会等自己的超时（默认 10s），
        shutdown 必须在封顶时间内放手，进程退出不能跟着挂。"""
        try:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter  # noqa: F401
        except ImportError:
            self.skipTest("没装 opentelemetry-exporter-otlp-proto-http")
        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        self.addCleanup(server.close)
        port = server.getsockname()[1]
        accepted: list[socket.socket] = []

        def swallow() -> None:
            with contextlib.suppress(OSError):
                conn, _ = server.accept()
                accepted.append(conn)
                time.sleep(15)

        threading.Thread(target=swallow, daemon=True).start()
        self.addCleanup(lambda: [c.close() for c in accepted])
        err = io.StringIO()
        env = {"OTEL_EXPORTER_OTLP_ENDPOINT": f"http://127.0.0.1:{port}"}
        with mock.patch.dict(os.environ, env), contextlib.redirect_stderr(err):
            tracer = otel._get_tracer()
            self.assertIsNotNone(tracer)
            tracer.start_span("invoke_agent hang").end()
            started = time.monotonic()
            otel.shutdown()
            elapsed = time.monotonic() - started
        self.assertLess(elapsed, otel.SHUTDOWN_TIMEOUT_S + 1.0)


if __name__ == "__main__":
    unittest.main()
