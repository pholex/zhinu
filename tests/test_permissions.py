"""权限规则的测试：解析、判定管线、bash/路径匹配、会话授权、持久化、Agent 集成。

不打网络。持久化路径全部打桩，不碰真实用户配置。
"""

from __future__ import annotations

import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from xiaoyu import permissions as perm_mod
from xiaoyu.permissions import Permissions, Rule, parse_rule

from .test_agent_paths import AgentTestCase


class ParseRuleTest(unittest.TestCase):
    def test_tool_only(self):
        self.assertEqual(parse_rule("allow write_file"), Rule("allow", "write_file", None))

    def test_with_spec(self):
        self.assertEqual(parse_rule("allow bash(git *)"), Rule("allow", "bash", "git *"))
        self.assertEqual(parse_rule("deny bash(curl *)"), Rule("deny", "bash", "curl *"))

    def test_bad_lines_fail_closed(self):
        for text in ("", "allow", "yolo bash", "allow bash()", "allow bash(git *", "随便写"):
            self.assertIsNone(parse_rule(text), text)

    def test_roundtrip_str(self):
        for text in ("allow bash(git *)", "deny write_file"):
            self.assertEqual(str(parse_rule(text)), text)


class DecideTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.workspace = Path(self.tmp.name).resolve()

    def perms(self, *rules: str) -> Permissions:
        return Permissions(self.workspace, [parse_rule(rule) for rule in rules])

    def test_no_rules_means_ask(self):
        self.assertEqual(self.perms().decide("bash", {"command": "ls"}), "ask")

    def test_allow_whole_tool(self):
        self.assertEqual(
            self.perms("allow write_file").decide("write_file", {"path": "a.py"}), "allow"
        )

    def test_bash_prefix_allow(self):
        perms = self.perms("allow bash(git *)")
        self.assertEqual(perms.decide("bash", {"command": "git status"}), "allow")
        self.assertEqual(perms.decide("bash", {"command": "rm -rf x"}), "ask")

    def test_deny_beats_allow(self):
        perms = self.perms("allow bash(git *)", "deny bash(git push*)")
        self.assertEqual(perms.decide("bash", {"command": "git push origin main"}), "deny")

    def test_compound_command_needs_every_segment_allowed(self):
        perms = self.perms("allow bash(git *)")
        #  后半段不在允许列表：整条命令退回确认
        self.assertEqual(perms.decide("bash", {"command": "git add . && rm -rf /x"}), "ask")
        perms2 = self.perms("allow bash(git *)", "allow bash(ls*)")
        self.assertEqual(perms2.decide("bash", {"command": "git add . && ls"}), "allow")

    def test_deny_matches_any_segment(self):
        perms = self.perms("deny bash(curl *)")
        self.assertEqual(
            perms.decide("bash", {"command": "echo ok; curl http://evil"}), "deny"
        )

    def test_command_substitution_and_redirect_never_auto_allowed(self):
        perms = self.perms("allow bash(git *)", "allow bash(echo *)")
        #  命令替换能把任意命令藏进允许前缀里；重定向能借允许的命令写任意文件
        self.assertEqual(perms.decide("bash", {"command": 'git commit -m "$(rm -rf x)"'}), "ask")
        self.assertEqual(perms.decide("bash", {"command": "echo x > ~/.zshrc"}), "ask")
        self.assertEqual(perms.decide("bash", {"command": "git log `rm x`"}), "ask")

    def test_path_glob_relative_to_workspace(self):
        perms = self.perms("allow write_file(src/*)")
        self.assertEqual(perms.decide("write_file", {"path": "src/a.py"}), "allow")
        self.assertEqual(perms.decide("write_file", {"path": "other/a.py"}), "ask")
        #  绝对路径写法也解析回工作区相对路径
        self.assertEqual(
            perms.decide("write_file", {"path": str(self.workspace / "src" / "b.py")}),
            "allow",
        )

    def test_path_outside_workspace_not_matched_by_relative_glob(self):
        perms = self.perms("allow write_file(*)")
        #  fnmatch 的 * 也能匹配到含 / 的路径，但工作区外的路径是绝对形式，
        #  以 / 开头，普通规则不该意外放行——用根锚定的规则才行
        self.assertEqual(perms.decide("write_file", {"path": "/etc/hosts"}), "allow")
        #  ↑ * 确实什么都匹配；关键场景是具体前缀不误放行：
        perms2 = self.perms("allow write_file(src/*)")
        self.assertEqual(perms2.decide("write_file", {"path": "/etc/hosts"}), "ask")

    def test_explain_returns_matched_deny_rule(self):
        perms = self.perms("deny bash(curl *)")
        decision, rule = perms.explain("bash", {"command": "curl http://x"})
        self.assertEqual(decision, "deny")
        self.assertEqual(str(rule), "deny bash(curl *)")
        #  allow / ask 没有单一出处，不带规则
        self.assertEqual(perms.explain("bash", {"command": "ls"}), ("ask", None))

    def test_session_grant(self):
        perms = self.perms()
        self.assertEqual(perms.decide("bash", {"command": "ls"}), "ask")
        perms.grant_session("bash")
        self.assertEqual(perms.decide("bash", {"command": "ls"}), "allow")
        #  但 deny 仍然压过会话授权
        perms.rules.append(parse_rule("deny bash(rm *)"))
        self.assertEqual(perms.decide("bash", {"command": "rm -rf x"}), "deny")


