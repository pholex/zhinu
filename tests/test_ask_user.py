"""ask_user 工具与通用提问面板的测试。

三层：
1. normalize_questions 校验与 handler 语义（无 TUI 依赖，永远跑）；
2. 明文 asker（编号问答）的解析（mock input，永远跑）；
3. question_select / ask_questions 面板（管道输入真实驱动，有依赖才跑）。
"""

from __future__ import annotations

import contextlib
import io
import unittest
from unittest import mock

from xiaoyu.agent import PLAN_MODE_TOOLS, normalize_questions
from xiaoyu.cli import ask_one_text, text_ask_questions

try:
    import prompt_toolkit  # noqa: F401
    import rich  # noqa: F401

    HAS_TUI = True
except ImportError:
    HAS_TUI = False

from .test_agent_paths import AgentTestCase

QUESTION = {
    "question": "用哪个方案？",
    "options": [
        {"label": "方案 A", "description": "稳妥"},
        {"label": "方案 B"},
    ],
}


class TestNormalizeQuestions(unittest.TestCase):
    def test_valid_input_is_normalized(self) -> None:
        result = normalize_questions([QUESTION])
        self.assertIsInstance(result, list)
        self.assertEqual(result[0]["question"], "用哪个方案？")
        self.assertEqual(
            result[0]["options"],
            [
                {"label": "方案 A", "description": "稳妥"},
                {"label": "方案 B", "description": ""},
            ],
        )
        self.assertFalse(result[0]["multi_select"])

    def test_bare_string_option_is_accepted_as_label(self) -> None:
        result = normalize_questions([{"question": "q?", "options": ["甲", "乙"]}])
        self.assertEqual(result[0]["options"][0], {"label": "甲", "description": ""})

    def test_invalid_shapes_return_error_text(self) -> None:
        for bad in (
            None,
            [],
            "not a list",
            [{"options": [{"label": "a"}]}],  # 缺 question
            [{"question": "q?"}],  # 缺 options
            [{"question": "q?", "options": []}],
            [{"question": "q?", "options": [{"label": ""}]}],  # 全空 label
            [{"question": "q?", "options": [123]}],
            [{"question": f"q{i}?", "options": ["a"]} for i in range(5)],  # 超 4 题
            [{"question": "q?", "options": [str(i) for i in range(10)]}],  # 超 9 项
        ):
            with self.subTest(bad=bad):
                self.assertIsInstance(normalize_questions(bad), str)

    def test_empty_labels_are_dropped_but_valid_ones_kept(self) -> None:
        result = normalize_questions(
            [{"question": "q?", "options": [{"label": " "}, {"label": "留下"}]}]
        )
        self.assertEqual(len(result[0]["options"]), 1)


class TestAskUserTool(AgentTestCase):
    def test_tool_hidden_without_asker_and_visible_with(self) -> None:
        agent = self.build([])
        names = [schema["function"]["name"] for schema in agent.toolbox.schemas()]
        self.assertNotIn("ask_user", names)
        agent.asker = lambda questions: {}
        names = [schema["function"]["name"] for schema in agent.toolbox.schemas()]
        self.assertIn("ask_user", names)

    def test_tool_needs_no_approval_and_is_allowed_in_plan_mode(self) -> None:
        agent = self.build([], asker=lambda questions: {})
        tool = agent.toolbox.get("ask_user")
        self.assertFalse(tool.requires_approval)
        self.assertIn("ask_user", PLAN_MODE_TOOLS)

    def test_run_without_asker_is_unavailable(self) -> None:
        agent = self.build([])
        out = agent.toolbox.run("ask_user", {"questions": [QUESTION]})
        self.assertIn("不可用", out)

    def test_handler_passes_normalized_questions_and_formats_answers(self) -> None:
        captured: dict = {}

        def asker(questions):
            captured["questions"] = questions
            return {"用哪个方案？": "方案 A"}

        agent = self.build([], asker=asker)
        out = agent.toolbox.run("ask_user", {"questions": [QUESTION]})
        self.assertEqual(captured["questions"][0]["question"], "用哪个方案？")
        self.assertIn("用户的回答", out)
        self.assertIn("方案 A", out)
        self.assertNotIn("没有回答其余问题", out)

    def test_dismissed_returns_guidance_not_error(self) -> None:
        agent = self.build([], asker=lambda questions: {})
        out = agent.toolbox.run("ask_user", {"questions": [QUESTION]})
        self.assertNotIn("ERROR", out)
        self.assertIn("关闭了提问", out)

    def test_partial_answers_name_the_skipped_questions(self) -> None:
        second = {"question": "还要测试吗？", "options": [{"label": "要"}, {"label": "不要"}]}
        agent = self.build([], asker=lambda questions: {"用哪个方案？": "方案 B"})
        out = agent.toolbox.run("ask_user", {"questions": [QUESTION, second]})
        self.assertIn("方案 B", out)
        self.assertIn("还要测试吗？", out)
        self.assertIn("没有回答其余问题", out)

    def test_invalid_args_return_error_for_model_to_retry(self) -> None:
        agent = self.build([], asker=lambda questions: {})
        self.assertIn("ERROR", agent.toolbox.run("ask_user", {"questions": "bad"}))
        self.assertIn("ERROR", agent.toolbox.run("ask_user", {"questions": [QUESTION], "x": 1}))


