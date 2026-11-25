"""xiaoyu chrome：native messaging host（帧、安装、找到或拉起 serve、stdout 纯净）。

真 serve 起一次要探测 provider、十来秒，这里用一个最小的假 serve（stdlib http.server）
顶替：它照真 serve 的约定读 XIAOYU_SERVE_TOKEN、带对 token 的 /health 才回 root。
"""

from __future__ import annotations

import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from xiaoyu import chrome_host as ch

ROOT = Path(__file__).resolve().parent.parent

FAKE_SERVE = r'''
import json, os, signal, sys
from http.server import BaseHTTPRequestHandler, HTTPServer
port, root, version, mode = int(sys.argv[1]), sys.argv[2], sys.argv[3], sys.argv[4]
if mode == "exit":
    print("boom: 假 serve 起不来", file=sys.stderr)
    sys.exit(3)
if mode == "hang":
    import time
    time.sleep(60)
if mode == "stubborn":
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
token = os.environ["XIAOYU_SERVE_TOKEN"]
class H(BaseHTTPRequestHandler):
    def do_GET(self):
        body = {"ok": True, "version": version}
        if self.headers.get("Authorization") == f"Bearer {token}":
            body["root"] = root
        data = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)
    def log_message(self, *a):
        pass
HTTPServer(("127.0.0.1", port), H).serve_forever()
'''

EXT = "a" * 32


@unittest.skipIf(os.name == "nt", "xiaoyu chrome 暂只支持 macOS 与 Linux")
class ChromeCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(os.path.realpath(self._tmp.name))
        self.home = self.tmp / "home"
        self.home.mkdir()
        env = mock.patch.dict(os.environ, {
            "HOME": str(self.home), "XDG_CONFIG_HOME": str(self.home / ".config"),
            "http_proxy": "", "HTTP_PROXY": "",
        })
        env.start()
        self.addCleanup(env.stop)
        #  别碰真机 8420 段（可能正跑着你自己的 serve）：每个用例借一段临时端口
        import socket

        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            base = sock.getsockname()[1]
        ports = mock.patch.object(ch, "PORT_RANGE", range(base, base + 10))
        ports.start()
        self.addCleanup(ports.stop)
        self.fake = self.tmp / "fake_serve.py"
        self.fake.write_text(FAKE_SERVE, encoding="utf-8")
        self.mode = "ok"
        self.pids: list[int] = []
        self.addCleanup(self._kill_all)

    def _kill_all(self) -> None:
        state = ch.load_state()
        if state:
            self.pids.append(state["pid"])
        while ch._DETACHED:
            process = ch._DETACHED.pop()
            process.kill()
            process.wait()
        for pid in self.pids:
            try:
                os.kill(pid, signal.SIGKILL)
                os.waitpid(pid, 0)
            except (ProcessLookupError, ChildProcessError):
                pass

    def argv_for(self, cfg, port, default_workspace):
        root = str(Path(cfg.workspace).resolve())
        return [sys.executable, str(self.fake), str(port), root, ch.current_version(), self.mode]

    def install_config(self, **extra) -> ch.HostConfig:
        cfg = ch.HostConfig(workspace=str(self.home), extension_ids=[EXT],
                            python=sys.executable, **extra)
        ch.save_config(cfg)
        return cfg

    def ensure(self, **kw):
        kw.setdefault("timeout", 10.0)
        reply = ch.ensure(argv_for=self.argv_for, **kw)
        state = ch.load_state()
        if state:
            self.pids.append(state["pid"])
        return reply


class TestFrames(unittest.TestCase):
    def test_roundtrip(self):
        frame = ch.encode_frame({"type": "ensure", "说明": "中文"})
        self.assertEqual(ch.read_frame(io.BytesIO(frame)), {"type": "ensure", "说明": "中文"})

    def test_eof_is_none(self):
        self.assertIsNone(ch.read_frame(io.BytesIO(b"")))

    def test_truncated_and_oversized(self):
        frame = ch.encode_frame({"type": "ensure"})
        with self.assertRaises(ch.HostError) as ctx:
            ch.read_frame(io.BytesIO(frame[:-2]))
        self.assertEqual(ctx.exception.code, "truncated")
        big = (ch.MAX_INCOMING + 1).to_bytes(4, sys.byteorder)
        with self.assertRaises(ch.HostError) as ctx:
            ch.read_frame(io.BytesIO(big))
        self.assertEqual(ctx.exception.code, "too_large")

    def test_unknown_type_replies_error(self):
        self.assertEqual(ch.handle({"type": "rm -rf"})["code"], "unknown_type")
        self.assertEqual(ch.handle([1, 2])["code"], "unknown_type")


