"""一次性模式（-p 免交互直跑）的测试。

锁住四件事：
1. 管道内容与命令行指令的拼装顺序（材料在前、任务在后）；
2. resume 位置参数消歧（纯数字=序号，其它=指令开头）；
3. headless 场景（json/stream-json/管道 stdin）自动换成拒绝式 approver；
4. --output-format json / stream-json 的收尾对象结构与退出码。
"""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from xiaoyu.agent import Usage
from xiaoyu.cli import (
    build_parser,
    compose_prompt,
    make_headless_deny,
    oneshot_frontend,
    open_session,
    prompt_words,
    run_once,
    split_resume_positionals,
)
from xiaoyu.events import ToolCompleted
from xiaoyu.permissions import Permissions
from xiaoyu.render import JsonlSink, NullSink


class StubAgent:
    """run_once 只碰 send/usage/config/session_log/last_assistant_text。"""

    def __init__(self, result: str = "搞定了", exc: BaseException | None = None) -> None:
        self.result = result
        self.exc = exc
        self.usage = Usage()
        self.usage.add("deepseek-v4-pro", 100, 20)
        self.config = SimpleNamespace(model="deepseek-v4-pro")
        self.session_log = SimpleNamespace(
            path=Path("/tmp/session.jsonl"), event=lambda *a, **k: None
        )
        self.sent: list[str] = []

    def send(self, prompt: str) -> None:
        self.sent.append(prompt)
        if self.exc:
            raise self.exc

    def last_assistant_text(self) -> str:
        return self.result


class ComposePromptTest(unittest.TestCase):
    def test_arg_only(self) -> None:
        self.assertEqual(compose_prompt(["把", "测试跑了"], ""), "把 测试跑了")

    def test_pipe_only(self) -> None:
        self.assertEqual(compose_prompt([], "diff 内容"), "diff 内容")

    def test_pipe_before_arg(self) -> None:
        #  材料在前、任务在后，中间空行分隔
        self.assertEqual(
            compose_prompt(["写 commit message"], "diff 内容"),
            "diff 内容\n\n写 commit message",
        )

    def test_empty(self) -> None:
        self.assertEqual(compose_prompt([], ""), "")


class AppendSystemPromptFlagTest(unittest.TestCase):
    """--append-system-prompt：宿主进程嵌入 xiaoyu 时注入身份/人格的 argv 入口。"""

    def test_flag_parsed_into_namespace(self) -> None:
        args = build_parser().parse_args(["--append-system-prompt", "你现在是小助手", "任务"])
        self.assertEqual(args.append_system_prompt, "你现在是小助手")

    def test_default_is_none(self) -> None:
        args = build_parser().parse_args(["任务"])
        self.assertIsNone(args.append_system_prompt)


class SplitResumePositionalsTest(unittest.TestCase):
    def test_no_positionals(self) -> None:
        self.assertEqual(split_resume_positionals(None, []), (None, []))

    def test_index_then_prompt(self) -> None:
        self.assertEqual(split_resume_positionals("3", ["继续"]), (3, ["继续"]))

    def test_prompt_without_index(self) -> None:
        self.assertEqual(
            split_resume_positionals("继续", ["跑测试"]), (None, ["继续", "跑测试"])
        )


class OneshotFrontendTest(unittest.TestCase):
    def setUp(self) -> None:
        self.permissions = Permissions(Path("."))

    def test_json_is_headless_and_silent(self) -> None:
        approver, sink = oneshot_frontend(self.permissions, "json")
        self.assertIsInstance(sink, NullSink)
        #  headless approver：返回非空 str = 拒绝并附理由
        verdict = approver("bash", {"command": "ls"})
        self.assertIsInstance(verdict, str)
        self.assertTrue(verdict.strip())

    def test_stream_json_uses_jsonl_sink(self) -> None:
        approver, sink = oneshot_frontend(self.permissions, "stream-json")
        self.assertIsInstance(sink, JsonlSink)
        self.assertIn("make_headless_deny", approver.__qualname__)

    def test_text_with_tty_keeps_interactive_confirm(self) -> None:
        with mock.patch("sys.stdin") as stdin:
            stdin.isatty.return_value = True
            approver, sink = oneshot_frontend(self.permissions, "text")
        self.assertIsNone(sink)
        self.assertIn("make_confirm", approver.__qualname__)

    def test_text_with_piped_stdin_goes_headless(self) -> None:
        with mock.patch("sys.stdin") as stdin:
            stdin.isatty.return_value = False
            approver, sink = oneshot_frontend(self.permissions, "text")
        self.assertIsNone(sink)
        self.assertIn("make_headless_deny", approver.__qualname__)