class CommandKeyGrantTest(unittest.TestCase):
    """bash 的"本会话允许"按命令头记，不按工具名：答一次 a 不交出整个 shell。"""

    def perms(self) -> Permissions:
        return Permissions(Path("/tmp"))

    def test_keys(self):
        from xiaoyu.permissions import command_keys

        self.assertEqual(command_keys("git status"), ("git status",))
        self.assertEqual(command_keys("git -C x status"), None)  # 参数注入口
        self.assertEqual(command_keys("rg foo src/"), ("rg",))
        self.assertEqual(command_keys("/usr/bin/rg foo"), ("rg",))
        self.assertEqual(command_keys("npm -v"), ("npm",))  # 选项不是子命令
        self.assertEqual(command_keys("git add . && git status | head"), ("git add", "git status", "head"))
        #  wrapper / shell -c / 批量执行器永远推不出键
        for command in ("sudo ls", "bash -c ls", "sh -c 'rm x'", "env FOO=1 ls", "xargs rm", "timeout 5 ls"):
            self.assertIsNone(command_keys(command), command)
        #  危险命令、看不懂的形状也不给键
        self.assertIsNone(command_keys("rm -rf build"))
        self.assertIsNone(command_keys("ls $(cat x)"))
        self.assertIsNone(command_keys(""))

    def test_git_status_grant_is_narrow(self):
        perms = self.perms()
        self.assertEqual(perms.grant_session_call("bash", {"command": "git status"}), "git status")
        self.assertEqual(perms.decide("bash", {"command": "git status -s"}), "allow")
        self.assertEqual(perms.decide("bash", {"command": "git commit -m x"}), "ask")
        self.assertEqual(perms.decide("bash", {"command": "curl http://x"}), "ask")
        #  git status 放行过，但 git -c 带注入口的形态照问
        self.assertEqual(perms.decide("bash", {"command": "git -c core.pager=sh status"}), "ask")

    def test_pipeline_needs_every_segment(self):
        perms = self.perms()
        perms.grant_session_call("bash", {"command": "git status"})
        self.assertEqual(perms.decide("bash", {"command": "git status | head"}), "ask")
        self.assertEqual(perms.grant_session_call("bash", {"command": "head x"}), "head")
        self.assertEqual(perms.decide("bash", {"command": "git status | head"}), "allow")

    def test_shell_c_never_session_keyed(self):
        perms = self.perms()
        self.assertIsNone(perms.session_grant_label("bash", {"command": "bash -c 'ls'"}))
        self.assertIsNone(perms.grant_session_call("bash", {"command": "bash -c 'ls'"}))
        self.assertEqual(perms.session_commands, set())
        self.assertEqual(perms.decide("bash", {"command": "bash -c 'ls'"}), "ask")

    def test_dangerous_not_granted_even_after_key(self):
        perms = self.perms()
        perms.grant_session_call("bash", {"command": "rm x"})
        self.assertEqual(perms.session_commands, {"rm"})
        self.assertEqual(perms.decide("bash", {"command": "rm y"}), "allow")
        self.assertEqual(perms.decide("bash", {"command": "rm -rf y"}), "ask")

    def test_non_bash_tools_unchanged(self):
        perms = self.perms()
        self.assertEqual(perms.session_grant_label("write_file", {"path": "a.py"}), "write_file")
        self.assertEqual(perms.grant_session_call("write_file", {"path": "a.py"}), "write_file")
        self.assertEqual(perms.session_allowed, {"write_file"})
        self.assertEqual(perms.decide("write_file", {"path": "b.py"}), "allow")
        self.assertIn("write_file", perms.describe())
        perms.grant_session_call("bash", {"command": "ls"})
        self.assertIn("ls", perms.describe())

    def test_deny_beats_session_key(self):
        perms = Permissions(Path("/tmp"), [parse_rule("deny bash(git push*)")])
        perms.grant_session_call("bash", {"command": "git push"})
        self.assertEqual(perms.decide("bash", {"command": "git push origin main"}), "deny")


class PersistenceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.workspace = Path(self.tmp.name).resolve()
        self.user_file = self.workspace / "userconf" / "permissions.txt"
        patcher = mock.patch.object(perm_mod, "user_rules_path", return_value=self.user_file)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_load_merges_user_and_workspace_files(self):
        self.user_file.parent.mkdir(parents=True)
        self.user_file.write_text("allow bash(git *)\n# 注释\n坏行\n", encoding="utf-8")
        ws_file = self.workspace / ".xiaoyu" / "permissions.txt"
        ws_file.parent.mkdir(parents=True)
        ws_file.write_text("deny bash(curl *)\n", encoding="utf-8")
        perms = Permissions.load(self.workspace)
        self.assertEqual(len(perms.rules), 2)
        self.assertEqual(perms.decide("bash", {"command": "git status"}), "allow")
        self.assertEqual(perms.decide("bash", {"command": "curl http://x"}), "deny")

    def test_load_without_files(self):
        perms = Permissions.load(self.workspace)
        self.assertEqual(perms.rules, [])

    def test_add_persistent_appends_and_takes_effect(self):
        perms = Permissions.load(self.workspace)
        path = perms.add_persistent(parse_rule("allow bash(git *)"))
        self.assertEqual(path, self.user_file)
        self.assertIn("allow bash(git *)", self.user_file.read_text(encoding="utf-8"))
        self.assertEqual(perms.decide("bash", {"command": "git status"}), "allow")
        #  重新 load 也还在
        self.assertEqual(len(Permissions.load(self.workspace).rules), 1)


class AgentIntegrationTest(AgentTestCase):
    """权限接进 _execute：deny bypass-immune、allow 免确认、ask 走确认。"""

    def execute(self, agent, name: str, arguments: str) -> str:
        call = {"id": "c1", "function": {"name": name, "arguments": arguments}}
        with contextlib.redirect_stdout(io.StringIO()):
            return agent._execute(call)["content"]

    def test_deny_rule_blocks_even_with_yolo(self):
        self.config.auto_approve = True
        agent = self.build([])
        agent.permissions.rules.append(parse_rule("deny bash(rm *)"))
        content = self.execute(agent, "bash", '{"command": "rm -rf sub"}')
        self.assertIn("deny 权限规则", content)
        #  拒绝信息点名命中的规则（携带规则原文）：
        #  模型知道该绕哪条，用户知道该改哪条
        self.assertIn("deny bash(rm *)", content)
        self.assertEqual(agent.trace[-1]["output"], "DENIED_BY_RULE")

    def test_rejection_reason_is_fed_back_to_model(self):
        """拒绝即反馈：approver 返回的文本原样进 tool result，是给模型的新指示。"""
        self.config.auto_approve = False
        agent = self.build([], approver=lambda name, args: "别删，先备份到 /tmp 再说")
        content = self.execute(agent, "bash", '{"command": "rm -rf sub"}')
        self.assertIn("别删，先备份到 /tmp 再说", content)
        self.assertEqual(agent.trace[-1]["output"], "DENIED")

    def test_plain_false_still_denies_without_reason(self):
        self.config.auto_approve = False
        agent = self.build([], approver=lambda name, args: False)
        content = self.execute(agent, "bash", '{"command": "rm -rf sub"}')
        self.assertIn("拒绝", content)
        self.assertEqual(agent.trace[-1]["output"], "DENIED")

    def test_allow_rule_skips_approver(self):
        self.config.auto_approve = False
        approver = mock.Mock(return_value=False)
        agent = self.build([], approver=approver)
        agent.permissions.rules.append(parse_rule("allow bash(echo *)"))
        content = self.execute(agent, "bash", '{"command": "echo hi"}')
        self.assertIn("exit_status: 0", content)
        approver.assert_not_called()

    def test_unmatched_call_still_asks(self):
        self.config.auto_approve = False
        approver = mock.Mock(return_value=False)
        agent = self.build([], approver=approver)
        agent.permissions.rules.append(parse_rule("allow bash(echo *)"))
        content = self.execute(agent, "bash", '{"command": "touch x"}')
        self.assertIn("拒绝", content)
        approver.assert_called_once()


