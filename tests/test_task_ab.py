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
                self.assertIn(task.category, ("locate", "edit", "debug", "feature", "doc"))
                self.assertTrue(bool(task.ref) != bool(task.cwd), "起点二选一：导出提交，或真实目录")
                self.assertTrue(task.tests or task.expect, "每题至少一条判据")
                for name in task.hidden:
                    self.assertTrue((tasks.CHECKS / name).is_file(), name)
                    self.assertIn("tests." + name.removesuffix(".py"), task.tests)
                if "{failure}" in task.prompt:
                    self.assertTrue(task.restore and task.tests and task.frozen)
                for name, _ in task.seed:
                    self.assertTrue((tasks.SEEDS / name).is_file(), name)
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