class RunOnceTest(unittest.TestCase):
    def run_capture(self, agent: StubAgent, output_format: str) -> tuple[int, str]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = run_once(agent, "干活", output_format)
        return code, out.getvalue()

    def test_text_prints_usage(self) -> None:
        code, out = self.run_capture(StubAgent(), "text")
        self.assertEqual(code, 0)
        self.assertIn("次模型调用", out)

    def test_json_single_object(self) -> None:
        code, out = self.run_capture(StubAgent(), "json")
        self.assertEqual(code, 0)
        lines = [line for line in out.splitlines() if line.strip()]
        self.assertEqual(len(lines), 1)
        payload = json.loads(lines[0])
        self.assertEqual(payload["result"], "搞定了")
        self.assertEqual(payload["model"], "deepseek-v4-pro")
        self.assertEqual(payload["usage"]["turns"], 1)
        #  Windows 上 str(Path("/tmp/...")) 是反斜杠形态，别写死 POSIX 字面量
        self.assertEqual(payload["session_log"], str(Path("/tmp/session.jsonl")))
        self.assertNotIn("error", payload)
        self.assertNotIn("kind", payload)

    def test_stream_json_final_record_has_kind(self) -> None:
        code, out = self.run_capture(StubAgent(), "stream-json")
        self.assertEqual(code, 0)
        payload = json.loads(out.splitlines()[-1])
        self.assertEqual(payload["kind"], "result")
        self.assertEqual(payload["result"], "搞定了")

    def test_json_error_is_structured(self) -> None:
        code, out = self.run_capture(StubAgent(exc=RuntimeError("模型挂了")), "json")
        self.assertEqual(code, 1)
        payload = json.loads(out)
        self.assertIn("RuntimeError: 模型挂了", payload["error"])

    def test_json_interrupt_exit_130(self) -> None:
        code, out = self.run_capture(StubAgent(exc=KeyboardInterrupt()), "json")
        self.assertEqual(code, 130)
        self.assertEqual(json.loads(out)["error"], "interrupted")

    def test_text_error_still_raises(self) -> None:
        with self.assertRaises(RuntimeError):
            run_once(StubAgent(exc=RuntimeError("x")), "干活", "text")


class HeadlessDenyTest(unittest.TestCase):
    def test_reason_mentions_recourse(self) -> None:
        reason = make_headless_deny()("write_file", {"path": "a.txt"})
        self.assertIn("allow", reason)
        self.assertIn("--yolo", reason)


class PromptFlagTest(unittest.TestCase):
    """`-p`：业界惯例拼写，且存在两种语义（有的当开关、有的吃值）都要接住。"""

    def words(self, argv: list[str]) -> list[str]:
        return prompt_words(build_parser().parse_args(argv))

    def test_value_style_takes_a_value(self) -> None:
        self.assertEqual(self.words(["-p", "写测试"]), ["写测试"])
        self.assertEqual(self.words(["--prompt", "写测试"]), ["写测试"])

    def test_switch_style_is_a_bare_switch(self) -> None:
        """`cat 材料 | <cli> -p`：不带值也合法，指令从管道来。"""
        args = build_parser().parse_args(["-p"])
        self.assertEqual(args.prompt_opt, "")  # 表了态但没给指令
        self.assertEqual(prompt_words(args), [])
        self.assertEqual(compose_prompt(prompt_words(args), "说你好"), "说你好")

    def test_value_goes_first_regardless_of_where_it_was_written(self) -> None:
        """两边各写一半时 `-p` 的值恒排最前——argparse 不保留跨 action 的书写次序。"""
        self.assertEqual(self.words(["-p", "总结", "这个仓库"]), ["总结", "这个仓库"])
        self.assertEqual(self.words(["这个仓库", "-p", "总结"]), ["总结", "这个仓库"])
        self.assertEqual(self.words(["写测试", "-p"]), ["写测试"])

    def test_following_option_is_not_eaten_as_the_value(self) -> None:
        args = build_parser().parse_args(["-p", "--model", "x"])
        self.assertEqual(args.model, "x")
        self.assertEqual(prompt_words(args), [])

    def test_absent_flag_is_none_not_empty(self) -> None:
        """None（没写 -p）与 ''（写了但没给值）必须分得开：前者进 REPL，后者报错。"""
        self.assertIsNone(build_parser().parse_args([]).prompt_opt)
        self.assertIsNone(build_parser().parse_args(["干活"]).prompt_opt)


class SessionIdFlagTest(unittest.TestCase):
    """`--session-id`：CLI 这一层只管"开新的还是接着写"，语义在 session_log。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        from xiaoyu import session_log as session_log_module

        patcher = mock.patch.object(
            session_log_module, "user_config_dir", lambda: Path(self.tmp.name) / "xiaoyu"
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.config = SimpleNamespace(model="m", workspace=Path("/ws"))

    def test_parser_accepts_short_and_long_flag(self) -> None:
        for argv in (["-s", "nightly"], ["--session-id", "nightly"]):
            self.assertEqual(build_parser().parse_args(argv).session_id, "nightly")
        self.assertIsNone(build_parser().parse_args([]).session_id)

    def test_without_flag_stays_anonymous(self) -> None:
        """不给名字 = 老行为：文件名不带 -id- 标记，也永远接不回历史。
        （文件名按 <时间戳>-<pid> 取，同进程同一秒会撞名——那是既有形态，
        真实调用各是一个进程，这里只锁"匿名"这一点。）"""
        log, restored = open_session(self.config, None)
        self.assertEqual(restored, [])
        self.assertNotIn("-id-", log.path.name)

    def test_same_name_continues_same_file(self) -> None:
        first, restored = open_session(self.config, "nightly")
        self.assertEqual(restored, [])
        first.append({"role": "user", "content": "第一步"})
        second, restored = open_session(self.config, "nightly")
        self.assertEqual(second.path, first.path)
        self.assertEqual([m["content"] for m in restored], ["第一步"])

    def test_bad_name_raises_for_caller_to_report(self) -> None:
        with self.assertRaises(ValueError):
            open_session(self.config, "../etc/passwd")


class JsonlSinkTest(unittest.TestCase):
    def test_event_per_line(self) -> None:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            JsonlSink().emit(ToolCompleted("bash", output="ok", ok=True, seconds=0.1))
        payload = json.loads(out.getvalue())
        self.assertEqual(payload["kind"], "tool.completed")
        self.assertEqual(payload["name"], "bash")


if __name__ == "__main__":
    unittest.main()
