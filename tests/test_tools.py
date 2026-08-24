"""工具层测试。跑法：.venv/bin/python -m unittest discover -s tests"""

from __future__ import annotations

import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from xiaoyu.config import Config
from xiaoyu.tools import PURPOSE_PARAM, Tool, Toolbox

SAMPLE = """def add(a, b):
    return a + b


def div(a, b):
    return a / b
"""


class ToolboxTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.config = Config(
            base_url="http://unused",
            model="unused",
            workspace=self.root,
            #  测试必须与跑测试机器上装的插件隔离
            enable_plugins=False,
        )
        self.box = Toolbox(self.config)
        self.sample = self.root / "calc.py"
        self.sample.write_text(SAMPLE, encoding="utf-8")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def read(self) -> str:
        return self.sample.read_text(encoding="utf-8")


class TestReadWrite(ToolboxTestCase):
    def test_read_missing(self) -> None:
        self.assertIn("文件不存在", self.box.run("read_file", {"path": "nope.py"}))

    def test_read_dir(self) -> None:
        self.assertIn("是目录", self.box.run("read_file", {"path": "."}))

    def test_read_with_offset_and_limit(self) -> None:
        result = self.box.run("read_file", {"path": "calc.py", "offset": 5, "limit": 2})
        self.assertIn("第 5-6 行", result)
        self.assertIn("def div(a, b):", result)
        self.assertNotIn("def add", result)

    def test_offset_beyond_end_is_an_error(self) -> None:
        self.assertIn("超出范围", self.box.run("read_file", {"path": "calc.py", "offset": 999}))

    def test_partial_read_does_not_authorize_overwrite(self) -> None:
        """只读了一段就允许覆盖整个文件，会丢掉没读到的部分。"""
        self.box.run("read_file", {"path": "calc.py", "offset": 1, "limit": 2})
        result = self.box.run("write_file", {"path": "calc.py", "content": "wiped"})
        self.assertIn("还没读过", result)
        self.assertEqual(self.read(), SAMPLE)

    def test_partial_read_does_not_authorize_str_replace(self) -> None:
        self.box.run("read_file", {"path": "calc.py", "offset": 1, "limit": 2})
        result = self.box.run(
            "str_replace", {"path": "calc.py", "old_str": "a + b", "new_str": "a - b"}
        )
        self.assertIn("还没读过", result)

    def test_full_read_authorizes_edit(self) -> None:
        self.box.run("read_file", {"path": "calc.py"})
        result = self.box.run(
            "str_replace", {"path": "calc.py", "old_str": "a + b", "new_str": "a - b"}
        )
        self.assertIn("已替换", result)

    def test_write_new_file_needs_no_prior_read(self) -> None:
        result = self.box.run("write_file", {"path": "fresh.py", "content": "x = 1\n"})
        self.assertIn("已创建", result)
        self.assertEqual((self.root / "fresh.py").read_text(encoding="utf-8"), "x = 1\n")

    def test_overwrite_without_read_is_blocked(self) -> None:
        result = self.box.run("write_file", {"path": "calc.py", "content": "wiped"})
        self.assertIn("还没读过", result)
        self.assertEqual(self.read(), SAMPLE)

    def test_overwrite_after_read_is_allowed(self) -> None:
        self.box.run("read_file", {"path": "calc.py"})
        result = self.box.run("write_file", {"path": "calc.py", "content": "x = 1\n"})
        self.assertIn("已覆盖", result)


