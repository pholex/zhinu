"""TUI 前端（第 1 步）的测试。

真正的按键交互（PromptSession）需要 pty，不在单测里跑；这里锁三层：
1. 确认语义的单一来源 interpret_confirm_answer（无 TUI 依赖，永远跑）；
2. RichSink 渲染与 PlainSink 的内容对应（有依赖才跑）；
3. 补全器 / 折叠提示 / 前端选择逻辑（有依赖才跑）。
"""

from __future__ import annotations

import contextlib
import io
import os
import unittest
from unittest import mock

from xiaoyu import keys
from xiaoyu.cli import GRANT_SESSION, interpret_confirm_answer, make_frontend, repl

try:
    import prompt_toolkit  # noqa: F401
    import rich  # noqa: F401

    HAS_TUI = True
except ImportError:
    HAS_TUI = False

from .test_agent_paths import AgentTestCase

#  最小合法 PNG 头 + 填充：只要能过 media.sniff_mime 的魔数判断即可
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"pretend pixels" * 20


class TestInterpretConfirmAnswer(unittest.TestCase):
    def test_semantics_match_plain_frontend(self) -> None:
        self.assertIs(interpret_confirm_answer("y"), True)
        self.assertIs(interpret_confirm_answer("YES"), True)
        self.assertIs(interpret_confirm_answer(""), False)
        self.assertIs(interpret_confirm_answer("n"), False)
        self.assertIs(interpret_confirm_answer("No"), False)
        self.assertIs(interpret_confirm_answer("a"), GRANT_SESSION)
        self.assertIs(interpret_confirm_answer(" A "), GRANT_SESSION)
        #  任意其它文本 = 拒绝理由（原文回灌模型），去除首尾空白
        self.assertEqual(interpret_confirm_answer(" 别动这个文件 "), "别动这个文件")

    def test_literal_grant_is_a_reason_not_a_sentinel(self) -> None:
        """用户拿 "grant" 当拒绝理由时不能被误判成会话授权。"""
        verdict = interpret_confirm_answer("grant")
        self.assertIsNot(verdict, GRANT_SESSION)
        self.assertEqual(verdict, "grant")


@unittest.skipUnless(HAS_TUI, "未安装 tui 可选依赖")
class TestRichSink(unittest.TestCase):
    def build(self):
        from rich.console import Console

        from xiaoyu.tui import RichSink

        buffer = io.StringIO()
        return RichSink(Console(file=buffer, soft_wrap=True, highlight=False)), buffer

    def test_tool_lines_match_plain_content(self) -> None:
        from xiaoyu.events import ToolCompleted, ToolPending

        sink, buffer = self.build()
        sink.emit(ToolPending("bash", {"command": "ls -la"}))
        sink.emit(ToolCompleted("bash", output="第一行\n第二行不该出现", ok=True, seconds=0.1))
        out = buffer.getvalue()
        self.assertIn("● bash", out)
        self.assertIn("ls -la", out)
        self.assertIn("⎿ 第一行", out)
        self.assertNotIn("第二行", out)
        #  被折叠的行数与查看入口要有交代
        self.assertIn("还有 1 行", out)

    def test_escape_sequences_never_reach_terminal(self) -> None:
        from xiaoyu.events import Notice, TextDelta, TextEnd, ToolCompleted, ToolPending

        sink, buffer = self.build()
        hostile = "结果\x1b]52;c;ZXZpbA==\x07\x1b[2J"
        with contextlib.redirect_stdout(buffer):
            sink.emit(TextDelta(hostile))
            sink.emit(TextEnd())
        sink.emit(ToolPending("bash", {"command": "cat \x1b]0;t\x07"}))
        sink.emit(ToolCompleted("bash", output=hostile, ok=True, seconds=0.1))
        sink.emit(Notice(hostile))
        out = buffer.getvalue()
        #  rich 自己的样式码也是 ESC 开头：这里的 Console 写 StringIO、无颜色，
        #  输出里出现的任何 ESC/BEL 都只可能来自未清洗的内容
        self.assertNotIn("\x1b]", out)
        self.assertNotIn("\x07", out)
        self.assertIn("结果", out)

    def test_rich_markup_in_args_is_not_interpreted(self) -> None:
        """参数里出现 [red] 这类 rich 标记必须原样显示，不能被吃掉或炸样式。"""
        from xiaoyu.events import ToolPending

        sink, buffer = self.build()
        sink.emit(ToolPending("bash", {"command": "echo [red]danger[/red]"}))
        self.assertIn("[red]danger[/red]", buffer.getvalue())

    def test_running_renders_no_spinner_off_terminal(self) -> None:
        """活区 spinner 只在真实终端出现：管道/测试环境静默，绝不能崩。"""
        from xiaoyu.events import ToolRunning

        sink, buffer = self.build()
        sink.emit(ToolRunning("bash", {"command": "ls"}))
        self.assertEqual(buffer.getvalue(), "")
        self.assertIsNone(sink._status)

    def test_denied_renders_frozen_line(self) -> None:
        from xiaoyu.events import ToolDenied

        sink, buffer = self.build()
        sink.emit(ToolDenied("bash", by="user"))
        sink.emit(ToolDenied("bash", by="rule"))
        out = buffer.getvalue()
        self.assertIn("⨯ bash 被用户拒绝", out)
        self.assertIn("⨯ bash 命中 deny 规则被拦截", out)

    def test_error_output_auto_expands(self) -> None:
        """ok=False 自动展开头几行：错误详情不该藏在折叠行后面。"""
        from xiaoyu.events import ToolCompleted

        sink, buffer = self.build()
        sink.emit(
            ToolCompleted(
                "bash", output="ERROR: 编译失败\n第二行详情\n第三行详情", ok=False, seconds=1.2
            )
        )
        out = buffer.getvalue()
        self.assertIn("✗ ERROR: 编译失败", out)
        self.assertIn("1.2s", out)
        self.assertIn("第二行详情", out)
        self.assertIn("第三行详情", out)

    def test_detail_toggle_expands_ok_output(self) -> None:
        """Ctrl-O 的开关位：detail=True 时成功输出也展开（对之后的事件生效）。"""
        from xiaoyu.events import ToolCompleted

        sink, buffer = self.build()
        sink.detail = True
        sink.emit(ToolCompleted("bash", output="第一行\n第二行", ok=True, seconds=0.1))
        out = buffer.getvalue()
        self.assertIn("⎿ 第一行", out)
        self.assertIn("第二行", out)

    def test_readonly_tools_collapse_into_summary_line(self) -> None:
        """连续只读工具折叠成一行汇总，非只读事件触发落盘。"""
        from xiaoyu.events import TextDelta, ToolCompleted, ToolPending

        sink, buffer = self.build()
        sink.emit(ToolPending("read_file", {"path": "a.py"}))
        sink.emit(ToolCompleted("read_file", output="内容", ok=True, seconds=0.01))
        sink.emit(ToolPending("grep", {"pattern": "x"}))
        sink.emit(ToolCompleted("grep", output="命中", ok=True, seconds=0.01))
        #  还没落盘：折叠组在攒
        self.assertEqual(buffer.getvalue(), "")
        with contextlib.redirect_stdout(io.StringIO()):
            sink.emit(TextDelta("正文"))
        out = buffer.getvalue()
        self.assertIn("⎿ 读文件 ×1 · 搜索 ×1", out)
        self.assertNotIn("● read_file", out)

    def test_readonly_error_is_never_collapsed(self) -> None:
        """只读工具出错不进折叠组：错误必须展开可见。"""
        from xiaoyu.events import ToolCompleted

        sink, buffer = self.build()
        sink.emit(ToolCompleted("grep", output="ERROR: 路径不存在", ok=False, seconds=0.1))
        self.assertIn("✗ ERROR: 路径不存在", buffer.getvalue())

    def test_detail_mode_shows_readonly_individually(self) -> None:
        """detail 模式下只读工具逐条显示，不进折叠组。"""
        from xiaoyu.events import ToolCompleted, ToolPending

        sink, buffer = self.build()
        sink.detail = True
        sink.emit(ToolPending("grep", {"pattern": "x"}))
        sink.emit(ToolCompleted("grep", output="一\n二", ok=True, seconds=0.1))
        out = buffer.getvalue()
        self.assertIn("● grep", out)
        self.assertIn("⎿ 一", out)
        self.assertIn("二", out)

    def test_quiet_child_shares_console_and_indents(self) -> None:
        """explore 子 agent 的 sink：共用 Console（活区不被搅花）、缩进、不刷正文。"""
        import contextlib as _contextlib
        import io as _io

        from xiaoyu.events import TextDelta, ToolCompleted, ToolPending

        sink, buffer = self.build()
        child = sink.quiet_child()
        self.assertIs(child.console, sink.console)
        stdout = _io.StringIO()
        with _contextlib.redirect_stdout(stdout):
            child.emit(TextDelta("子 agent 的正文不该出现"))
        self.assertEqual(stdout.getvalue(), "")
        #  子 agent 的只读调用同样折叠；遇到非只读工具落盘成一行汇总
        child.emit(ToolPending("grep", {"pattern": "x"}))
        child.emit(ToolCompleted("grep", output="命中", ok=True, seconds=0.01))
        child.emit(ToolPending("bash", {"command": "ls"}))
        out = buffer.getvalue()
        self.assertIn("    ⎿ 搜索 ×1", out)
        self.assertIn("    ● bash", out)

    def test_plan_marks(self) -> None:
        from xiaoyu.events import PlanUpdated

        sink, buffer = self.build()
        sink.emit(
            PlanUpdated(
                [
                    {"step": "读文件", "status": "completed"},
                    {"step": "改代码", "status": "in_progress"},
                    {"step": "跑测试", "status": "pending"},
                ],
                "开始动手",
            )
        )
        out = buffer.getvalue()
        self.assertIn("✎ 开始动手", out)
        self.assertIn("✔ 读文件", out)
        self.assertIn("▶ 改代码", out)
        self.assertIn("○ 跑测试", out)

    def test_delta_streams_to_stdout(self) -> None:
        from xiaoyu.events import TextDelta, TextEnd

        sink, _ = self.build()
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            sink.emit(TextDelta("正文"))
            sink.emit(TextEnd())
        self.assertEqual(buffer.getvalue(), "正文\n")

    def test_notice_levels(self) -> None:
        from xiaoyu.events import Notice

        sink, buffer = self.build()
        for level in ("info", "warn", "error"):
            sink.emit(Notice(f"[{level} 提示]", level))
        out = buffer.getvalue()
        for level in ("info", "warn", "error"):
            self.assertIn(f"[{level} 提示]", out)


