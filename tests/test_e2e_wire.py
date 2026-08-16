"""wire 协议黑盒 e2e（真子进程 + scripted 桩，不打网络）。

驱动方式：Popen 双向管道，后台线程逐行收 JSON（Windows 管道没有 select，
线程读是唯一跨平台姿势）。
环境隔离与归一化复用 test_e2e_scripted.E2ECase。
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import unittest
from pathlib import Path
from typing import Any

from .test_e2e_scripted import E2ECase

_READ_TIMEOUT = 60.0


class WireProcess:
    def __init__(self, proc: subprocess.Popen) -> None:
        self.proc = proc
        self._lines: queue.Queue[str | None] = queue.Queue()
        self._reader = threading.Thread(target=self._pump, daemon=True)
        self._reader.start()

    def _pump(self) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            self._lines.put(line)
        self._lines.put(None)  # EOF 哨兵

    def send(self, message: dict[str, Any]) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
        self.proc.stdin.flush()

    def read_json(self, timeout: float = _READ_TIMEOUT) -> dict[str, Any]:
        line = self._lines.get(timeout=timeout)
        if line is None:
            raise AssertionError("wire 进程已关闭 stdout（提前退出？）")
        return json.loads(line)

    def read_until(self, predicate, timeout: float = _READ_TIMEOUT) -> tuple[dict, list[dict]]:
        """读到第一条满足条件的消息，返回 (命中消息, 途中略过的消息)。"""
        skipped: list[dict[str, Any]] = []
        while True:
            message = self.read_json(timeout)
            if predicate(message):
                return message, skipped
            skipped.append(message)

    def close(self) -> None:
        if self.proc.stdin is not None:
            try:
                self.proc.stdin.close()
            except OSError:
                pass
        try:
            self.proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=5)
        self._reader.join(timeout=5)
        for stream in (self.proc.stdout, self.proc.stderr):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass


class WireCase(E2ECase):
    """公共驱动：写脚本 → 起 --wire 子进程。"""

    def start_wire(self, script: str, extra_args: list[str] | None = None) -> WireProcess:
        env = self.scripted_env(script)
        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "xiaoyu",
                "--wire",
                "--workspace",
                str(self.workspace),
                *(extra_args or []),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            #  wire 是双向管道：不指定编码时，Windows 上写中文请求会
            #  UnicodeEncodeError（cp1252），读中文事件会 UnicodeDecodeError
            encoding="utf-8",
            errors="replace",
            env=env,
            cwd=self.tmp,
        )
        wire = WireProcess(proc)
        self.addCleanup(wire.close)
        return wire

    @staticmethod
    def is_response(req_id: str):
        return lambda m: m.get("id") == req_id and "method" not in m

    @staticmethod
    def is_approval(m: dict) -> bool:
        return m.get("method") == "request" and m.get("params", {}).get("type") == "approval"

    @staticmethod
    def event_kinds(messages: list[dict]) -> list[str]:
        return [
            m["params"]["kind"]
            for m in messages
            if m.get("method") == "event" and "kind" in m.get("params", {})
        ]


_TOOL_SCRIPT = (
    'tool_call: {"name": "bash", "arguments": {"command": "echo wire-ok"}}\n'
    "---\n"
    "text: 完成\n"
)


class HandshakeTest(WireCase):
    def test_initialize(self):
        wire = self.start_wire("text: ok\n")
        wire.send({"jsonrpc": "2.0", "id": "1", "method": "initialize"})
        response, _ = wire.read_until(self.is_response("1"))
        result = response["result"]
        self.assertEqual(result["protocol_version"], "1.0")
        self.assertEqual(result["server"]["name"], "xiaoyu")
        self.assertTrue(result["workspace"].endswith("ws"))

    def test_unknown_method_and_bad_json(self):
        wire = self.start_wire("text: ok\n")
        wire.send({"jsonrpc": "2.0", "id": "1", "method": "teleport"})
        response, _ = wire.read_until(self.is_response("1"))
        self.assertEqual(response["error"]["code"], -32601)
        assert wire.proc.stdin is not None
        wire.proc.stdin.write("这不是 json\n")
        wire.proc.stdin.flush()
        response = wire.read_json()
        self.assertEqual(response["error"]["code"], -32700)


class PromptTest(WireCase):
    def test_text_turn_events_then_response(self):
        wire = self.start_wire("text: 你好世界\n")
        wire.send({"jsonrpc": "2.0", "id": "p1", "method": "prompt", "params": {"text": "打个招呼"}})
        response, skipped = wire.read_until(self.is_response("p1"))
        kinds = self.event_kinds(skipped)
        self.assertEqual(kinds[0], "request.started")
        self.assertIn("text.delta", kinds)
        self.assertEqual(kinds[-1], "request.ended")
        self.assertEqual(response["result"]["status"], "finished")
        self.assertEqual(response["result"]["result"], "你好世界")

    def test_prompt_while_busy_is_invalid_state(self):
        wire = self.start_wire(_TOOL_SCRIPT + "---\ntext: 没人看\n")
        wire.send({"jsonrpc": "2.0", "id": "p1", "method": "prompt", "params": {"text": "干活"}})
        #  审批请求挂起 = 本轮确定还在跑
        approval, _ = wire.read_until(self.is_approval)
        wire.send({"jsonrpc": "2.0", "id": "p2", "method": "prompt", "params": {"text": "再来"}})
        response, _ = wire.read_until(self.is_response("p2"))
        self.assertEqual(response["error"]["code"], -32000)
        #  收尾：放行让本轮跑完
        wire.send({"jsonrpc": "2.0", "id": approval["id"], "result": {"verdict": "allow"}})
        wire.read_until(self.is_response("p1"))


class ApprovalTest(WireCase):
    def test_allow_executes_tool(self):
        wire = self.start_wire(_TOOL_SCRIPT)
        wire.send({"jsonrpc": "2.0", "id": "p1", "method": "prompt", "params": {"text": "干活"}})
        approval, _ = wire.read_until(self.is_approval)
        payload = approval["params"]["payload"]
        self.assertEqual(payload["name"], "bash")
        self.assertEqual(payload["args"]["command"], "echo wire-ok")
        wire.send({"jsonrpc": "2.0", "id": approval["id"], "result": {"verdict": "allow"}})
        response, skipped = wire.read_until(self.is_response("p1"))
        kinds = self.event_kinds(skipped)
        self.assertIn("tool.running", kinds)
        self.assertIn("tool.completed", kinds)
        self.assertEqual(response["result"]["status"], "finished")
        self.assertEqual(response["result"]["result"], "完成")

    def test_deny_with_reason_feeds_back(self):
        wire = self.start_wire(_TOOL_SCRIPT)
        wire.send({"jsonrpc": "2.0", "id": "p1", "method": "prompt", "params": {"text": "干活"}})
        approval, _ = wire.read_until(self.is_approval)
        wire.send(
            {"jsonrpc": "2.0", "id": approval["id"],
             "result": {"verdict": "deny", "reason": "只许只读"}}
        )
        response, skipped = wire.read_until(self.is_response("p1"))
        kinds = self.event_kinds(skipped)
        self.assertIn("tool.denied", kinds)
        self.assertNotIn("tool.running", kinds)
        self.assertEqual(response["result"]["status"], "finished")

    def test_allow_with_updated_args_rewrites_command(self):
        wire = self.start_wire(_TOOL_SCRIPT)
        wire.send({"jsonrpc": "2.0", "id": "p1", "method": "prompt", "params": {"text": "干活"}})
        approval, _ = wire.read_until(self.is_approval)
        wire.send(
            {"jsonrpc": "2.0", "id": approval["id"],
             "result": {"verdict": "allow", "updated_args": {"command": "echo rewritten"}}}
        )
        response, skipped = wire.read_until(self.is_response("p1"))
        completed = [
            m["params"] for m in skipped
            if m.get("method") == "event" and m["params"].get("kind") == "tool.completed"
        ]
        self.assertEqual(len(completed), 1)
        self.assertIn("rewritten", completed[0]["output"])
        self.assertEqual(response["result"]["status"], "finished")


class SteerCancelTest(WireCase):
    def test_steer_lands_in_running_turn(self):
        """用审批挂起当同步点：插话必然在工具批次收尾后被消费。"""
        wire = self.start_wire(_TOOL_SCRIPT)
        wire.send({"jsonrpc": "2.0", "id": "p1", "method": "prompt", "params": {"text": "干活"}})
        approval, _ = wire.read_until(self.is_approval)
        wire.send({"jsonrpc": "2.0", "id": "s1", "method": "steer", "params": {"text": "顺便看下 README"}})
        wire.read_until(self.is_response("s1"))
        wire.send({"jsonrpc": "2.0", "id": approval["id"], "result": {"verdict": "allow"}})
        response, skipped = wire.read_until(self.is_response("p1"))
        kinds = self.event_kinds(skipped)
        self.assertIn("steer.accepted", kinds)
        self.assertEqual(response["result"]["status"], "finished")

    def test_cancel_interrupts_turn_and_denies_approval(self):
        wire = self.start_wire(_TOOL_SCRIPT + "---\ntext: 不会被看到\n")
        wire.send({"jsonrpc": "2.0", "id": "p1", "method": "prompt", "params": {"text": "干活"}})
        wire.read_until(self.is_approval)
        wire.send({"jsonrpc": "2.0", "id": "c1", "method": "cancel"})
        #  tool.denied 由工作线程发（_cancel_turn 先 event.set() 放开它），
        #  cancel 响应由主线程发，两者顺序不定——Windows 调度下实测是反过来的。
        #  所以两段读到的事件都要收，别假定 denied 一定在 c1 响应之后。
        _, before = wire.read_until(self.is_response("c1"))
        response, after = wire.read_until(self.is_response("p1"))
        self.assertEqual(response["result"]["status"], "interrupted")
        self.assertIn("tool.denied", self.event_kinds(before + after))


if __name__ == "__main__":
    unittest.main()