class TestStrReplace(ToolboxTestCase):
    def test_requires_prior_read(self) -> None:
        result = self.box.run(
            "str_replace",
            {"path": "calc.py", "old_str": "return a / b", "new_str": "return 0"},
        )
        self.assertIn("还没读过", result)
        self.assertEqual(self.read(), SAMPLE)

    def test_happy_path(self) -> None:
        self.box.run("read_file", {"path": "calc.py"})
        result = self.box.run(
            "str_replace",
            {
                "path": "calc.py",
                "old_str": "    return a / b",
                "new_str": "    if b == 0:\n        raise ZeroDivisionError\n    return a / b",
            },
        )
        self.assertIn("已替换", result)
        self.assertIn("ZeroDivisionError", self.read())
        #  返回里带行号上下文，方便模型自查
        self.assertIn("|", result)

    def test_whitespace_mismatch_fuzzy_matches(self) -> None:
        """制表符缩进 vs 文件里的空格：精确匹配落空后由容错匹配接住。

        写进文件的必须是**原文的缩进**，不能把模型抄错的 tab 带进去。
        """
        self.box.run("read_file", {"path": "calc.py"})
        result = self.box.run(
            "str_replace",
            {"path": "calc.py", "old_str": "\treturn a / b", "new_str": "\treturn 0"},
        )
        self.assertIn("已替换", result)
        self.assertIn("容错匹配", result)
        self.assertIn("    return 0", self.read())
        self.assertNotIn("\t", self.read())

    def test_totally_absent_old_str_echoes_input(self) -> None:
        """连容错也匹配不上时报错，并回显模型给的 old_str 帮它自查。"""
        self.box.run("read_file", {"path": "calc.py"})
        result = self.box.run(
            "str_replace",
            {"path": "calc.py", "old_str": "def multiply(a, b):", "new_str": "def mul(a, b):"},
        )
        self.assertIn("找不到 old_str", result)
        self.assertIn("def multiply(a, b):", result)  # 回显原文
        self.assertEqual(self.read(), SAMPLE)

    def test_fuzzy_ambiguity_still_refused(self) -> None:
        """容错匹配多处命中必须打回——唯一性护栏不因容错而放松。"""
        (self.root / "dup.py").write_text("x = 1 \nx = 1  \n", encoding="utf-8")
        self.box.run("read_file", {"path": "dup.py"})
        #  行尾带 tab：精确匹配不上（两行都是空格结尾），rstrip 级容错两处命中
        result = self.box.run(
            "str_replace", {"path": "dup.py", "old_str": "x = 1\t", "new_str": "x = 2"}
        )
        self.assertIn("多处近似命中", result)
        self.assertEqual((self.root / "dup.py").read_text(encoding="utf-8"), "x = 1 \nx = 1  \n")

    def test_midline_start_with_multiline_new_str_refused(self) -> None:
        self.box.run("read_file", {"path": "calc.py"})
        result = self.box.run(
            "str_replace",
            {
                "path": "calc.py",
                "old_str": "return a / b",
                "new_str": "if b == 0:\n    raise ZeroDivisionError\nreturn a / b",
            },
        )
        self.assertIn("从行中间开始", result)
        self.assertEqual(self.read(), SAMPLE)

    def test_midline_single_line_replacement_allowed(self) -> None:
        self.box.run("read_file", {"path": "calc.py"})
        result = self.box.run(
            "str_replace",
            {"path": "calc.py", "old_str": "a / b", "new_str": "a // b"},
        )
        self.assertIn("已替换", result)
        self.assertIn("return a // b", self.read())

    def test_ambiguous_match_is_refused(self) -> None:
        self.sample.write_text("x = 1\nx = 1\n", encoding="utf-8")
        self.box.run("read_file", {"path": "calc.py"})
        result = self.box.run(
            "str_replace", {"path": "calc.py", "old_str": "x = 1", "new_str": "x = 2"}
        )
        self.assertIn("出现了 2 次", result)
        self.assertEqual(self.read(), "x = 1\nx = 1\n")

    def test_deletion_with_empty_new_str(self) -> None:
        self.box.run("read_file", {"path": "calc.py"})
        result = self.box.run(
            "str_replace",
            {"path": "calc.py", "old_str": "\n\ndef div(a, b):\n    return a / b\n", "new_str": ""},
        )
        self.assertIn("已删除", result)
        self.assertNotIn("div", self.read())

    def test_empty_old_str_refused(self) -> None:
        self.box.run("read_file", {"path": "calc.py"})
        result = self.box.run(
            "str_replace", {"path": "calc.py", "old_str": "", "new_str": "x"}
        )
        self.assertIn("不能为空", result)

    def test_identical_strings_refused(self) -> None:
        self.box.run("read_file", {"path": "calc.py"})
        result = self.box.run(
            "str_replace", {"path": "calc.py", "old_str": "a + b", "new_str": "a + b"}
        )
        self.assertIn("完全相同", result)

    def test_stale_read_is_refused(self) -> None:
        self.box.run("read_file", {"path": "calc.py"})
        #  模拟外部改动：内容变了，mtime 也往后推
        self.sample.write_text(SAMPLE.replace("a + b", "a - b"), encoding="utf-8")
        stat = self.sample.stat()
        os.utime(self.sample, (stat.st_atime + 10, stat.st_mtime + 10))
        result = self.box.run(
            "str_replace",
            {"path": "calc.py", "old_str": "    return a / b", "new_str": "    return 0"},
        )
        self.assertIn("被改动过", result)