@unittest.skipUnless(HAS_TUI, "未安装 tui 可选依赖")
class TestTuiFrontend(AgentTestCase):
    def make_tui(self, agent=None):
        from rich.console import Console

        from xiaoyu.permissions import Permissions
        from xiaoyu.tui import Tui

        tui = Tui(Permissions(self.root), console=Console(file=io.StringIO(), soft_wrap=True))
        tui.agent = agent
        return tui

    def completions(self, tui, text: str) -> list[str]:
        from prompt_toolkit.document import Document

        from xiaoyu.tui import SlashCompleter

        completer = SlashCompleter(tui)
        document = Document(text, cursor_position=len(text))
        return [item.text for item in completer.get_completions(document, None)]

    def test_ctrl_c_keeps_the_cleared_draft_for_recall(self) -> None:
        """单次 Ctrl-C 清掉的半截输入存进 Esc-Esc 的取回槽，不加新键。"""
        agent = self.build([])
        tui = self.make_tui(agent)

        class _Buffer:
            text = "  写到一半的话  "

        class _Session:
            default_buffer = _Buffer()
            calls = 0

            def prompt(self, *args, **kwargs):
                self.calls += 1
                if self.calls == 1:
                    raise KeyboardInterrupt
                raise EOFError

        session = _Session()
        with mock.patch.object(tui, "_input_session", return_value=session), mock.patch(
            "builtins.print"
        ):
            self.assertEqual(tui._run_loop(agent), 0)
        self.assertEqual(tui._last_input, "写到一半的话")
        self.assertIn("Esc Esc 可取回", tui.console.file.getvalue())

    def test_ctrl_c_on_an_empty_line_does_not_touch_recall(self) -> None:
        agent = self.build([])
        tui = self.make_tui(agent)
        tui._last_input = "上一条"

        class _Buffer:
            text = ""

        class _Session:
            default_buffer = _Buffer()
            calls = 0

            def prompt(self, *args, **kwargs):
                self.calls += 1
                raise KeyboardInterrupt if self.calls == 1 else EOFError

        with mock.patch.object(tui, "_input_session", return_value=_Session()), mock.patch(
            "builtins.print"
        ):
            tui._run_loop(agent)
        self.assertEqual(tui._last_input, "上一条")
        self.assertNotIn("可取回", tui.console.file.getvalue())

    def test_slash_command_completion(self) -> None:
        tui = self.make_tui()
        self.assertIn("/model", self.completions(tui, "/mo"))
        self.assertIn("/compact", self.completions(tui, "/c"))
        self.assertIn("/context", self.completions(tui, "/c"))
        self.assertEqual(self.completions(tui, "正文不补全"), [])

    def test_skill_names_complete_after_builtins_and_shadowed_get_prefix(self) -> None:
        """技能名进补全菜单；与内建命令撞名的以 /skill: 形态出现，内建不被遮。"""
        from xiaoyu.skills import Skill

        agent = self.build([])
        agent.skills = [
            Skill(name="deploy", description="部署流程" * 30, path=self.root / "deploy" / "SKILL.md"),
            Skill(name="help", description="同名技能", path=self.root / "help" / "SKILL.md"),
        ]
        tui = self.make_tui(agent)
        items = self.completions(tui, "/dep")
        self.assertIn("/deploy", items)
        #  /help 补的是内建；撞名技能走 /skill:help
        self.assertEqual(self.completions(tui, "/help"), ["/help", "/skill:help"])
        self.assertIn("/skill:help", self.completions(tui, "/skill:h"))
        #  内建在前、技能在后；`/<技能名>` 占位行不进菜单
        everything = self.completions(tui, "/")
        self.assertLess(everything.index("/model"), everything.index("/deploy"))
        self.assertFalse(any("<" in item for item in everything))
        from prompt_toolkit.document import Document

        from xiaoyu.tui import SlashCompleter

        document = Document("/deploy", cursor_position=7)
        (item,) = list(SlashCompleter(tui).get_completions(document, None))
        meta = str(item.display_meta_text)
        self.assertTrue(meta.startswith("技能："))
        self.assertLess(len(meta), 80)

    def test_model_name_completion_uses_chain(self) -> None:
        self.config.fallback_models = ["backup-model"]
        agent = self.build([])
        tui = self.make_tui(agent)
        self.assertEqual(self.completions(tui, "/model ma"), ["main-model"])
        self.assertEqual(self.completions(tui, "/model b"), ["backup-model"])
        #  第三个词不再补模型名
        self.assertEqual(self.completions(tui, "/model main-model x"), [])

    def test_search_backend_completion(self) -> None:
        tui = self.make_tui(self.build([]))
        self.assertIn("/search", self.completions(tui, "/se"))
        self.assertEqual(self.completions(tui, "/search "), ["deepseek", "xai", "bedrock"])
        self.assertEqual(self.completions(tui, "/search d"), ["deepseek"])
        self.assertEqual(self.completions(tui, "/search xai extra"), [])

    def test_truncation_hint_prints_once_at_turn_end(self) -> None:
        """折叠提示是整轮的一个状态：
        每个折叠行只说"藏了多少"，"怎么展开"在一轮收尾统一提一次。"""
        from xiaoyu.events import ToolCompleted

        agent = self.build([])
        tui = self.make_tui(agent)
        #  没折叠过就不该出现
        tui._print_expand_hint()
        self.assertNotIn("Ctrl-O", tui.console.file.getvalue())

        tui.sink.begin_turn()
        tui.sink.emit(ToolCompleted(name="bash", output="a\nb\nc", ok=True, seconds=0.0))
        out = tui.console.file.getvalue()
        self.assertIn("还有 2 行", out)  # 藏了多少：留在行内
        self.assertNotIn("Ctrl-O", out)  # 怎么展开：不在行内重复
        tui._print_expand_hint()  # 而是收尾统一提一次
        self.assertEqual(tui.console.file.getvalue().count("Ctrl-O"), 1)

        #  开了详情就没什么可展开的了；新一轮重置后折叠标记清零——都不再提示
        before = tui.console.file.getvalue()
        tui.sink.detail = True
        tui._print_expand_hint()
        tui.sink.detail = False
        tui.sink.begin_turn()
        tui._print_expand_hint()
        self.assertEqual(tui.console.file.getvalue(), before)

    def test_folded_output_is_kept_and_replayed(self) -> None:
        """"还有 N 行"必须兑现：折叠内容留底，Ctrl-O 开详情时回放补打。"""
        from xiaoyu.events import ToolCompleted, ToolPending

        agent = self.build([])
        tui = self.make_tui(agent)
        sink = tui.sink

        sink.begin_turn()
        #  只读折叠组：锚点要自报家门（工具名 + 参数预览）
        sink.emit(ToolPending("read_file", {"path": "a.py"}))
        sink.emit(ToolCompleted(name="read_file", output="x = 1\ny = 2", ok=True, seconds=0.0))
        #  普通工具折叠尾巴
        sink.emit(ToolCompleted(name="bash", output="ok\n尾巴1\n尾巴2", ok=True, seconds=0.0))
        sink._flush_ro_group()

        self.assertEqual(len(sink.folded), 2)
        self.assertIn("read_file", sink.folded[0][0])
        self.assertIn("a.py", sink.folded[0][0])  # 参数预览进锚点
        self.assertEqual(sink.folded[0][1], ["x = 1", "y = 2"])
        self.assertEqual(sink.folded[1][1], ["尾巴1", "尾巴2"])

        sink.replay_folded()
        out = tui.console.file.getvalue()
        self.assertIn("y = 2", out)
        self.assertIn("尾巴2", out)
        self.assertEqual(sink.folded, [])  # 一次性：打完即清，再按不重复刷屏

    def test_error_tail_beyond_cap_is_kept(self) -> None:
        from xiaoyu.events import ToolCompleted

        agent = self.build([])
        tui = self.make_tui(agent)
        sink = tui.sink

        sink.begin_turn()
        rows = [f"行{i}" for i in range(sink._ERROR_LINES + 3)]
        sink.emit(ToolCompleted(name="bash", output="崩了\n" + "\n".join(rows), ok=False, seconds=0.0))
        self.assertEqual(len(sink.folded), 1)
        self.assertEqual(sink.folded[0][1], rows)  # 留底是完整尾巴，不止被截掉的部分

        #  截到 _ERROR_LINES 内的错误全量已展开，无需留底
        sink.begin_turn()
        sink.emit(ToolCompleted(name="bash", output="崩了\n只有一行", ok=False, seconds=0.0))
        self.assertEqual(sink.folded, [])

    def test_replay_caps_each_item(self) -> None:
        agent = self.build([])
        tui = self.make_tui(agent)
        sink = tui.sink

        sink.folded = [("⎿ 大文件", [f"L{i}" for i in range(sink._REPLAY_LINES + 5)])]
        sink.replay_folded()
        out = tui.console.file.getvalue()
        self.assertIn(f"L{sink._REPLAY_LINES - 1}", out)
        self.assertNotIn(f"L{sink._REPLAY_LINES}\n", out)
        self.assertIn("还有 5 行", out)

    def test_tool_summary_respects_console_width(self) -> None:
        """折叠成"一行"就要真的放得下一行，否则终端会折行、折叠白折。"""
        from rich.console import Console

        from xiaoyu.events import ToolPending
        from xiaoyu.permissions import Permissions
        from xiaoyu.tui import Tui

        narrow = Tui(Permissions(self.root), console=Console(file=io.StringIO(), width=60))
        narrow.sink.emit(
            ToolPending(name="bash", args={"command": "echo " + "x" * 500})
        )
        for line in narrow.console.file.getvalue().splitlines():
            self.assertLessEqual(len(line), 60)

    def test_preview_helpers_do_not_crash(self) -> None:
        tui = self.make_tui()
        tui._preview_write("calc.py", "def add(a, b):\n" * 20)
        tui._preview_replace("calc.py", "old\nline", "new\nline")
        out = tui.console.file.getvalue()
        self.assertIn("将写入：calc.py", out)
        self.assertIn("还有", out)
        self.assertIn("将修改：calc.py", out)
        #  unified diff：变更行带 -/+ 前缀，公共行（line）不重复染色
        self.assertIn("-old", out)
        self.assertIn("+new", out)

    def test_diff_rows_unified(self) -> None:
        from xiaoyu.tui import _diff_rows

        rows = _diff_rows("a\nb\nc", "a\nB\nc", 80)
        texts = ["".join(text for _style, text in row) for row in rows]
        self.assertIn("-b", texts)
        self.assertIn("+B", texts)
        styles = {row[0][1]: row[0][0] for row in rows}
        #  样式是语义 token，不是裸颜色（配色住在 theme，见 theme.py）
        self.assertEqual(styles["-b"], "diff.removed")
        self.assertEqual(styles["+B"], "diff.added")
        #  超长 diff 截断
        capped = _diff_rows("\n".join(f"x{i}" for i in range(100)), "y", 80, cap=10)
        self.assertEqual(len(capped), 11)
        self.assertIn("还有", capped[-1][0][1])

    def test_diff_rows_respects_width(self) -> None:
        """预览按传入宽度截断，不再写死 160——窄终端上"一行"会折成两行，
        折叠摘要就白折了。"""
        from xiaoyu.tui import _diff_rows

        long_line = "z" * 300
        rows = _diff_rows(long_line, "y", 40)
        widest = max(len("".join(text for _style, text in row)) for row in rows)
        self.assertLessEqual(widest, 40)

    def test_diff_rows_word_level_highlight(self) -> None:
        """配对 -/+ 行词级高亮：小改动标出变化的词；
        变化占比超 40% 回退整行红绿（大改写词级反而花）。"""
        from xiaoyu.tui import _diff_rows

        rows = _diff_rows("a\nfoo = 1\nb", "a\nfoo = 2\nb", 80)
        minus = next(row for row in rows if "".join(t for _s, t in row).startswith("-foo"))
        #  词级：行内有多个片段，变化处走 .word token（theme 里配的是反白底）
        self.assertGreater(len(minus), 1)
        self.assertIn("diff.removed.word", [style for style, _t in minus])
        #  整行重写：回退为单片段整行红
        rows = _diff_rows("alpha beta gamma", "xxx yyy zzz", 80)
        minus = next(row for row in rows if row[0][1].startswith("-"))
        self.assertEqual(minus, [("diff.removed", "-alpha beta gamma")])

    def test_diff_rows_bounded_on_huge_input(self) -> None:
        """新旧两边行数之和超上限时不跑 difflib（相似的大文本上是平方级，确认框会
        卡在算 diff 上）：改按全删 / 全增给出，内容仍完整、cap 仍生效。"""
        import time

        from xiaoyu import theme
        from xiaoyu.tui import _DIFF_INPUT_LINES, _diff_rows

        #  两坨彼此相似、只差零星几行的长文本：difflib 的最坏情形
        old = "\n".join(f"line {i} same" for i in range(_DIFF_INPUT_LINES))
        new = "\n".join(f"line {i} {'same' if i % 97 else 'diff'}" for i in range(_DIFF_INPUT_LINES))
        started = time.monotonic()
        rows = _diff_rows(old, new, 80, cap=None)
        self.assertLess(time.monotonic() - started, 2.0)
        self.assertIn("未逐行比对", rows[0][0][1])
        texts = ["".join(t for _s, t in row) for row in rows]
        self.assertEqual(sum(t.startswith("-") for t in texts), _DIFF_INPUT_LINES)
        self.assertEqual(sum(t.startswith("+") for t in texts), _DIFF_INPUT_LINES)
        self.assertLessEqual({s for row in rows for s, _t in row}, set(theme.tokens()))
        capped = _diff_rows(old, new, 80, cap=10)
        self.assertEqual(len(capped), 11)
        self.assertIn("还有", capped[-1][0][1])
        #  上限之内照旧走配对 diff
        small = _diff_rows("a\nb", "a\nc", 80)
        self.assertNotIn("未逐行比对", small[0][0][1])

    def test_preview_replace_expand_closure(self) -> None:
        """diff 超限时返回"补打完整 diff"闭包——"还有 N 行"在确认框里
        也得是可兑现的承诺；未截断时返回 None，提示行不许诺空键。"""
        tui = self.make_tui()
        #  200 行删改必然超 cap（cap 上限 40，与终端高度无关）
        old = "\n".join(f"x{i}" for i in range(200))
        expand = tui._preview_replace("calc.py", old, "y")
        self.assertIsNotNone(expand)
        before = tui.console.file.getvalue()
        self.assertIn("还有", before)
        self.assertNotIn("-x199", before)  # 截掉的行不在预览里
        expand()
        out = tui.console.file.getvalue()
        self.assertIn("完整 diff：calc.py", out)
        self.assertIn("-x199", out)  # 承诺兑现：截掉的行补出来了
        self.assertIsNone(tui._preview_replace("calc.py", "a", "b"))

    def test_preview_write_expand_closure(self) -> None:
        tui = self.make_tui()
        content = "\n".join(f"line{i}" for i in range(30))
        expand = tui._preview_write("notes.txt", content)
        self.assertIsNotNone(expand)
        self.assertNotIn("line29", tui.console.file.getvalue())
        expand()
        out = tui.console.file.getvalue()
        self.assertIn("全文：notes.txt", out)
        self.assertIn("line29", out)
        self.assertIsNone(tui._preview_write("notes.txt", "短内容"))

    def test_slash_completion_substring_and_meta(self) -> None:
        """/tex 这类中段输入也能找到 /context；补全带一行说明。"""
        from prompt_toolkit.document import Document

        from xiaoyu.tui import SlashCompleter

        tui = self.make_tui()
        completer = SlashCompleter(tui)
        document = Document("/tex", cursor_position=4)
        items = list(completer.get_completions(document, None))
        self.assertIn("/context", [item.text for item in items])
        metas = [str(item.display_meta_text) for item in items]
        self.assertTrue(any("上下文" in meta for meta in metas))

    def test_at_file_completion(self) -> None:
        """@ 前缀模糊补全工作区文件，可出现在消息中间，保留 @ 进文本。"""
        tui = self.make_tui()
        items = self.completions(tui, "帮我看下 @ca")
        self.assertIn("@calc.py", items)
        #  非 @ 词不触发文件补全
        self.assertEqual(self.completions(tui, "帮我看下 ca"), [])

    def test_paste_fold_and_expand(self) -> None:
        """大段粘贴折叠成 chip（旁路存储），提交前展开回原文。"""
        tui = self.make_tui()
        #  小粘贴原样插入
        self.assertEqual(tui._fold_paste("两行\n以内"), "两行\n以内")
        #  超 800 字符折叠成占位符
        big = "x" * 900
        chip = tui._fold_paste(big)
        self.assertEqual(chip, "[粘贴 #1 · 1 行]")
        self.assertEqual(tui._expand_pastes(f"看下 {chip}"), f"看下 {big}")
        #  多行粘贴同样折叠（行数计入占位符）
        self.assertEqual(tui._fold_paste("一\n二\n三\n四"), "[粘贴 #2 · 4 行]")
        #  被手改坏的占位符按字面保留，不炸
        self.assertEqual(tui._expand_pastes("[粘贴 #99 · 1 行]"), "[粘贴 #99 · 1 行]")

    def test_paste_at_line_start_is_literal_input(self) -> None:
        """行首是粘贴 chip 的提交按字面发模型：贴进来的 `!` / `#` / `/` 不是用户敲的前缀。"""
        tui = self.make_tui()
        chip = tui._fold_paste("!rm -rf /\n" + "y\n" * 5)
        self.assertTrue(tui._starts_with_paste(chip))
        self.assertTrue(tui._starts_with_paste(f"  {chip} 帮我看这段"))
        #  chip 不在行首：用户自己敲的前缀照常生效
        self.assertFalse(tui._starts_with_paste(f"!cat {chip}"))
        #  手改坏 / 不存在的 chip 不算
        self.assertFalse(tui._starts_with_paste("[粘贴 #99 · 1 行]"))
        self.assertFalse(tui._starts_with_paste(""))
        #  展开后的整行交给路由：literal 下必须是 send 而不是 shell
        line = tui._expand_pastes(chip)
        self.assertTrue(line.startswith("!rm"))
        self.assertEqual(keys.classify_input(line, literal=True).kind, "send")
        self.assertEqual(keys.classify_input(line).kind, "shell")

    def test_image_chip_folds_and_becomes_content_parts(self) -> None:
        """图片 chip：占位符留在文本里，图片作为部件追加——指代关系不能丢。"""
        from xiaoyu import media, providers

        tui = self.make_tui()
        tui.agent = mock.Mock()
        tui.agent.config.model = "m-see"
        tui.agent.registry = providers.Registry(
            [providers.Provider("p", "", "", ("m-see",), "直连", (), ("m-see",))],
            clients={"p": mock.MagicMock()},
        )
        chip = tui._take_image(PNG_BYTES, "剪贴板里的图片")
        self.assertEqual(chip, "[图片 #1 · 1 KB]")

        content = tui._content_of(f"这张 {chip} 里的箭头指哪")
        self.assertIsInstance(content, list)
        #  文本部件保留 chip 原文：模型要知道图片插在句子的哪个位置
        self.assertIn(chip, media.text_of(content))
        self.assertEqual(len(media.images_of(content)), 1)

    def test_image_chip_not_sent_to_blind_model(self) -> None:
        """看不了图的模型：只发文字并提示换模型，chip 留着可重发。"""
        from xiaoyu import providers

        tui = self.make_tui()
        tui.agent = mock.Mock()
        tui.agent.config.model = "m-blind"
        tui.agent.registry = providers.Registry(
            [providers.Provider("p", "", "", ("m-blind",), "直连")],
            clients={"p": mock.MagicMock()},
        )
        #  没配代读模型（XIAOYU_VISION_FALLBACK）：caption_images 给空串
        tui.agent.caption_images.return_value = ("", "")
        chip = tui._take_image(PNG_BYTES)
        content = tui._content_of(f"看看 {chip}")
        self.assertEqual(content, f"看看 {chip}", "降级成纯文本，不静默塞图")
        #  引用仍在表里：/model 换完模型 Esc-Esc 取回上一条即可重发
        self.assertEqual(len(tui._images), 1)

    def test_image_chip_captioned_for_blind_model_when_reader_configured(self) -> None:
        """配了代读模型：图换成文字随本条一起发，chip 与提示都还在。

        提示照打是刻意的——代读有损，"凑合看懂"和"真看见了"必须让用户分得清，
        而原图始终只有 /model 换视觉模型这一条路。
        """
        from xiaoyu import media, providers

        tui = self.make_tui()
        tui.agent = mock.Mock()
        tui.agent.config.model = "m-blind"
        tui.agent.registry = providers.Registry(
            [providers.Provider("p", "", "", ("m-blind",), "直连")],
            clients={"p": mock.MagicMock()},
        )
        tui.agent.caption_images.return_value = ("[图片代读 · 1 张]\n一行报错", "  已代读")
        chip = tui._take_image(PNG_BYTES)
        content = tui._content_of(f"看看 {chip}")
        self.assertIsInstance(content, str, "代读产物是文本，图片部件不入历史")
        self.assertIn(chip, content, "chip 原文保留：指代关系不能丢")
        self.assertIn("一行报错", content)
        self.assertEqual(media.images_of(content), [])
        #  取景框是用户这一行原话
        self.assertIn(chip, tui.agent.caption_images.call_args.kwargs["guide"])
        self.assertEqual(len(tui._images), 1)

    def test_bad_image_rejected_with_reason(self) -> None:
        """收不下的图要说原因——"按了没反应"是最难自查的失败形态。"""
        tui = self.make_tui()
        self.assertIsNone(tui._take_image(b"not an image at all"))
        self.assertIsNone(tui._take_image(b"\x89PNG\r\n\x1a\n" + b"x" * (8 << 20)))

    def test_multiple_paths_become_chips_and_paths(self) -> None:
        """拖多个文件进终端：图片各折一颗 chip，非图片原样留路径给模型自己读。"""
        import tempfile
        from pathlib import Path

        from xiaoyu import media

        tui = self.make_tui()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first, second = root / "a.png", root / "b.png"
            first.write_bytes(PNG_BYTES)
            second.write_bytes(PNG_BYTES[:-1] + b"!")  # 内容不同 → 不同引用
            doc = root / "note.pdf"
            doc.write_text("x")
            inserted = tui._paths_to_text(media.split_paths(f"{first} {second} {doc}"))
        self.assertEqual(inserted.count("[图片 #"), 2)
        self.assertIn(str(doc), inserted)
        self.assertEqual(len(tui._images), 2)

    def paste_handler(self, tui):
        """取出 `input.paste` 真正绑定的那个处理器。

        ⚠️ 别再绕过它直接测 helper：v0.29.0 就是这么漏的——`_paths_to_text` /
        `split_paths` 都有测试且全绿，但 `_paste` 里留着重构前的
        `media.looks_like_image_path()` 和两参数的 `_fold_image()`，
        用户一拖文件进终端就 AttributeError 崩在 prompt_toolkit 的事件循环里。
        单测跑不了真 pty，但按键表 → 处理器这一跳必须真跑一次。
        """
        from prompt_toolkit.keys import Keys

        bindings = [
            item
            for item in tui._key_bindings().bindings
            if item.keys == (Keys.BracketedPaste,)
        ]
        self.assertEqual(len(bindings), 1, "input.paste 应当只绑一个 <bracketed-paste>")
        return bindings[0].handler

    def test_dragging_files_into_terminal_goes_through_the_real_handler(self) -> None:
        """拖文件进终端：走真正的 bracketed-paste 处理器，图片折 chip、其余留路径。"""
        import tempfile
        from pathlib import Path

        from prompt_toolkit.buffer import Buffer

        tui = self.make_tui()
        handler = self.paste_handler(tui)
        buffer = Buffer()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            image, doc = root / "shot.png", root / "note.pdf"
            image.write_bytes(PNG_BYTES)
            doc.write_text("x")
            handler(mock.Mock(data=f"{image} {doc}", current_buffer=buffer))
        self.assertEqual(buffer.text.count("[图片 #"), 1)
        self.assertIn("note.pdf", buffer.text)
        self.assertEqual(len(tui._images), 1)

    def test_pasting_plain_text_is_not_mistaken_for_paths(self) -> None:
        """普通文本照旧走折叠：判据必须收紧，把要讨论的文字吃成附件更烦人。"""
        from prompt_toolkit.buffer import Buffer

        tui = self.make_tui()
        handler = self.paste_handler(tui)
        buffer = Buffer()
        handler(mock.Mock(data="这段是正文 不是路径", current_buffer=buffer))
        self.assertEqual(buffer.text, "这段是正文 不是路径")
        self.assertEqual(tui._images, {})

    def test_image_chip_label_shows_dimensions(self) -> None:
        """chip 上带尺寸：贴完一眼确认贴对了没有。解析不出时退回只报体积。"""
        tui = self.make_tui()
        real = b"\x89PNG\r\n\x1a\n\x00\x00\x00\x0dIHDR" + (800).to_bytes(4, "big")
        real += (600).to_bytes(4, "big") + b"padding" * 100
        self.assertIn("800×600", tui._take_image(real) or "")
        #  伪造头认不出尺寸 → 只有体积，不猜
        chip = tui._take_image(PNG_BYTES) or ""
        self.assertRegex(chip, r"^\[图片 #\d+ · \d+ KB\]$")

    def test_image_chip_backspace_is_atomic(self) -> None:
        from xiaoyu.tui import _IMAGE_REF_TAIL

        match = _IMAGE_REF_TAIL.search("这张 [图片 #3 · 42 KB]")
        self.assertIsNotNone(match)
        self.assertEqual(match.group(0), "[图片 #3 · 42 KB]")

    def test_paste_chip_backspace_is_atomic(self) -> None:
        """退格对 chip 原子删除：光标紧邻占位符时整颗删掉。"""
        from xiaoyu.tui import _PASTE_REF_TAIL

        match = _PASTE_REF_TAIL.search("帮我看下 [粘贴 #1 · 12 行]")
        self.assertIsNotNone(match)
        self.assertEqual(match.group(0), "[粘贴 #1 · 12 行]")
        self.assertIsNone(_PASTE_REF_TAIL.search("[粘贴 #1 · 12 行]后面还有字"))

    def test_tab_inserts_longest_common_prefix_first(self) -> None:
        """Tab 先补到所有候选的最长公共前缀，再按才轮换（readline 直觉）。"""
        from prompt_toolkit.buffer import Buffer
        from prompt_toolkit.completion import Completer, Completion
        from prompt_toolkit.document import Document

        class StubCompleter(Completer):
            def get_completions(self, document, complete_event):
                yield Completion("/compact", start_position=-4)
                yield Completion("/compress", start_position=-4)

        tui = self.make_tui()
        buffer = Buffer(
            completer=StubCompleter(), document=Document("/com", cursor_position=4)
        )
        tui._tab_complete(buffer)
        #  只补到公共前缀（/compact 与 /compress 的共同前缀是 /comp），不落定候选
        self.assertEqual(buffer.text, "/comp")

        class SingleCompleter(Completer):
            def get_completions(self, document, complete_event):
                yield Completion("/context", start_position=-4)

        buffer = Buffer(
            completer=SingleCompleter(), document=Document("/con", cursor_position=4)
        )
        tui._tab_complete(buffer)
        #  唯一候选直接落定
        self.assertEqual(buffer.text, "/context")

    def select(self, tui, keys: str, options=None):
        """用管道输入真实驱动行内菜单（不需要 pty）。"""
        from prompt_toolkit.application import create_app_session
        from prompt_toolkit.input.defaults import create_pipe_input
        from prompt_toolkit.output import DummyOutput

        options = options or [("one", "允许一次", "y"), ("two", "本会话都允许", "a")]
        with create_pipe_input() as pipe:
            pipe.send_text(keys)
            with create_app_session(input=pipe, output=DummyOutput()):
                return tui._inline_select("允许执行 bash 吗？", options)

    def test_inline_select_by_number_shortcut_and_arrows(self) -> None:
        tui = self.make_tui()
        self.assertEqual(self.select(tui, "2"), "two")
        self.assertEqual(self.select(tui, "a"), "two")
        self.assertEqual(self.select(tui, "\r"), "one")  # 默认选中第一项
        self.assertEqual(self.select(tui, "\x1b[B\r"), "two")  # ↓ + Enter
        #  无 expand 时 Ctrl-O 是 no-op，不影响后续选择
        self.assertEqual(self.select(tui, "\x0f\r"), "one")

    def test_inline_select_ctrl_o_expands_once(self) -> None:
        """Ctrl-O 补打全文：只兑现一次（再按不重复倾倒），且不影响菜单本身的选择。"""
        from prompt_toolkit.application import create_app_session
        from prompt_toolkit.input.defaults import create_pipe_input
        from prompt_toolkit.output import DummyOutput

        from xiaoyu.tui import inline_select

        calls: list[int] = []
        options = [("one", "允许一次", "y"), ("two", "本会话都允许", "a")]
        with create_pipe_input() as pipe:
            pipe.send_text("\x0f\x0f\r")  # Ctrl-O ×2 + Enter
            with create_app_session(input=pipe, output=DummyOutput()):
                result = inline_select("要修改 calc.py 吗？", options, expand=lambda: calls.append(1))
        self.assertEqual(result, "one")
        self.assertEqual(calls, [1])

    def prompt_lines(self, tui, *batches: str, count: int) -> list[str]:
        """用管道输入真实驱动输入行（带小羽自己的按键表），返回每次提交的文本。

        每个 batch 在上一次提交返回后才写进管道，模拟"两批输入之间隔着人手的
        时间"；同一个 batch 内的按键一次写入，模拟终端一次交付的一批输入。
        """
        from prompt_toolkit import PromptSession
        from prompt_toolkit.application import create_app_session
        from prompt_toolkit.input.defaults import create_pipe_input
        from prompt_toolkit.output import DummyOutput

        results: list[str] = []
        pending = list(batches)
        with create_pipe_input() as pipe:
            with create_app_session(input=pipe, output=DummyOutput()):
                session = PromptSession(key_bindings=tui._key_bindings(), multiline=True)
                for _ in range(count):
                    if pending:
                        pipe.send_text(pending.pop(0))
                    results.append(session.prompt("› "))
        return results

    def test_enter_inside_unbracketed_paste_inserts_newline(self) -> None:
        """bracketed paste 失效（tmux 未透传等）时，粘贴里的换行以回车按键到达：
        同一批里回车后面还有按键 → 当换行插入，整段只提交一次。"""
        tui = self.make_tui()
        self.assertEqual(
            self.prompt_lines(tui, "line1\rline2\r\rline3\r", count=1),
            ["line1\nline2\n\nline3"],
        )

    @unittest.skipUnless(os.name == "posix", "分块读取补强只在 posix 生效")
    def test_enter_at_read_chunk_boundary_still_pasted(self) -> None:
        """大段粘贴被分多次 read：回车恰好是一批的最后一个字节时，stdin 里还有
        后续数据，同样当换行。prompt_toolkit 每次读 1024 字节。"""
        tui = self.make_tui()
        head = "a" * 1023
        self.assertEqual(
            self.prompt_lines(tui, f"{head}\rtail\r", count=1), [f"{head}\ntail"]
        )

    def test_enter_in_separate_batches_submits_each(self) -> None:
        """人手敲的回车与下一次按键分属不同批次：照常逐条提交。"""
        tui = self.make_tui()
        self.assertEqual(
            self.prompt_lines(tui, "先跑测试\r", "/help\r", count=2), ["先跑测试", "/help"]
        )

    def test_bracketed_paste_still_folds_and_submits(self) -> None:
        """正常的 bracketed paste 路径不受影响：整段进缓冲区，回车提交展开后的原文。"""
        tui = self.make_tui()
        body = "\n".join(f"第 {n} 行" for n in range(10))
        self.assertEqual(
            self.prompt_lines(tui, f"\x1b[200~{body}\x1b[201~\r", count=1), [body]
        )

    def test_confirm_session_choice_grants_session(self) -> None:
        tui = self.make_tui()
        with mock.patch.object(tui, "_inline_select", return_value="session"):
            self.assertIs(tui.confirm("bash", {"command": "ls"}), True)
        self.assertEqual(tui.permissions.session_commands, {"ls"})

    def test_confirm_always_choice_persists_rule(self) -> None:
        from xiaoyu import permissions as perm_mod

        tui = self.make_tui()
        rules_path = self.root / "rules.txt"
        with mock.patch.object(perm_mod, "user_rules_path", return_value=rules_path):
            with mock.patch.object(tui, "_inline_select", return_value="always"):
                #  规则写入前可编辑：用户原样回车 = 接受预填的规则
                with mock.patch.object(tui, "_ask_rule", return_value="allow bash(git status*)"):
                    verdict = tui.confirm("bash", {"command": "git status -sb"})
        self.assertIs(verdict, True)
        self.assertIn("allow bash(git status*)", rules_path.read_text(encoding="utf-8"))
        #  规则立即生效：同类调用不再进确认
        self.assertEqual(
            tui.permissions.decide("bash", {"command": "git status --short"}), "allow"
        )

    def test_confirm_always_rule_is_editable(self) -> None:
        """「总是允许」的规则写入前可改（预填可编辑的前缀规则）。"""
        from xiaoyu import permissions as perm_mod

        tui = self.make_tui()
        rules_path = self.root / "rules.txt"
        with mock.patch.object(perm_mod, "user_rules_path", return_value=rules_path):
            with mock.patch.object(tui, "_inline_select", return_value="always"):
                #  用户把规则改窄再回车
                with mock.patch.object(tui, "_ask_rule", return_value="allow bash(git diff*)"):
                    verdict = tui.confirm("bash", {"command": "git status -sb"})
                self.assertIs(verdict, True)
                self.assertIn(
                    "allow bash(git diff*)", rules_path.read_text(encoding="utf-8")
                )
                #  置空 = 不写规则、仅本次允许
                with mock.patch.object(tui, "_ask_rule", return_value=""):
                    self.assertIs(tui.confirm("bash", {"command": "git status"}), True)
                self.assertNotIn("git status", rules_path.read_text(encoding="utf-8"))
                #  Esc / Ctrl-C = 取消：规则不写，本次也不执行
                with mock.patch.object(tui, "_ask_rule", return_value=None):
                    self.assertIs(tui.confirm("bash", {"command": "git status"}), False)

    def test_confirm_tab_amend_attaches_note(self) -> None:
        """Tab = 批准并附言：
        返回 (True, 附言) 也是批准；空附言退化为普通批准。"""
        tui = self.make_tui()
        with mock.patch.object(tui, "_inline_select", return_value=("amend", "once")):
            with mock.patch.object(tui, "_ask_note", return_value="跑完顺带看下退出码"):
                self.assertEqual(
                    tui.confirm("bash", {"command": "ls"}), (True, "跑完顺带看下退出码")
                )
        with mock.patch.object(tui, "_inline_select", return_value=("amend", "once")):
            with mock.patch.object(tui, "_ask_note", return_value=""):
                self.assertIs(tui.confirm("bash", {"command": "ls"}), True)

    def test_confirm_title_names_the_target(self) -> None:
        """问句带宾语：「要修改 foo.py 吗？」比「允许执行吗？」信息密度高。"""
        tui = self.make_tui()
        self.assertEqual(tui._confirm_title("bash", {"command": "ls"}), "要执行这条命令吗？")
        self.assertEqual(
            tui._confirm_title("str_replace", {"path": "src/foo.py"}), "要修改 foo.py 吗？"
        )
        self.assertEqual(
            tui._confirm_title("write_file", {"path": "bar.py"}), "要写入 bar.py 吗？"
        )
        #  带升权申请的 bash：问句本身就得说出"升权"，不能和普通命令长一样
        self.assertEqual(
            tui._confirm_title(
                "bash", {"command": "ls", "sandbox_permissions": "danger-full-access"}
            ),
            "要升权执行这条命令吗？",
        )

    def test_confirm_deny_with_reason_returns_text(self) -> None:
        """拒绝即改指令：用户给的一句话原文回灌模型。"""
        tui = self.make_tui()
        with mock.patch.object(tui, "_inline_select", return_value="deny"):
            with mock.patch.object(tui, "_ask_reason", return_value="改用 uv 跑"):
                self.assertEqual(tui.confirm("bash", {"command": "pip install x"}), "改用 uv 跑")
            with mock.patch.object(tui, "_ask_reason", return_value=""):
                self.assertIs(tui.confirm("bash", {"command": "pip install x"}), False)

    def test_confirm_cancel_is_plain_deny(self) -> None:
        tui = self.make_tui()
        with mock.patch.object(tui, "_inline_select", return_value=None):
            self.assertIs(tui.confirm("bash", {"command": "ls /"}), False)

    def test_confirm_never_choice_persists_deny_rule(self) -> None:
        """「以后都拒绝」：写 deny 规则（写入前可编辑），本次拒绝并把规则告诉模型。"""
        from xiaoyu import permissions as perm_mod

        tui = self.make_tui()
        rules_path = self.root / "rules.txt"
        with mock.patch.object(perm_mod, "user_rules_path", return_value=rules_path):
            with mock.patch.object(tui, "_inline_select", return_value="never") as select:
                with mock.patch.object(tui, "_ask_rule", return_value="deny bash(pip install*)") as ask:
                    verdict = tui.confirm("bash", {"command": "pip install requests"})
        #  菜单里有这一项，预填的是推导出的 deny 规则
        options = select.call_args.args[1]
        self.assertIn(("never", "以后都拒绝（规则写入前可编辑：deny bash(pip install*)）", "x"), options)
        self.assertEqual(ask.call_args.kwargs.get("empty_means"), "仅本次拒绝")
        self.assertIsInstance(verdict, str)
        self.assertIn("deny bash(pip install*)", verdict)
        self.assertIn("deny bash(pip install*)", rules_path.read_text(encoding="utf-8"))
        #  规则立即生效：同类调用直接被拦
        self.assertEqual(tui.permissions.decide("bash", {"command": "pip install x"}), "deny")

    def test_confirm_never_refuses_allow_edits_and_empty_means_plain_deny(self) -> None:
        from xiaoyu import permissions as perm_mod

        tui = self.make_tui()
        rules_path = self.root / "rules.txt"
        with mock.patch.object(perm_mod, "user_rules_path", return_value=rules_path):
            with mock.patch.object(tui, "_inline_select", return_value="never"):
                #  改成 allow 不收：这个入口只写 deny
                with mock.patch.object(tui, "_ask_rule", return_value="allow bash(pip install*)"):
                    self.assertIs(tui.confirm("bash", {"command": "pip install x"}), False)
                #  置空 / 取消 = 普通拒绝
                with mock.patch.object(tui, "_ask_rule", return_value=""):
                    self.assertIs(tui.confirm("bash", {"command": "pip install x"}), False)
                with mock.patch.object(tui, "_ask_rule", return_value=None):
                    self.assertIs(tui.confirm("bash", {"command": "pip install x"}), False)
        self.assertFalse(rules_path.exists())

    def test_confirm_never_absent_when_underivable(self) -> None:
        tui = self.make_tui()
        with mock.patch.object(tui, "_inline_select", return_value="once") as select:
            tui.confirm("bash", {"command": "ls -la && make"})
        self.assertNotIn("never", [value for value, _, _ in select.call_args.args[1]])

    def test_bang_shell_output_enters_context(self) -> None:
        """! 前缀：命令在工作区执行，退出码与输出进对话历史（供模型参考）。"""
        agent = self.build([])
        tui = self.make_tui(agent)
        with contextlib.redirect_stdout(io.StringIO()):
            tui._run_shell("echo hello-from-shell")
        last = agent.messages[-1]
        self.assertEqual(last["role"], "user")
        self.assertIn("$ echo hello-from-shell", last["content"])
        self.assertIn("hello-from-shell", last["content"])
        self.assertIn("退出码 0", last["content"])

    def test_hash_note_appends_file_and_context(self) -> None:
        """# 前缀：备忘落进项目指令文件（下个会话自动加载），同时灌进当前对话。"""
        agent = self.build([])
        tui = self.make_tui(agent)
        tui._note_memory("测试统一跑 python -m pytest")
        target = self.root / "XIAOYU.md"
        self.assertTrue(target.is_file())
        self.assertIn("- 测试统一跑 python -m pytest", target.read_text(encoding="utf-8"))
        self.assertIn("测试统一跑", agent.messages[-1]["content"])
        #  已有 AGENTS.md 时追加到它，不另开新文件
        (self.root / "AGENTS.md").write_text("# 已有指令\n", encoding="utf-8")
        tui._note_memory("第二条")
        self.assertIn("第二条", (self.root / "AGENTS.md").read_text(encoding="utf-8"))


