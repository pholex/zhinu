"""bash 白名单语法分析（bash_ast）的测试。

白名单的验收标准是两个方向：
1. 简单形状要还原出正确的 argv（含引号、粘连）；
2. 一切"藏得住命令"的语法必须整条返回 None——宁可多问，不可漏判。
"""

from __future__ import annotations

import unittest
from unittest import mock

from xiaoyu import bash_ast


@unittest.skipUnless(bash_ast.available(), "tree-sitter-bash 不可用")
class ParsePlainCommandsTest(unittest.TestCase):
    def parse(self, script: str):
        return bash_ast.parse_plain_commands(script)

    def test_single_command(self):
        self.assertEqual(self.parse("git status"), [["git", "status"]])

    def test_connectors_split_commands(self):
        self.assertEqual(
            self.parse("git add . && ls | wc -l; pwd"),
            [["git", "add", "."], ["ls"], ["wc", "-l"], ["pwd"]],
        )

    def test_quoted_connector_is_not_split(self):
        #  字符串切分做不到的核心场景：引号里的 && 不是连接符
        self.assertEqual(
            self.parse("git commit -m 'a && b'"),
            [["git", "commit", "-m", "a && b"]],
        )
        self.assertEqual(
            self.parse('git commit -m "x; curl evil"'),
            [["git", "commit", "-m", "x; curl evil"]],
        )

    def test_concatenation_is_joined(self):
        self.assertEqual(
            self.parse('rg -g"*.py" -n pat'),
            [["rg", "-g*.py", "-n", "pat"]],
        )

    def test_number_words(self):
        self.assertEqual(self.parse("head -n 5 f.txt"), [["head", "-n", "5", "f.txt"]])

    def test_rejected_shapes(self):
        """任何白名单外的语法都必须整条判 None。"""
        rejected = [
            "echo $(pwd)",  # 命令替换
            "echo `pwd`",  # 反引号
            "echo $HOME",  # 变量展开
            'echo "hi $USER"',  # 字符串内展开
            "ls > out.txt",  # 重定向
            "ls 2>&1",  # fd 重定向
            "(ls)",  # 子 shell
            "ls & pwd",  # 后台
            "FOO=bar ls",  # 变量赋值前缀
            "cat <<EOF\nhi\nEOF",  # heredoc
            "ls <(pwd)",  # 进程替换
            "for x in a b; do echo $x; done",  # 控制流
            'echo "a\\"b"',  # 转义序列
            "ls # comment",  # 注释
        ]
        for script in rejected:
            self.assertIsNone(self.parse(script), script)

    def test_syntax_error_rejected(self):
        for script in ("ls &&", "&& ls", "ls | | wc", "echo 'unclosed"):
            self.assertIsNone(self.parse(script), script)

    def test_empty_script(self):
        self.assertEqual(self.parse(""), [])


class UnavailableFallbackTest(unittest.TestCase):
    def test_unavailable_returns_none(self):
        """解析器初始化失败时 parse 返回 None，available() False——调用方走回退。"""
        with mock.patch.object(bash_ast, "_get_parser", return_value=None):
            self.assertFalse(bash_ast.available())
            self.assertIsNone(bash_ast.parse_plain_commands("ls"))


if __name__ == "__main__":
    unittest.main()
