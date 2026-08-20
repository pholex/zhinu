"""命令风险分析（command_check）的测试：注入口识别 + 危险命令/提权剥 wrapper。"""

from __future__ import annotations

import unittest

from xiaoyu.command_check import (
    command_risk,
    dangerous_command,
    injection_risk,
    privileged_command,
)


class InjectionRiskTest(unittest.TestCase):
    def test_plain_git_is_clean(self):
        self.assertIsNone(injection_risk("git status"))
        self.assertIsNone(injection_risk("git log --oneline -5"))
        self.assertIsNone(injection_risk("git commit -m 'fix: x'"))

    def test_git_config_injection(self):
        #  git -c core.pager='!sh …' log 是经典的 allow 规则逃逸
        self.assertIsNotNone(injection_risk("git -c core.pager='!sh evil' log"))
        self.assertIsNotNone(injection_risk("git -ccore.pager=evil log"))
        self.assertIsNotNone(injection_risk("git -p log"))
        self.assertIsNotNone(injection_risk("git --exec-path=/tmp/evil status"))
        self.assertIsNotNone(injection_risk("git --git-dir /tmp/other status"))

    def test_git_subcommand_options(self):
        self.assertIsNotNone(injection_risk("git diff --ext-diff"))
        self.assertIsNotNone(injection_risk("git diff --output=/tmp/x"))

    def test_absolute_path_and_exe_suffix_normalized(self):
        #  argv[0] 归一：/usr/bin/git 和 git.exe 都不能绕过按名字写的检查
        self.assertIsNotNone(injection_risk("/usr/bin/git -c a.b=c log"))
        self.assertIsNotNone(injection_risk("git.exe -p log"))

    def test_find_exec(self):
        self.assertIsNotNone(injection_risk("find . -name '*.py' -exec rm {} ;"))
        self.assertIsNotNone(injection_risk("find . -delete"))
        self.assertIsNone(injection_risk("find . -name '*.py'"))

    def test_rg_pre(self):
        self.assertIsNotNone(injection_risk("rg --pre=evil pattern"))
        self.assertIsNotNone(injection_risk("rg --pre evil pattern"))
        self.assertIsNotNone(injection_risk("rg -z pattern"))
        self.assertIsNone(injection_risk("rg -n pattern src"))

    def test_tar_to_command(self):
        self.assertIsNotNone(injection_risk("tar --to-command=evil -xf a.tar"))
        self.assertIsNone(injection_risk("tar -tzf a.tar"))

    def test_ssh_proxycommand(self):
        self.assertIsNotNone(injection_risk("ssh -o ProxyCommand=evil host"))
        self.assertIsNotNone(injection_risk("ssh -oProxyCommand=evil host"))
        self.assertIsNone(injection_risk("ssh host uptime"))

    def test_xargs_flagged(self):
        self.assertIsNotNone(injection_risk("xargs rm"))

    def test_sed_exec_flag(self):
        #  s///e 的 e 是"把替换结果当命令执行"，危险；关键是别误伤把字面量 e
        #  当作被替换文本的普通替换
        self.assertIsNotNone(injection_risk("sed s/a/b/e file"))
        self.assertIsNotNone(injection_risk("sed 's/a/b/e'"))
        self.assertIsNotNone(injection_risk("sed 's/a/b/ge'"))  # 多标志里含 e
        self.assertIsNotNone(injection_risk("sed -e 's/x/y/e'"))  # -e 传脚本
        self.assertIsNotNone(injection_risk("sed 's|a|b|e'"))  # 非 / 定界符

    def test_sed_exec_command(self):
        #  独立 e 命令（GNU）同样执行 shell
        self.assertIsNotNone(injection_risk("sed 'e id'"))
        self.assertIsNotNone(injection_risk("sed 'p;e cat /etc/passwd'"))

    def test_sed_benign_not_flagged(self):
        #  e 只是被替换/匹配的字面量，或普通标志——不该误伤
        self.assertIsNone(injection_risk("sed 's/e/x/'"))
        self.assertIsNone(injection_risk("sed 's/e/x/g'"))
        self.assertIsNone(injection_risk("sed 's/a/b/g'"))
        self.assertIsNone(injection_risk("sed -n 'p'"))
        self.assertIsNone(injection_risk("sed 's/foo/bar/' input.txt"))
        self.assertIsNone(injection_risk(r"sed 's/a\/b/c/'"))  # 转义定界符
        self.assertIsNone(injection_risk("sed 'y/abc/xyz/'"))

    def test_vim_ex_command(self):
        #  -c/--cmd/+cmd/-S 都能跑 ex 命令，:!shell 逃逸
        self.assertIsNotNone(injection_risk("vim -c :!sh"))
        self.assertIsNotNone(injection_risk("vim +!sh file"))
        self.assertIsNotNone(injection_risk("nvim --cmd :!id"))
        self.assertIsNotNone(injection_risk("vim -S evil.vim"))
        self.assertIsNotNone(injection_risk("ex -c '!sh' file"))

    def test_vim_plain_edit_not_flagged(self):
        self.assertIsNone(injection_risk("vim file.txt"))
        self.assertIsNone(injection_risk("vim -R readonly.txt"))

    def test_unparsable_is_risky(self):
        #  引号不闭合 → 看不清楚 → 保守方向按有风险处理
        self.assertIsNotNone(injection_risk("git commit -m 'unclosed"))