@unittest.skipUnless(HAS_TUI, "未安装 tui 可选依赖")
class TestRunningLine(unittest.TestCase):
    """spinner 文案的三段式。"""

    def render(self, elapsed: float, timeout: int | None = None) -> str:
        import time

        from xiaoyu.tui import _RunningLine

        line = _RunningLine("bash", timeout)
        line.started = time.monotonic() - elapsed
        return line.__rich__().plain

    def test_progress_replaces_the_tip(self) -> None:
        """工具自报的进度顶掉轮播提示：它才回答"还要等多久"。"""
        import time

        from xiaoyu.tui import _RunningLine

        line = _RunningLine("mcp__s__slow")
        line.started = time.monotonic() - 10
        line.progress = "2/3（66%） · step 2"
        text = line.__rich__().plain
        self.assertIn("mcp__s__slow 运行中 · 10s · 2/3（66%） · step 2", text)
        self.assertNotIn("Ctrl-C", text)

    def test_short_runs_stay_quiet(self) -> None:
        """绝大多数工具一闪而过，那半秒里插提示纯属打扰。"""
        from xiaoyu import keys

        text = self.render(1.0)
        self.assertIn("Ctrl-C 中断", text)
        for tip in keys.tips():
            self.assertNotIn(tip, text)

    def test_medium_runs_teach(self) -> None:
        """等待时间当教学位：按键类来自绑定表，非按键类在 _EXTRA_TIPS 补充。"""
        from xiaoyu import keys, tui

        seen = {self.render(elapsed) for elapsed in (5, 13, 21, 29)}
        rotation = [*keys.tips(), *tui._EXTRA_TIPS]
        self.assertTrue(any(tip in text for text in seen for tip in rotation))
        #  确实在轮换，不是卡在同一条
        self.assertGreater(len(seen), 1)

    def test_rotation_reaches_every_tip_across_runs(self) -> None:
        """起点逐实例推进：单次窗口只够三四条，跨运行必须把整张表轮完
        ——否则表尾的提示（XIAOYU_MODE 一类）永远见不了天日。"""
        from xiaoyu import keys, tui

        rotation = [*keys.tips(), *tui._EXTRA_TIPS]
        seen = {self.render(5.0) for _ in range(len(rotation))}
        for tip in rotation:
            self.assertTrue(any(tip in text for text in seen), tip)

    def test_long_runs_report_the_timeout_budget(self) -> None:
        """跑久了，用户要判断的是"还该不该等"——能回答这个的是剩余预算。"""
        self.assertIn("上限 120s", self.render(45, timeout=120))
        #  没有超时上限就不要凭空编一个
        self.assertNotIn("上限", self.render(45))

    def test_timeout_comes_from_explicit_arg_then_config(self) -> None:
        import io

        from rich.console import Console

        from xiaoyu.events import ToolRunning
        from xiaoyu.tui import RichSink

        sink = RichSink(Console(file=io.StringIO()))
        sink.bash_timeout = 90
        self.assertEqual(sink._timeout_for(ToolRunning("bash", {})), 90)
        self.assertEqual(sink._timeout_for(ToolRunning("bash", {"timeout": 5})), 5)
        #  只有 bash 有超时，别的工具不该报一个不存在的上限
        self.assertIsNone(sink._timeout_for(ToolRunning("read_file", {"path": "a"})))


