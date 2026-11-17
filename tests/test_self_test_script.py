"""scripts/self_test.py：用假的 xiaoyu（一个打印收尾对象的 Python 脚本）走完整条记录链路，不调模型。"""

from __future__ import annotations

import datetime as dt
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
_SPEC = importlib.util.spec_from_file_location("self_test_script", REPO / "scripts" / "self_test.py")
self_test = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
_SPEC.loader.exec_module(self_test)

FAKE_RESULT = {
    "result": "done", "model": "fake-model",
    "output": {"total": 3, "passed": 2, "skipped": 1, "rate": 1.0,
               "items": [{"id": "P1-1", "status": "pass", "evidence": "ok"},
                         {"id": "P1-2", "status": "pass", "evidence": "ok"},
                         {"id": "P5-1", "status": "skip", "evidence": "无 mcp"}]},
}

FAKE_XIAOYU = """\
import json, os, sys
#  把收到的参数与环境记下来，测试断言用
record = {"argv": sys.argv[1:], "cwd": os.getcwd(), "cfg": os.environ.get("XDG_CONFIG_HOME", ""),
          "env_file": os.environ.get("XIAOYU_ENV_FILE", "")}
open(os.environ["FAKE_LOG"], "w", encoding="utf-8").write(json.dumps(record))
print("some progress line")
print(json.dumps(%s))
"""


class RecordTest(unittest.TestCase):
    def test_parse_result_takes_last_json_line(self):
        self.assertIsNone(self_test.parse_result(""))
        self.assertIsNone(self_test.parse_result("not json"))
        self.assertIsNone(self_test.parse_result('{"no": "output"}'))
        got = self_test.parse_result("noise\n" + json.dumps(FAKE_RESULT) + "\n")
        self.assertEqual(got["output"]["total"], 3)

    def test_finish_and_save_keeps_latest_and_prunes(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            for day in range(1, 5):
                record = self_test.finish_record(FAKE_RESULT, source={"version": "0.1.0", "branch": "main", "commit": "abc"},
                                                 now=dt.datetime(2026, 10, day, 3, 30))
                self_test.save_record(record, out, keep=2)
            names = sorted(p.name for p in out.glob("*.json"))
            self.assertEqual(names, ["20261003T033000.json", "20261004T033000.json", "latest.json"])
            latest = json.loads((out / "latest.json").read_text(encoding="utf-8"))
            self.assertEqual(latest["recorded_at"], "2026-10-04T03:30:00")
            self.assertEqual(latest["source"]["version"], "0.1.0")
            self.assertTrue(latest["platform"])
        self.assertAlmostEqual(self_test.rate_of(FAKE_RESULT), 1.0)
        self.assertEqual(self_test.rate_of({"output": {"total": 2, "passed": 0, "skipped": 2}}), 0.0)


class RunTest(unittest.TestCase):
    def _fake(self, tmp: Path, result=FAKE_RESULT) -> Path:
        fake = tmp / "fake_xiaoyu.py"
        fake.write_text(FAKE_XIAOYU % json.dumps(result), encoding="utf-8")
        return fake

    def test_runs_in_temp_workspace_with_isolated_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            fake = self._fake(tmp_path)
            log = tmp_path / "log.json"
            checklist = tmp_path / "self_test.md"
            checklist.write_text("清单正文", encoding="utf-8")
            env_file = tmp_path / ".env"
            env_file.write_text("", encoding="utf-8")
            with mock.patch.dict(os.environ, {"FAKE_LOG": str(log)}):
                data, problem = self_test.run_self_test([sys.executable, str(fake)], model="m", timeout=60,
                                                        env_file=env_file, checklist=checklist)
            self.assertEqual(problem, "")
            self.assertEqual(data["output"]["passed"], 2)
            seen = json.loads(log.read_text(encoding="utf-8"))
            self.assertEqual(seen["argv"][:3], ["-p", "清单正文", "--yolo"])
            self.assertIn("--output-schema", seen["argv"])
            self.assertEqual(seen["argv"][-2:], ["--model", "m"])
            self.assertEqual(seen["env_file"], str(env_file))
            #  工作区与配置目录都是临时的，而且跑完就清掉
            self.assertNotEqual(seen["cwd"], os.getcwd())
            self.assertFalse(Path(seen["cwd"]).exists())
            self.assertTrue(seen["cfg"].startswith(tempfile.gettempdir()))
            self.assertFalse(Path(seen["cfg"]).exists())

    def test_bad_output_reports_problem(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            fake = tmp_path / "fake.py"
            fake.write_text("print('no json here')\nraise SystemExit(3)\n", encoding="utf-8")
            checklist = tmp_path / "c.md"
            checklist.write_text("x", encoding="utf-8")
            data, problem = self_test.run_self_test([sys.executable, str(fake)], model=None, timeout=60,
                                                    env_file=None, checklist=checklist)
            self.assertIsNone(data)
            self.assertIn("退出码 3", problem)

    def test_main_writes_record_and_exit_code_follows_rate(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            out = tmp_path / "records"
            checklist = tmp_path / "c.md"
            checklist.write_text("x", encoding="utf-8")
            with mock.patch.dict(os.environ, {"FAKE_LOG": str(tmp_path / "l.json")}), \
                    mock.patch.object(self_test, "CHECKLIST", checklist), redirect_stdout(io.StringIO()):
                good = self._fake(tmp_path)
                code = self_test.main(["--xiaoyu", str(good), "--out-dir", str(out)])
                self.assertEqual(code, 0)
                self.assertTrue((out / "latest.json").is_file())
                low = dict(FAKE_RESULT, output=dict(FAKE_RESULT["output"], passed=1))
                bad = self._fake(tmp_path, low)
                self.assertEqual(self_test.main(["--xiaoyu", str(bad), "--out-dir", str(out)]), 1)

class PlistTest(unittest.TestCase):
    def test_render_plist_shape(self):
        #  期望值也经 Path 转字符串：Windows 上分隔符是反斜杠，plist 只在 macOS 用但测试三平台都跑
        script, out_dir, env_file = Path("/r/scripts/self_test.py"), Path("/h/.config/xiaoyu/self-test"), Path("/r/.env")
        plist = self_test.render_plist(python="/usr/bin/python3", script=script, out_dir=out_dir,
                                       hour=3, minute=30, env_file=env_file, model=None)
        self.assertEqual(plist["Label"], self_test.LAUNCHD_LABEL)
        self.assertEqual(plist["ProgramArguments"],
                         ["/usr/bin/python3", str(script), "--out-dir", str(out_dir), "--env-file", str(env_file)])
        self.assertEqual(plist["StartCalendarInterval"], {"Hour": 3, "Minute": 30})
        self.assertEqual(plist["StandardOutPath"], str(out_dir / "launchd.log"))
        self.assertFalse(plist["RunAtLoad"])


if __name__ == "__main__":
    unittest.main()