class ConfirmSessionGrantTest(unittest.TestCase):
    """v0.9 的真 bug：确认框答 a 只放行一次。现在 a 必须写进会话授权。"""

    def test_answer_a_grants_session(self):
        from xiaoyu.cli import make_confirm

        perms = Permissions(Path("/tmp"))
        confirm = make_confirm(perms)
        with mock.patch("builtins.input", return_value="a"), contextlib.redirect_stdout(
            io.StringIO()
        ):
            self.assertTrue(confirm("bash", {"command": "ls"}))
        #  bash 的会话授权按命令头记：ls 放行了，别的命令照问
        self.assertEqual(perms.session_commands, {"ls"})
        self.assertEqual(perms.session_allowed, set())
        self.assertEqual(perms.decide("bash", {"command": "ls -la"}), "allow")
        self.assertEqual(perms.decide("bash", {"command": "任何命令"}), "ask")

    def test_answer_y_allows_once_without_grant(self):
        from xiaoyu.cli import make_confirm

        perms = Permissions(Path("/tmp"))
        confirm = make_confirm(perms)
        with mock.patch("builtins.input", return_value="y"), contextlib.redirect_stdout(
            io.StringIO()
        ):
            self.assertTrue(confirm("bash", {"command": "ls"}))
        self.assertEqual(perms.session_allowed, set())

    def test_default_is_deny(self):
        from xiaoyu.cli import make_confirm

        perms = Permissions(Path("/tmp"))
        confirm = make_confirm(perms)
        with mock.patch("builtins.input", return_value=""), contextlib.redirect_stdout(
            io.StringIO()
        ):
            self.assertFalse(confirm("bash", {"command": "ls"}))

    def test_free_text_becomes_rejection_reason(self):
        """确认框里随手打的一句话 = 拒绝理由，原文返回给 agent 回灌模型。"""
        from xiaoyu.cli import make_confirm

        perms = Permissions(Path("/tmp"))
        confirm = make_confirm(perms)
        with mock.patch("builtins.input", return_value="改用 git mv"), contextlib.redirect_stdout(
            io.StringIO()
        ):
            self.assertEqual(confirm("bash", {"command": "mv a b"}), "改用 git mv")
        self.assertEqual(perms.session_allowed, set())

    def test_explicit_no_is_plain_deny(self):
        from xiaoyu.cli import make_confirm

        perms = Permissions(Path("/tmp"))
        confirm = make_confirm(perms)
        for answer in ("n", "N", "no"):
            with mock.patch("builtins.input", return_value=answer), contextlib.redirect_stdout(
                io.StringIO()
            ):
                self.assertIs(confirm("bash", {"command": "ls"}), False, answer)