@unittest.skipUnless(HAS_TUI, "未安装 tui 可选依赖")
class TestDedupedHistory(unittest.TestCase):
    def history(self):
        import tempfile
        from pathlib import Path

        from xiaoyu.tui import DedupedHistory

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return DedupedHistory(str(Path(tmp.name) / "hist"))

    def test_consecutive_duplicates_collapse(self) -> None:
        """反复回车重跑同一条是常态；不去重的话 ↑ 要按十几次才翻得过去。"""
        history = self.history()
        list(history.load_history_strings())
        for line in ("ls", "ls", "ls", "pwd", "pwd", "ls"):
            history.append_string(line)
        #  最近在前；间隔出现的 ls 保留（那是"我又跑了一次"，不是重复）
        self.assertEqual(history._loaded_strings, ["ls", "pwd", "ls"])

    def test_dedup_also_applies_when_reading_an_old_file(self) -> None:
        """改这条之前写下的历史文件里已经堆了重复，读取时要一并去掉。"""
        from prompt_toolkit.history import FileHistory

        from xiaoyu.tui import DedupedHistory

        history = self.history()
        plain = FileHistory(history.filename)  # 用不去重的实现制造脏历史
        for line in ("a", "a", "a", "b"):
            plain.append_string(line)
        self.assertEqual(list(DedupedHistory(history.filename).load_history_strings()), ["b", "a"])


