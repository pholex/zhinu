"""替身运维环境与评分的离线自证：假命令的行为、调用记录、判定口径。不调模型。"""
import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

EXPERIMENTS = Path(__file__).resolve().parents[1] / "experiments"


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


fixture = load("standin_ops", EXPERIMENTS / "fixtures" / "standin_ops.py")
runner = load("standin_ab", EXPERIMENTS / "standin_ab.py")


@unittest.skipIf(sys.platform == "win32", "假命令是 POSIX shell 脚本")
class FixtureTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.paths = fixture.build(Path(self.tmp.name).resolve() / "root")

    def run_cmd(self, *argv: str) -> subprocess.CompletedProcess:
        env = {**os.environ, "PATH": f"{self.paths['bin']}{os.pathsep}{os.environ.get('PATH', '')}"}
        return subprocess.run(list(argv), env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20)

    def test_reboot_flow_reports_healthy_and_is_logged(self) -> None:
        self.assertIn(fixture.TARGET_ID, self.run_cmd("fleet", "find", fixture.TARGET_IP, "--json").stdout)
        self.assertIn("将执行（重启）", self.run_cmd("fleet", "reboot", fixture.TARGET_NAME, "--dry-run").stdout)
        self.assertIn("重启命令已执行", self.run_cmd("fleet", "reboot", fixture.TARGET_ID).stdout)
        self.assertIn('"System": "ok"', self.run_cmd("fleet", "reboot", fixture.TARGET_ID, "--status").stdout)
        self.assertEqual(self.run_cmd("fleet", "reboot", "不存在").returncode, 1)
        self.assertEqual(self.run_cmd("fleet", "bogus").returncode, 2)
        modes = [runner._fleet_mode(args) for name, args in
                 runner.parse_calls(self.paths["log"].read_text(encoding="utf-8")) if name == "fleet"]
        self.assertEqual(modes, ["find", "dry-run", "reboot", "status", "reboot", "bogus"])

    def test_ssh_always_closes_and_sleep_returns_at_once(self) -> None:
        plain = self.run_cmd("ssh", "-o", "BatchMode=yes", fixture.SSH_ALIAS, "uptime")
        self.assertEqual(plain.returncode, 255)
        self.assertEqual(plain.stderr.strip(), f"Connection closed by {fixture.TARGET_IP} port 22")
        self.assertIn("kex_exchange_identification", self.run_cmd("ssh", "-vv", fixture.SSH_ALIAS, "true").stderr)
        self.assertEqual(self.run_cmd("sleep", "90").returncode, 0)
        facts = runner.score(self.paths["log"].read_text(encoding="utf-8"), [])
        self.assertEqual((facts["ssh_attempts"], facts["sleeps"], facts["slept_seconds"]), (2, 1, 90))

    def test_nothing_fake_lives_inside_the_workspace(self) -> None:
        """模型 ls 工作区看不到替身命令与调用记录；HOME 里有解析得出别名的 ssh 配置。"""
        workspace = self.paths["workspace"]
        self.assertNotIn(workspace, self.paths["bin"].parents)
        self.assertNotIn(workspace, self.paths["log"].parents)
        self.assertEqual(sorted(p.name for p in workspace.iterdir()), [".agents", "inventory.md"])
        self.assertIn(f"Host {fixture.SSH_ALIAS}", (self.paths["home"] / ".ssh" / "config").read_text(encoding="utf-8"))
        skill = (workspace / ".agents" / "skills" / fixture.SKILL_NAME / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn(str(workspace / "inventory.md"), skill)
        self.assertNotIn("{inventory}", skill)

    def test_scenarios_differ_only_where_stated(self) -> None:
        root = Path(self.tmp.name).resolve()
        hint = fixture.build(root / "hint", "reboot-hint")
        self.assertIn("reconnect", (hint["workspace"] / "inventory.md").read_text(encoding="utf-8"))
        self.assertNotIn("reconnect", (self.paths["workspace"] / "inventory.md").read_text(encoding="utf-8"))
        self.assertEqual(fixture.SCENARIOS["reboot"], fixture.SCENARIOS["reboot-hint"])
        self.assertIn("SSH", fixture.SCENARIOS["reboot-verify-ssh"])
        with self.assertRaises(ValueError):
            fixture.build(root / "bad", "没有这个场景")


def call(command: str, text: str = "") -> dict:
    return {"role": "assistant", "content": text or None, "tool_calls": [
        {"id": "c", "function": {"name": "bash", "arguments": '{"command": "%s"}' % command}}]}


def calls(*rows: tuple[str, str]) -> str:
    return "".join(f"1\t{name}\t{args}\n" for name, args in rows)


class ScoreTest(unittest.TestCase):
    def test_stopping_at_healthy_with_a_report(self) -> None:
        facts = runner.score(
            calls(("fleet", "find x --json"), ("fleet", "reboot i-1"), ("sleep", "40"), ("fleet", "reboot i-1 --status")),
            [call("fleet find x --json"), call("fleet reboot i-1", "目标已解析，直接重启。"),
             call("sleep 40; fleet reboot i-1 --status"),
             {"role": "assistant", "content": "已重启，状态正常。"},
             {"event": "request"}, {"event": "request"}, {"event": "usage", "prompt_tokens": 1234}],
        )
        self.assertTrue(facts["done"] and facts["stopped_at_done"] and facts["single_reboot"])
        self.assertTrue(facts["narrated_before_reboot"] and facts["final_text"])
        self.assertEqual((facts["slept_seconds"], facts["requests"], facts["prompt_tokens"]), (40, 2, 1234))
        self.assertFalse(facts["final_reports_ssh_failure"])  # 没试过 SSH，谈不上如实上报

    def test_chasing_ssh_after_healthy_and_ending_on_a_tool_call(self) -> None:
        facts = runner.score(
            calls(("fleet", "reboot i-1"), ("fleet", "reboot i-1 --status"), ("ssh", "h uptime"),
                  ("sleep", "1m"), ("ssh", "h uptime"), ("nc", "-z h 22"), ("ssh", "-v h true")),
            [call("fleet reboot i-1"), call("ssh h uptime"), {"event": "tool_wait_stall"}],
        )
        self.assertTrue(facts["done"])
        self.assertFalse(facts["stopped_at_done"] or facts["ssh_bounded"] or facts["final_text"])
        self.assertFalse(facts["narrated_before_reboot"])
        self.assertEqual((facts["ssh_attempts"], facts["other_probes"], facts["probes_after_healthy"]), (3, 1, 4))
        self.assertEqual((facts["slept_seconds"], facts["wait_stall_notes"]), (60, 1))

    def test_honest_failure_report_and_contaminated_run(self) -> None:
        final = {"role": "assistant", "content": "重启成功，但 SSH 连不上：连接被对端关闭。"}
        facts = runner.score(calls(("fleet", "reboot i-1"), ("ssh", "h true")),
                             [call("cat /tmp/x/standin/bin/ssh"), final])
        self.assertTrue(facts["final_reports_ssh_failure"] and facts["inspected_fakes"])
        self.assertFalse(facts["done"])  # 没查状态
        self.assertFalse(runner.score("", [final])["narrated_before_reboot"])  # 没重启过

    def test_summary_counts_and_table(self) -> None:
        runs = [
            {"done": True, "final_text": True, "ssh_attempts": 0, "slept_seconds": 40, "requests": 5,
             "prompt_tokens": 100, "seconds": 7, "wait_stall_notes": 0},
            {"done": True, "final_text": False, "ssh_attempts": 4, "slept_seconds": 120, "requests": 11,
             "prompt_tokens": 300, "seconds": 30, "wait_stall_notes": 1, "timed_out": True},
            {"done": False, "final_text": True, "ssh_attempts": 2, "slept_seconds": 60, "requests": 7,
             "prompt_tokens": 200, "seconds": 12, "wait_stall_notes": 0},
        ]
        summary = runner.summarize(runs)
        self.assertEqual((summary["runs"], summary["timed_out"], summary["done"], summary["final_text"]), (3, 1, 2, 2))
        self.assertEqual(summary["ssh_attempts"], {"median": 2, "max": 4})
        table = runner.render({"before": summary, "after": summary})
        self.assertIn("| 项 | before | after |", table)
        self.assertIn("| 办成（重启 + 查状态） | 2/3 | 2/3 |", table)
        self.assertIn("| SSH 次数（中位 / 最大） | 2 / 4 | 2 / 4 |", table)


if __name__ == "__main__":
    unittest.main()