class TestReadStreakGuard(ToolboxTestCase):
    """连续读文件的引导与拦截。

    背景：实测靠 prompt 引导用 explore 的采用率只有 1/3（3 次运行里 1 次），
    而 explore 用起来能省一半主模型上下文。措辞劝不动，就在 harness 层面卡。
    """

    def setUp(self) -> None:
        super().setUp()
        for index in range(8):
            (self.root / f"m{index}.py").write_text(f"VALUE = {index}\n", encoding="utf-8")
        #  只有挂了 explore 才谈得上引导它去用
        self.box.register(
            Tool(
                name="explore",
                description="假的 explore，仅用于测试",
                parameters={"type": "object", "properties": {}},
                handler=lambda: "结论",
            )
        )

    def read(self, index: int) -> str:
        return self.box.run("read_file", {"path": f"m{index}.py"})

    def test_no_note_before_threshold(self) -> None:
        for index in range(2):
            self.assertNotIn("[提示]", self.read(index))

    def test_note_appears_at_threshold(self) -> None:
        outputs = [self.read(index) for index in range(3)]
        self.assertIn("[提示]", outputs[2])
        self.assertIn("explore", outputs[2])
        #  提示是追加的，原始内容不能被吞掉
        self.assertIn("VALUE = 2", outputs[2])

    def test_blocked_after_five(self) -> None:
        outputs = [self.read(index) for index in range(5)]
        self.assertTrue(outputs[4].startswith("ERROR:"))
        self.assertIn("explore", outputs[4])
        #  被拦截时不该返回文件内容
        self.assertNotIn("VALUE = 4", outputs[4])

    def test_other_tool_resets_the_streak(self) -> None:
        for index in range(4):
            self.read(index)
        self.box.run("grep", {"pattern": "VALUE"})
        result = self.read(5)
        self.assertFalse(result.startswith("ERROR:"), "用了别的工具后应该允许继续读")
        self.assertIn("VALUE = 5", result)

    def test_explore_call_also_resets(self) -> None:
        for index in range(4):
            self.read(index)
        self.box.run("explore", {})
        self.assertIn("VALUE = 5", self.read(5))

    def test_no_guard_when_explore_unavailable(self) -> None:
        """没有 explore 可用时，引导它去用 explore 是荒谬的 —— 必须完全不触发。"""
        box = Toolbox(self.config)
        self.assertIsNone(box.get("explore"))
        for index in range(8):
            result = box.run("read_file", {"path": f"m{index}.py"})
            self.assertFalse(result.startswith("ERROR:"), f"第 {index + 1} 次读被误拦")
            self.assertNotIn("[提示]", result)

    def test_thresholds_are_configurable(self) -> None:
        self.config.read_streak_warn = 1
        self.config.read_streak_block = 2
        self.assertIn("[提示]", self.read(0))
        self.assertTrue(self.read(1).startswith("ERROR:"))