@unittest.skipUnless(HAS_TUI, "未安装 tui 可选依赖")
@unittest.skipIf(os.name == "nt", "权限位只在 POSIX 上有语义")
class TestInputHistoryPermissions(unittest.TestCase):
    """输入历史里可能有用户贴过的密钥：文件仅本人可读写，新建目录 0700。"""

    def setUp(self) -> None:
        import tempfile
        from pathlib import Path

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def open_history(self, config_dir):
        from unittest import mock

        from xiaoyu import tui

        shell = object.__new__(tui.Tui)  # 只测建会话这一步，不起整个 TUI
        shell._session = None
        with mock.patch.object(tui, "user_config_dir", return_value=config_dir), \
                mock.patch.object(tui, "PromptSession") as session_cls, \
                mock.patch.object(tui, "SlashCompleter"), \
                mock.patch.object(tui.Tui, "_key_bindings"):
            shell._input_session()
        return session_cls.call_args.kwargs["history"].filename

    @staticmethod
    def mode(path) -> int:
        import stat

        return stat.S_IMODE(os.stat(path).st_mode)

    def test_fresh_dirs_and_file_are_private(self) -> None:
        config_dir = self.root / "home" / "xiaoyu"
        filename = self.open_history(config_dir)
        self.assertEqual(self.mode(filename), 0o600)
        #  本次新建的每一层都是 0700
        self.assertEqual(self.mode(config_dir), 0o700)
        self.assertEqual(self.mode(config_dir.parent), 0o700)

    def test_existing_loose_file_tightened_dir_left_alone(self) -> None:
        config_dir = self.root / "xiaoyu"
        config_dir.mkdir()
        os.chmod(config_dir, 0o755)
        history = config_dir / "input_history"
        history.write_text("\n# 旧版按 umask 建的\n+ls\n", encoding="utf-8")
        os.chmod(history, 0o644)
        self.open_history(config_dir)
        self.assertEqual(self.mode(history), 0o600)
        #  已存在的目录可能是用户自己建的，不替人改权限；内容原样
        self.assertEqual(self.mode(config_dir), 0o755)
        self.assertIn("+ls", history.read_text(encoding="utf-8"))