class TestInstall(ChromeCase):
    def run_cli(self, *argv) -> int:
        from xiaoyu.cli_chrome import chrome_command

        with mock.patch("sys.stdout", io.StringIO()), mock.patch("sys.stderr", io.StringIO()):
            return chrome_command(list(argv))

    def test_install_writes_manifest_only_for_existing_browsers(self):
        dirs = ch.browser_dirs()
        dirs[0][1].mkdir(parents=True)
        self.assertEqual(self.run_cli("install", "--extension-id", EXT), 0)
        manifest = json.loads(ch.manifest_file(dirs[0][1]).read_text(encoding="utf-8"))
        self.assertEqual(manifest["name"], ch.HOST_NAME)
        self.assertEqual(manifest["type"], "stdio")
        self.assertEqual(manifest["allowed_origins"],
                         [f"chrome-extension://{i}/" for i in (*ch.KNOWN_EXTENSION_IDS, EXT)])
        self.assertEqual(manifest["path"], str(ch.launcher_path()))
        self.assertTrue(os.access(ch.launcher_path(), os.X_OK))
        self.assertIn("-m xiaoyu chrome host", ch.launcher_path().read_text(encoding="utf-8"))
        for _, other in dirs[1:]:
            self.assertFalse(other.exists())
        cfg = ch.load_config()
        self.assertEqual(cfg.workspace, str(self.home.resolve()))
        self.assertEqual(cfg.approval, "ask")
        self.assertTrue(ch.scratch_dir().is_dir())
        #  配置文件与状态文件一样是 0600
        self.assertEqual(ch.config_path().stat().st_mode & 0o777, 0o600)

    def test_reinstall_keeps_ids_and_settings(self):
        self.run_cli("install", "--extension-id", EXT, "--approval", "allow_all")
        self.run_cli("install", "--extension-id", "b" * 32)
        cfg = ch.load_config()
        self.assertEqual(cfg.extension_ids, [*ch.KNOWN_EXTENSION_IDS, EXT, "b" * 32])
        self.assertEqual(cfg.approval, "allow_all")

    def test_install_needs_valid_extension_id(self):
        self.assertEqual(self.run_cli("install", "--extension-id", "not-an-id"), 2)
        self.assertIsNone(ch.load_config())

    def test_install_defaults_to_known_extension_ids(self):
        self.assertEqual(self.run_cli("install"), 0)
        self.assertEqual(ch.load_config().extension_ids, list(ch.KNOWN_EXTENSION_IDS))
        with mock.patch.object(ch, "KNOWN_EXTENSION_IDS", ()):
            ch.config_path().unlink()
            self.assertEqual(self.run_cli("install"), 2)

    def test_uninstall_removes_only_our_manifest(self):
        dirs = ch.browser_dirs()
        for _, d in dirs[:2]:
            d.mkdir(parents=True)
        self.run_cli("install", "--extension-id", EXT)
        foreign = ch.manifest_file(dirs[1][1])
        foreign.write_text(json.dumps({"name": ch.HOST_NAME, "path": "/elsewhere"}), encoding="utf-8")
        self.assertEqual(self.run_cli("uninstall"), 0)
        self.assertFalse(ch.manifest_file(dirs[0][1]).exists())
        self.assertTrue(foreign.exists())
        self.assertFalse(ch.launcher_path().exists())
        self.assertIsNone(ch.load_config())


