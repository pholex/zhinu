"""提交路由单点（keys.classify_input）与 !/# 动作核心的测试。

要点仍是**断言能 fail**：
- 分类表驱动，语义逐字锁住（! 去空白、# 连打多个也认、句中 ! 不误路由）；
- 防漂移断言把 keys.BINDINGS 的 PREFIX 文档表和路由器绑在一起——
  表里加了提交前缀而路由器没跟上（或反之），这里必须红；
- user_shell / user_memo 用真 Agent（假 client，不打网络）验证灌上下文与落盘。
"""

from __future__ import annotations

import tempfile
import types
import unittest
from pathlib import Path

from xiaoyu import keys
from xiaoyu.agent import Agent
from xiaoyu.cli import user_memo, user_shell
from xiaoyu.config import Config
from xiaoyu.providers import Registry
from xiaoyu.tools import Toolbox


class TestClassifyInput(unittest.TestCase):
    def test_classification_table(self) -> None:
        cases = [
            ("", "empty", ""),
            ("   ", "empty", ""),
            ("/help", "slash", "/help"),
            ("/", "slash", "/"),  # 裸 / 也交给 handle_slash（它自己报未知命令）
            ("!ls", "shell", "ls"),
            ("! ls -la", "shell", "ls -la"),
            ("  !git status  ", "shell", "git status"),
            ("#note", "memo", "note"),
            ("### 多个井号也认", "memo", "多个井号也认"),
            ("修个 bug", "send", "修个 bug"),
            #  贴进来的绝对路径不是命令（用户反馈的真实误伤：整行只有一个 PDF 路径）
            ("/Users/me/Documents/简历 final.pdf", "send", "/Users/me/Documents/简历 final.pdf"),
            ("/etc/hosts 是什么", "send", "/etc/hosts 是什么"),
            ("/v2.0 的接口呢", "send", "/v2.0 的接口呢"),
            #  真敲错的命令仍走 slash（handle_slash 报未知，保住可发现性）
            ("/halp", "slash", "/halp"),
            ("/skill:deploy 参数", "slash", "/skill:deploy 参数"),
            ("注意！这句是中文感叹号开头之外的普通话", "send", "注意！这句是中文感叹号开头之外的普通话"),
            ("句中有 !ls 不算前缀", "send", "句中有 !ls 不算前缀"),
        ]
        for line, kind, args in cases:
            with self.subTest(line=line):
                action = keys.classify_input(line)
                self.assertEqual(action.kind, kind)
                self.assertEqual(action.args, args)

    def test_bare_prefixes_return_usage_hint(self) -> None:
        """前缀对了但没内容：给用法提示，两个前端打的是同一句。"""
        for line in ("!", "!   ", "#", "##  "):
            with self.subTest(line=line):
                action = keys.classify_input(line)
                self.assertEqual(action.kind, "usage")
                self.assertIn("用法", action.hint)

    def test_router_matches_prefix_binding_table(self) -> None:
        """防漂移：BINDINGS 里 PREFIX 分类的提交层前缀 == 路由器认识的前缀。

        `prefix.file`（@）是补全层前缀，行提交时已展开成路径，刻意除外。
        表里新增提交前缀而路由器没跟上（或路由器多认了表里没有的），这里要红。
        """
        documented = {
            item.label
            for item in keys.BINDINGS
            if item.category == keys.PREFIX and item.command != "prefix.file"
        }
        routed = {
            prefix
            for prefix in ("/", "!", "#", "@", "$", "%", ">")
            if keys.classify_input(prefix + "x").kind != "send"
        }
        self.assertEqual(documented, routed)


class ActionCoreTestCase(unittest.TestCase):
    """user_shell / user_memo：无打印核心，真 Agent + 假 client（不打网络）。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        config = Config(
            base_url="http://unused",
            model="main-model",
            summary_model="cheap-model",
            explore_model="cheap-model",
            workspace=self.root,
            auto_approve=True,
            #  测试必须与跑测试机器上装的技能库/插件隔离
            enable_skills=False,
            enable_agents=False,
            enable_hooks=False,
            enable_plugins=False,
        )
        client = types.SimpleNamespace(
            chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=None))
        )
        self.agent = Agent(config, Toolbox(config), registry=Registry.for_client(client))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _last_message(self) -> str:
        return self.agent.messages[-1]["content"]

    def test_user_shell_records_output_and_returncode(self) -> None:
        result = user_shell(self.agent, "echo hi && echo err >&2")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.strip(), "hi")
        self.assertEqual(result.stderr.strip(), "err")
        recorded = self._last_message()
        self.assertIn("$ echo hi && echo err >&2", recorded)
        self.assertIn("退出码 0", recorded)
        self.assertIn("hi", recorded)
        self.assertIn("err", recorded)

    def test_user_shell_truncates_context_but_not_result(self) -> None:
        """屏幕上全量打印（result 原样），进上下文的部分带截断标记。"""
        result = user_shell(self.agent, "python3 -c \"print('x' * 20000)\"")
        self.assertEqual(len(result.stdout.strip()), 20000)
        recorded = self._last_message()
        self.assertIn("已截断", recorded)
        self.assertLess(len(recorded), 10000)

    def test_user_shell_records_nonzero_exit(self) -> None:
        result = user_shell(self.agent, "exit 3")
        self.assertEqual(result.returncode, 3)
        self.assertIn("退出码 3", self._last_message())
        self.assertIn("（无输出）", self._last_message())

    def test_user_memo_creates_default_file(self) -> None:
        target = user_memo(self.agent, "测试统一跑 python -m unittest")
        self.assertEqual(target, "XIAOYU.md")
        content = (self.root / "XIAOYU.md").read_text(encoding="utf-8")
        self.assertIn("# 项目备忘", content)
        self.assertIn("- 测试统一跑 python -m unittest", content)
        self.assertIn("测试统一跑 python -m unittest", self._last_message())

    def test_user_memo_appends_to_existing_instruction_file(self) -> None:
        (self.root / "AGENTS.md").write_text("# 已有指令\n", encoding="utf-8")
        target = user_memo(self.agent, "新备忘")
        self.assertEqual(target, "AGENTS.md")
        content = (self.root / "AGENTS.md").read_text(encoding="utf-8")
        self.assertIn("# 已有指令", content)
        self.assertIn("- 新备忘", content)
        #  不该因为"文件已存在"再补一遍标题
        self.assertNotIn("# 项目备忘", content)
        self.assertFalse((self.root / "XIAOYU.md").exists())

    def test_user_memo_write_failure_raises_oserror(self) -> None:
        """OSError 上抛由前端打印——核心不吞错也不打印。"""
        (self.root / "XIAOYU.md").mkdir()  # 同名目录让 open 必炸
        with self.assertRaises(OSError):
            user_memo(self.agent, "写不进去")


if __name__ == "__main__":
    unittest.main()