@unittest.skipUnless(HAS_TUI, "未安装 tui 可选依赖")
class TestRequestSpinner(unittest.TestCase):
    """等模型时的活区指示：补上"请求已发出、还没有任何输出"那段空白。"""

    def build(self):
        from rich.console import Console

        from xiaoyu.tui import RichSink

        #  force_terminal：活区只在真终端上开，否则这几条断言全是空转
        sink = RichSink(Console(file=io.StringIO(), force_terminal=True, width=80))
        self.addCleanup(sink.interrupt)  # 断言失败时别把 spinner 线程留下
        return sink

    def test_tool_progress_updates_the_live_line(self) -> None:
        from xiaoyu.events import ToolCompleted, ToolProgress, ToolRunning

        sink = self.build()
        sink.emit(ToolRunning("mcp__s__slow", {}))
        self.assertIsNotNone(sink._running_line)
        sink.emit(ToolProgress(name="mcp__s__slow", message="step 1", progress=1, total=3))
        self.assertEqual(sink._running_line.progress, "1/3（33%） · step 1")
        #  控制序列按终端注入面处理
        sink.emit(ToolProgress(name="mcp__s__slow", message="x\x1b[31my", progress=2, total=3))
        self.assertNotIn("\x1b", sink._running_line.progress)
        sink.emit(ToolCompleted("mcp__s__slow", "done", True, 0.1))
        self.assertIsNone(sink._running_line)
        #  没有活区时进度直接丢，不崩
        sink.emit(ToolProgress(name="mcp__s__slow", message="late", progress=3, total=3))

    def test_spinner_runs_while_waiting_for_the_model(self) -> None:
        from xiaoyu.events import RequestStarted

        sink = self.build()
        self.assertIsNone(sink._status)
        sink.emit(RequestStarted("deepseek-flash"))
        self.assertIsNotNone(sink._status, "等模型期间必须有活区指示")

    def test_first_text_delta_takes_down_the_spinner(self) -> None:
        """正文是裸 print，活区必须先收掉——否则 rich 的刷新线程会和它抢同一片
        屏幕（RichSink 文档注释里记的那条风险）。"""
        from xiaoyu.events import RequestStarted, TextDelta

        sink = self.build()
        #  redirect 必须套在 Live 之外：rich 的 Live 启动时会接管 sys.stdout /
        #  sys.stderr，停止时把它们还原成**启动那一刻**的对象。在 Live 内部再
        #  redirect，stop() 会把它一并冲掉（正文就漏到真实终端上了）。
        with contextlib.redirect_stdout(io.StringIO()):
            sink.emit(RequestStarted("m"))
            sink.emit(TextDelta("答"))
            self.assertIsNone(sink._status)

    def test_tool_call_takes_down_the_spinner(self) -> None:
        """模型决定调工具：思考中到此为止，接下来是工具自己的 spinner。"""
        from xiaoyu.events import RequestStarted, ToolPending

        sink = self.build()
        sink.emit(RequestStarted("m"))
        sink.emit(ToolPending("bash", {"command": "ls"}))
        self.assertIsNone(sink._status)

    def test_request_ended_is_the_backstop(self) -> None:
        """一个字都没吐就结束（空响应/异常/中断）时，活区靠它收掉。"""
        from xiaoyu.events import RequestEnded, RequestStarted

        sink = self.build()
        sink.emit(RequestStarted("m"))
        sink.emit(RequestEnded())
        self.assertIsNone(sink._status)

    def test_quiet_subagent_does_not_open_a_second_live_region(self) -> None:
        """同一个 Console 同时只允许一个 Live；子 agent 抢开会把父级 spinner 搅花。"""
        from xiaoyu.events import RequestStarted

        sink = self.build()
        child = sink.quiet_child()
        child.emit(RequestStarted("m"))
        self.assertIsNone(child._status)

    def test_wait_line_names_the_model_and_counts_seconds(self) -> None:
        import time

        from xiaoyu.tui import _RunningLine

        line = _RunningLine("deepseek-flash", verb="思考中")
        line.started = time.monotonic() - 2
        text = line.__rich__().plain
        self.assertIn("deepseek-flash 思考中", text)
        self.assertIn("2s", text)
        self.assertIn("Ctrl-C 中断", text)


@unittest.skipUnless(HAS_TUI, "未安装 tui 可选依赖")
class TestNoticeCapture(unittest.TestCase):
    """第三方库的 warn/log 收进事件流。"""

    def build(self):
        from rich.console import Console

        from xiaoyu.tui import RichSink

        buffer = io.StringIO()
        return RichSink(Console(file=buffer, width=80)), buffer

    def test_warnings_and_logs_go_through_the_sink(self) -> None:
        import logging
        import warnings

        from xiaoyu.tui import NoticeCapture

        sink, buffer = self.build()
        #  过滤规则自己定：DeprecationWarning 默认是被忽略的，能不能走到
        #  showwarning 取决于跑用例时的 -W 参数——测的是"走到了之后去哪"
        with warnings.catch_warnings():
            warnings.simplefilter("always")
            with NoticeCapture(sink):
                warnings.warn("接口要弃用了", DeprecationWarning, stacklevel=1)
                logging.getLogger("httpx").error("连接被重置")
        out = buffer.getvalue()
        self.assertIn("DeprecationWarning: 接口要弃用了", out)
        self.assertIn("httpx: 连接被重置", out)

    def test_everything_is_restored_on_exit(self) -> None:
        """劫持必须是临时的：退出后第三方的 warn/log 要回到原来的去处。"""
        import logging
        import warnings

        from xiaoyu.tui import NoticeCapture

        sink, _buffer = self.build()
        original = warnings.showwarning
        with NoticeCapture(sink):
            self.assertIsNot(warnings.showwarning, original)
        self.assertIs(warnings.showwarning, original)
        self.assertEqual(logging.getLogger().handlers, [])

    def test_does_not_steal_a_configured_logging_setup(self) -> None:
        """宿主自己配过 logging 就别抢——那是人家的输出编排。"""
        import logging

        from xiaoyu.tui import NoticeCapture

        sink, _buffer = self.build()
        root = logging.getLogger()
        existing = logging.NullHandler()
        root.addHandler(existing)
        self.addCleanup(root.removeHandler, existing)
        with NoticeCapture(sink):
            self.assertEqual(root.handlers, [existing])

    def test_stdout_is_not_hijacked(self) -> None:
        """流式正文走裸 print，全局劫持 stdout 会把自己的正文吃掉。"""
        import sys

        from xiaoyu.tui import NoticeCapture

        sink, _buffer = self.build()
        with NoticeCapture(sink):
            self.assertIs(sys.stdout, sys.__stdout__)


@unittest.skipUnless(HAS_TUI, "未安装 tui 可选依赖")
class TestTypeaheadDrain(AgentTestCase):
    """等待期敲的整行输入要被接住，而不是连回车一起被下一轮 prompt 吃掉。"""

    def drain_with_stdin(self, pending: bytes) -> str:
        import os
        import sys

        try:
            import pty
        except ImportError:
            self.skipTest("本平台没有 pty")

        from rich.console import Console

        from xiaoyu.permissions import Permissions
        from xiaoyu.tui import Tui

        master, slave = pty.openpty()
        self.addCleanup(os.close, master)
        self.addCleanup(os.close, slave)
        if pending:
            os.write(master, pending)

        class _Stdin:
            def fileno(self) -> int:
                return slave

            def isatty(self) -> bool:
                return True

        tui = Tui(Permissions(self.root), console=Console(file=io.StringIO()))
        saved = sys.stdin
        sys.stdin = _Stdin()
        try:
            return tui._drain_typeahead()
        finally:
            sys.stdin = saved

    def test_completed_lines_are_captured(self) -> None:
        typed = "顺便把测试也跑一下"
        self.assertEqual(self.drain_with_stdin(f"{typed}\n".encode()), typed)

    def test_multiple_lines_are_kept(self) -> None:
        self.assertEqual(self.drain_with_stdin("第一句\n第二句\n".encode()), "第一句\n第二句")

    def test_nothing_typed_gives_empty(self) -> None:
        self.assertEqual(self.drain_with_stdin(b""), "")