class BannedAllowTest(unittest.TestCase):
    """持久 allow 不许覆盖任意代码执行入口。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.workspace = Path(self.tmp.name).resolve()
        self.user_file = self.workspace / "userconf" / "permissions.txt"
        patcher = mock.patch.object(perm_mod, "user_rules_path", return_value=self.user_file)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_interpreter_and_shell_rules_rejected(self):
        perms = Permissions(self.workspace)
        for line in (
            "allow bash",  # 无 spec = 放行一切
            "allow bash(*)",
            "allow bash(python *)",
            "allow bash(bash -c *)",
            "allow bash(sudo *)",
            "allow bash(env *)",
            "allow bash(npm *)",  # 会覆盖 npm run（跑 package.json 里的任意脚本）
            "allow bash(rm *)",
        ):
            with self.assertRaises(ValueError, msg=line):
                perms.add_persistent(parse_rule(line))

    def test_narrow_rules_still_allowed(self):
        perms = Permissions(self.workspace)
        for line in ("allow bash(git *)", "allow bash(python -m pytest*)", "allow bash(ls*)"):
            perms.add_persistent(parse_rule(line))
        self.assertEqual(len(perms.rules), 3)

    def test_deny_rules_never_restricted(self):
        perms = Permissions(self.workspace)
        perms.add_persistent(parse_rule("deny bash(python *)"))
        self.assertEqual(perms.decide("bash", {"command": "python -c 'x'"}), "deny")

    def test_hand_edited_banned_rule_ignored_at_load(self):
        self.user_file.parent.mkdir(parents=True)
        self.user_file.write_text("allow bash(python *)\nallow bash(git *)\n", encoding="utf-8")
        with contextlib.redirect_stderr(io.StringIO()) as err:
            perms = Permissions.load(self.workspace)
        self.assertEqual(len(perms.rules), 1)  # 只剩 git *
        self.assertIn("权限规则被忽略", err.getvalue())


class RuleSelfTestTest(unittest.TestCase):
    """规则文件里的 #test 断言在加载时立即验证。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.workspace = Path(self.tmp.name).resolve()
        self.user_file = self.workspace / "userconf" / "permissions.txt"
        patcher = mock.patch.object(perm_mod, "user_rules_path", return_value=self.user_file)
        patcher.start()
        self.addCleanup(patcher.stop)

    def write_rules(self, body: str):
        self.user_file.parent.mkdir(parents=True, exist_ok=True)
        self.user_file.write_text(body, encoding="utf-8")

    def test_passing_assertions_are_silent(self):
        self.write_rules(
            "allow bash(git *)\n"
            "deny bash(curl *)\n"
            "#test allow bash git status\n"
            "#test deny bash curl http://x\n"
            "#test ask bash rm -rf /tmp/x\n"
        )
        with contextlib.redirect_stderr(io.StringIO()) as err:
            Permissions.load(self.workspace)
        self.assertNotIn("自测失败", err.getvalue())

    def test_failing_assertion_warns_at_load(self):
        self.write_rules(
            "allow bash(git *)\n"
            "#test ask bash git status\n"  # 故意写错：实际是 allow
        )
        with contextlib.redirect_stderr(io.StringIO()) as err:
            Permissions.load(self.workspace)
        self.assertIn("权限规则自测失败", err.getvalue())
        self.assertIn("git status", err.getvalue())

    def test_file_tool_assertions(self):
        self.write_rules(
            "deny write_file(.env)\n"
            "#test deny write_file .env\n"
        )
        with contextlib.redirect_stderr(io.StringIO()) as err:
            Permissions.load(self.workspace)
        self.assertNotIn("自测失败", err.getvalue())


class InjectionGuardTest(unittest.TestCase):
    """allow 规则不放行带注入口/破坏性参数的命令——批准的是"这类命令"，
    不是"关掉所有确认"。"""

    def perms(self, *lines: str) -> Permissions:
        rules = [parse_rule(line) for line in lines]
        return Permissions(Path("/tmp"), [rule for rule in rules if rule])

    def test_git_config_injection_not_auto_allowed(self):
        perms = self.perms("allow bash(git *)")
        self.assertEqual(perms.decide("bash", {"command": "git status"}), "allow")
        self.assertEqual(
            perms.decide("bash", {"command": "git -c core.pager='!sh evil' log"}), "ask"
        )
        self.assertEqual(perms.decide("bash", {"command": "git diff --ext-diff"}), "ask")

    def test_dangerous_wrapped_rm_not_auto_allowed(self):
        #  哪怕规则看起来覆盖了前缀，破坏性操作也要问过人
        perms = self.perms("allow bash(find *)")
        self.assertEqual(perms.decide("bash", {"command": "find . -name '*.py'"}), "allow")
        self.assertEqual(
            perms.decide("bash", {"command": "find . -name '*.py' -exec rm {} ;"}), "ask"
        )

    def test_find_exec_not_auto_allowed(self):
        perms = self.perms("allow bash(rg *)")
        self.assertEqual(perms.decide("bash", {"command": "rg -n pattern"}), "allow")
        self.assertEqual(perms.decide("bash", {"command": "rg --pre=evil pattern"}), "ask")