class DangerousCommandTest(unittest.TestCase):
    def test_forced_rm_variants(self):
        for command in (
            "rm -rf /tmp/x",
            "rm -fr /tmp/x",
            "rm --force /tmp/x",
            "rm /tmp/x -f",
        ):
            self.assertIsNotNone(dangerous_command(command), command)

    def test_wrappers_are_stripped(self):
        for command in (
            "sudo rm -rf /tmp/x",
            "env TARGET=/tmp/x rm -rf /tmp/x",
            "env -i rm -rf /tmp/x",
            "bash -c 'rm -rf /tmp/x'",
            "bash -lc 'rm -rf /tmp/x'",
            "nohup rm -rf /tmp/x",
            "timeout 5 rm -rf /tmp/x",
            "trap 'rm -rf /tmp/x' EXIT",
            "xargs rm -rf",
            "sudo env A=1 bash -c 'rm -rf /tmp/x'",  # 多层嵌套
        ):
            self.assertIsNotNone(dangerous_command(command), command)

    def test_compound_segments_are_scanned(self):
        self.assertIsNotNone(dangerous_command("echo hi && rm -rf /tmp/x"))
        self.assertIsNotNone(dangerous_command("printf x | xargs rm -rf"))

    def test_benign_rm_not_flagged(self):
        #  没有 -f 不算强制删除；`rm -- -f` 是删一个叫 -f 的文件
        self.assertIsNone(dangerous_command("rm -r /tmp/x"))
        self.assertIsNone(dangerous_command("rm -- -f"))
        self.assertIsNone(dangerous_command("rm /tmp/x"))

    def test_string_literal_not_flagged(self):
        #  只是在说 rm，不是在跑 rm
        self.assertIsNone(dangerous_command("echo 'rm -rf /tmp/x'"))
        self.assertIsNone(dangerous_command("trap 'echo done' EXIT"))


class PrivilegedCommandTest(unittest.TestCase):
    """提权识别（auto 档靠它决定"这条命令还得问人"）。与危险命令共用剥法，
    所以包起来的写法同样要挖得出来。"""

    def test_direct_escalators(self):
        for command in ("sudo id", "doas ls", "su - root", "pkexec whoami"):
            self.assertIsNotNone(privileged_command(command), command)

    def test_wrapped_escalators(self):
        for command in (
            "bash -lc 'sudo apt install foo'",
            "xargs sudo tee /etc/hosts",
            "env A=1 sudo ls",
            "echo hi && sudo id",
            "nohup sudo id",
        ):
            self.assertIsNotNone(privileged_command(command), command)

    def test_benign_commands_not_flagged(self):
        for command in ("pytest -q", "git status", "echo sudo", "grep -r sudo ."):
            self.assertIsNone(privileged_command(command), command)


class CommandRiskTest(unittest.TestCase):
    def test_combines_both_directions(self):
        self.assertIsNotNone(command_risk("sudo rm -rf /tmp/x"))
        self.assertIsNotNone(command_risk("git -c a.b=c log"))
        self.assertIsNone(command_risk("git status && ls"))


if __name__ == "__main__":
    unittest.main()