class TestMakeFrontend(AgentTestCase):
    def test_non_tty_falls_back_to_plain(self) -> None:
        from xiaoyu.permissions import Permissions

        #  单测环境 stdin/stdout 不是 tty，应直接选明文 REPL 且不给提示
        approver, sink, repl_fn, note, asker, secret = make_frontend(Permissions(self.root))
        self.assertIsNone(sink)
        self.assertIs(repl_fn, repl)
        self.assertIsNone(note)
        #  明文 REPL 也有人在：提问通道是编号问答，不是 None；sudo 密码通道同理
        from xiaoyu.cli import text_ask_questions, text_secret_prompt

        self.assertIs(asker, text_ask_questions)
        self.assertIs(secret, text_secret_prompt)

    def test_no_tui_flag_forces_plain(self) -> None:
        from xiaoyu.permissions import Permissions

        with mock.patch("sys.stdin") as fake_in, mock.patch("sys.stdout") as fake_out:
            fake_in.isatty.return_value = True
            fake_out.isatty.return_value = True
            approver, sink, repl_fn, note, asker, _secret = make_frontend(
                Permissions(self.root), no_tui=True
            )
        self.assertIsNone(sink)
        self.assertIs(repl_fn, repl)

    @unittest.skipUnless(HAS_TUI, "未安装 tui 可选依赖")
    def test_tty_with_deps_selects_tui(self) -> None:
        from xiaoyu.permissions import Permissions
        from xiaoyu.tui import RichSink

        with mock.patch("sys.stdin") as fake_in, mock.patch("sys.stdout") as fake_out:
            fake_in.isatty.return_value = True
            fake_out.isatty.return_value = True
            approver, sink, repl_fn, note, asker, secret = make_frontend(Permissions(self.root))
        self.assertIsInstance(sink, RichSink)
        self.assertIsNone(note)
        self.assertEqual(secret.__name__, "secret_prompt")
        #  approver / repl / asker 来自同一个 Tui 实例
        self.assertEqual(approver.__self__, repl_fn.__self__)
        self.assertEqual(asker.__self__, repl_fn.__self__)


if __name__ == "__main__":
    unittest.main()


@unittest.skipUnless(HAS_TUI, "未安装 tui 可选依赖")
class TestPrefixLexer(unittest.TestCase):
    """输入行只给行首前缀上色，判定与提交路由同一函数。"""

    def _fragments(self, text: str, lineno: int = 0):
        from prompt_toolkit.document import Document

        from xiaoyu.tui import PrefixLexer

        return PrefixLexer().lex_document(Document(text))(lineno)

    def test_prefixes_take_their_token_color(self) -> None:
        from xiaoyu import theme

        for text, token in (
            ("! brew upgrade", "status.warning"),
            ("/help", "text.accent"),
            ("# 测试统一跑 unittest", "text.secondary"),
            ("!", "status.warning"),  # 只敲了前缀：颜色先亮
        ):
            with self.subTest(text=text):
                fragments = self._fragments(text)
                self.assertEqual(fragments[0], (theme.ptk(token), text[0]))
                self.assertEqual("".join(piece for _style, piece in fragments), text)
                self.assertTrue(all(style == "" for style, _piece in fragments[1:]))

    def test_non_prefix_text_stays_default(self) -> None:
        for text in ("echo a ! b", "/Users/me/简历.pdf 看看", "/etc/hosts 是什么", "普通一句话"):
            with self.subTest(text=text):
                self.assertEqual(self._fragments(text), [("", text)])

    def test_only_first_line_is_examined(self) -> None:
        self.assertEqual(self._fragments("第一行\n! 第二行", lineno=1), [("", "! 第二行")])


@unittest.skipUnless(HAS_TUI, "未安装 tui 可选依赖")
class TestNextStepSuggest(unittest.TestCase):
    """输入行幽灵建议：空行给「接着问」第一条，敲字按前缀选，都不中退回历史。"""

    def _suggester(self, items: list[str]):
        from xiaoyu.tui import NextStepSuggest

        return NextStepSuggest(lambda: items)

    def _buffer(self, history: list[str]):
        from prompt_toolkit.buffer import Buffer
        from prompt_toolkit.history import InMemoryHistory

        memory = InMemoryHistory()
        for line in history:
            memory.append_string(line)
        return Buffer(history=memory)

    def test_empty_line_shows_first_suggestion(self) -> None:
        from prompt_toolkit.document import Document

        buffer = self._buffer(["! ls"])
        got = self._suggester(["跑测试", "提交"]).get_suggestion(buffer, Document(""))
        self.assertEqual(got.text, "跑测试")
        self.assertIsNone(self._suggester([]).get_suggestion(buffer, Document("")))

    def test_prefix_picks_a_later_suggestion(self) -> None:
        from prompt_toolkit.document import Document

        buffer = self._buffer([])
        got = self._suggester(["跑测试", "提交到 main"]).get_suggestion(buffer, Document("提交"))
        self.assertEqual(got.text, "到 main")
        #  已经敲完整条：没有剩余可补，别给一个空建议
        self.assertIsNone(self._suggester(["提交"]).get_suggestion(buffer, Document("提交")))

    def test_falls_back_to_history(self) -> None:
        from prompt_toolkit.document import Document

        buffer = self._buffer(["! brew upgrade arp-scan"])
        got = self._suggester(["跑测试"]).get_suggestion(buffer, Document("! brew"))
        self.assertEqual(got.text, " upgrade arp-scan")


@unittest.skipUnless(HAS_TUI, "未安装 tui 可选依赖")
class TestStreamStyler(unittest.TestCase):
    """流式正文的行级轻渲染：每个字符只打一次；去掉样式码后与原文逐字一致。"""

    _ANSI = __import__("re").compile(r"\x1b\[[0-9;]*m")

    def build(self):
        from rich.console import Console

        from xiaoyu.tui import RichSink

        buffer = io.StringIO()
        #  legacy_windows=False：这组测的是 ANSI 直写那条路。Windows CI 的 runner 没有
        #  VT 控制台，rich 会判成旧式控制台走 console.print 回退（tab 被展开），
        #  那是刻意保留的退路，不是这里要验的行为
        console = Console(
            file=buffer, force_terminal=True, soft_wrap=True, highlight=False, width=80,
            legacy_windows=False,
        )
        return RichSink(console), buffer

    def stream(self, text: str, chunk: int) -> str:
        from xiaoyu.events import TextDelta, TextEnd

        sink, buffer = self.build()
        #  OSC 133 锚点与收尾换行走裸 print，不进 Console 的缓冲
        with contextlib.redirect_stdout(io.StringIO()):
            for start in range(0, len(text), chunk):
                sink.emit(TextDelta(text[start : start + chunk]))
            sink.emit(TextEnd())
        return buffer.getvalue()

    SAMPLE = (
        "# 标题行\n"
        "先用 `subprocess.run` 跑一下，**注意** 2*3 这种星号不是粗体：\n"
        "```python\n"
        'def f():\n    """doc\n    more"""\n    return "s"\n'
        "```\n"
        "~~~\n"
        "no lang here\n"
        "~~~\n"
        "\n"
        "结尾没有换行"
    )

    def test_plain_text_survives_any_chunking(self) -> None:
        """分片边界落在哪都不该改变打出去的字符：标记符保留、不丢不重。"""
        for chunk in (1, 2, 3, 7, 1000):
            with self.subTest(chunk=chunk):
                self.assertEqual(self._ANSI.sub("", self.stream(self.SAMPLE, chunk)), self.SAMPLE)

    def test_fenced_code_is_highlighted_and_fences_dimmed(self) -> None:
        out = self.stream(self.SAMPLE, 5)
        lines = out.split("\n")
        code = next(line for line in lines if "return" in line)
        #  围栏内的行带语法样式码；围栏线本身只弱化
        self.assertIn("\x1b[", code)
        self.assertRegex(code, r"\x1b\[[0-9;]*m\s*return")
        fence = next(line for line in lines if self._ANSI.sub("", line) == "```python")
        self.assertIn("\x1b[2m", fence)
        #  多行字符串的续行按字符串着色：着色用的是整块上下文而不是孤立的一行
        more = next(line for line in lines if "more" in line)
        self.assertIn("\x1b[", more)
        #  没有语言名的围栏不炸、原样打出
        self.assertIn("no lang here", self._ANSI.sub("", out))

    def test_inline_markup_outside_fences(self) -> None:
        out = self.stream(self.SAMPLE, 4)
        lines = out.split("\n")
        heading = lines[0]
        self.assertIn("\x1b[1m", heading)  # 标题行加粗
        prose = lines[1]
        plain = self._ANSI.sub("", prose)
        self.assertIn("`subprocess.run`", plain)
        self.assertIn("**注意**", plain)
        #  行内代码整段着色、粗体整段加粗（分片边界会把一段切成多个片段，按字符查
        #  样式而不是查样式码的位置）；单个星号（2*3）不触发任何样式切换
        from rich.console import Console
        from rich.text import Text

        console = Console(file=io.StringIO(), force_terminal=True)
        rendered = Text.from_ansi(prose)
        start = plain.index("subprocess.run")
        for offset in range(start, start + len("subprocess.run")):
            self.assertIsNotNone(rendered.get_style_at_offset(console, offset).color)
        start = plain.index("注意")
        self.assertTrue(rendered.get_style_at_offset(console, start).bold)
        start = plain.index("2*3")
        for offset in range(start, start + 3):
            style = rendered.get_style_at_offset(console, offset)
            self.assertFalse(style.bold)
            self.assertIsNone(style.color)

    def test_unclosed_fence_and_partial_line_flush_on_end(self) -> None:
        """围栏没闭合 / 末行没换行：TextEnd 要把攒着的东西全打出来。"""
        text = "```sh\nls -la"
        self.assertEqual(self._ANSI.sub("", self.stream(text, 3)), text)

    def test_interrupt_flushes_pending_text(self) -> None:
        from xiaoyu.events import TextDelta

        sink, buffer = self.build()
        with contextlib.redirect_stdout(io.StringIO()):
            sink.emit(TextDelta("半行 `co"))
            sink.interrupt()
        self.assertEqual(self._ANSI.sub("", buffer.getvalue()), "半行 `co")

    def test_non_terminal_console_prints_raw(self) -> None:
        """管道 / 非 tty：逐字节直出，连样式码都没有（与 test_delta_streams_to_stdout 同口径）。"""
        from rich.console import Console

        from xiaoyu.events import TextDelta, TextEnd
        from xiaoyu.tui import RichSink

        sink = RichSink(Console(file=io.StringIO(), soft_wrap=True, highlight=False))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            sink.emit(TextDelta("```py\nx = 1\n```"))
            sink.emit(TextEnd())
        self.assertEqual(out.getvalue(), "```py\nx = 1\n```\n")

    def test_state_resets_between_text_blocks(self) -> None:
        """上一段正文以未闭合的围栏结束，下一段正文不能还当自己在代码块里。"""
        from xiaoyu.events import TextDelta, TextEnd

        sink, buffer = self.build()
        with contextlib.redirect_stdout(io.StringIO()):
            sink.emit(TextDelta("```\ncode"))
            sink.emit(TextEnd())
            sink.emit(TextDelta("# 新标题\n"))
            sink.emit(TextEnd())
        lines = buffer.getvalue().split("\n")
        heading = next(line for line in lines if "新标题" in line)
        self.assertIn("\x1b[1m", heading)

    def test_tabs_in_code_keep_tabs_and_colors(self) -> None:
        """围栏内的 tab 原样打出（Makefile / Go 的 tab 不能变空格），样式按单元格
        位置映射回原行，tab 后面的 token 仍然着色。"""
        from rich.console import Console
        from rich.text import Text

        text = "```go\nfunc main() {\n\t中文 := 1\n\t\treturn \"s\"\n}\n```\n"
        out = self.stream(text, 6)
        self.assertEqual(self._ANSI.sub("", out), text)
        console = Console(file=io.StringIO(), force_terminal=True)
        line = next(row for row in out.split("\n") if "return" in row)
        rendered = Text.from_ansi(line)
        start = rendered.plain.index('"s"')
        self.assertIsNotNone(rendered.get_style_at_offset(console, start).color)
        start = rendered.plain.index("return")
        self.assertIsNotNone(rendered.get_style_at_offset(console, start).color)


