"""同题对照跑批的离线自证：题目清单自洽、起点不带历史、判据按事实给。不调模型。"""
import importlib.util
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
EXPERIMENTS = REPO / "experiments"


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


tasks = load("ab_tasks", EXPERIMENTS / "fixtures" / "ab_tasks.py")
runner = load("task_ab", EXPERIMENTS / "task_ab.py")


def git(*argv: str, cwd: Path) -> str:
    return subprocess.run(["git", *argv], cwd=cwd, check=True, capture_output=True, text=True,
                          encoding="utf-8", errors="replace").stdout


class TaskTableTest(unittest.TestCase):
    def test_names_unique_and_fields_consistent(self) -> None:
        names = [t.name for t in tasks.TASKS]
        self.assertEqual(len(names), len(set(names)))
        for task in tasks.TASKS:
            with self.subTest(task.name):
                self.assertIn(task.category, ("locate", "edit", "debug", "feature", "doc", "ops", "skill"))
                if task.standin:  # 运维题：起点与判据都由替身场景给
                    self.assertIn(task.standin, runner.standin_ops.SCENARIOS)
                    self.assertFalse(task.ref or task.cwd or task.tests or task.expect)
                    continue
                self.assertTrue(sum(map(bool, (task.ref, task.cwd, task.seed and not task.ref))) == 1,
                                "起点三选一：导出提交、真实目录、只用种子")
                self.assertTrue(task.tests or task.expect or task.checker, "每题至少一条判据")
                for name in task.skills:
                    self.assertTrue((tasks.SKILLS / name / "SKILL.md").is_file(), name)
                if task.checker:
                    self.assertTrue((tasks.CHECKS / task.checker).is_file(), task.checker)
                for name in task.hidden:
                    self.assertTrue((tasks.CHECKS / name).is_file(), name)
                    self.assertIn("tests." + name.removesuffix(".py"), task.tests)
                if "{failure}" in task.prompt:
                    self.assertTrue(task.restore and task.tests and task.frozen)
                for name, _ in task.seed:
                    self.assertTrue((tasks.SEEDS / name).exists(), name)
                if task.category == "doc":
                    self.assertTrue(task.artifact and task.scope and task.expect)
                if task.category == "locate":
                    self.assertTrue(task.readonly or task.cwd)

    def test_hidden_checks_are_not_collected_by_the_repo_suite(self) -> None:
        #  验收用例针对的是题目起点那份代码，不该被本仓的全量单测捡到
        self.assertNotEqual(tasks.CHECKS.parent, REPO / "tests")
        self.assertFalse((tasks.CHECKS / "__init__.py").exists())