class TestBashAndSafety(ToolboxTestCase):
    def test_bash_runs_in_workspace(self) -> None:
        result = self.box.run("bash", {"command": "pwd"})
        self.assertIn("exit_status: 0", result)
        self.assertIn(str(self.root), result)

    def test_bash_reports_failure(self) -> None:
        result = self.box.run("bash", {"command": "exit 3"})
        self.assertIn("exit_status: 3", result)

    def test_bash_timeout(self) -> None:
        result = self.box.run("bash", {"command": "sleep 5", "timeout": 1})
        self.assertIn("超时", result)

    def test_outside_workspace_needs_approval(self) -> None:
        self.assertTrue(self.box.needs_approval("read_file", {"path": "/etc/hosts"}))
        self.assertFalse(self.box.needs_approval("read_file", {"path": "calc.py"}))

    def test_unknown_tool(self) -> None:
        self.assertIn("未知工具", self.box.run("nope", {}))

    def test_bad_arguments(self) -> None:
        self.assertIn("参数不对", self.box.run("read_file", {"wrong": 1}))

    def test_hardline_blocks_destructive_commands(self) -> None:
        """不可撤销的破坏性命令必须被拦，且不因 auto_approve 放行。"""
        from xiaoyu.tools import hardline_violation

        blocked = [
            "rm -rf /",
            "rm -rf /*",
            "rm -rf ~",
            "sudo rm -fr $HOME",
            "echo hi && rm -rf / ",
            ":(){ :|:& };:",
            "mkfs.ext4 /dev/sda1",
            "dd if=/dev/zero of=/dev/sda",
            "cat garbage > /dev/sda",
            "diskutil eraseDisk free X disk0",
            "format c:",
            "rd /s /q C:\\",
            "Remove-Item -Recurse -Force C:\\",
        ]
        for command in blocked:
            self.assertIsNotNone(hardline_violation(command), msg=command)

        allowed = [
            "rm -rf ./build",
            "rm -rf /tmp/xiaoyu-test",
            "rm file.txt",
            "dd if=/dev/zero of=./disk.img bs=1M count=10",
            "echo '> /dev/null'",
            "git status",
            "Remove-Item -Recurse -Force .\\build",
        ]
        for command in allowed:
            self.assertIsNone(hardline_violation(command), msg=command)

    def test_hardline_blocks_even_with_auto_approve(self) -> None:
        self.config.auto_approve = True
        result = self.box.run("bash", {"command": "rm -rf /"})
        self.assertIn("硬性拦截", result)
        self.assertTrue(result.startswith("ERROR:"))

    def test_shell_argv_per_platform(self) -> None:
        """Windows 上没有 /bin/bash：必须分派到 PowerShell，否则所有命令 WinError 2。

        没装 pwsh 的机器退回 PS 5.1，且必须带 UTF-8 前缀——否则中文 Windows 的
        报错全按 GBK 输出、Python 侧解成一串 �，模型只能盲猜。
        """
        from unittest import mock

        from xiaoyu import tools as tools_module

        with mock.patch.object(tools_module.os, "name", "nt"), mock.patch.object(
            tools_module, "_pwsh_path", return_value=None
        ):
            argv = tools_module._shell_argv("Get-Date")
            self.assertEqual(argv[0], "powershell")
            self.assertIn("-NoProfile", argv)
            self.assertIn("Get-Date", argv[-1])
            self.assertIn("UTF8", argv[-1])
        with mock.patch.object(tools_module.os, "name", "nt"), mock.patch.object(
            tools_module, "_pwsh_path", return_value="C:\\pwsh\\pwsh.exe"
        ):
            argv = tools_module._shell_argv("Get-Date")
            #  pwsh 默认就是 UTF-8，命令原样传、不加前缀
            self.assertEqual(argv[0], "C:\\pwsh\\pwsh.exe")
            self.assertEqual(argv[-1], "Get-Date")
        with mock.patch.object(tools_module.os, "name", "posix"):
            self.assertEqual(tools_module._shell_argv("pwd"), ["/bin/bash", "-lc", "pwd"])

    def test_bash_description_matches_platform(self) -> None:
        """工具描述里的平台提示必须和实际执行的 shell 一致，别再教 Windows 用户 BSD sed。"""
        from unittest import mock

        from xiaoyu import tools as tools_module

        with mock.patch.object(tools_module.os, "name", "nt"):
            note = tools_module._platform_shell_note()
            self.assertIn("PowerShell", note)
            self.assertNotIn("macOS", note)
        with mock.patch.object(tools_module.os, "name", "posix"), mock.patch.object(
            tools_module.sys, "platform", "darwin"
        ):
            self.assertIn("macOS", tools_module._platform_shell_note())
        with mock.patch.object(tools_module.os, "name", "posix"), mock.patch.object(
            tools_module.sys, "platform", "linux"
        ):
            self.assertIn("Linux", tools_module._platform_shell_note())

    def test_bash_receives_extra_env(self) -> None:
        """eval 靠 extra_env 挡住"pip install 到系统 Python"，这条链路不能断。"""
        self.config.extra_env = {"XIAOYU_PROBE": "ok", "PIP_REQUIRE_VIRTUALENV": "true"}
        #  Windows 上命令由 PowerShell 执行，环境变量语法不同
        command = (
            "echo $env:XIAOYU_PROBE-$env:PIP_REQUIRE_VIRTUALENV"
            if os.name == "nt"
            else "echo $XIAOYU_PROBE-$PIP_REQUIRE_VIRTUALENV"
        )
        result = self.box.run("bash", {"command": command})
        self.assertIn("ok-true", result)

    def test_output_truncation(self) -> None:
        """超长输出走 spill：完整落盘 + 内联留预览和短召回 id。"""
        self.config.max_tool_output = 50
        result = self.box.run("bash", {"command": "python3 -c \"print('x' * 500)\""})
        self.assertIn("召回 id: 1", result)
        #  完整内容真的落盘了
        spilled = self.box._spills["1"]["path"]  # noqa: SLF001
        self.assertTrue(spilled.is_file())
        self.assertIn("x" * 500, spilled.read_text(encoding="utf-8"))
        #  召回不该再弹确认：spill 文件按工作区内对待
        self.assertFalse(self.box.outside_workspace({"path": str(spilled)}))
        self.assertFalse(self.box.needs_approval("read_file", {"path": str(spilled)}))
        #  recall 按 id 取回中段
        self.config.max_tool_output = 5000
        retrieved = self.box.run("recall", {"id": "1", "pattern": "xxx"})
        self.assertIn("xxx", retrieved)
        #  read_file(path) 仍可直读、不带"工作区之外"标注（power-user 通道）
        direct = self.box.run("read_file", {"path": str(spilled), "offset": 1, "limit": 1})
        self.assertNotIn("工作区之外", direct)

    def test_spill_write_failure_falls_back_to_truncate(self) -> None:
        """落盘失败（磁盘满等）退回纯截断，工具本身不能受影响。"""
        self.config.max_tool_output = 50
        with mock.patch.object(self.box, "_spill", return_value=None):
            result = self.box.run("bash", {"command": "python3 -c \"print('x' * 500)\""})
        self.assertIn("已截断", result)

    def test_truncation_keeps_head_and_tail(self) -> None:
        """超长输出保头保尾砍中段：测试失败汇总、构建错误都在结尾。"""
        self.config.max_tool_output = 200
        text = "HEAD_MARK\n" + "x" * 2000 + "\nTAIL_MARK"
        result = self.box._truncate(text)
        self.assertIn("HEAD_MARK", result)
        self.assertIn("TAIL_MARK", result)
        self.assertIn("中间已截断", result)
        #  顶部声明原始规模，模型才能决定要不要换姿势重取
        self.assertIn(str(len(text)), result)

    @unittest.skipIf(os.name == "nt", "POSIX 专属：partial output 行为")
    def test_bash_timeout_returns_partial_output(self) -> None:
        """超时不当 error：超时前的输出往往已经包含答案。"""
        result = self.box.run(
            "bash", {"command": "echo before_timeout; sleep 5", "timeout": 1}
        )
        self.assertIn("超时", result)
        self.assertIn("124", result)
        self.assertIn("before_timeout", result)
        self.assertFalse(result.startswith("ERROR:"))

    @unittest.skipIf(os.name == "nt", "POSIX 专属：进程组语义")
    def test_bash_timeout_kills_whole_process_tree(self) -> None:
        """孙进程握着管道时超时必须仍然按时返回。

        真实 Windows 会话里"15s 超时"实际挂了 18~29 分钟：只杀 shell、
        后台子进程继承的管道让 communicate 一直阻塞。POSIX 上用
        `sleep 30 &` 复现同一形态——修复后整组被杀，秒级返回。
        """
        import time as time_module

        started = time_module.monotonic()
        result = self.box.run(
            "bash", {"command": "sleep 30 & echo spawned; sleep 30", "timeout": 1}
        )
        elapsed = time_module.monotonic() - started
        self.assertLess(elapsed, 10, f"超时未整树生效，等了 {elapsed:.0f}s")
        self.assertIn("超时", result)
        self.assertIn("进程树", result)
        self.assertIn("spawned", result)

    def test_bash_stdin_is_devnull(self) -> None:
        """子进程不许继承 tty stdin。

        ssh 等命令默认读取并转发 stdin：继承 tty 会和输入 prompt、插话线程
        抢键入。行为测法（cat 是否阻塞）在 stdin 本就是 /dev/null 的环境里
        测不出回退，这里直接断言 Popen 参数。
        """
        import subprocess

        from xiaoyu import tools as tools_module

        real_popen = subprocess.Popen
        seen: dict[str, object] = {}

        def spy_popen(*args: object, **kwargs: object) -> subprocess.Popen:
            seen.update(kwargs)
            return real_popen(*args, **kwargs)  # type: ignore[arg-type]

        with mock.patch.object(tools_module.subprocess, "Popen", spy_popen):
            result = self.box.run("bash", {"command": "echo ok"})
        self.assertIn("exit_status: 0", result)
        self.assertEqual(seen.get("stdin"), subprocess.DEVNULL)

    @unittest.skipIf(os.name == "nt", "POSIX 专属：进程组语义")
    def test_bash_interrupt_kills_process_tree(self) -> None:
        """Ctrl-C 中断后子进程必须已死。

        start_new_session 让子进程收不到 tty 的 SIGINT：中断路径不主动杀，
        遗孤（如 ssh）就继续跑并偷 tty 键入——用户侧表现为"中断后提示符在、
        打字没反应"（iTerm2 实测）。
        """
        import subprocess

        from xiaoyu import tools as tools_module

        real_popen = subprocess.Popen
        spawned: list[subprocess.Popen] = []

        def spy_popen(*args: object, **kwargs: object) -> subprocess.Popen:
            proc = real_popen(*args, **kwargs)  # type: ignore[arg-type]
            spawned.append(proc)

            def interrupt(timeout: float | None = None) -> tuple[bytes, bytes]:
                raise KeyboardInterrupt

            proc.communicate = interrupt  # type: ignore[method-assign]
            return proc

        with mock.patch.object(tools_module.subprocess, "Popen", spy_popen):
            with self.assertRaises(KeyboardInterrupt):
                self.box.run("bash", {"command": "sleep 30"})
        (proc,) = spawned
        #  整组应已被 SIGKILL；若还活着，wait 会在 5s 后抛 TimeoutExpired
        proc.wait(timeout=5)
        self.assertIsNotNone(proc.poll())

    def test_decode_output_falls_back_to_gbk(self) -> None:
        """中文 Windows 的 GBK 输出不能解成一串 �——报错可读性就是自愈能力。

        必须 mock 掉 locale：这个测试断言的是"**中文 Windows 上**（preferred
        =cp936）GBK 输出能解出来"。裸跑在 en-US Windows（CI）上 preferred 是
        cp1252——一个几乎永不失败的单字节解码器，会抢在 GBK 前面把字节解成
        mojibake，测试红的是环境不是代码（CI 两个 Windows 格子因此红了三轮）。
        """
        import xiaoyu.tools as tools_module
        from xiaoyu.tools import _decode_output

        gbk_bytes = "无法将 lark-cli 识别为 cmdlet".encode("gbk")
        with mock.patch.object(
            tools_module.locale, "getpreferredencoding", return_value="cp936"
        ):
            self.assertEqual(_decode_output(gbk_bytes), "无法将 lark-cli 识别为 cmdlet")
        self.assertEqual(_decode_output("已是文本".encode("utf-8")), "已是文本")
        self.assertEqual(_decode_output(None), "")
        self.assertEqual(_decode_output("原样"), "原样")

    def test_interactive_auth_hint(self) -> None:
        """超时输出里带浏览器授权链接时，要把正确姿势直接告诉模型。"""
        from xiaoyu.tools import _interactive_auth_hint

        hint = _interactive_auth_hint(
            "在浏览器中打开以下链接进行认证:\n"
            "https://accounts.feishu.cn/oauth/v1/device/verify?flow_id=xxx"
        )
        self.assertIn("交互式认证", hint)
        #  普通输出（有 URL 但不是授权场景 / 无 URL）不加噪音
        self.assertEqual(_interactive_auth_hint("clone https://example.com/repo.git"), "")
        self.assertEqual(_interactive_auth_hint("tests passed"), "")

    def test_bash_env_is_hardened(self) -> None:
        """LD_*/DYLD_* 注入通道不能传给子进程。"""
        from xiaoyu.tools import _hardened_env

        with mock.patch.dict(
            os.environ, {"LD_PRELOAD": "/tmp/evil.so", "DYLD_INSERT_LIBRARIES": "x", "KEEP": "1"}
        ):
            env = _hardened_env({"EXTRA": "2"})
        self.assertNotIn("LD_PRELOAD", env)
        self.assertNotIn("DYLD_INSERT_LIBRARIES", env)
        self.assertEqual(env["KEEP"], "1")
        self.assertEqual(env["EXTRA"], "2")

    def test_bash_env_strips_provider_secrets(self) -> None:
        """小羽自己的模型密钥不传给模型跑的命令：继承的剔、extra 里的也剔、
        大小写不敏感；extra 里的其它变量照传（操作者的明确决定）。"""
        from xiaoyu.tools import _hardened_env, non_inheritable_env_names

        names = non_inheritable_env_names()
        for expected in ("XIAOYU_API_KEY", "LITELLM_API_KEY", "DEEPSEEK_API_KEY",
                         "ANTHROPIC_API_KEY", "DASHSCOPE_API_KEY", "XIAOYU_SERVE_TOKEN"):
            self.assertIn(expected, names)
        with mock.patch.dict(
            os.environ,
            {"XIAOYU_API_KEY": "sk-1", "OPENAI_API_KEY": "sk-2", "GITHUB_TOKEN": "gh"},
        ):
            env = _hardened_env({"deepseek_api_key": "sk-3", "LARK_TOKEN": "t"})
        self.assertNotIn("XIAOYU_API_KEY", env)
        self.assertNotIn("OPENAI_API_KEY", env)
        self.assertNotIn("deepseek_api_key", env)
        self.assertEqual(env["GITHUB_TOKEN"], "gh")
        self.assertEqual(env["LARK_TOKEN"], "t")

    def test_shared_key_envs_stay_inheritable(self) -> None:
        """多产品通用名（shared_key_envs，如 GOOGLE_API_KEY）不剥：gcloud 一类
        无关命令正当地需要它；同一 preset 的专名（GEMINI_API_KEY）照剥。"""
        from xiaoyu.tools import _hardened_env, non_inheritable_env_names

        names = non_inheritable_env_names()
        self.assertIn("GEMINI_API_KEY", names)
        self.assertNotIn("GOOGLE_API_KEY", names)
        with mock.patch.dict(
            os.environ, {"GEMINI_API_KEY": "sk-g", "GOOGLE_API_KEY": "AIza-maps"}
        ):
            env = _hardened_env()
        self.assertNotIn("GEMINI_API_KEY", env)
        self.assertEqual(env["GOOGLE_API_KEY"], "AIza-maps")