class BashAstAllowTest(unittest.TestCase):
    """allow 判定的白名单解析路径（tree-sitter-bash）。

    核心增量：引号感知——字符串切分会把引号里的连接符错切，语法树不会。
    """

    def perms(self, *lines: str) -> Permissions:
        rules = [parse_rule(line) for line in lines]
        return Permissions(Path("/tmp"), [rule for rule in rules if rule])

    def setUp(self):
        from xiaoyu import bash_ast

        if os.name == "nt" or not bash_ast.available():
            self.skipTest("白名单解析路径仅 POSIX + tree-sitter 可用时生效")

    def test_quoted_connector_still_allowed(self):
        #  旧字符串路径会把 "a && b" 错切成两段导致 ask；语法树知道它是一个参数
        perms = self.perms("allow bash(git *)")
        self.assertEqual(
            perms.decide("bash", {"command": "git commit -m 'a && b'"}), "allow"
        )

    def test_quoted_injection_text_is_just_text(self):
        #  引号里的 $( ) 只是字符串内容……但字符串内展开（"$(…)" 双引号内）本身
        #  是白名单外节点会整条拒绝；单引号内是纯字面量，可以放行
        perms = self.perms("allow bash(git *)")
        self.assertEqual(
            perms.decide("bash", {"command": "git commit -m 'see $(docs)'"}), "allow"
        )

    def test_unparsable_shapes_ask(self):
        perms = self.perms("allow bash(git *)", "allow bash(ls*)", "allow bash(echo *)")
        for command in (
            "echo $(pwd)",
            "echo $HOME",
            "ls > out.txt",
            "(ls)",
            "FOO=bar ls",
            "ls & echo done",
            "for x in a b; do echo $x; done",
        ):
            self.assertEqual(perms.decide("bash", {"command": command}), "ask", command)

    def test_concatenated_glob_arg_allowed(self):
        perms = self.perms("allow bash(rg *)")
        self.assertEqual(perms.decide("bash", {"command": 'rg -g"*.py" -n pat'}), "allow")

    def test_compound_still_needs_every_segment(self):
        perms = self.perms("allow bash(git *)")
        self.assertEqual(perms.decide("bash", {"command": "git add . && ls"}), "ask")
        perms2 = self.perms("allow bash(git *)", "allow bash(ls)")
        self.assertEqual(perms2.decide("bash", {"command": "git add . && ls"}), "allow")


class SuggestAllowRuleTest(unittest.TestCase):
    """确认框「总是允许」选项的规则推导：范围要恰好覆盖刚批准的这类调用，
    推不出（复合/危险/注入/任意代码执行入口）就返回 None——选项不出现。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.addCleanup(self.tmp.cleanup)

    def suggest(self, name, args):
        return perm_mod.suggest_allow_rule(name, args, self.root)

    def test_bash_subcommand_granularity(self):
        rule = self.suggest("bash", {"command": "git status -sb"})
        self.assertEqual(rule, Rule("allow", "bash", "git status*"))

    def test_bash_bare_command_is_exact(self):
        """裸命令用精确匹配：ls* 会连 lsof 一起放行，宁窄勿宽。"""
        self.assertEqual(self.suggest("bash", {"command": "ls"}), Rule("allow", "bash", "ls"))

    def test_bash_generic_command_gets_arg_wildcard(self):
        rule = self.suggest("bash", {"command": "cat foo.txt"})
        self.assertEqual(rule, Rule("allow", "bash", "cat *"))

    def test_compound_and_unsafe_not_derived(self):
        for command in ("git add . && ls", "echo hi > ~/.zshrc", "git log $(cmd)"):
            self.assertIsNone(self.suggest("bash", {"command": command}), command)

    def test_dangerous_not_derived(self):
        self.assertIsNone(self.suggest("bash", {"command": "rm -rf build"}))

    def test_banned_code_exec_entrypoints_not_derived(self):
        """python * 这类推导结果会被 banned_allow_reason 拦下：
        界面不该提供一个落盘时才报错的选项。"""
        self.assertIsNone(self.suggest("bash", {"command": "python -c 'print(1)'"}))
        self.assertIsNone(self.suggest("bash", {"command": "sudo whoami"}))

    def test_file_tool_scoped_to_directory(self):
        rule = self.suggest("write_file", {"path": "src/app/x.py"})
        self.assertEqual(rule, Rule("allow", "write_file", "src/app/*"))

    def test_file_tool_at_root_falls_back_to_tool_level(self):
        self.assertEqual(
            self.suggest("write_file", {"path": "x.py"}), Rule("allow", "write_file", None)
        )

    def test_derived_rules_actually_allow_the_approved_call(self):
        """闭环校验：推导出的规则喂回判定器，原调用必须直接 allow。"""
        cases = [
            ("bash", {"command": "git status -sb"}),
            ("bash", {"command": "cat foo.txt"}),
            ("write_file", {"path": "src/app/x.py"}),
        ]
        for name, args in cases:
            rule = self.suggest(name, args)
            self.assertIsNotNone(rule, (name, args))
            perms = Permissions(self.root, [rule])
            self.assertEqual(perms.decide(name, args), "allow", (name, args))


if __name__ == "__main__":
    unittest.main()
