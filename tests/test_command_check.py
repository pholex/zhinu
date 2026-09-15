"""命令风险分析（command_check）的测试：注入口识别 + 危险命令/提权剥 wrapper。"""

from __future__ import annotations

import unittest

from xiaoyu.command_check import (
    command_risk,
    _split_script,
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


class WrapperPeelingTest(unittest.TestCase):
    """wrapper 选项表：选项值不是命令名，表外选项与病态嵌套走 fail-safe。
    覆盖面样本在 fixtures/command_corpus/wrapper_options.jsonl，这里测边界。"""

    def test_unwrap_skips_option_values(self):
        from xiaoyu.command_check import unwrap_argv

        self.assertEqual(unwrap_argv(["sudo", "-u", "root", "ls"]), [["ls"]])
        self.assertEqual(unwrap_argv(["timeout", "-s", "KILL", "5", "ls", "-l"]), [["ls", "-l"]])
        self.assertEqual(unwrap_argv(["su", "-c", "id"]), [["sh", "-c", "id"]])
        self.assertIsNone(unwrap_argv(["ls", "-l"]))

    def test_unknown_option_scans_both_readings(self):
        from xiaoyu.command_check import unwrap_argv

        inners = unwrap_argv(["nice", "--bogus", "5", "ls"])
        self.assertIn(["ls"], inners)
        self.assertIn(["5", "ls"], inners)

    def test_pathological_nesting_fails_safe(self):
        #  扫不完不能当安全：嵌套过深按有风险返回，且不能把调用栈打爆
        deep_wrappers = "nice " * 40 + "ls"
        deep_substitution = "echo " + "$(" * 300 + "ls" + ")" * 300
        for command in (deep_wrappers, deep_substitution):
            self.assertIsNotNone(dangerous_command(command))
            self.assertIsNotNone(privileged_command(command))
        self.assertIsNotNone(injection_risk(deep_wrappers))

    def test_long_flat_script_is_not_too_complex(self):
        #  工作量上限只数嵌套层：几千行的扁平脚本（heredoc 写文件）不该因为长而被判风险
        script = "\n".join(f"echo line {i}" for i in range(3000))
        self.assertIsNone(dangerous_command(script))
        self.assertIsNone(privileged_command(script))

    def test_wrapper_does_not_hide_injection(self):
        self.assertIsNotNone(injection_risk("timeout 60 git -c core.pager=sh log"))
        self.assertIsNotNone(injection_risk("nice -n 5 find . -delete"))
        self.assertIsNotNone(injection_risk("su -c 'git status'"))
        self.assertIsNone(injection_risk("timeout 60 git status"))
        self.assertIsNone(injection_risk("nice -n 10 make"))


class WrapperTableTest(unittest.TestCase):
    """选项表按手册/源码复核后的边界：带值、可选值、位置参数、单横线长选项。"""

    def test_value_options_are_not_commands(self):
        from xiaoyu.command_check import unwrap_argv

        self.assertEqual(unwrap_argv(["nsenter", "-N", "3", "ls"]), [["ls"]])
        self.assertEqual(unwrap_argv(["ltrace", "-w", "3", "ls"]), [["ls"]])
        self.assertEqual(unwrap_argv(["arch", "-arch", "arm64", "ls"]), [["ls"]])
        self.assertEqual(unwrap_argv(["caffeinate", "-t", "60", "ls"]), [["ls"]])

    def test_chrt_priority_only_when_numeric(self):
        from xiaoyu.command_check import unwrap_argv

        self.assertIn(["ls"], unwrap_argv(["chrt", "-o", "ls"]))
        self.assertIn(["ls"], unwrap_argv(["chrt", "-f", "10", "ls"]))

    def test_sudo_assignments_skipped(self):
        from xiaoyu.command_check import unwrap_argv

        self.assertEqual(unwrap_argv(["sudo", "FOO=1", "ls"]), [["ls"]])


class CommandCarrierTest(unittest.TestCase):
    """把后续参数当命令/脚本执行的工具：watch、script、sg、find -exec。"""

    def test_watch_joins_arguments_into_shell_script(self):
        from xiaoyu.command_check import unwrap_argv

        self.assertEqual(unwrap_argv(["watch", "-n", "1", "echo", "a;", "ls"]),
                         [["sh", "-c", "echo a; ls"]])
        #  -x 直接 exec，不经 shell
        self.assertEqual(unwrap_argv(["watch", "-x", "echo", "a;", "ls"]),
                         [["echo", "a;", "ls"]])

    def test_script_both_forms(self):
        from xiaoyu.command_check import unwrap_argv

        self.assertIn(["sh", "-c", "make"], unwrap_argv(["script", "-qc", "make", "log"]))
        self.assertIn(["make", "test"], unwrap_argv(["script", "-q", "/dev/null", "make", "test"]))

    def test_sg_command_is_script(self):
        from xiaoyu.command_check import unwrap_argv

        self.assertEqual(unwrap_argv(["sg", "docker", "-c", "docker ps"]), [["sh", "-c", "docker ps"]])
        self.assertEqual(unwrap_argv(["sg", "docker", "docker ps"]), [["sh", "-c", "docker ps"]])

    def test_find_exec_segments(self):
        from xiaoyu.command_check import _find_exec_commands

        self.assertEqual(
            _find_exec_commands(["find", ".", "-exec", "rm", "-f", "{}", ";", "-print",
                                 "-execdir", "echo", "{}", "+"]),
            [["rm", "-f", "{}"], ["echo", "{}"]],
        )
        #  {} 之后才认 + 为结束：单独的 + 是命令参数
        self.assertEqual(_find_exec_commands(["find", "-exec", "chmod", "+", "x", "{}", "+"]),
                         [["chmod", "+", "x", "{}"]])
        #  没有结束符：取到末尾
        self.assertEqual(_find_exec_commands(["find", "-ok", "rm", "{}"]), [["rm", "{}"]])
        self.assertEqual(_find_exec_commands(["find", ".", "-name", "x"]), [])

    def test_find_delete_is_injection_not_forced_rm(self):
        self.assertIsNone(dangerous_command("find . -delete"))
        self.assertIsNotNone(injection_risk("find . -delete"))


class ShellScriptArgumentTest(unittest.TestCase):
    """`sh -c 脚本 $0 $1…`：只有 -c 之后第一个非选项参数是脚本。"""

    def test_only_first_operand_is_script(self):
        from xiaoyu.command_check import _inner_scripts

        self.assertEqual(_inner_scripts("bash", ["bash", "-c", "echo", "rm -rf ~", "x"]), ["echo"])
        self.assertEqual(_inner_scripts("sh", ["sh", "-eo", "pipefail", "-c", "ls", "a"]), ["ls"])
        self.assertEqual(_inner_scripts("bash", ["bash", "-co", "pipefail", "ls"]), ["ls"])
        self.assertEqual(_inner_scripts("bash", ["bash", "--rcfile", "x", "-c", "ls"]), ["ls"])
        self.assertEqual(_inner_scripts("bash", ["bash", "-c", "--", "ls", "a"]), ["ls"])

    def test_script_file_and_stdin_forms_unchanged(self):
        from xiaoyu.command_check import _inner_scripts

        self.assertEqual(_inner_scripts("bash", ["bash", "script.sh", "-c", "ls"]), [])
        self.assertEqual(_inner_scripts("bash", ["bash", "-s", "ls"]), [])
        self.assertEqual(_inner_scripts("bash", ["bash"]), [])

    def test_ambiguous_letters_scan_both_readings(self):
        from xiaoyu.command_check import _inner_scripts

        #  ksh 的 -R 在 ksh93 带值，其他实现未必：两种读法的脚本都扫
        scripts = _inner_scripts("ksh", ["ksh", "-R", "x", "-c", "ls"])
        self.assertIn("ls", scripts)

    def test_fish_command_values(self):
        from xiaoyu.command_check import _inner_scripts

        self.assertEqual(_inner_scripts("fish", ["fish", "-c", "echo", "rm -rf ~"]), ["echo"])
        self.assertEqual(_inner_scripts("fish", ["fish", "-C", "a", "--command=b", "-cc"]),
                         ["a", "b", "c"])


class EnvSplitStringTest(unittest.TestCase):
    """`env -S` 按 GNU env 的规则拆分（引号、\\_、\\c、#、${VAR}）。"""

    def split(self, payload):
        from xiaoyu.command_check import _split_env_payload

        return _split_env_payload(payload)

    def test_quotes_and_separators(self):
        self.assertEqual(self.split("rm -rf '/a b'"), ["rm", "-rf", "/a b"])
        self.assertEqual(self.split('echo "a\\_b"\\_c'), ["echo", "a b", "c"])
        self.assertEqual(self.split("a\\_b"), ["a", "b"])

    def test_backslash_c_terminates_outside_quotes_only(self):
        self.assertEqual(self.split("rm -rf x \\c y"), ["rm", "-rf", "x"])
        #  单引号内 \c 是字面量（只有 \\ 和 \' 特殊）
        self.assertEqual(self.split("sh -c 'printf \\c; rm'"), ["sh", "-c", "printf \\c; rm"])

    def test_comment_and_escapes(self):
        self.assertEqual(self.split("rm -rf #x y"), ["rm", "-rf"])
        self.assertEqual(self.split("a#b"), ["a#b"])
        self.assertEqual(self.split("rm -r\\f"), ["rm", "-r\f"])
        self.assertEqual(self.split("echo ${HOME}/x"), ["echo", "${HOME}/x"])

    def test_invalid_payload_falls_back_to_lenient_split(self):
        #  GNU env 遇到这些会直接报错退出（什么都不执行）；宽松拆分只会多扫
        self.assertEqual(self.split("rm -rf 'x"), ["rm", "-rf", "'x"])
        self.assertEqual(self.split("rm \\q -rf"), ["rm", "\\q", "-rf"])


class CommandRiskTest(unittest.TestCase):
    def test_combines_both_directions(self):
        self.assertIsNotNone(command_risk("sudo rm -rf /tmp/x"))
        self.assertIsNotNone(command_risk("git -c a.b=c log"))
        self.assertIsNone(command_risk("git status && ls"))

    def test_quoted_connector_not_split_no_false_warning(self):
        #  引号内的 | && ; 属于参数，不该被当连接符切开（切开会把引号劈成两半
        #  → 下游 shlex 误判"引号不闭合"）。这是本地模型爱写的交替正则的常见形态。
        self.assertIsNone(
            command_risk("grep -oiE 'name=\"[a-z]+\"|formcheck|checkcode' /tmp/a.html | head -20")
        )
        self.assertIsNone(command_risk('echo "a && b; c | d"'))
        #  引号外的连接符照常切成多段
        self.assertEqual(_split_script("grep x a || grep y b ; ls | wc -l"),
                         ["grep x a", "grep y b", "ls", "wc -l"])

    def test_quoted_connector_split_keeps_segment_whole(self):
        self.assertEqual(
            _split_script("grep -oiE 'a|b|c' f | head"),
            ["grep -oiE 'a|b|c' f", "head"],
        )

    def test_genuinely_unbalanced_quote_still_warns(self):
        #  修复不能把真·不闭合引号也放过：它会一直吃到末尾成一段，shlex 仍如实报
        self.assertIsNotNone(command_risk('curl -sS -m 10 -A "Mozilla/5.0'))

    def test_escaped_connector_outside_quotes_is_literal(self):
        self.assertEqual(_split_script(r"echo a \| b"), [r"echo a \| b"])

    def test_dangerous_inside_quotes_not_split_into_false_rm(self):
        #  引号内的 rm 字样是字面量，不该因切分而被误判——沿用既有 string_literal 精神
        self.assertIsNone(command_risk('echo "rm -rf / is dangerous"'))


if __name__ == "__main__":
    unittest.main()
