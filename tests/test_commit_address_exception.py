"""提交内容和提交说明只豁免用户批准的完整服务地址。"""

import os
from pathlib import Path
import subprocess
import tempfile
import unittest


HOOKS = Path(__file__).resolve().parents[1] / ".githooks"
ADDRESS = "https://api.githubcopilot.com/mcp/"


class CommitAddressExceptionTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.env = {**os.environ, "HOME": str(self.root)}
        self.env.pop("XIAOYU_SKIP_SENSITIVE", None)
        for key in list(self.env):
            if key.startswith("GIT_"):
                del self.env[key]
        patterns = self.root / ".agents/sensitive-patterns/zhinu-block.txt"
        patterns.parent.mkdir(parents=True)
        patterns.write_text(ADDRESS.split(".")[1] + "\nblocked-example\n")
        self.git("init", "-q")

    def git(self, *args):
        return subprocess.run(
            ["git", *args], cwd=self.root, env=self.env,
            check=True, capture_output=True,
        )

    def check_hooks(self, content, allowed):
        source = self.root / "example.txt"
        source.write_text(content + "\n", encoding="utf-8", newline="\n")
        self.git("add", "example.txt")
        message = self.root / "message.txt"
        message.write_text(content + "\n", encoding="utf-8", newline="\n")
        for hook, args in (("pre-commit", []), ("commit-msg", [str(message)])):
            with self.subTest(hook=hook, content=content):
                result = subprocess.run(
                    ["sh", str(HOOKS / hook), *args], cwd=self.root,
                    env=self.env, capture_output=True,
                )
                self.assertEqual(result.returncode, 0 if allowed else 1, result.stderr)

    def test_exact_addresses_are_allowed(self):
        for content in (f'url="{ADDRESS}"', f"地址 `{ADDRESS}`", f"<{ADDRESS}>",
                        f'"{ADDRESS}" "{ADDRESS}"'):
            self.check_hooks(content, True)

    def test_modified_addresses_are_rejected(self):
        for address in (ADDRESS + "extra", ADDRESS + "?token=test", ADDRESS + "#part",
                        ADDRESS.rstrip("/"), ADDRESS.replace("https:", "http:"),
                        "prefix" + ADDRESS, ADDRESS.upper()):
            self.check_hooks(f'url="{address}"', False)

    def test_surrounding_content_is_still_checked(self):
        for content in (f'blocked-example "{ADDRESS}"',
                        f'"{ADDRESS}" blocked-example',
                        f'"{ADDRESS}" "{ADDRESS}extra"'):
            self.check_hooks(content, False)


if __name__ == "__main__":
    unittest.main()