class PurposeParamTest(unittest.TestCase):
    """__tool_use_purpose 注入：只进需确认工具的 schema。"""

    PARAMS = {
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"],
    }

    def test_injected_for_approval_tools_only(self):
        risky = Tool(name="t", description="d", parameters=self.PARAMS, handler=lambda **kw: "")
        schema = risky.schema()["function"]["parameters"]
        self.assertIn(PURPOSE_PARAM, schema["properties"])
        #  可选参数：绝不能进 required，否则模型不写目的就调不了工具
        self.assertNotIn(PURPOSE_PARAM, schema["required"])
        safe = Tool(
            name="s",
            description="d",
            parameters=self.PARAMS,
            handler=lambda **kw: "",
            requires_approval=False,
        )
        self.assertNotIn(PURPOSE_PARAM, safe.schema()["function"]["parameters"]["properties"])

    def test_original_parameters_not_mutated(self):
        params = {"type": "object", "properties": {"path": {"type": "string"}}}
        tool = Tool(name="t", description="d", parameters=params, handler=lambda **kw: "")
        tool.schema()
        self.assertNotIn(PURPOSE_PARAM, params["properties"], "注入必须走拷贝，不改原 dict")


class TestSandboxEscalation(unittest.TestCase):
    """bash 升权协议：成对参数、只能向上、一次一授。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def build(self, sandbox_on: bool = True, network: bool = True) -> Toolbox:
        config = Config(
            base_url="http://unused",
            model="unused",
            workspace=self.root,
            enable_plugins=False,
            sandbox=sandbox_on,
            sandbox_network=network,
        )
        with mock.patch("xiaoyu.tools.sandbox.available", return_value=sandbox_on):
            return Toolbox(config)

    def test_schema_gains_params_only_when_sandboxed(self) -> None:
        with mock.patch("xiaoyu.tools.sandbox.available", return_value=True):
            box = self.build(sandbox_on=True)
            props = box.get("bash").parameters["properties"]
            self.assertIn("sandbox_permissions", props)
            self.assertIn("justification", props)
        #  没有沙箱就没有权限可升：参数不该出现在 schema 里
        box_plain = self.build(sandbox_on=False)
        self.assertNotIn("sandbox_permissions", box_plain.get("bash").parameters["properties"])

    def test_modes_only_offer_not_yet_granted(self) -> None:
        """只能向上：已放行的能力不出现在档位表（网络已开就没有 allow-network）。"""
        with mock.patch("xiaoyu.tools.sandbox.available", return_value=True):
            self.assertEqual(
                self.build(network=False)._escalation_modes(),
                ["allow-network", "danger-full-access"],
            )
            self.assertEqual(
                self.build(network=True)._escalation_modes(),
                ["danger-full-access"],
            )

    def test_pairing_is_mandatory(self) -> None:
        with mock.patch("xiaoyu.tools.sandbox.available", return_value=True):
            box = self.build()
            #  只有档位没有理由
            result = box.run(
                "bash", {"command": "true", "sandbox_permissions": "danger-full-access"}
            )
            self.assertIn("成对出现", result)
            #  只有理由没有档位
            result = box.run("bash", {"command": "true", "justification": "要写系统目录"})
            self.assertIn("成对出现", result)

    def test_unknown_or_non_expanding_mode_rejected(self) -> None:
        with mock.patch("xiaoyu.tools.sandbox.available", return_value=True):
            box = self.build(network=True)
            #  网络已放行，allow-network 是"非扩大"请求：直接失败、不打扰任何人
            result = box.run(
                "bash",
                {
                    "command": "true",
                    "sandbox_permissions": "allow-network",
                    "justification": "要联网",
                },
            )
            self.assertIn("未知的升权档位", result)

    def test_escalation_without_sandbox_rejected(self) -> None:
        box = self.build(sandbox_on=False)
        result = box.run(
            "bash",
            {
                "command": "true",
                "sandbox_permissions": "danger-full-access",
                "justification": "test",
            },
        )
        self.assertIn("没有沙箱在生效", result)

    def test_danger_full_access_skips_wrap(self) -> None:
        """danger-full-access = 本次不套沙箱：argv 不经过 sandbox.wrap。"""
        with mock.patch("xiaoyu.tools.sandbox.available", return_value=True):
            box = self.build()
            with mock.patch("xiaoyu.tools.sandbox.wrap") as wrap:
                argv = box._command_argv("true", escalation="danger-full-access")
            wrap.assert_not_called()
            self.assertIn("true", " ".join(argv))

    def test_allow_network_wraps_with_network(self) -> None:
        """allow-network = 仍套沙箱但放行网络。"""
        with mock.patch("xiaoyu.tools.sandbox.available", return_value=True):
            box = self.build(network=False)
            with mock.patch(
                "xiaoyu.tools.sandbox.wrap", side_effect=lambda argv, ws, net: argv
            ) as wrap:
                box._command_argv("true", escalation="allow-network")
                self.assertTrue(wrap.call_args.args[2], "allow-network 必须放行网络")
                box._command_argv("true", escalation=None)
                self.assertFalse(wrap.call_args.args[2], "未升权时维持断网")

    def test_escalated_run_is_marked(self) -> None:
        """升权执行的结果要打标：用户和模型都看得出这次是升权跑的。"""
        with mock.patch("xiaoyu.tools.sandbox.available", return_value=True):
            box = self.build()
            result = box.run(
                "bash",
                {
                    "command": "echo ok",
                    "sandbox_permissions": "danger-full-access",
                    "justification": "测试打标",
                },
            )
        self.assertIn("升权执行", result)
        self.assertIn("ok", result)


if __name__ == "__main__":
    unittest.main()
