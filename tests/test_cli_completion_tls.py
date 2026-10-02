"""`xiaoyu completion`（从子命令表与 parser 现取词表）、`serve --tls-cert/--tls-key`
（成对校验、传到 uvicorn）、`sessions export/rename` 的命令行外壳。"""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from xiaoyu import cli


class CompletionTest(unittest.TestCase):
    def test_words_come_from_the_tables(self) -> None:
        subcommands, flags = cli.completion_words()
        names = [name for name, _ in subcommands]
        for entry in cli.SUBCOMMANDS:
            for name in entry[0]:
                self.assertIn(name, names)
        flag_names = [flag for flag, _ in flags]
        self.assertIn("--model", flag_names)
        self.assertIn("--stats", flag_names)
        self.assertNotIn("-s", flag_names)  # 只补长旗标

    def test_each_shell_script_mentions_every_subcommand(self) -> None:
        for shell in cli.COMPLETION_SHELLS:
            script = cli.completion_script(shell)
            with self.subTest(shell=shell):
                for entry in cli.SUBCOMMANDS:
                    self.assertIn(entry[0][0], script)
                #  fish 的长旗标写法是 `-l model`，没有连字符
                self.assertIn("-l model" if shell == "fish" else "--model", script)
        self.assertIn("complete -o default -F _xiaoyu_complete xiaoyu", cli.completion_script("bash"))
        self.assertIn("compdef _xiaoyu xiaoyu", cli.completion_script("zsh"))
        self.assertIn("complete -c xiaoyu -l model", cli.completion_script("fish"))

    def test_command_prints_script_and_rejects_unknown_shell(self) -> None:
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(cli.completion_command(["zsh"]), 0)
        self.assertTrue(out.getvalue().startswith("# xiaoyu zsh 补全"))
        with self.assertRaises(SystemExit), redirect_stderr(io.StringIO()):
            cli.completion_command(["powershell"])


class ServeTlsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.cert = self.root / "cert.pem"
        self.key = self.root / "key.pem"
        self.cert.write_text("cert", encoding="utf-8")
        self.key.write_text("key", encoding="utf-8")

    def _run(self, argv: list[str]):
        captured: dict = {}

        def fake_serve(cfg):
            captured["cfg"] = cfg
            return 0

        try:
            from xiaoyu import serve as serve_module
        except Exception:  # noqa: BLE001 - 没装 [serve] 时 import 本身会炸
            self.skipTest("需要可选额外 [serve]")
        err = io.StringIO()
        with mock.patch.object(serve_module, "serve", fake_serve), redirect_stdout(
            io.StringIO()
        ), redirect_stderr(err):
            code = cli.serve_command(["--workspace", str(self.root), *argv])
        return code, captured.get("cfg"), err.getvalue()

    def test_only_one_of_the_pair_is_rejected(self) -> None:
        code, cfg, err = self._run(["--tls-cert", str(self.cert)])
        self.assertEqual(code, 2)
        self.assertIsNone(cfg)
        self.assertIn("--tls-cert 与 --tls-key 必须一起给", err)

    def test_missing_file_is_rejected(self) -> None:
        code, cfg, err = self._run(["--tls-cert", str(self.cert), "--tls-key", str(self.root / "nope.pem")])
        self.assertEqual(code, 2)
        self.assertIn("--tls-key 指向的文件不存在", err)

    def test_pair_reaches_config_and_uvicorn(self) -> None:
        code, cfg, _ = self._run(["--tls-cert", str(self.cert), "--tls-key", str(self.key)])
        self.assertEqual(code, 0)
        self.assertEqual((cfg.tls_cert, cfg.tls_key), (self.cert, self.key))
        self.assertEqual(cfg.scheme, "https")
        #  serve() 把两个路径原样交给 uvicorn
        from xiaoyu import serve as serve_module

        fake_uvicorn = mock.MagicMock()
        with mock.patch.dict("sys.modules", {"uvicorn": fake_uvicorn}), mock.patch.object(
            serve_module, "create_app", return_value="app"
        ):
            serve_module.serve(cfg)
        kwargs = fake_uvicorn.run.call_args.kwargs
        self.assertEqual(kwargs["ssl_certfile"], str(self.cert))
        self.assertEqual(kwargs["ssl_keyfile"], str(self.key))

    def test_without_tls_nothing_is_passed(self) -> None:
        code, cfg, _ = self._run([])
        self.assertEqual(code, 0)
        self.assertEqual(cfg.scheme, "http")
        from xiaoyu import serve as serve_module

        fake_uvicorn = mock.MagicMock()
        with mock.patch.dict("sys.modules", {"uvicorn": fake_uvicorn}), mock.patch.object(
            serve_module, "create_app", return_value="app"
        ):
            serve_module.serve(cfg)
        self.assertNotIn("ssl_certfile", fake_uvicorn.run.call_args.kwargs)


class SessionsCliTest(unittest.TestCase):
    """export / rename 的命令行外壳：找会话、落文件、symlink 拒绝、锁冲突报错。"""

    def setUp(self) -> None:
        from xiaoyu import session_log as session_log_module

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        patcher = mock.patch.object(
            session_log_module, "user_config_dir", lambda: self.root / "xiaoyu"
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        from xiaoyu.session_log import SessionLog

        self.log = SessionLog.create("m", str(Path.cwd().resolve()), session_id="job")
        self.log.append({"role": "system", "content": "内部"})
        self.log.append({"role": "user", "content": "任务一"})
        self.log.append({"role": "assistant", "content": "做完了"})
        self.log.release()

    def _run(self, argv: list[str]) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.sessions_command(argv)
        return code, out.getvalue(), err.getvalue()

    def test_export_markdown_to_stdout(self) -> None:
        code, out, _ = self._run(["export", "job"])
        self.assertEqual(code, 0)
        self.assertIn("## 用户\n\n任务一", out)
        self.assertNotIn("内部", out)

    def test_export_json_to_file(self) -> None:
        target = self.root / "out.json"
        code, out, _ = self._run(["export", "1", "--format", "json", "-o", str(target)])
        self.assertEqual(code, 0)
        payload = json.loads(target.read_text(encoding="utf-8"))
        self.assertEqual(payload["session"]["session_id"], "job")
        self.assertEqual([m["role"] for m in payload["messages"]], ["user", "assistant"])

    @unittest.skipIf(os.name == "nt", "symlink 需要权限")
    def test_export_refuses_symlink_target(self) -> None:
        link = self.root / "link.md"
        link.symlink_to(self.root / "real.md")
        code, _, err = self._run(["export", "job", "-o", str(link)])
        self.assertEqual(code, 2)
        self.assertIn("符号链接", err)

    def test_unknown_reference(self) -> None:
        code, _, err = self._run(["export", "nope"])
        self.assertEqual(code, 2)
        self.assertIn("找不到会话", err)

    def test_rename_then_listed_with_title(self) -> None:
        from xiaoyu.session_log import list_sessions

        code, out, _ = self._run(["rename", "job", "登录修复"])
        self.assertEqual(code, 0)
        self.assertIn("已改名：登录修复", out)
        self.assertEqual(list_sessions()[0].title, "登录修复")

    def test_rename_locked_session_reports_pid(self) -> None:
        from xiaoyu.session_log import SessionLog

        holder = SessionLog(self.log.path)
        self.addCleanup(holder.release)
        code, _, err = self._run(["rename", "job", "x"])
        self.assertEqual(code, 2)
        self.assertIn("正在被续写", err)


if __name__ == "__main__":
    unittest.main()
