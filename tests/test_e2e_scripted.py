"""CLI 层黑盒 e2e：真子进程 + scripted 桩，不打网络。

被测的是整条链路：argparse → Config/Registry → Agent 主循环 → 工具执行 →
事件流 → stream-json 输出。唯一被替换的是 LLM 网络调用（XIAOYU_SCRIPTED_SCRIPTS）。

方法四件套：
- 可编排假 provider：脚本队列，每次 LLM 调用弹一轮（xiaoyu/scripted.py）；
- 真实进程 + 真实输出协议：`python -m xiaoyu <prompt> --output-format stream-json`；
- 激进归一化：路径 → <tmp>，耗时 → 0，会话路径 → <session_log>——
  归一化后的事件列表才可与手写期望直接比对；
- 环境隔离：用户目录/配置目录（posix 的 HOME/XDG_CONFIG_HOME、Windows 的
  USERPROFILE/APPDATA）四个都指到临时目录，技能/插件/MCP/explore 全关。

没有 pytest / inline-snapshot（venv 刻意只有标准库 unittest），期望值手写：
事件序列短（个位数），手写比快照更能表达"为什么该是这个序列"。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

_TIMEOUT = 120  # 单个子进程的硬上限（秒）：卡死要响亮，不要挂住整个套件


def _normalize(event: dict[str, Any], tmp: str) -> dict[str, Any]:
    """把不确定字段压成常量，让事件可与手写期望直接比对。"""
    out: dict[str, Any] = {}
    for key, value in event.items():
        if key == "seconds":
            out[key] = 0
        elif key == "session_log":
            out[key] = "<session_log>"
        elif isinstance(value, str):
            out[key] = value.replace(tmp, "<tmp>")
        elif isinstance(value, dict):
            out[key] = _normalize(value, tmp)
        else:
            out[key] = value
    return out


class E2ECase(unittest.TestCase):
    """公共驱动：写脚本 → 起子进程 → 收 NDJSON → 归一化。"""

    def setUp(self):
        #  ignore_cleanup_errors：Windows 上子进程刚退出时，句柄/杀毒扫描可能还
        #  按着临时目录（WinError 32/5），清不掉不该把用例判失败。
        self._tmpdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        #  清理走 addCleanup 而非 tearDown：cleanup 是 LIFO，子类 addCleanup 注册
        #  的"关子进程"一定排在删目录之前。反过来（tearDown 删目录）会在子进程还
        #  占着 cwd 时删，Windows 上直接 PermissionError。
        self.addCleanup(self._tmpdir.cleanup)
        self.tmp = os.path.realpath(self._tmpdir.name)
        self.workspace = Path(self.tmp) / "ws"
        self.workspace.mkdir()
        (Path(self.tmp) / "home").mkdir()
        (Path(self.tmp) / "config").mkdir()

    def scripted_env(self, script: str) -> dict[str, str]:
        """隔离环境：脚本文本落到临时文件再走 env_for。其它 e2e 文件复用这份。"""
        script_path = Path(self.tmp) / "script.txt"
        script_path.write_text(script, encoding="utf-8")
        return self.env_for(script_path)

    def env_for(self, script_path: Path) -> dict[str, str]:
        """隔离环境：配置/会话/技能全圈进临时目录；HOME 也指走，防读真机的
        ~/.agents/skills 或 macOS Keychain 弹窗。snapshot 套件用现成 fixture
        路径直接调这层。"""
        env = {
            **os.environ,
            "XIAOYU_SCRIPTED_SCRIPTS": str(script_path),
            #  用户目录/配置目录各平台推法不同，四个变量得一起指走，漏一个就等于
            #  没隔离：posix 看 HOME/XDG_CONFIG_HOME，Windows 看 USERPROFILE
            #  （Path.home）和 APPDATA（user_config_dir）——只设前两个的话，
            #  Windows 上子进程会去读写真机的 %APPDATA%\xiaoyu。
            "HOME": str(Path(self.tmp) / "home"),
            "USERPROFILE": str(Path(self.tmp) / "home"),
            "XDG_CONFIG_HOME": str(Path(self.tmp) / "config"),
            "APPDATA": str(Path(self.tmp) / "config"),
            "XIAOYU_ENABLE_SKILLS": "0",
            "XIAOYU_ENABLE_PLUGINS": "0",
            "XIAOYU_ENABLE_MCP": "0",
            "XIAOYU_ENABLE_HOOKS": "0",
            "XIAOYU_ENABLE_AGENTS": "0",
            "XIAOYU_ENABLE_EXPLORE": "0",
            "XIAOYU_ENABLE_WEB_SEARCH": "0",
            #  browser 的可用性=「宿主装没装 playwright」（GitHub runner 的
            #  ubuntu/macos 镜像就预装了包）——不关掉的话 golden 工具表随环境漂移
            "XIAOYU_ENABLE_BROWSER": "0",
            #  沙箱可用性因机器而异（bwrap/Seatbelt），e2e 断言的不是沙箱
            "XIAOYU_SANDBOX": "0",
        }
        #  本机 shell 里若残留 snapshot 相关开关（录制/捕获/strict），会让所有
        #  e2e 子进程行为漂移——一律剔除，要用的套件（snapshot）自己显式再设
        for stray in ("XIAOYU_SNAPSHOT_RECORD", "XIAOYU_SCRIPTED_CAPTURE", "XIAOYU_SCRIPTED_STRICT"):
            env.pop(stray, None)
        return env

    def run_cli(
        self,
        script: str,
        prompt: str = "干活",
        extra_args: list[str] | None = None,
        extra_env: dict[str, str] | None = None,
    ) -> tuple[list[dict[str, Any]], dict[str, Any], int, str]:
        """返回 (归一化事件列表, result 收尾对象, 退出码, stderr)。"""
        env = self.scripted_env(script)
        env.update(extra_env or {})
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "xiaoyu",
                prompt,
                "--output-format",
                "stream-json",
                "--workspace",
                str(self.workspace),
                *(extra_args or []),
            ],
            #  stdin 必须显式断开：不给的话子进程继承调用方的 stdin，而
            #  headless 纪律下非 tty 的 stdin 会被当指令读——调用方的 stdin
            #  若是不 EOF 的管道/socket（CI 之外的各种壳层就是），子进程
            #  会一直读下去，表现为"整套测试匀速变慢直到超时"，且只在某些
            #  调用方身上复现。
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            #  子进程恒输出 UTF-8（见 ui.py 的 reconfigure）；不显式指定的话
            #  Windows 上父进程按 cp1252 解码，读取线程直接崩、stdout 变 None
            encoding="utf-8",
            errors="replace",
            timeout=_TIMEOUT,
            env=env,
            cwd=self.tmp,
        )
        events: list[dict[str, Any]] = []
        result: dict[str, Any] = {}
        for line in proc.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            #  stream-json 契约：stdout 只准出现结构化行——解析失败即测试失败
            payload = json.loads(line)
            payload = _normalize(payload, self.tmp)
            if payload.get("kind") == "result":
                result = payload
            else:
                events.append(payload)
        return events, result, proc.returncode, proc.stderr

    def kinds(self, events: list[dict[str, Any]]) -> list[str]:
        return [event["kind"] for event in events]


class TextTurnTest(E2ECase):
    def test_single_text_turn_event_sequence(self):
        events, result, code, stderr = self.run_cli(
            'text: 你好\ntext: "，世界"\nusage: {"prompt_tokens": 100, "completion_tokens": 5}\n'
        )
        self.assertEqual(code, 0, stderr)
        self.assertEqual(
            self.kinds(events),
            ["request.started", "text.delta", "text.delta", "text.end", "request.ended"],
        )
        self.assertEqual(events[1]["text"], "你好")
        self.assertEqual(result["result"], "你好，世界")
        self.assertNotIn("error", result)

    def test_usage_is_accounted_per_qualified_route(self):
        _, result, code, stderr = self.run_cli(
            'text: ok\nusage: {"prompt_tokens": 100, "completion_tokens": 5}\n'
        )
        self.assertEqual(code, 0, stderr)
        usage = result["usage"]
        self.assertEqual(usage["prompt_tokens"], 100)
        self.assertEqual(usage["completion_tokens"], 5)
        #  分模型记账键是全限定名 provider/model；模型名来自默认配置，不写死
        (qualified,) = usage["by_model"].keys()
        self.assertTrue(qualified.startswith("_scripted/"), qualified)
        self.assertEqual(usage["by_model"][qualified]["calls"], 1)


class EncodingTest(E2ECase):
    """把 Windows 的编码处境搬到本机跑，不必等 Windows CI 才发现炸了。

    `PYTHONIOENCODING` 直接决定子进程三个标准流的默认编码，设成 iso8859-1 就
    等价于中文 Windows 上 GBK / 英文 Windows 上 cp1252 的默认处境：CLI 的自有
    文案全是中文，一打印就 UnicodeEncodeError。ui.py 启动时无条件把三个流钉成
    UTF-8，这条才不成立——本用例锁的就是那个"无条件"。
    """

    def test_child_output_stays_utf8_under_legacy_locale(self):
        events, result, code, stderr = self.run_cli(
            "text: 你好，世界\n", extra_env={"PYTHONIOENCODING": "iso8859-1"}
        )
        self.assertEqual(code, 0, stderr)
        self.assertEqual(result["result"], "你好，世界")
        self.assertNotIn("error", result)
        self.assertEqual(events[1]["text"], "你好，世界")


class ToolTurnTest(E2ECase):
    _TOOL_SCRIPT = (
        'tool_call: {"name": "bash", "arguments": {"command": "echo e2e-ok"}}\n'
        "---\n"
        "text: 完成\n"
    )

    def test_tool_flow_with_yolo(self):
        events, result, code, stderr = self.run_cli(self._TOOL_SCRIPT, extra_args=["--yolo"])
        self.assertEqual(code, 0, stderr)
        self.assertEqual(
            self.kinds(events),
            [
                "request.started",
                "request.ended",
                "tool.pending",
                "tool.running",
                "tool.completed",
                "request.started",
                "text.delta",
                "text.end",
                "request.ended",
            ],
        )
        completed = events[4]
        self.assertEqual(completed["name"], "bash")
        self.assertTrue(completed["ok"])
        self.assertIn("e2e-ok", completed["output"])
        self.assertEqual(completed["seconds"], 0)  # 归一化生效
        self.assertEqual(result["result"], "完成")

    def test_streamed_tool_call_parts_assemble(self):
        """参数拆三段流式下发，链路必须拼回完整 JSON 再执行。"""
        script = (
            'tool_call_part: {"index": 0, "id": "c1", "name": "bash"}\n'
            'tool_call_part: {"index": 0, "arguments": "{\\"command\\": \\"ec"}\n'
            'tool_call_part: {"index": 0, "arguments": "ho parts-ok\\"}"}\n'
            "---\n"
            "text: 完成\n"
        )
        events, result, code, stderr = self.run_cli(script, extra_args=["--yolo"])
        self.assertEqual(code, 0, stderr)
        completed = [e for e in events if e["kind"] == "tool.completed"]
        self.assertEqual(len(completed), 1)
        self.assertIn("parts-ok", completed[0]["output"])
        self.assertEqual(result["result"], "完成")

    def test_headless_denies_tool_without_yolo(self):
        """stream-json 无 --yolo：审批器是 headless 拒绝，工具不执行、拒因回灌。"""
        events, result, code, stderr = self.run_cli(self._TOOL_SCRIPT)
        self.assertEqual(code, 0, stderr)
        self.assertIn("tool.denied", self.kinds(events))
        self.assertNotIn("tool.running", self.kinds(events))
        self.assertEqual(result["result"], "完成")


class ErrorTurnTest(E2ECase):
    def test_fatal_error_is_structured_not_traceback(self):
        events, result, code, stderr = self.run_cli("error: 上游炸了\n")
        self.assertEqual(code, 1, stderr)
        self.assertIn("上游炸了", result["error"])
        #  事件流仍然规整闭合：started 有配对的 ended
        self.assertEqual(self.kinds(events), ["request.started", "request.ended"])

    def test_retryable_error_recovers_on_next_script_turn(self):
        """带 429 字样 → 分类为限流可重试 → 退避后弹下一轮脚本成功。"""
        events, result, code, stderr = self.run_cli(
            "error: rate limit 429\n---\ntext: 恢复了\n"
        )
        self.assertEqual(code, 0, stderr)
        self.assertEqual(result["result"], "恢复了")
        notices = [e for e in events if e["kind"] == "notice" and e["level"] == "warn"]
        self.assertTrue(any("重试" in n["text"] for n in notices), notices)


class ExhaustionTest(E2ECase):
    def test_extra_llm_call_fails_loudly(self):
        """脚本轮次不够（工具轮之后没有收尾轮）→ ScriptedExhausted 浮出为结构化错误。"""
        _, result, code, stderr = self.run_cli(
            'tool_call: {"name": "bash", "arguments": {"command": "true"}}\n',
            extra_args=["--yolo"],
        )
        self.assertEqual(code, 1, stderr)
        self.assertIn("ScriptedExhausted", result["error"])


if __name__ == "__main__":
    unittest.main()