@unittest.skipUnless((REPO / ".git").exists(), "要在本仓的 git 检出里跑")
class WorkspaceTest(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        head = git("rev-parse", "HEAD", cwd=REPO).strip()
        self.task = tasks.Task(name="t", category="edit", prompt="p", ref=head,
                               restore=[(head, "AGENTS.md")], frozen=["tests"], readonly=True)
        self.ready = runner.prepare(self.task, self.root, sys.executable)
        self.workspace = self.ready["workspace"]

    def test_start_has_one_commit_and_no_history(self) -> None:
        self.assertEqual(git("rev-list", "--count", "HEAD", cwd=self.workspace).strip(), "1")
        self.assertEqual(git("status", "--porcelain", cwd=self.workspace), "")
        self.assertTrue((self.workspace / "xiaoyu" / "__init__.py").is_file())

    def test_untouched_workspace_passes_readonly_and_frozen(self) -> None:
        facts = runner.judge(self.task, self.ready, "", self.root, sys.executable)
        self.assertEqual((facts["untouched"], facts["frozen_kept"], facts["done"]), (True, True, True))

    def test_new_file_under_frozen_path_is_caught_even_if_committed(self) -> None:
        (self.workspace / "tests" / "test_extra.py").write_text("x = 1\n", encoding="utf-8")
        git("add", "-A", cwd=self.workspace)
        git(*runner.GIT_ID, "commit", "-q", "-m", "改了测试", cwd=self.workspace)
        facts = runner.judge(self.task, self.ready, "", self.root, sys.executable)
        self.assertEqual((facts["untouched"], facts["frozen_kept"], facts["done"]), (False, False, False))
        self.assertIn("test_extra.py", (self.root / "diff.patch").read_text(encoding="utf-8"))

    def test_answer_facts_are_counted(self) -> None:
        task = tasks.Task(name="q", category="locate", prompt="p", ref=self.task.ref,
                          expect=[r"10\s*秒", r"第\s*3\s*次"])
        facts = runner.judge(task, self.ready, "至少 10 秒，第 2 次提示", self.root, sys.executable)
        self.assertEqual((facts["answer_hits"], facts["answer_ok"], facts["done"]), ("1/2", False, False))
        facts = runner.judge(task, self.ready, "至少 **10 秒**，第 **3** 次要求收敛", self.root, sys.executable)
        self.assertEqual((facts["answer_hits"], facts["answer_ok"]), ("2/2", True))


    def test_artifact_is_judged_by_its_content_and_scope(self) -> None:
        task = tasks.Task(name="d", category="doc", prompt="p", ref=self.task.ref,
                          seed=[("results.json", "data/results.json")],
                          artifact="report.md", scope=["report.md"], expect=[r"\b47\b"])
        root = self.root / "doc"
        root.mkdir()
        ready = runner.prepare(task, root, sys.executable)
        workspace = ready["workspace"]
        self.assertTrue((workspace / "data" / "results.json").is_file())
        self.assertEqual(git("status", "--porcelain", cwd=workspace), "")  # 种子文件算起点的一部分
        #  收尾正文里说了数字不算，文件没交就是没办成
        facts = runner.judge(task, ready, "中位数是 47", root, sys.executable)
        self.assertEqual((facts["artifact_exists"], facts["answer_ok"], facts["done"]), (False, False, False))
        (workspace / "report.md").write_text("耗时中位 47 秒\n", encoding="utf-8")
        facts = runner.judge(task, ready, "", root, sys.executable)
        self.assertEqual((facts["artifact_exists"], facts["answer_ok"], facts["in_scope"], facts["done"]),
                         (True, True, True, True))
        (workspace / "AGENTS.md").write_text("顺手改了别的\n", encoding="utf-8")
        facts = runner.judge(task, ready, "", root, sys.executable)
        self.assertEqual((facts["in_scope"], facts["done"]), (False, False))


class SkillTaskTest(unittest.TestCase):
    """技能题的验收脚本要能分出好坏：起点不过，照技能做完才过。"""

    def prepare(self, name: str):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.task = next(t for t in tasks.TASKS if t.name == name)
        self.ready = runner.prepare(self.task, self.root, sys.executable, ("extra/skills",))
        return self.ready["workspace"]

    def facts(self) -> dict:
        return runner.judge(self.task, self.ready, "", self.root, sys.executable)

    def test_changelog_entry(self) -> None:
        workspace = self.prepare("skill-changelog-entry")
        self.assertTrue((workspace / "extra" / "skills" / "changelog-entry" / "SKILL.md").is_file())
        self.assertTrue((workspace / ".agents" / "skills" / "changelog-entry" / "SKILL.md").is_file())
        self.assertFalse(self.facts()["rules_kept"])
        entry = ("## 0.3.0 — 2026-11-20\n\n- 新增：导出支持选择列（#38）\n- 新增：列表页可以保存筛选条件（#52）\n"
                 "- 修复：重复提交同一张表单时生成两条记录（#47）\n- 修复：时区为负偏移时日期差一天（#55）\n\n")
        log = workspace / "CHANGELOG.md"
        log.write_text(log.read_text(encoding="utf-8").replace("## 0.2.1", entry + "## 0.2.1", 1), encoding="utf-8")
        facts = self.facts()
        self.assertEqual((facts["rules_kept"], facts["rules_hits"]), (False, "8/9"))  # 片段还没删
        for fragment in (workspace / "CHANGES" / "pending").glob("*.md"):
            fragment.unlink()
        facts = self.facts()
        self.assertEqual((facts["rules_kept"], facts["rules_hits"], facts["done"]), (True, "9/9", True))

    def test_ship_fix(self) -> None:
        workspace = self.prepare("skill-ship-fix")
        self.assertEqual(git("branch", "--show-current", cwd=workspace).strip(), "main")
        self.assertFalse(self.facts()["rules_kept"])
        cart = workspace / "src" / "cart.py"
        cart.write_text(cart.read_text(encoding="utf-8").replace("amount > coupon", "amount >= coupon"), encoding="utf-8")
        with (workspace / "FIXES.md").open("a", encoding="utf-8") as handle:
            handle.write("- cart.py：小计正好等于门槛时满减券没生效\n")
        git("checkout", "-q", "-b", "fix/cart-coupon-threshold", cwd=workspace)
        git("add", "-A", cwd=workspace)
        git(*runner.GIT_ID, "commit", "-q", "-m", "fix(cart): 小计等于门槛时也用券", cwd=workspace)
        facts = self.facts()
        self.assertEqual((facts["rules_kept"], facts["rules_hits"]), (True, "11/11"), (self.root / "check.txt").read_text())


class OpsTest(unittest.TestCase):
    def test_facts_come_from_the_call_record(self) -> None:
        calls = "\n".join(f"1\tfleet\t{args}" for args in (
            "find 203.0.113.7 --json", "reboot jump-west", "reboot jump-west --status",
            "ssh jump-west uptime", "ssh jump-west uptime", "ssh jump-west uptime"))
        facts = runner.ops_facts(calls, "已重启，状态健康，但 SSH 登录三次都被对端关闭。")
        self.assertEqual((facts["done"], facts["ops_single_reboot"], facts["ops_stopped_at_done"],
                          facts["ops_ssh_attempts"], facts["ops_reports_ssh_failure"]),
                         (True, True, False, 3, True))
        #  查过状态就收尾：登录那一项没有意义，不计入分母
        facts = runner.ops_facts("1\tfleet\treboot jump-west\n1\tfleet\treboot jump-west --status\n", "好了")
        self.assertEqual((facts["done"], facts["ops_stopped_at_done"], facts["ops_reports_ssh_failure"]),
                         (True, True, None))
        self.assertFalse(runner.ops_facts("1\tfleet\treboot jump-west\n", "好了")["done"])
        self.assertEqual(runner._count([facts, {"ops_reports_ssh_failure": True}], "ops_reports_ssh_failure"), "1/1")

    def test_narration_is_read_from_the_timeline(self) -> None:
        Step, Timeline = runner.session_compare.Step, runner.session_compare.Timeline
        status = Step(1.0, "tool", 1, "bash", "fleet reboot jump-west --status")
        reboot = Step(2.0, "tool", 2, "bash", "fleet reboot jump-west")
        said = Step(1.5, "say", 2, detail="先重启")
        self.assertIs(runner.narrated_before_reboot(Timeline("x", "f", steps=[status, reboot])), False)
        self.assertIs(runner.narrated_before_reboot(Timeline("x", "f", steps=[status, said, reboot])), True)
        self.assertIsNone(runner.narrated_before_reboot(Timeline("x", "f", steps=[status])))

    def test_environment_uses_only_the_fake_cli(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        task = next(t for t in tasks.TASKS if t.name == "ops-reboot-verify-ssh")
        ready = runner.prepare(task, Path(temp.name).resolve(), sys.executable, ("extra/skills",))
        paths = ready["standin"]
        self.addCleanup(runner.shutil.rmtree, paths["bin"].parent, True)
        self.assertEqual([p.name for p in paths["bin"].iterdir()], ["fleet"])
        self.assertNotIn(str(Path(temp.name).resolve()), str(paths["bin"]))  # 不在工作区一侧
        skill = (ready["workspace"] / "extra" / "skills" / "fleet-ops" / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("fleet ssh", skill)
        self.assertNotIn("~/.ssh/config", skill)
        self.assertNotIn("SSH 别名", (ready["workspace"] / "inventory.md").read_text(encoding="utf-8"))
        self.assertIn("SSH", ready["prompt"])


class RenderTest(unittest.TestCase):
    def test_table_lists_arms_side_by_side_and_skips_empty_rows(self) -> None:
        run = {"done": True, "tests_pass": True, "timed_out": False, "tool_steps": 4, "requests": 5,
               "said_requests": 1, "seconds": 30, "ends_with_text": True}
        other = {**run, "done": False, "tests_pass": False, "tool_steps": 10}
        table = runner.render({"t": {"yu": [run, run, other], "xue": [run]}},
                              {"t": tasks.Task(name="t", category="debug", prompt="p", ref="x")})
        self.assertIn("| 项 | yu | xue |", table)
        self.assertIn("| 办成 | 2/3 | 1/1 |", table)
        self.assertIn("| 工具步数 中位（最小–最大） | 4（4–10） | 4（4–4） |", table)
        self.assertIn("| 带正文的请求占比（中位） | 20% | 20% |", table)
        self.assertNotIn("答案里的事实齐全", table)

    def test_final_text_of_the_json_side_is_unwrapped(self) -> None:
        self.assertEqual(runner.final_text_of("xue", '{"result": "第 2 次"}'), "第 2 次")
        self.assertEqual(runner.final_text_of("xue", "不是 JSON"), "不是 JSON")
        self.assertEqual(runner.final_text_of("yu", '{"result": "x"}'), '{"result": "x"}')


if __name__ == "__main__":
    unittest.main()