class TestEnsure(ChromeCase):
    def test_not_installed(self):
        with self.assertRaises(ch.HostError) as ctx:
            ch.ensure(argv_for=self.argv_for)
        self.assertEqual(ctx.exception.code, "not_installed")

    def test_starts_then_reuses(self):
        self.install_config()
        first = self.ensure()
        self.assertTrue(first["ok"] and first["started"])
        self.assertTrue(first["baseUrl"].startswith("http://127.0.0.1:"))
        self.assertEqual(len(first["token"]), 32)
        self.assertEqual(ch.state_path().stat().st_mode & 0o777, 0o600)
        second = self.ensure()
        self.assertFalse(second["started"])
        self.assertEqual(second["token"], first["token"])

    def test_scratch_is_default_workspace_inside_root(self):
        cfg = self.install_config()
        self.assertEqual(ch.default_workspace_for(cfg), ch.scratch_dir().resolve())
        argv = ch.serve_argv(cfg, 8420, ch.default_workspace_for(cfg))
        self.assertIn("--default-workspace", argv)
        self.assertNotIn(cfg.workspace, " ".join(a for a in argv if "TOKEN" in a))
        narrow = ch.HostConfig(workspace=str(self.tmp / "elsewhere"))
        self.assertIsNone(ch.default_workspace_for(narrow))

    def test_dead_pid_restarts(self):
        self.install_config()
        first = self.ensure()
        pid = ch.load_state()["pid"]
        os.kill(pid, signal.SIGKILL)
        os.waitpid(pid, 0)
        second = self.ensure()
        self.assertTrue(second["started"])
        self.assertFalse(second["restarted"])
        self.assertNotEqual(second["token"], first["token"])

    def test_version_change_restarts(self):
        self.install_config()
        self.ensure()
        old = ch.load_state()["pid"]
        with mock.patch.object(ch, "current_version", return_value="999.0.0"):
            reply = self.ensure()
        self.assertTrue(reply["restarted"])
        self.assertEqual(reply["version"], "999.0.0")
        self.assertFalse(ch.pid_alive(old))

    def test_config_change_restarts(self):
        self.install_config()
        self.ensure()
        self.install_config(approval="allow_all")
        self.assertTrue(self.ensure()["restarted"])

    def test_stubborn_serve_gets_killed(self):
        self.install_config()
        self.mode = "stubborn"
        self.ensure()
        old = ch.load_state()["pid"]
        self.mode = "ok"
        with mock.patch.object(ch, "current_version", return_value="999.0.0"):
            reply = self.ensure(grace=0.5)
        self.assertTrue(reply["restarted"])
        self.assertFalse(ch.pid_alive(old))

    def test_token_mismatch_is_not_ours(self):
        """pid 活着但 token 对不上（pid 被复用 / 别人的 serve）：不杀它，另起一个。"""
        self.install_config()
        self.ensure()
        state = ch.load_state()
        victim = state["pid"]
        state["token"] = "0" * 32
        ch._write_private_json(ch.state_path(), state)
        self.pids.append(victim)
        reply = self.ensure()
        self.assertTrue(reply["started"])
        self.assertFalse(reply["restarted"])
        self.assertTrue(ch.pid_alive(victim))
        self.assertNotEqual(ch.load_state()["port"], state["port"])

    def test_start_failure_reports_log_tail(self):
        self.install_config()
        self.mode = "exit"
        with mock.patch.object(ch, "serve_argv", self.argv_for):
            reply = ch.handle({"type": "ensure"})
        self.assertFalse(reply["ok"])
        self.assertEqual(reply["code"], "start_failed")
        self.assertIn("boom", reply["detail"])

    def test_start_timeout(self):
        self.install_config()
        self.mode = "hang"
        with self.assertRaises(ch.HostError) as ctx:
            self.ensure(timeout=1.0)
        self.assertEqual(ctx.exception.code, "start_timeout")

    def test_concurrent_ensure_starts_one(self):
        self.install_config()
        replies: list[dict] = []
        threads = [threading.Thread(target=lambda: replies.append(self.ensure())) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len({r["token"] for r in replies}), 1)
        self.assertEqual(sum(r["started"] for r in replies), 1)

    def test_stop_running(self):
        self.install_config()
        self.ensure()
        pid = ch.load_state()["pid"]
        self.assertTrue(ch.stop_running(grace=2.0))
        self.assertFalse(ch.pid_alive(pid))
        self.assertIsNone(ch.load_state())
        self.assertFalse(ch.stop_running())


@unittest.skipIf(os.name == "nt", "xiaoyu chrome 暂只支持 macOS 与 Linux")
class TestHostProcess(unittest.TestCase):
    def test_stdout_carries_exactly_one_frame(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {**os.environ, "HOME": tmp, "XDG_CONFIG_HOME": str(Path(tmp) / "config"),
                   "PYTHONPATH": str(ROOT), "PYTHONWARNINGS": "always"}
            proc = subprocess.run(
                [sys.executable, "-P", "-m", "xiaoyu", "chrome", "host", f"chrome-extension://{EXT}/"],
                input=ch.encode_frame({"type": "status"}), capture_output=True, env=env,
                cwd=tmp, timeout=60,
            )
        self.assertEqual(proc.returncode, 0, proc.stderr.decode(errors="replace"))
        out = proc.stdout
        length = int.from_bytes(out[:4], sys.byteorder)
        self.assertEqual(len(out), 4 + length, out)
        reply = json.loads(out[4:])
        self.assertTrue(reply["ok"])
        self.assertFalse(reply["installed"])
        self.assertFalse(reply["running"])


if __name__ == "__main__":
    unittest.main()