@unittest.skipUnless(HAS_TUI, "未安装 tui 可选依赖")
class TestSearchSelect(unittest.TestCase):
    """会话选择器：敲字过滤、无数字直选、序号沿用原编号、窗口恒高。"""

    OPTIONS = [
        ("a", "2026-08-09T10:00  deepseek  zhinu  修 calc.py", ""),
        ("b", "2026-08-10T09:00  gpt  zhinu  写 README", ""),
        ("c", "2026-08-10T11:00  deepseek  noc  排查 fallback", ""),
    ]

    def run_select(self, keys_text: str, options=None, visible: int = 10):
        from prompt_toolkit.application import create_app_session
        from prompt_toolkit.input.defaults import create_pipe_input
        from prompt_toolkit.output import DummyOutput

        from xiaoyu.tui import search_select

        with create_pipe_input() as pipe:
            pipe.send_text(keys_text)
            with create_app_session(input=pipe, output=DummyOutput()):
                return search_select("切到哪个会话？", options or self.OPTIONS, visible=visible)

    def test_typing_filters_and_enter_picks_first_match(self) -> None:
        self.assertEqual(self.run_select("\r"), "a")  # 没过滤：第一项
        self.assertEqual(self.run_select("noc\r"), "c")
        self.assertEqual(self.run_select("README\r"), "b")
        #  空格分词，每个词都要命中；大小写不敏感
        self.assertEqual(self.run_select("DEEPSEEK 0810\r"), "c")

    def test_digits_are_query_not_shortcuts(self) -> None:
        """时间戳就是数字：敲 "0810" 是在找 8 月 10 日的会话，不是跳到第 0 项。"""
        self.assertEqual(self.run_select("0810\r"), "b")
        self.assertEqual(self.run_select("2\r"), "a")  # 三条都含 "2"，仍是第一项

    def test_arrows_move_within_matches_and_backspace_edits(self) -> None:
        self.assertEqual(self.run_select("\x1b[B\r"), "b")  # ↓
        self.assertEqual(self.run_select("deepseek\x1b[B\r"), "c")  # 过滤后 ↓ 只在匹配里走
        self.assertEqual(self.run_select("\x1b[A\r"), "c")  # ↑ 绕到末尾
        #  退格改词：noX → no → 命中 noc
        self.assertEqual(self.run_select("noX\x7f\r"), "c")

    def test_no_match_enter_is_noop_and_esc_cancels(self) -> None:
        self.assertIsNone(self.run_select("zzz\r\x03"))
        self.assertIsNone(self.run_select("\x03"))
        #  删掉不匹配的词之后又能选
        self.assertEqual(self.run_select("zzz\x7f\x7f\x7f\r"), "a")

    def test_fragments_keep_height_and_original_numbers(self) -> None:
        """渲染内容：序号沿用原列表编号、窗口外的数量有交代、高度不随匹配数变。"""
        from prompt_toolkit.application import create_app_session
        from prompt_toolkit.input.defaults import create_pipe_input
        from prompt_toolkit.output import DummyOutput

        from xiaoyu import tui as tui_module

        captured: list[list[tuple[str, str]]] = []
        original = tui_module.FormattedTextControl

        class Spy(original):
            def __init__(self, fragments, **kwargs):
                captured.append(fragments)
                super().__init__(fragments, **kwargs)

        many = [(i, f"2026-08-{i:02d}  会话 {i}", "") for i in range(1, 16)]
        with mock.patch.object(tui_module, "FormattedTextControl", Spy):
            with create_pipe_input() as pipe:
                pipe.send_text("\x03")
                with create_app_session(input=pipe, output=DummyOutput()):
                    tui_module.search_select("标题", many, visible=5)
        text_all = "".join(t for _s, t in captured[0]())
        self.assertIn("  1. 2026-08-01", text_all)
        self.assertIn("还有 10 个", text_all)
        self.assertNotIn(" 6. ", text_all)
        self.assertIn("输入文字 过滤", text_all)
        self.assertNotIn("Tab", text_all)
        #  不同匹配数下行数一致（标题 + 过滤行 + visible + 提示）
        self.assertEqual(text_all.count("\n"), 5 + 2)


@unittest.skipUnless(HAS_TUI, "未安装 tui 可选依赖")
class TestEditDiffOnCompletion(AgentTestCase):
    """没经确认框预览就放行的编辑（auto / --yolo / allow 规则）：完成行带 +N −M，
    diff 折叠进留底（Ctrl-O 回放带红绿）或 detail 下直接展开。"""

    def make_sink(self):
        from rich.console import Console

        from xiaoyu.tui import RichSink

        buffer = io.StringIO()
        sink = RichSink(Console(file=buffer, soft_wrap=True, highlight=False, width=100))
        sink.workspace = self.root
        sink.begin_turn()
        return sink, buffer

    def test_str_replace_counts_and_folded_diff(self) -> None:
        from xiaoyu.events import ToolCompleted, ToolPending

        sink, buffer = self.make_sink()
        sink.emit(ToolPending("str_replace", {"path": "calc.py", "old_str": "a\nfoo = 1\nb", "new_str": "a\nfoo = 2\nb"}))
        sink.emit(ToolCompleted("str_replace", output="已替换 calc.py 第 2 行处的内容\n改动后该处上下文：\nfoo = 2", ok=True, seconds=0.0))
        out = buffer.getvalue()
        self.assertIn("⎿ 已替换 calc.py", out)
        self.assertIn("+1 −1", out)
        #  diff 顶掉了"改动后上下文"那几行：未展开时都不在屏上
        self.assertNotIn("改动后该处上下文", out)
        self.assertEqual(len(sink.folded), 1)
        anchor, rows = sink.folded[0]
        self.assertIn("+1 −1", anchor)
        styles = {style for row in rows for style, _t in row}
        self.assertIn("diff.added", styles)
        self.assertIn("diff.removed", styles)
        sink.replay_folded()
        out = buffer.getvalue()
        self.assertIn("-foo = 1", out)
        self.assertIn("+foo = 2", out)

    def test_write_file_reads_previous_content_from_workspace(self) -> None:
        from xiaoyu.events import ToolCompleted, ToolPending

        (self.root / "notes.md").write_text("one\ntwo\n", encoding="utf-8")
        sink, buffer = self.make_sink()
        #  相对路径按工作区解析，改前内容在工具动手之前读到
        sink.emit(ToolPending("write_file", {"path": "notes.md", "content": "one\nthree\nfour\n"}))
        sink.emit(ToolCompleted("write_file", output="已覆盖 notes.md：15 字符 / 3 行", ok=True, seconds=0.0))
        self.assertIn("+2 −1", buffer.getvalue())
        #  新建文件：全是 +，没有假的 "-" 空行
        sink2, buffer2 = self.make_sink()
        sink2.emit(ToolPending("write_file", {"path": "brand_new.txt", "content": "x\ny"}))
        sink2.emit(ToolCompleted("write_file", output="已创建 brand_new.txt：3 字符 / 2 行", ok=True, seconds=0.0))
        self.assertIn("+2 −0", buffer2.getvalue())

    def test_previewed_in_confirm_box_is_not_repeated(self) -> None:
        """确认框已把 diff 打进 scrollback 的，完成行不再补一份。"""
        from xiaoyu.events import ToolCompleted, ToolPending

        sink, buffer = self.make_sink()
        sink.emit(ToolPending("str_replace", {"path": "calc.py", "old_str": "x = 1", "new_str": "x = 2"}))
        sink.note_previewed()
        sink.emit(ToolCompleted("str_replace", output="已替换 calc.py 第 1 行处的内容\n上下文", ok=True, seconds=0.0))
        out = buffer.getvalue()
        self.assertNotIn("−", out)
        self.assertEqual(sink.folded[0][1], ["上下文"])

    def test_detail_mode_expands_diff_and_failure_has_no_counts(self) -> None:
        from xiaoyu.events import ToolCompleted, ToolPending

        sink, buffer = self.make_sink()
        sink.detail = True
        sink.emit(ToolPending("str_replace", {"path": "calc.py", "old_str": "x = 1", "new_str": "x = 2"}))
        sink.emit(ToolCompleted("str_replace", output="已替换 calc.py 第 1 行处的内容", ok=True, seconds=0.0))
        out = buffer.getvalue()
        self.assertIn("+1 −1", out)
        self.assertIn("+x = 2", out)
        self.assertEqual(sink.folded, [])
        #  失败：没有改动，不报计数；快照用完即清，下一个工具不受影响
        sink2, buffer2 = self.make_sink()
        sink2.emit(ToolPending("str_replace", {"path": "calc.py", "old_str": "x = 1", "new_str": "x = 2"}))
        sink2.emit(ToolCompleted("str_replace", output="ERROR: 文件不存在", ok=False, seconds=0.0))
        self.assertNotIn("−", buffer2.getvalue())
        self.assertIsNone(sink2._edit)

    def test_confirm_flow_marks_preview(self) -> None:
        """走确认框的编辑：预览打过之后 sink 的快照被清，完成行不重复。"""
        from xiaoyu.events import ToolPending
        from xiaoyu.tui import Tui
        from xiaoyu.permissions import Permissions
        from rich.console import Console

        tui = Tui(Permissions(self.root), console=Console(file=io.StringIO(), soft_wrap=True))
        tui.agent = self.build([])
        tui.sink.emit(ToolPending("str_replace", {"path": "calc.py", "old_str": "x = 1", "new_str": "x = 2"}))
        self.assertIsNotNone(tui.sink._edit)
        with mock.patch.object(tui, "_inline_select", return_value="once"):
            tui.confirm("str_replace", {"path": "calc.py", "old_str": "x = 1", "new_str": "x = 2"})
        self.assertIsNone(tui.sink._edit)

    def test_diff_rows_treat_empty_side_as_absent(self) -> None:
        from xiaoyu.tui import _diff_counts, _diff_rows

        self.assertEqual(_diff_counts(_diff_rows("", "a\nb", 80)), (2, 0))
        self.assertEqual(_diff_counts(_diff_rows("a\nb", "", 80)), (0, 2))
