"""新版本提示：什么时候查、什么时候提、出了错怎么收场。

联网一律打本机起的假索引，不碰真实 PyPI；缓存落在每条用例自己的临时目录里。
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from xiaoyu import update_check

DAY = 24 * 3600.0


def wheel(version: str, **extra) -> dict:
    return {"filename": f"xiaoyu_agent-{version}-py3-none-any.whl", "yanked": False, **extra}


class UpdateCheckCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name) / "update_check.json"
        for target, value in (
            (mock.patch.object(update_check, "state_path", lambda: self.state), None),
            #  包级基线把提示关着（tests/__init__），这里的用例要它开着
            (mock.patch.dict(os.environ, {update_check.ENABLE_ENV: "1"}), None),
            (mock.patch.object(update_check, "upgrade_command", lambda: "xiaoyu update"), None),
        ):
            target.start()
            self.addCleanup(target.stop)

    def write_state(self, **values) -> None:
        self.state.write_text(json.dumps(values), encoding="utf-8")

    def read_state(self) -> dict:
        return json.loads(self.state.read_text(encoding="utf-8"))


class VersionParsingTest(unittest.TestCase):
    def test_only_plain_release_numbers_count(self) -> None:
        self.assertEqual(update_check.parse_version("0.52.0"), (0, 52, 0))
        self.assertEqual(update_check.parse_version(" 1.2 "), (1, 2))
        for text in ("0.53.0rc1", "0.53.0.dev1", "0.53.0+local", "v0.53.0", "", "latest", "1..2"):
            with self.subTest(text=text):
                self.assertIsNone(update_check.parse_version(text))

    def test_comparison_is_numeric_not_textual(self) -> None:
        self.assertGreater(update_check.parse_version("0.100.0"), update_check.parse_version("0.52.0"))
        self.assertGreater(update_check.parse_version("0.52.10"), update_check.parse_version("0.52.9"))

    def test_index_gives_highest_installable_release(self) -> None:
        payload = {"files": [
            wheel("0.51.0"),
            wheel("0.52.0"),
            {"filename": "xiaoyu_agent-0.52.1.tar.gz", "yanked": False},
            wheel("0.60.0", yanked="坏包，已撤回"),  # 撤回的 pip 默认不装，不能拿来提示
            wheel("0.61.0", yanked=True),
            wheel("0.70.0rc1"),  # 预发布不提
            {"filename": "someone_else-9.9.9-py3-none-any.whl", "yanked": False},
            {"filename": 12345},
            "不是对象",
        ]}
        self.assertEqual(update_check.latest_in_index(payload), "0.52.1")

    def test_unreadable_index_is_none(self) -> None:
        for payload in (None, [], {}, {"files": "x"}, {"files": []}, {"files": [wheel("1.0rc1")]}):
            with self.subTest(payload=payload):
                self.assertIsNone(update_check.latest_in_index(payload))


class _Index(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    body = b""
    status = 200
    seen: list[dict]

    def log_message(self, *args):
        pass

    def do_GET(self) -> None:
        type(self).seen.append({"path": self.path, **{k.lower(): v for k, v in self.headers.items()}})
        self.send_response(type(self).status)
        self.send_header("Content-Length", str(len(type(self).body)))
        self.end_headers()
        self.wfile.write(type(self).body)


class FetchTest(UpdateCheckCase):
    def serve(self, body: bytes, status: int = 200) -> list[dict]:
        handler = type("Handler", (_Index,), {"body": body, "status": status, "seen": []})
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        url = f"http://127.0.0.1:{httpd.server_address[1]}/simple/xiaoyu-agent/"
        patcher = mock.patch.object(update_check, "INDEX_URL", url)
        patcher.start()
        self.addCleanup(patcher.stop)
        return handler.seen

    def test_asks_the_simple_index_and_identifies_only_by_version(self) -> None:
        seen = self.serve(json.dumps({"files": [wheel("0.51.0"), wheel("9.9.9")]}).encode())
        self.assertEqual(update_check.fetch_latest(), "9.9.9")
        (request,) = seen
        self.assertEqual(request["path"], "/simple/xiaoyu-agent/")
        self.assertEqual(request["accept"], "application/vnd.pypi.simple.v1+json")
        self.assertEqual(request["user-agent"], f"xiaoyu/{update_check.__version__}")
        #  除了版本号，请求里没有任何能认出这台机器或这个人的东西
        self.assertNotIn("cookie", request)
        self.assertNotIn("authorization", request)

    def test_every_failure_is_silently_none(self) -> None:
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(err):
            self.serve(b"<html>not json</html>")
            self.assertIsNone(update_check.fetch_latest())
            self.serve(b"{}", status=503)
            self.assertIsNone(update_check.fetch_latest())
            with mock.patch.object(update_check, "MAX_BYTES", 8):
                self.serve(json.dumps({"files": [wheel("9.9.9")]}).encode())
                self.assertIsNone(update_check.fetch_latest())
            with mock.patch.object(update_check, "INDEX_URL", "http://127.0.0.1:1/nothing"):
                self.assertIsNone(update_check.fetch_latest(timeout=1.0))
        self.assertEqual(err.getvalue(), "")


class NoticeTest(UpdateCheckCase):
    def test_newer_release_is_mentioned_with_how_to_upgrade_and_how_to_silence(self) -> None:
        self.write_state(latest="0.53.0")
        notice = update_check.pending_notice("0.52.0", now=1000.0)
        self.assertIn("0.53.0", notice)
        self.assertIn("0.52.0", notice)
        self.assertIn("xiaoyu update", notice)
        self.assertIn("XIAOYU_UPDATE_CHECK=0", notice)

    def test_same_release_is_mentioned_at_most_once_a_day(self) -> None:
        self.write_state(latest="0.53.0")
        self.assertIsNotNone(update_check.pending_notice("0.52.0", now=1000.0))
        self.assertIsNone(update_check.pending_notice("0.52.0", now=1000.0 + DAY - 1))
        self.assertIsNotNone(update_check.pending_notice("0.52.0", now=1000.0 + DAY))

    def test_a_newer_release_is_mentioned_right_away(self) -> None:
        self.write_state(latest="0.53.0")
        self.assertIsNotNone(update_check.pending_notice("0.52.0", now=1000.0))
        state = self.read_state()
        state["latest"] = "0.54.0"
        self.write_state(**state)
        self.assertIn("0.54.0", update_check.pending_notice("0.52.0", now=1001.0))

    def test_clock_set_back_does_not_silence_forever(self) -> None:
        self.write_state(latest="0.53.0", notified_version="0.53.0", notified_at=5_000_000.0)
        self.assertIsNotNone(update_check.pending_notice("0.52.0", now=1000.0))

    def test_nothing_to_say(self) -> None:
        for state in ({}, {"latest": "0.52.0"}, {"latest": "0.51.9"}, {"latest": "0.53.0rc1"},
                      {"latest": 53}, {"latest": None}):
            with self.subTest(state=state):
                self.write_state(**state)
                self.assertIsNone(update_check.pending_notice("0.52.0", now=1000.0))
        self.state.write_text("{坏掉的缓存", encoding="utf-8")
        self.assertIsNone(update_check.pending_notice("0.52.0", now=1000.0))
        self.write_state(latest="0.53.0")
        self.assertIsNone(update_check.pending_notice("开发中的版本", now=1000.0))

    def test_development_checkout_is_not_told_to_upgrade(self) -> None:
        self.write_state(latest="0.53.0")
        with mock.patch.object(update_check, "upgrade_command", lambda: None):
            self.assertIsNone(update_check.pending_notice("0.52.0", now=1000.0))
        #  没提就不该记成"提过了"
        self.assertNotIn("notified_version", self.read_state())


class UpgradeCommandTest(unittest.TestCase):
    def command_for(self, form: str | None) -> str | None:
        from importlib import metadata

        from xiaoyu import diagnostics

        if form is None:
            lookup = mock.patch(
                "importlib.metadata.distribution", side_effect=metadata.PackageNotFoundError("x")
            )
        else:
            lookup = mock.patch("importlib.metadata.distribution", return_value=mock.Mock())
        with lookup, mock.patch.object(diagnostics, "install_form", return_value=form or ""), \
                mock.patch.object(update_check, "source_checkout", return_value=False):
            return update_check.upgrade_command()

    def test_matches_how_it_was_installed(self) -> None:
        self.assertEqual(self.command_for("pip"), "xiaoyu update")
        self.assertEqual(self.command_for("uv"), "xiaoyu update")
        self.assertEqual(self.command_for("pipx"), "pipx upgrade xiaoyu-agent")
        self.assertEqual(self.command_for("uv tool"), "uv tool upgrade xiaoyu-agent")

    def test_source_checkouts_upgrade_through_git(self) -> None:
        self.assertIsNone(self.command_for("可编辑安装（指向源码目录）"))
        self.assertIsNone(self.command_for(None))

    def test_running_from_a_checkout_wins_over_any_install_record(self) -> None:
        #  源码目录里留着的 egg-info 会被认成一份普通安装：以代码在哪为准
        self.assertTrue(update_check.source_checkout(), "用例本身就是从源码检出里跑的")
        with mock.patch("importlib.metadata.distribution", return_value=mock.Mock()) as lookup:
            self.assertIsNone(update_check.upgrade_command())
        lookup.assert_not_called()


class RefreshTest(UpdateCheckCase):
    def test_due_once_a_day(self) -> None:
        self.assertTrue(update_check.check_due(now=1000.0))  # 从没查过
        self.write_state(checked_at=1000.0)
        self.assertFalse(update_check.check_due(now=1000.0 + DAY - 1))
        self.assertTrue(update_check.check_due(now=1000.0 + DAY))
        self.assertTrue(update_check.check_due(now=500.0))  # 时钟往回拨过

    def test_result_is_cached_and_other_fields_survive(self) -> None:
        self.write_state(latest="0.52.0", notified_version="0.52.0", notified_at=7.0)
        with mock.patch.object(update_check, "fetch_latest", return_value="0.53.0"):
            update_check.refresh(now=2000.0)
        self.assertEqual(
            self.read_state(),
            {"latest": "0.53.0", "notified_version": "0.52.0", "notified_at": 7.0, "checked_at": 2000.0},
        )

    def test_failed_lookup_still_counts_as_checked_and_keeps_what_we_knew(self) -> None:
        #  断着网不该每次启动都去试一遍
        self.write_state(latest="0.53.0")
        with mock.patch.object(update_check, "fetch_latest", return_value=None):
            update_check.refresh(now=2000.0)
        self.assertEqual(self.read_state(), {"latest": "0.53.0", "checked_at": 2000.0})
        self.assertFalse(update_check.check_due(now=2001.0))

    def test_unwritable_cache_is_not_an_error(self) -> None:
        blocker = self.state.parent / "blocker"
        blocker.write_text("x", encoding="utf-8")
        with mock.patch.object(update_check, "state_path", lambda: blocker / "update_check.json"), \
                mock.patch.object(update_check, "fetch_latest", return_value="0.53.0"):
            update_check.refresh(now=2000.0)


class StartupTest(UpdateCheckCase):
    #  用例里的版本号都是造出来的，与小羽真实的版本号无关：
    #  跟真实版本号挂钩的用例，发一次版就可能变一次脸
    RUNNING, NEWER = "1.2.3", "1.3.0"

    def start(self, interactive: bool = True):
        """返回 (提示, 联网次数)。后台线程等它跑完再数。"""
        with mock.patch.object(update_check, "__version__", self.RUNNING), \
                mock.patch.object(update_check, "fetch_latest", return_value=self.NEWER) as fetch:
            out = io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
                notice = update_check.startup_notice(interactive=interactive)
                for thread in threading.enumerate():
                    if thread.name == "xiaoyu-update-check":
                        thread.join(5.0)
        #  后台线程只写缓存，绝不往终端打字
        self.assertEqual(out.getvalue(), "")
        return notice, fetch.call_count

    def test_first_launch_checks_quietly_and_the_next_one_tells(self) -> None:
        self.assertEqual(self.start(), (None, 1))
        notice, fetched = self.start()
        self.assertIn(self.NEWER, notice)
        self.assertIn(self.RUNNING, notice)
        self.assertEqual(fetched, 0)  # 一天内不再查

    def test_running_version_is_read_at_call_time(self) -> None:
        #  已经是最新版的不该被提醒：换一个"正在跑的版本"结论就该跟着变
        self.write_state(latest=self.NEWER, checked_at=9e18)
        with mock.patch.object(update_check, "__version__", self.NEWER):
            self.assertIsNone(update_check.pending_notice(now=1000.0))
        with mock.patch.object(update_check, "__version__", self.RUNNING):
            self.assertIn(self.NEWER, update_check.pending_notice(now=1000.0))

    def test_switched_off_means_no_lookup_and_no_cache(self) -> None:
        for value in ("0", "false", "No", "OFF"):
            with self.subTest(value=value), mock.patch.dict(os.environ, {update_check.ENABLE_ENV: value}):
                self.assertEqual(self.start(), (None, 0))
        self.assertFalse(self.state.exists())

    def test_non_interactive_runs_never_look_anything_up(self) -> None:
        self.write_state(latest="9.9.9")
        self.assertEqual(self.start(interactive=False), (None, 0))

    def test_default_follows_the_terminal(self) -> None:
        #  用例里的 stdin / stdout 都不是终端：不给 interactive 就是不查
        with mock.patch.object(update_check, "fetch_latest") as fetch:
            self.assertIsNone(update_check.startup_notice())
        fetch.assert_not_called()

    def test_trouble_never_reaches_the_user(self) -> None:
        with mock.patch.object(update_check, "pending_notice", side_effect=RuntimeError("坏了")):
            self.assertIsNone(update_check.startup_notice(interactive=True))


class CliHookTest(UpdateCheckCase):
    def test_banner_is_followed_by_the_notice(self) -> None:
        from xiaoyu import cli

        out = io.StringIO()
        with mock.patch.object(update_check, "startup_notice", return_value="有新版本 9.9.9"), \
                contextlib.redirect_stdout(out):
            cli.print_update_notice()
        self.assertIn("有新版本 9.9.9", out.getvalue())
        out = io.StringIO()
        with mock.patch.object(update_check, "startup_notice", return_value=None), \
                contextlib.redirect_stdout(out):
            cli.print_update_notice()
        self.assertEqual(out.getvalue(), "")

    def test_both_interactive_entry_points_call_it(self) -> None:
        #  横幅只在交互式启动时打印：提示跟着横幅走，就不会漏进 -p / --wire
        source = (Path(update_check.__file__).parent / "cli.py").read_text(encoding="utf-8")
        lines = source.splitlines()
        banners = [index for index, line in enumerate(lines) if "print(build_banner(" in line]
        self.assertEqual(len(banners), 2)
        for index in banners:
            self.assertEqual(lines[index + 1].strip(), "print_update_notice()")


if __name__ == "__main__":
    unittest.main()
