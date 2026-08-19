"""snapshot 套件：真子进程 + 录制回放 fixture + golden 对比
（设计与纪律见 docs/test_snapshot.md）。

与 test_e2e_scripted 的分工：那边是"手写脚本 + 手写期望"锁单点行为，
期望短、能表达"为什么该是这个序列"；这边是"fixture + golden"锁**整个
可见表面**——归一化后的完整 stream-json 事件流、完整会话 JSONL、
pinned header（system prompt / tool schemas）。改动只要碰到任何可见面，
golden 的 diff 就会把它摆出来。

三模式（XIAOYU_SNAPSHOT 环境变量：record/replay/refresh）：
- 缺省 = replay：keyless，用提交的 model.txt 驱动，逐面与 golden 比对；
- refresh：keyless，重写 golden 与 pin sidecar（改了 prompt/事件面之后跑
  一次，diff 审阅后提交；refresh 完跑一遍默认模式验证）；
- record：真 API 重录 model.txt（花真钱，只覆盖 recorded=True 的场景；
  用真机配置的默认模型，XIAOYU_SNAPSHOT_MODEL 可指定），录完自动做一遍
  refresh——录出来的 fixture 当场证明能回放。

assertConsumed 双向都响亮：多调模型 → ScriptedExhausted 浮出为
结构化错误；少调模型 → XIAOYU_SCRIPTED_STRICT 在子进程退出时向 stderr 打
ScriptedUnconsumed，本套件断言 stderr 干净。

pin 纪律：恰好一个场景钉住完整 system prompt 与
tool schemas（sidecar 文件），其余场景只做一致性核对——改一次 prompt 只
churn 一处。机器相关段已被归一化成 token（见 snapshot_support），sidecar
锁的是 xiaoyu 自己控制的模板内容；Windows 上跳过 pin 对比（posix 语气的
shell 注记/工具面不同），stdout 与会话 golden 照常全平台比。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from xiaoyu.scripted import CAPTURE_ENV, STRICT_ENV, SCRIPTED_ENV, load_scripts
from xiaoyu.snapshot import RECORD_ENV

from .snapshot_support import (
    normalize_event,
    normalize_session_lines,
    normalize_system_prompt,
    normalize_tool_schemas,
)
from .test_e2e_scripted import _TIMEOUT, E2ECase

MODE = os.environ.get("XIAOYU_SNAPSHOT", "").strip() or "replay"
if MODE not in ("replay", "record", "refresh"):
    raise RuntimeError(f"未知的 XIAOYU_SNAPSHOT 模式：{MODE}（合法：replay/record/refresh）")

SNAPSHOTS = Path(__file__).resolve().parent / "snapshots"
#  回放用固定模型名：scripted 桩接住任何名字，事件与 golden 里因此是常量
REPLAY_MODEL = "snapshot-model"


@dataclass(frozen=True)
class Scenario:
    name: str
    prompt: str
    args: tuple[str, ...] = ()
    pins_header: bool = False  # 恰好一个场景为 True（钉住完整请求头）
    recorded: bool = True  # False = 纯手写 fixture（错误时序真 API 录不出来）
    expect_exit: int = 0


SCENARIOS = (
    Scenario("text-turn", "打个招呼", pins_header=True),
    Scenario("tool-turn", "用 bash 运行 echo snapshot-ok，然后汇报结果", args=("--yolo",)),
    Scenario("error-retry", "试试限流恢复", recorded=False),
    Scenario("error-fatal", "撞一个致命错误", recorded=False, expect_exit=1),
)

_PIN = next(sc for sc in SCENARIOS if sc.pins_header)


def _scenario_dir(name: str) -> Path:
    return SNAPSHOTS / name


class SnapshotCase(E2ECase):
    """场景驱动器：一次回放跑出四个捕获面（stdout / 会话 / 请求头 / stderr）。"""

    maxDiff = None

    #  —— 驱动 ——

    def replay_run(self, sc: Scenario) -> dict[str, Any]:
        capture_dir = Path(self.tmp) / "capture"
        env = self.env_for(_scenario_dir(sc.name) / "model.txt")
        env[CAPTURE_ENV] = str(capture_dir)
        env[STRICT_ENV] = "1"
        proc = subprocess.run(
            [
                sys.executable, "-m", "xiaoyu", sc.prompt,
                "--output-format", "stream-json",
                "--workspace", str(self.workspace),
                "--model", REPLAY_MODEL,
                *sc.args,
            ],
            stdin=subprocess.DEVNULL,  # 见 test_e2e_scripted 里的同款说明
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=_TIMEOUT, env=env, cwd=self.tmp,
        )
        self.assertEqual(proc.returncode, sc.expect_exit, proc.stderr)
        self.assertNotIn(
            "ScriptedUnconsumed", proc.stderr,
            f"场景比 fixture 少驱动了几次模型调用：\n{proc.stderr}",
        )
        stdout_lines = [
            json.dumps(normalize_event(json.loads(line), self.tmp), ensure_ascii=False)
            for line in proc.stdout.splitlines() if line.strip()
        ]
        session_files = sorted((Path(self.tmp) / "config").rglob("*.jsonl"))
        self.assertEqual(len(session_files), 1, f"应恰好落一个会话文件：{session_files}")
        records = [
            json.loads(line)
            for line in session_files[0].read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        captures = [
            json.loads(path.read_text(encoding="utf-8"))
            for path in sorted(capture_dir.glob("request-*.json"),
                               key=lambda p: int(p.stem.split("-")[1]))
        ]
        self.assertTrue(captures, "回放至少应捕获一次请求头")
        return {
            "stdout": stdout_lines,
            "session": normalize_session_lines(records, self.tmp),
            "captures": captures,
        }

    #  —— 三模式的场景主流程 ——

    def check_scenario(self, sc: Scenario) -> None:
        directory = _scenario_dir(sc.name)
        if MODE == "record":
            if not sc.recorded:
                self.skipTest("纯手写 fixture（错误时序），record 不覆盖")
            self._record(sc, directory)
        run = self.replay_run(sc)
        if MODE in ("refresh", "record"):
            self._write_goldens(sc, directory, run)
        self._compare_goldens(sc, directory, run)

    def _write_goldens(self, sc: Scenario, directory: Path, run: dict[str, Any]) -> None:
        (directory / "stdout.expected.jsonl").write_text(
            "\n".join(run["stdout"]) + "\n", encoding="utf-8"
        )
        (directory / "session.expected.jsonl").write_text(
            "\n".join(run["session"]) + "\n", encoding="utf-8"
        )
        if sc.pins_header:
            first = run["captures"][0]
            (directory / "system-prompt.expected.md").write_text(
                normalize_system_prompt(first["system"], self.tmp), encoding="utf-8"
            )
            tools = next((c["tools"] for c in run["captures"] if c["tools"]), None)
            self.assertIsNotNone(tools, "pin 场景至少要有一次带工具的调用")
            (directory / "tool-schemas.expected.json").write_text(
                normalize_tool_schemas(tools, self.tmp) + "\n", encoding="utf-8"
            )

    def _compare_goldens(self, sc: Scenario, directory: Path, run: dict[str, Any]) -> None:
        expected_stdout = (directory / "stdout.expected.jsonl").read_text(
            encoding="utf-8"
        ).splitlines()
        self.assertEqual(run["stdout"], expected_stdout, "stream-json 事件流偏离 golden")
        expected_session = (directory / "session.expected.jsonl").read_text(
            encoding="utf-8"
        ).splitlines()
        self.assertEqual(run["session"], expected_session, "会话 JSONL 偏离 golden")
        #  pin 对比：pin 场景在所有模式比（refresh 刚重写，比对是自证）；
        #  非 pin 场景只在 replay 比（refresh 模式下 sidecar 可能尚未按用例
        #  顺序重写，全量 refresh 后跑一遍默认模式即补上核对）
        if os.name == "nt" or (not sc.pins_header and MODE != "replay"):
            return
        pin_dir = _scenario_dir(_PIN.name)
        expected_system = (pin_dir / "system-prompt.expected.md").read_text(encoding="utf-8")
        expected_tools = (pin_dir / "tool-schemas.expected.json").read_text(encoding="utf-8")
        for index, capture in enumerate(run["captures"], start=1):
            self.assertEqual(
                normalize_system_prompt(capture["system"], self.tmp),
                expected_system,
                f"第 {index} 次调用的 system prompt 偏离 pin（改了 prompt 请跑 refresh）",
            )
            if capture["tools"]:
                self.assertEqual(
                    normalize_tool_schemas(capture["tools"], self.tmp) + "\n",
                    expected_tools,
                    f"第 {index} 次调用的 tool schemas 偏离 pin（改了工具面请跑 refresh）",
                )

    def _record(self, sc: Scenario, directory: Path) -> None:
        """真 API 重录（串行、花真钱）：真机配置 + 真密钥，只圈住工作区。"""
        env = {**os.environ, RECORD_ENV: str(directory)}
        model = os.environ.get("XIAOYU_SNAPSHOT_MODEL", "").strip()
        proc = subprocess.run(
            [
                sys.executable, "-m", "xiaoyu", sc.prompt,
                "--output-format", "stream-json",
                "--workspace", str(self.workspace),
                *(["--model", model] if model else []),
                *sc.args,
            ],
            stdin=subprocess.DEVNULL,  # 见 test_e2e_scripted 里的同款说明
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=_TIMEOUT, env=env, cwd=self.tmp,
        )
        self.assertEqual(proc.returncode, 0, f"录制失败：\n{proc.stderr}\n{proc.stdout}")
        self.assertTrue((directory / "model.txt").is_file(), "录制没有产出 model.txt")
        #  录制期的请求头 dump 含真模型名与机器路径，属易变副产物：
        #  pin sidecar 由紧随其后的回放捕获重写，这些直接清掉、不入库
        for leftover in directory.glob("request-*.json"):
            leftover.unlink()


class TextTurnSnapshot(SnapshotCase):
    def test_scenario(self) -> None:
        self.check_scenario(SCENARIOS[0])


class ToolTurnSnapshot(SnapshotCase):
    def test_scenario(self) -> None:
        self.check_scenario(SCENARIOS[1])


class ErrorRetrySnapshot(SnapshotCase):
    def test_scenario(self) -> None:
        self.check_scenario(SCENARIOS[2])


class ErrorFatalSnapshot(SnapshotCase):
    def test_scenario(self) -> None:
        self.check_scenario(SCENARIOS[3])


class UnconsumedGuardTest(SnapshotCase):
    """strict 未耗尽告警本身要证明会红：故意多给一轮，stderr 必须出现标记。"""

    def test_leftover_turns_complain_on_stderr(self) -> None:
        script = Path(self.tmp) / "over.txt"
        script.write_text("text: 只用这轮\n---\ntext: 多出来的一轮\n", encoding="utf-8")
        env = self.env_for(script)
        env[STRICT_ENV] = "1"
        proc = subprocess.run(
            [sys.executable, "-m", "xiaoyu", "说句话", "--output-format", "stream-json",
             "--workspace", str(self.workspace)],
            stdin=subprocess.DEVNULL,  # 见 test_e2e_scripted 里的同款说明
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=_TIMEOUT, env=env, cwd=self.tmp,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("ScriptedUnconsumed", proc.stderr)
        self.assertIn("剩 1 轮", proc.stderr)


class FixtureGuardTest(unittest.TestCase):
    """fixture 守卫：孤儿目录、缺文件、多 pin、易变副产物入库都拒绝。"""

    def test_exactly_one_pin(self) -> None:
        self.assertEqual(sum(1 for sc in SCENARIOS if sc.pins_header), 1)

    def test_scenario_dirs_match_table(self) -> None:
        names = {sc.name for sc in SCENARIOS}
        on_disk = {p.name for p in SNAPSHOTS.iterdir() if p.is_dir()}
        self.assertEqual(on_disk, names, "snapshots/ 下的目录必须与场景表一一对应")

    def test_required_files_present_and_no_strays(self) -> None:
        for sc in SCENARIOS:
            directory = _scenario_dir(sc.name)
            expected = {"model.txt", "stdout.expected.jsonl", "session.expected.jsonl"}
            if sc.pins_header:
                expected |= {"system-prompt.expected.md", "tool-schemas.expected.json"}
            with self.subTest(scenario=sc.name):
                self.assertEqual(
                    {p.name for p in directory.iterdir() if p.is_file()},
                    expected,
                    "缺 golden 或混入了不该入库的文件（如录制留下的 request-*.json）",
                )

    def test_fixtures_parse_as_scripted_dsl(self) -> None:
        for sc in SCENARIOS:
            with self.subTest(scenario=sc.name):
                turns = load_scripts(str(_scenario_dir(sc.name) / "model.txt"))
                self.assertTrue(turns, "fixture 至少要有一轮")

    def test_env_names_stay_wired(self) -> None:
        #  常量拼写走真源头 import：套件与产品侧的开关名永远同步
        self.assertEqual(SCRIPTED_ENV, "XIAOYU_SCRIPTED_SCRIPTS")
        self.assertEqual(RECORD_ENV, "XIAOYU_SNAPSHOT_RECORD")


if __name__ == "__main__":
    unittest.main()
