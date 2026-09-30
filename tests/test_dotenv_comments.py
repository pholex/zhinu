""".env 的行尾注释不进值；写回去的值读回来还是原样。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from xiaoyu import config


class DotenvValueTest(unittest.TestCase):
    def parse(self, body: str) -> dict[str, str]:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text(body, encoding="utf-8")
            return config._parse_dotenv(path)  # noqa: SLF001

    def test_trailing_comment_is_not_part_of_the_value(self) -> None:
        parsed = self.parse(
            "XIAOYU_MODEL=m1  # 主模型\n"
            "KEY=sk-abc\t# 临时的\n"
            "export OTHER=v # x\n"
        )
        self.assertEqual(parsed, {"XIAOYU_MODEL": "m1", "KEY": "sk-abc", "OTHER": "v"})

    def test_hash_glued_to_the_value_is_content(self) -> None:
        parsed = self.parse("URL=https://x/y#frag\nKEY=sk#1\nEMPTY=#\n")
        self.assertEqual(parsed, {"URL": "https://x/y#frag", "KEY": "sk#1", "EMPTY": "#"})

    def test_quoted_values_keep_their_hashes_and_drop_what_follows(self) -> None:
        parsed = self.parse('A="v # 不是注释"  # 这才是注释\nB=\'x # y\'\nC="plain"\n')
        self.assertEqual(parsed, {"A": "v # 不是注释", "B": "x # y", "C": "plain"})

    def test_plain_values_are_unchanged(self) -> None:
        parsed = self.parse("A=1\nB= spaced \nC=\n# 整行注释\n")
        self.assertEqual(parsed, {"A": "1", "B": "spaced", "C": ""})


class RoundTripTest(unittest.TestCase):
    def test_saved_values_read_back_identical(self) -> None:
        values = {
            "PLAIN": "abc",
            "HASHY": "pass #1 word",
            "QUOTED": '"leading quote',
            "URL": "https://x/y#frag",
            "BOTH": "it's \"x\" #y",
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            with mock.patch.object(config, "user_env_path", return_value=path):
                config.save_user_env(values)
            parsed = config._parse_dotenv(path)  # noqa: SLF001
            text = path.read_text(encoding="utf-8")
        for key in ("PLAIN", "HASHY", "QUOTED", "URL"):
            self.assertEqual(parsed[key], values[key], key)
        #  不需要引号的值照旧裸写：别人手里的 .env 不该被无故改样子
        self.assertIn("PLAIN=abc\n", text)
        self.assertIn("URL=https://x/y#frag\n", text)


if __name__ == "__main__":
    unittest.main()
