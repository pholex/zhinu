"""同题对照工具的离线自证：两种会话格式归一成同一种步骤记录，汇总口径可核对。不调模型。"""
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

EXPERIMENTS = Path(__file__).resolve().parents[1] / "experiments"


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


compare = load("session_compare", EXPERIMENTS / "session_compare.py")


def call(call_id: str, name: str, **args) -> dict:
    return {"id": call_id, "type": "function",
            "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)}}


#  小羽：一段输入、三次请求——直接重启、sleep 后查状态被打断前先失败一次、最后正文收尾
YU = [
    {"ts": "2026-01-01T10:00:00", "event": "meta", "format": 2, "model": "model-a"},
    {"ts": "2026-01-01T10:00:05", "role": "user", "content": "重启 web-1"},
    {"ts": "2026-01-01T10:00:05", "role": "user",
     "content": "<system-reminder>\n注入的提示，不是用户敲的\n</system-reminder>"},
    {"ts": "2026-01-01T10:00:06", "event": "request", "provider": "p", "model": "model-a",
     "outcome": "ok", "prompt_tokens": 1000, "completion_tokens": 10},
    {"ts": "2026-01-01T10:00:06", "role": "assistant", "content": None,
     "tool_calls": [call("c1", "bash", command="fleet reboot web-1")]},
    {"ts": "2026-01-01T10:00:08", "role": "tool", "tool_call_id": "c1",
     "content": "exit_status: 0\nstdout:\n已重启"},
    {"ts": "2026-01-01T10:00:09", "event": "request", "provider": "p", "model": "model-a",
     "outcome": "ok", "prompt_tokens": 3000, "completion_tokens": 20},
    {"ts": "2026-01-01T10:00:09", "role": "assistant", "content": "等 40 秒再查状态。",
     "tool_calls": [call("c2", "bash", command="sleep 40; fleet status web-1 | tail -1"),
                    call("c3", "read_file", path="notes.md")]},
    {"ts": "2026-01-01T10:00:49", "role": "tool", "tool_call_id": "c2",
     "content": "exit_status: 1\nstdout:\n超时"},
    {"ts": "2026-01-01T10:00:49", "role": "tool", "tool_call_id": "c3",
     "content": "用户拒绝了这次工具调用"},
    {"ts": "2026-01-01T10:00:50", "event": "request", "provider": "p", "model": "model-a",
     "outcome": "ok", "prompt_tokens": 2000, "completion_tokens": 30},
    {"ts": "2026-01-01T10:00:51", "role": "assistant", "content": "已重启，状态查询超时。"},
    {"ts": "2026-01-01T10:05:00", "event": "exit", "reason": "normal"},
]


def xue_line(kind: str, stamp: str, content, **extra) -> dict:
    return {"type": kind, "timestamp": f"2026-01-01T02:00:{stamp}.000Z", "isSidechain": False,
            "message": {"role": kind, "content": content, **extra.pop("message", {})}, **extra}


def xue_assistant(stamp: str, message_id: str, block: dict, context: int) -> dict:
    usage = {"input_tokens": 2, "cache_read_input_tokens": context - 12,
             "cache_creation_input_tokens": 10, "output_tokens": 7}
    return xue_line("assistant", stamp, [block],
                    message={"id": message_id, "model": "model-b", "usage": usage})


#  小雪：同一次请求的正文与工具调用分两行写（同一个 message.id）；sleep 被客户端拦下
XUE = [
    {"type": "mode", "mode": "normal"},
    xue_line("user", "00", "<local-command-caveat>客户端代写</local-command-caveat>", isMeta=True),
    xue_line("user", "10", "重启 web-1"),
    xue_assistant("12", "m1", {"type": "thinking", "thinking": ""}, 5000),
    xue_assistant("12", "m1", {"type": "text", "text": "先重启。"}, 5000),
    xue_assistant("13", "m1", {"type": "tool_use", "id": "t1", "name": "Bash",
                               "input": {"command": "fleet reboot web-1"}}, 5000),
    xue_line("user", "16", [{"type": "tool_result", "tool_use_id": "t1", "content": "已重启"}]),
    xue_line("user", "16", [{"type": "text", "text": "技能正文，客户端注入"}], isMeta=True),
    xue_assistant("17", "m2", {"type": "tool_use", "id": "t2", "name": "Bash",
                               "input": {"command": "sleep 40; fleet status web-1"}}, 7000),
    xue_line("user", "17", [{"type": "tool_result", "tool_use_id": "t2", "is_error": True,
                             "content": "Blocked: sleep 40"}]),
    {**xue_assistant("18", "m9", {"type": "tool_use", "id": "t9", "name": "Bash",
                                  "input": {"command": "ls"}}, 99), "isSidechain": True},
    xue_assistant("20", "m3", {"type": "text", "text": "已重启。"}, 6000),
    xue_line("user", "30", "客户端写的压缩摘要", isCompactSummary=True),
    xue_line("user", "31", "客户端转来的后台任务通知", promptSource="system"),
    xue_line("user", "32", "<bash-input>ls</bash-input>"),
    "这一行不是 JSON",
]


def write_jsonl(path: Path, records: list) -> Path:
    path.write_text("".join((r if isinstance(r, str) else json.dumps(r, ensure_ascii=False)) + "\n"
                            for r in records), encoding="utf-8")
    return path


class Base(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.yu_path = write_jsonl(self.dir / "yu.jsonl", YU)
        self.xue_path = write_jsonl(self.dir / "xue.jsonl", XUE)


class YuAdapterTest(Base):
    def setUp(self) -> None:
        super().setUp()
        self.timeline = compare.load_yu(self.yu_path)

    def test_steps_in_order_with_relative_seconds(self) -> None:
        steps = [(s.at, s.kind, s.tool, s.status) for s in self.timeline.steps]
        self.assertEqual(steps, [
            (0.0, "user", "", ""),
            (1.0, "tool", "bash", "ok"),
            (4.0, "say", "", ""),
            (4.0, "tool", "bash", "fail"),
            (4.0, "tool", "read_file", "denied"),
            (46.0, "say", "", ""),
        ])

    def test_injected_user_message_is_not_a_step(self) -> None:
        users = [s.detail for s in self.timeline.steps if s.kind == "user"]
        self.assertEqual(users, ["重启 web-1"])

    def test_command_shown_bare_and_sleep_counted(self) -> None:
        waiting = self.timeline.steps[3]
        self.assertEqual(waiting.detail, "sleep 40; fleet status web-1 | tail -1")
        self.assertEqual((waiting.asked_wait, waiting.took, waiting.request), (40.0, 40.0, 2))

    def test_summary(self) -> None:
        summary = compare.summarize(self.timeline)
        self.assertEqual(summary, {
            "tool_steps": 3, "bad_steps": 2, "user_inputs": 1,
            "seconds": 46,  # 到最后一句正文为止，不含退出前停在提示符上的四分钟
            "requests": 3, "context_median": 2000, "context_max": 3000, "context_total": 6000,
            "output_total": 60,
            "compactions": 0,
            "delegated_requests": 0, "delegated_context_total": 0, "delegated_output_total": 0,
            "tool_seconds": 42,  # 同批并行的两步只算一段
             "wait_asked": 40, "wait_seconds": 40,
            "said_requests": 2, "ends_with_text": True,
        })

    def test_sub_agent_requests_are_counted_apart_from_the_main_chain(self) -> None:
        sub = [{"ts": "2026-01-01T10:00:02", "event": "explore_request", "run": "r", "step": n,
                "prompt_tokens": 700, "completion_tokens": 5} for n in (1, 2)]
        timeline = compare.load_yu(write_jsonl(self.dir / "sub.jsonl", YU + sub))
        summary = compare.summarize(timeline)
        self.assertEqual((summary["delegated_requests"], summary["delegated_context_total"],
                          summary["delegated_output_total"]), (2, 1400, 10))
        self.assertEqual((summary["requests"], summary["context_total"]), (3, 6000))
        self.assertIn("| 子 agent 请求数 / 输入 token（不含在上面各项里） | 2 / 1,400 |",
                      compare.render_summary([timeline]))

    def test_interrupted_call_is_marked_and_ends_without_text(self) -> None:
        records = YU[:8] + [{"ts": "2026-01-01T10:00:20", "role": "tool", "tool_call_id": "c2",
                             "content": compare.INTERRUPTED + "。"}]
        timeline = compare.load_yu(write_jsonl(self.dir / "cut.jsonl", records))
        waiting, unanswered = timeline.steps[3], timeline.steps[4]
        self.assertEqual((waiting.status, waiting.took), ("interrupted", 11.0))
        self.assertEqual((unanswered.status, unanswered.took), ("pending", None))
        summary = compare.summarize(timeline)
        #  申请了 40 秒，11 秒就被打断：只计实际等掉的
        self.assertEqual((summary["wait_asked"], summary["wait_seconds"]), (40, 11))
        self.assertFalse(summary["ends_with_text"])


class XueAdapterTest(Base):
    def setUp(self) -> None:
        super().setUp()
        self.timeline = compare.load_xue(self.xue_path)

    def test_steps_in_order_with_relative_seconds(self) -> None:
        steps = [(s.at, s.kind, s.request, s.tool, s.status) for s in self.timeline.steps]
        self.assertEqual(steps, [
            (0.0, "user", None, "", ""),
            (2.0, "say", 1, "", ""),
            (3.0, "tool", 1, "Bash", "ok"),
            (7.0, "tool", 2, "Bash", "fail"),
            (10.0, "say", 3, "", ""),
        ])

    def test_blocks_of_one_message_are_one_request(self) -> None:
        self.assertEqual([(r.n, r.context, r.output, r.said) for r in self.timeline.requests],
                         [(1, 5000, 7, True), (2, 7000, 7, False), (3, 6000, 7, True)])
        self.assertEqual(self.timeline.model, "model-b")

    def test_client_written_text_and_side_chain_are_skipped(self) -> None:
        details = " ".join(s.detail for s in self.timeline.steps)
        self.assertNotIn("客户端", details)
        self.assertNotIn("bash-input", details)
        self.assertNotIn("ls", [s.detail for s in self.timeline.steps])

    def test_corrupt_line_is_a_warning_not_a_crash(self) -> None:
        self.assertEqual(self.timeline.warnings, ["L16: 损坏的日志记录"])

    def test_blocked_sleep_does_not_count_as_waiting(self) -> None:
        summary = compare.summarize(self.timeline)
        self.assertEqual((summary["wait_asked"], summary["wait_seconds"]), (40, 0))
        self.assertEqual((summary["seconds"], summary["bad_steps"], summary["said_requests"]), (10, 1, 2))
        self.assertTrue(summary["ends_with_text"])


class RenderTest(Base):
    def run_main(self, *argv: str) -> str:
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(compare.main(list(argv)), 0)
        return out.getvalue()

    def test_markdown_puts_both_sides_in_one_summary(self) -> None:
        text = self.run_main("--yu", str(self.yu_path), "--xue", str(self.xue_path))
        self.assertIn("| 项 | 小羽 | 小雪 |", text)
        self.assertIn("| 请求数 | 3 | 3 |", text)
        self.assertIn("| 前台等待秒数（计入 / 申请） | 40 / 40 | 0 / 40 |", text)
        #  管道符不能把表格列撑破
        self.assertIn("fleet status web-1 \\| tail -1（sleep 40s） | ✗ | 40s | 有 |", text)
        for line in text.splitlines():
            if line.startswith("| ") and "sleep" in line:
                self.assertEqual(line.replace("\\|", "").count("|"), 7, line)

    def test_long_detail_is_cut_to_width(self) -> None:
        text = self.run_main("--yu", str(self.yu_path), "--width", "20")
        self.assertIn("`bash` sleep 40; fleet sta…（sleep 40s）", text)

    def test_json_carries_steps_and_summary(self) -> None:
        data = json.loads(self.run_main("--xue", str(self.xue_path), "--json"))
        self.assertEqual(data[0]["label"], "小雪")
        self.assertEqual(data[0]["summary"]["requests"], 3)
        self.assertEqual(data[0]["steps"][2]["tool"], "Bash")

    def test_xue_id_is_found_under_root(self) -> None:
        nested = self.dir / "projects" / "some-workspace"
        nested.mkdir(parents=True)
        write_jsonl(nested / "abc-123.jsonl", XUE)
        self.assertEqual(compare.resolve_xue("abc-123", str(self.dir / "projects")).name, "abc-123.jsonl")
        with self.assertRaises(SystemExit):
            compare.resolve_xue("abc-123", None)

    def test_secrets_in_commands_are_redacted(self) -> None:
        token = "sk-" + "a" * 40
        records = YU[:4] + [
            {"ts": "2026-01-01T10:00:06", "role": "assistant", "content": None,
             "tool_calls": [call("c1", "bash", command=f"curl -H 'Authorization: Bearer {token}' x")]}]
        text = compare.render([compare.load_yu(write_jsonl(self.dir / "s.jsonl", records))])
        self.assertNotIn(token, text)


if __name__ == "__main__":
    unittest.main()