class TestTextAsker(unittest.TestCase):
    def ask(self, answers: list[str], questions=None):
        questions = questions or [QUESTION]
        with contextlib.redirect_stdout(io.StringIO()):
            with mock.patch("builtins.input", side_effect=answers):
                return text_ask_questions(questions)

    def test_digit_picks_option(self) -> None:
        self.assertEqual(self.ask(["2"]), {"用哪个方案？": "方案 B"})

    def test_free_text_is_kept_verbatim(self) -> None:
        self.assertEqual(self.ask(["都不好，先写测试"]), {"用哪个方案？": "都不好，先写测试"})

    def test_out_of_range_digit_is_free_text(self) -> None:
        self.assertEqual(self.ask(["9"]), {"用哪个方案？": "9"})

    def test_multi_select_joins_labels(self) -> None:
        multi = {
            "question": "开哪些？",
            "options": [{"label": "甲"}, {"label": "乙"}, {"label": "丙"}],
            "multi_select": True,
        }
        self.assertEqual(self.ask(["1 3"], [multi]), {"开哪些？": "甲, 丙"})

    def test_single_select_takes_first_digit_only(self) -> None:
        self.assertEqual(self.ask(["1 2"]), {"用哪个方案？": "方案 A"})

    def test_empty_answer_stops_asking(self) -> None:
        second = {"question": "q2?", "options": [{"label": "a"}, {"label": "b"}]}
        self.assertEqual(self.ask([""], [QUESTION, second]), {})

    def test_eof_stops_asking(self) -> None:
        with contextlib.redirect_stdout(io.StringIO()):
            with mock.patch("builtins.input", side_effect=EOFError):
                self.assertEqual(text_ask_questions([QUESTION]), {})

    def test_second_question_gets_answered_too(self) -> None:
        second = {"question": "q2?", "options": [{"label": "a"}, {"label": "b"}]}
        self.assertEqual(
            self.ask(["1", "2"], [QUESTION, second]),
            {"用哪个方案？": "方案 A", "q2?": "b"},
        )


@unittest.skipUnless(HAS_TUI, "未安装 tui 可选依赖")
class TestQuestionPanel(unittest.TestCase):
    def drive(self, fn, keys: str):
        """用管道输入真实驱动面板（不需要 pty）。"""
        from prompt_toolkit.application import create_app_session
        from prompt_toolkit.input.defaults import create_pipe_input
        from prompt_toolkit.output import DummyOutput

        with create_pipe_input() as pipe:
            pipe.send_text(keys)
            with create_app_session(input=pipe, output=DummyOutput()):
                return fn()

    def select(self, keys: str, **kwargs):
        from xiaoyu.tui import question_select

        options = [("方案 A", "稳妥"), ("方案 B", ""), ("其他（自由输入）", "")]
        return self.drive(lambda: question_select("用哪个方案？", options, **kwargs), keys)

    def test_single_select_digit_arrows_enter_and_cancel(self) -> None:
        self.assertEqual(self.select("2"), 1)  # 数字直选
        self.assertEqual(self.select("\r"), 0)  # 默认第一项
        self.assertEqual(self.select("\x1b[B\r"), 1)  # ↓ + Enter
        self.assertEqual(self.select("\t"), 0)  # Tab 等同 Enter（无附言语义）
        self.assertIsNone(self.select("\x03"))  # Ctrl-C 取消
        self.assertEqual(self.select(" \r"), 0)  # 单选里 Space 无效果

    def test_multi_select_space_and_digit_toggle(self) -> None:
        self.assertEqual(self.select(" \r", multi=True), [0])  # Space 勾选当前项
        self.assertEqual(self.select("12\r", multi=True), [0, 1])  # 数字勾选
        self.assertEqual(self.select("11\r", multi=True), [])  # 再按一次取消勾选
        self.assertEqual(self.select("\r", multi=True, checked={1}), [1])  # 预置勾选保留

    def ask(self, questions, keys: str):
        from rich.console import Console

        from xiaoyu.tui import ask_questions

        buffer = io.StringIO()
        console = Console(file=buffer, soft_wrap=True, highlight=False)
        answers = self.drive(lambda: ask_questions(questions, console), keys)
        return answers, buffer.getvalue()

    def test_pick_option_echoes_into_scrollback(self) -> None:
        answers, echoed = self.ask([dict(QUESTION)], "1")
        self.assertEqual(answers, {"用哪个方案？": "方案 A"})
        self.assertIn("✔", echoed)
        self.assertIn("方案 A", echoed)

    def test_other_option_takes_free_input(self) -> None:
        #  「其他」是自动附加的第 3 项：数字 3 选中后转行内自由输入
        answers, _ = self.ask([dict(QUESTION)], "3都不要\r")
        self.assertEqual(answers, {"用哪个方案？": "都不要"})

    def test_cancel_returns_partial_answers(self) -> None:
        second = {"question": "还要测试吗？", "options": [{"label": "要"}, {"label": "不要"}]}
        answers, _ = self.ask([dict(QUESTION), second], "1\x03")
        self.assertEqual(answers, {"用哪个方案？": "方案 A"})

    def test_multi_select_combines_choice_and_other_text(self) -> None:
        multi = {
            "question": "开哪些？",
            "options": [{"label": "甲"}, {"label": "乙"}],
            "multi_select": True,
        }
        #  勾 1（甲）+ 勾 3（其他）→ Enter 提交 → 自由输入"丁"
        answers, _ = self.ask([multi], "13\r丁\r")
        self.assertEqual(answers, {"开哪些？": "甲, 丁"})


if __name__ == "__main__":
    unittest.main()
