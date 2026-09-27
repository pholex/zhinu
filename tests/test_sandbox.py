"""沙箱测试（macOS Seatbelt + Linux bubblewrap）。

策略行为的用例会真的调 `sandbox-exec` / `bwrap`（沙箱不可用的平台自动跳过）——
沙箱这种东西只有真跑过内核才算数，mock 出来的"通过"毫无意义。
两个后端策略语义对齐，所以真内核用例是同一套，哪个平台可用就在哪跑。
纯函数部分（argv 拼装、拒绝识别）全平台都跑。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from xiaoyu import sandbox
from xiaoyu.config import Config
from xiaoyu.tools import Toolbox

#  真内核用例的开关：macOS 上是 Seatbelt，Linux 上是 bwrap（含 userns 探针）
SANDBOXED = sandbox.available()


class PolicyTextTest(unittest.TestCase):
    def test_closed_by_default(self):
        text = sandbox.policy_text(1, allow_network=True)
        self.assertIn("(deny default)", text)

    def test_writable_roots_use_params_not_inline_paths(self):
        """路径必须走 -D 参数，不能内联进策略——否则含引号的路径能破坏策略语法。"""
        text = sandbox.policy_text(2, allow_network=True)
        self.assertIn('(param "WRITABLE_0")', text)
        self.assertIn('(param "WRITABLE_1")', text)
        self.assertNotIn('(param "WRITABLE_2")', text)

    def test_network_toggle(self):
        self.assertIn("(allow network*)", sandbox.policy_text(0, allow_network=True))
        self.assertNotIn("(allow network*)", sandbox.policy_text(0, allow_network=False))


class BwrapArgsTest(unittest.TestCase):
    def test_root_mounted_readonly_first(self):
        """`--ro-bind / /` 必须最先出现：后写的挂载覆盖先写的，顺序反了就全盘可写。"""
        args = sandbox.bwrap_args([], allow_network=True)
        self.assertEqual(args[:3], ["--ro-bind", "/", "/"])

    def test_pid_namespace_paired_with_proc(self):
        """挂新 procfs 内核要求持有 pid namespace——两个参数必须成对。"""
        args = sandbox.bwrap_args([], allow_network=True)
        self.assertIn("--unshare-pid", args)
        self.assertIn("--proc", args)

    def test_network_toggle(self):
        self.assertNotIn("--unshare-net", sandbox.bwrap_args([], allow_network=True))
        self.assertIn("--unshare-net", sandbox.bwrap_args([], allow_network=False))

    def test_writable_roots_become_binds(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = sandbox.bwrap_args([tmp, "/xiaoyu_no_such_dir"], allow_network=True)
            index = args.index("--bind")
            self.assertEqual(args[index + 1 : index + 3], [tmp, tmp])
        #  不存在的目录必须过滤：bwrap bind 不存在的源会整个启动失败
        self.assertNotIn("/xiaoyu_no_such_dir", args)

    def test_die_with_parent(self):
        """小羽死则沙箱死——不留孤儿进程。"""
        self.assertIn("--die-with-parent", sandbox.bwrap_args([], allow_network=True))

    def test_hardening_flags(self):
        """脱离控制终端（防 TIOCSTI 注键）、隔离 IPC、清空 capability。"""
        args = sandbox.bwrap_args([], allow_network=True)
        self.assertIn("--new-session", args)
        self.assertIn("--unshare-ipc", args)
        index = args.index("--cap-drop")
        self.assertEqual(args[index + 1], "ALL")


def home_probe(stem: str) -> Path:
    """家目录里的探针路径（沙箱挡的就是往这里写，探针只能放这）。

    名字带进程号与随机串：固定名字的话两份测试并行跑会互相踩，上一次跑到一半
    被杀留下的残骸也会让这一次的断言看到假象。
    """
    import uuid

    return Path.home() / f".{stem}-{os.getpid()}-{uuid.uuid4().hex[:8]}.txt"


class LinuxWrapTest(unittest.TestCase):
    def test_wrap_builds_bwrap_call(self):
        with (
            mock.patch.object(sandbox.sys, "platform", "linux"),
            mock.patch.object(sandbox, "available", return_value=True),
            mock.patch.object(sandbox, "_bwrap_path", return_value="/usr/bin/bwrap"),
        ):
            argv = sandbox.wrap(["/bin/bash", "-lc", "ls"], Path("/tmp/ws"))
        self.assertEqual(argv[0], "/usr/bin/bwrap")
        #  原命令原样排在 -- 之后
        self.assertEqual(argv[argv.index("--") + 1 :], ["/bin/bash", "-lc", "ls"])

    def test_candidates_are_absolute(self):
        """不能走 PATH：PATH 上放个假 bwrap 就等于沙箱可以自己关掉自己。"""
        for candidate in sandbox.BWRAP_CANDIDATES:
            self.assertTrue(candidate.startswith("/"), candidate)


@unittest.skipIf(os.name == "nt", "用可执行位伪造程序，POSIX 才有")
class HostProgramLookupTest(unittest.TestCase):
    """宿主侧自己要跑的程序只在模型写不进去的 PATH 目录里找。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ws = Path(self.tmp.name).resolve() / "ws"
        self.planted = self.ws / ".venv" / "bin"
        self.planted.mkdir(parents=True)
        self.marker = self.ws / "executed.txt"
        #  登记表与缓存是进程级的：用例之间互不串味
        for name, value in (("_workspaces", {}), ("_untrusted_cache", None)):
            patcher = mock.patch.object(sandbox, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def plant(self, name: str) -> Path:
        program = self.planted / name
        program.write_text(f"#!/bin/sh\necho ran > '{self.marker}'\nexit 1\n", encoding="utf-8")
        program.chmod(0o755)
        return program

    def path_with_planted_first(self) -> dict:
        return {"PATH": f"{self.planted}{os.pathsep}{os.environ.get('PATH', '')}"}

    def test_program_planted_in_workspace_is_skipped(self):
        self.plant("git")
        real = sandbox.host_which("git")
        with mock.patch.dict(os.environ, self.path_with_planted_first()):
            self.assertEqual(shutil.which("git"), str(self.planted / "git"), "前提：PATH 确实被顶了")
            sandbox.note_workspace(self.ws)
            self.assertEqual(sandbox.host_which("git"), real)
            self.assertNotEqual(sandbox.host_command("git"), "git")

    def test_program_only_in_workspace_counts_as_missing(self):
        self.plant("xiaoyu-only-here")
        with mock.patch.dict(os.environ, self.path_with_planted_first()):
            sandbox.note_workspace(self.ws)
            self.assertIsNone(sandbox.host_which("xiaoyu-only-here"))
            command = sandbox.host_command("xiaoyu-only-here")
        #  不退回裸名字（那会再被 PATH 顶替），而是一个必然不存在的绝对路径
        self.assertTrue(os.path.isabs(command))
        self.assertFalse(os.path.exists(command))

    def test_relative_and_temp_entries_are_untrusted(self):
        sandbox.note_workspace(self.ws)
        self.assertFalse(sandbox.trusted_program_dir(""))
        self.assertFalse(sandbox.trusted_program_dir("."))
        self.assertFalse(sandbox.trusted_program_dir("node_modules/.bin"))
        self.assertFalse(sandbox.trusted_program_dir(tempfile.gettempdir()))
        self.assertFalse(sandbox.trusted_program_dir(str(self.ws)))
        self.assertTrue(sandbox.trusted_program_dir("/usr/bin"))

    def test_grep_tool_does_not_execute_planted_ripgrep(self):
        self.plant("rg")
        (self.ws / "a.txt").write_text("hello\n", encoding="utf-8")
        with mock.patch.dict(os.environ, self.path_with_planted_first()):
            box = Toolbox(
                Config(base_url="http://unused", model="m", workspace=self.ws, enable_plugins=False)
            )
            result = box.run("grep", {"pattern": "hello", "path": "a.txt"})
        self.assertFalse(self.marker.exists(), "工作区里的假 rg 被宿主执行了")
        self.assertIn("hello", result)

    def test_host_git_does_not_execute_planted_git(self):
        from xiaoyu import gitsafe

        self.plant("git")
        with mock.patch.dict(os.environ, self.path_with_planted_first()):
            sandbox.note_workspace(self.ws)
            argv, env = gitsafe.prepare(["--version"], self.ws)
            subprocess.run(argv, env=env, capture_output=True, timeout=30, cwd=str(self.ws))
        self.assertFalse(self.marker.exists(), "工作区里的假 git 被宿主执行了")


class SecretPathsTest(unittest.TestCase):
    def test_lists_user_env_and_user_mcp_config(self):
        from xiaoyu import config as config_mod

        paths = sandbox.secret_paths()
        self.assertIn(str(config_mod.user_env_path()), paths)
        self.assertIn(str(config_mod.user_config_dir() / "mcp.json"), paths)

    def test_explicit_env_file_is_included(self):
        #  Windows 上没带盘符的路径不算绝对路径，先补成当前盘下的
        explicit = os.path.abspath("/opt/keys/xiaoyu.env")
        with mock.patch.dict(os.environ, {"XIAOYU_ENV_FILE": explicit}):
            self.assertIn(explicit, sandbox.secret_paths())

    def test_deny_covers_reads_and_comes_last(self):
        text = sandbox.policy_text(1, True, secret_files=1)
        rule = '(deny file-read* file-write* (literal (param "SECRET_FILE_0")))'
        self.assertGreater(text.index(rule), text.rindex("(allow "))

    def test_bwrap_masks_existing_secret_with_empty_device(self):
        with tempfile.TemporaryDirectory() as tmp:
            secret = Path(tmp) / ".env"
            secret.write_text("K=1", encoding="utf-8")
            args = sandbox.bwrap_args([], True, [], [str(secret), str(Path(tmp) / "missing")])
        index = args.index(str(secret))
        self.assertEqual(args[index - 2 : index], ["--ro-bind", "/dev/null"])
        self.assertNotIn(str(Path(tmp) / "missing"), args)


class ProtectedPathsTest(unittest.TestCase):
    def test_deny_rules_come_after_every_allow(self):
        text = sandbox.policy_text(2, allow_network=True, protected_files=1, protected_dirs=1)
        last_allow = text.rindex("(allow ")
        self.assertGreater(text.index('(deny file-write* (literal (param "PROTECTED_FILE_0")))'), last_allow)
        self.assertGreater(text.index('(deny file-write* (subpath (param "PROTECTED_DIR_0")))'), last_allow)

    def test_paths_travel_as_parameters_not_policy_text(self):
        """路径里的引号、括号破坏不了策略语法：只经 -D 传值。"""
        with (
            mock.patch.object(sandbox.sys, "platform", "darwin"),
            mock.patch.object(sandbox, "available", return_value=True),
        ):
            argv = sandbox.wrap(["/bin/true"], Path('/tmp/w") (allow default) ("'))
        policy = argv[2]
        self.assertNotIn("allow default", policy)
        self.assertTrue(any(a.startswith("PROTECTED_FILE_0=") and a.endswith(".mcp.json") for a in argv))
        self.assertTrue(any(a.startswith("PROTECTED_DIR_0=") and a.endswith(".xiaoyu") for a in argv))

    def test_bwrap_remounts_existing_config_read_only_after_binds(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / ".mcp.json"
            config.write_text("{}", encoding="utf-8")
            missing = str(Path(tmp) / ".xiaoyu")
            args = sandbox.bwrap_args([tmp], True, [str(config), missing])
        self.assertGreater(args.index("--ro-bind", 3), args.index("--bind"))
        self.assertIn(str(config), args)
        self.assertNotIn(missing, args, "不存在的路径挂不了，挂了会在磁盘上留下空目录")

    def test_symlinked_config_protects_both_ends(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            (root / "real.json").write_text("{}", encoding="utf-8")
            try:
                (root / ".mcp.json").symlink_to(root / "real.json")
            except (OSError, NotImplementedError):
                self.skipTest("这台机器建不了符号链接")
            files, _ = sandbox.protected_paths(root)
        self.assertEqual(files, [str(root / ".mcp.json"), str(root / "real.json")])


class WrapTest(unittest.TestCase):
    def test_wrap_builds_sandbox_exec_call(self):
        with (
            mock.patch.object(sandbox.sys, "platform", "darwin"),
            mock.patch.object(sandbox, "available", return_value=True),
        ):
            argv = sandbox.wrap(["/bin/bash", "-lc", "ls"], Path("/tmp/ws"))
        self.assertEqual(argv[0], sandbox.SANDBOX_EXEC)
        self.assertEqual(argv[1], "-p")
        self.assertIn("--", argv)
        #  原命令原样排在 -- 之后
        self.assertEqual(argv[argv.index("--") + 1 :], ["/bin/bash", "-lc", "ls"])
        #  可写根目录以 -D 传入
        self.assertTrue(any(a.startswith("WRITABLE_0=") for a in argv))

    def test_absolute_path_is_hardcoded(self):
        """不能走 PATH：PATH 上放个假 sandbox-exec 就等于沙箱可以自己关掉自己。"""
        self.assertTrue(sandbox.SANDBOX_EXEC.startswith("/usr/bin/"))

    def test_unavailable_returns_argv_unchanged(self):
        with mock.patch.object(sandbox, "available", return_value=False):
            argv = sandbox.wrap(["/bin/bash", "-lc", "ls"], Path("/tmp/ws"))
        self.assertEqual(argv, ["/bin/bash", "-lc", "ls"])

    def test_extra_writable_roots_from_env(self):
        #  跟 Path 对象比，别跟路径字面量比——Windows 上 Path 渲染成反斜杠，
        #  拿 "/tmp/a" 断言会在非 POSIX 平台假失败
        with mock.patch.dict(os.environ, {"XIAOYU_SANDBOX_WRITABLE": "/tmp/a:/tmp/b"}):
            roots = sandbox.extra_writable_roots()
        self.assertEqual(roots, [Path("/tmp/a"), Path("/tmp/b")])

    def test_workspace_is_writable_root(self):
        roots = sandbox.default_writable_roots(Path("/tmp/myws"))
        self.assertIn(Path("/tmp/myws"), roots)

    def test_enabled_requires_both_config_and_platform(self):
        with mock.patch.object(sandbox, "available", return_value=True):
            self.assertTrue(sandbox.enabled(True))
            self.assertFalse(sandbox.enabled(False))
        with mock.patch.object(sandbox, "available", return_value=False):
            self.assertFalse(sandbox.enabled(True))


class DenialDetectionTest(unittest.TestCase):
    def test_recognizes_denial_signatures(self):
        for output in (
            "rm: /Users/x/f.txt: Operation not permitted",
            "bash: /Users/x/.zshrc: Permission denied",
        ):
            self.assertTrue(sandbox.looks_denied(output), output)

    def test_dialect_is_per_backend_not_union(self):
        """拒绝方言按当前后端匹配：不做跨后端并集。

        "Read-only file system" 是 bwrap ro-bind 的方言；macOS 上没有 ro-bind，
        它只能来自命令自身语境，算成沙箱拒绝会把模型带偏。
        """
        output = "error: Read-only file system"
        with mock.patch.object(sandbox.sys, "platform", "linux"):
            self.assertTrue(sandbox.looks_denied(output))
        with mock.patch.object(sandbox.sys, "platform", "darwin"):
            self.assertFalse(sandbox.looks_denied(output))

    def test_network_markers_only_when_network_disabled(self):
        """网络放行时 "Network is unreachable" 就是普通网络故障，与沙箱无关。"""
        output = "curl: (7) Network is unreachable"
        self.assertFalse(sandbox.looks_denied(output, network_disabled=False))
        self.assertTrue(sandbox.looks_denied(output, network_disabled=True))

    def test_ordinary_failures_not_flagged(self):
        for output in ("ModuleNotFoundError: No module named 'x'", "exit_status: 1\nsyntax error"):
            self.assertFalse(sandbox.looks_denied(output), output)

    def test_runner_failure_detected_by_prefix(self):
        """runner 自身失败 = 命令根本没跑，与"被沙箱挡"正交。"""
        line = sandbox.runner_failure(
            "stderr:\nbwrap: setting up uid map: Permission denied"
        )
        self.assertIsNotNone(line)
        self.assertIn("bwrap:", line)
        self.assertIsNotNone(
            sandbox.runner_failure("sandbox-exec: sandbox_apply: Operation not permitted")
        )

    def test_runner_failure_needs_line_start_prefix(self):
        """exit code 或正文里引用的 "bwrap:" 字样都不能证明 runner 失败。"""
        self.assertIsNone(sandbox.runner_failure("exit_status: 125\n(无输出)"))
        self.assertIsNone(
            sandbox.runner_failure("src/main.c:12: see bwrap: docs for detail")
        )

    def test_hint_mentions_workspace_and_network_state(self):
        workspace = Path("/tmp/ws")
        hint = sandbox.denial_hint(workspace, allow_network=False)
        #  用 str(Path) 而不是路径字面量：Windows 上渲染成反斜杠
        self.assertIn(str(workspace), hint)
        self.assertIn("网络", hint)
        #  要给模型下一步指引，不能只报"被拒了"
        self.assertIn("不要反复重试", hint)

    def test_hint_advertises_escalation_when_available(self):
        """升权协议可用时，拒绝提示改为广告"带理由原样重试一次"。"""
        hint = sandbox.denial_hint(Path("/tmp/ws"), allow_network=True, escalation=True)
        self.assertIn("sandbox_permissions", hint)
        self.assertIn("justification", hint)
        self.assertIn("最窄", hint)
        self.assertNotIn("XIAOYU_SANDBOX=0", hint)


@unittest.skipUnless(SANDBOXED, "本平台沙箱不可用")
class RealSandboxTest(unittest.TestCase):
    """真的过一遍内核验证行为（macOS 走 sandbox-exec，Linux 走 bwrap）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ws = Path(self.tmp.name).resolve()

    def run_in_sandbox(self, command: str, network: bool = True):
        argv = sandbox.wrap(["/bin/bash", "-lc", command], self.ws, network)
        return subprocess.run(
            argv, capture_output=True, text=True,
            encoding="utf-8", errors="replace", cwd=str(self.ws), timeout=60,
        )

    def test_write_inside_workspace_allowed(self):
        result = self.run_in_sandbox("echo hi > f.txt && cat f.txt")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "hi")

    def test_write_outside_workspace_denied(self):
        """沙箱的全部价值所在：工作区之外写不了。

        ⚠️ 探针必须落在真正不可写的地方：临时目录的父目录就是 $TMPDIR，
        本身在可写根里——拿它当"外面"会测出假通过。用家目录。
        """
        victim = home_probe("xiaoyu_sandbox_should_not_exist")
        self.addCleanup(lambda: victim.exists() and victim.unlink())
        result = self.run_in_sandbox(f"echo pwned > {victim}")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(victim.exists())
        self.assertTrue(sandbox.looks_denied(result.stderr))

    def test_existing_file_outside_cannot_be_deleted(self):
        """真实存在的文件删不掉——不存在的路径 rm -f 会假成功，测不出保护。"""
        home_victim = home_probe("xiaoyu_sandbox_test_victim")
        home_victim.write_text("precious", encoding="utf-8")
        self.addCleanup(lambda: home_victim.exists() and home_victim.unlink())
        result = self.run_in_sandbox(f"rm -f {home_victim}")
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(home_victim.exists(), "家目录里的文件被删掉了，沙箱没生效")

    def test_read_outside_workspace_allowed(self):
        #  读全盘放行是刻意的取舍（见 sandbox.py docstring）
        result = self.run_in_sandbox("cat /etc/hosts > /dev/null && echo read-ok")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("read-ok", result.stdout)

    def test_executable_config_is_read_only_inside_workspace(self):
        """小羽自己启动时会读的配置，沙箱内的命令改不了、删不掉、也顶替不了。"""
        (self.ws / ".mcp.json").write_text("{}", encoding="utf-8")
        (self.ws / ".xiaoyu").mkdir()
        (self.ws / ".xiaoyu" / "permissions.txt").write_text("# 空\n", encoding="utf-8")
        attempts = [
            "echo '{\"mcpServers\": {}}' > .mcp.json",
            "rm .mcp.json",
            "echo x > swap.json && mv swap.json .mcp.json",
            "echo 'allow bash' >> .xiaoyu/permissions.txt",
            "mkdir .xiaoyu/agents",
            "mv .xiaoyu moved",
        ]
        for command in attempts:
            with self.subTest(command=command):
                result = self.run_in_sandbox(command)
                self.assertNotEqual(result.returncode, 0, command)
        self.assertEqual((self.ws / ".mcp.json").read_text(encoding="utf-8"), "{}")
        self.assertEqual(
            (self.ws / ".xiaoyu" / "permissions.txt").read_text(encoding="utf-8"), "# 空\n"
        )
        self.assertFalse((self.ws / ".xiaoyu" / "agents").exists())

    def test_own_secret_files_are_unreadable(self):
        """子进程环境里剥掉的密钥，不能让一条 cat 从配置文件里读回来。"""
        conf = tempfile.TemporaryDirectory()
        self.addCleanup(conf.cleanup)
        secret = Path(conf.name).resolve() / ".env"
        secret.write_text("DEEPSEEK_API_KEY=sk-very-secret\n", encoding="utf-8")
        neighbour = secret.parent / "notes.txt"
        neighbour.write_text("plain\n", encoding="utf-8")
        with mock.patch.object(sandbox, "secret_paths", return_value=[str(secret)]):
            result = self.run_in_sandbox(f"cat '{secret}'")
            self.assertNotIn("sk-very-secret", result.stdout + result.stderr)
            copied = self.run_in_sandbox(f"cp '{secret}' stolen.txt; cat stolen.txt")
            self.assertNotIn("sk-very-secret", copied.stdout)
            #  同目录里别的文件照常可读
            other = self.run_in_sandbox(f"cat '{neighbour}'")
            self.assertEqual(other.stdout.strip(), "plain")

    def test_ordinary_dotfiles_stay_writable(self):
        (self.ws / ".git").mkdir()
        result = self.run_in_sandbox(
            "cp /etc/hosts .env && echo x > .git/config && echo y > .gitignore && "
            "mkdir sub && echo z > sub/notes.json && echo ok"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("ok", result.stdout)

    @unittest.skipUnless(sys.platform == "darwin", "bwrap 挡不住尚不存在的路径")
    def test_executable_config_cannot_be_created_either(self):
        result = self.run_in_sandbox("echo '{}' > .mcp.json")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.ws / ".mcp.json").exists())
        result = self.run_in_sandbox("mkdir -p .xiaoyu && echo 'allow bash' > .xiaoyu/permissions.txt")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.ws / ".xiaoyu").exists())

    def test_temp_dirs_writable(self):
        result = self.run_in_sandbox("echo x > /tmp/xiaoyu_probe && rm /tmp/xiaoyu_probe")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_python_and_git_still_work(self):
        """常用工具链不能被策略打死——这是沙箱最容易翻车的地方。"""
        result = self.run_in_sandbox("python3 -c 'import json, os; print(\"py-ok\")'")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("py-ok", result.stdout)
        result = self.run_in_sandbox("git init -q . && git status --short && echo git-ok")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("git-ok", result.stdout)

    def test_network_disabled_blocks_dns(self):
        result = self.run_in_sandbox(
            "curl -sS -m 8 -o /dev/null https://pypi.org/simple/ || echo blocked",
            network=False,
        )
        self.assertIn("blocked", result.stdout + result.stderr)


@unittest.skipUnless(SANDBOXED, "本平台沙箱不可用")
class BashToolIntegrationTest(unittest.TestCase):
    """沙箱接进 bash 工具后的端到端行为。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.config = Config(
            base_url="http://unused",
            model="m",
            workspace=self.root,
            enable_skills=False,
            enable_agents=False,
            enable_hooks=False,
            enable_plugins=False,
        )

    def test_sandbox_on_by_default(self):
        self.assertTrue(self.config.sandbox)
        box = Toolbox(self.config)
        #  家目录才是真正的"工作区之外"（$TMPDIR 在可写根里）
        victim = home_probe("xiaoyu_tool_probe")
        self.addCleanup(lambda: victim.exists() and victim.unlink())
        output = box.run("bash", {"command": f"echo pwned > {victim}"})
        self.assertFalse(victim.exists())
        #  失败要带沙箱说明，否则模型会把 EPERM 当命令写错而反复重试
        self.assertIn("沙箱提示", output)

    def test_sandbox_can_be_disabled(self):
        self.config.sandbox = False
        box = Toolbox(self.config)
        victim = home_probe("xiaoyu_tool_probe_off")
        self.addCleanup(lambda: victim.exists() and victim.unlink())
        box.run("bash", {"command": f"echo ok > {victim}"})
        self.assertTrue(victim.exists())

    def test_bash_description_mentions_sandbox(self):
        box = Toolbox(self.config)
        tool = box.get("bash")
        self.assertIn("沙箱", tool.description)


if __name__ == "__main__":
    unittest.main()
