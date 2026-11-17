"""browser 工具的测试。

纯函数部分（参数校验、注册、check_fn 门控）全平台零依赖跑；
真浏览器用例只在 playwright + chromium 内核都在时跑（CI 不装，本地验证），
页面用 data: URL——不打网络。
"""

from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from xiaoyu import browser
from xiaoyu.config import Config
from xiaoyu.tools import Toolbox

_PAGE = (
    "data:text/html,"
    "<title>probe</title>"
    "<button onclick=\"document.getElementById('out').textContent='clicked'\">Add</button>"
    "<input id='inp'><div id='out'></div>"
)


def _real_browser_ready() -> bool:
    """playwright 装了且 chromium 内核也装了（只装包没跑 install 的机器要跳过）。"""
    if not browser.available():
        return False
    try:
        session = browser.BrowserSession()
        session.run("open", url="data:text/html,<title>ok</title>")
        session.close()
        return True
    except Exception:  # noqa: BLE001
        return False


REAL = _real_browser_ready()


class ValidationTest(unittest.TestCase):
    """参数校验在启动浏览器之前完成——这些用例不需要 playwright。"""

    def setUp(self):
        self.session = browser.BrowserSession()

    def test_unknown_action(self):
        out = self.session.run("teleport")
        self.assertIn("ERROR", out)
        self.assertIn("open", out)  # 报错要列出可用 action

    def test_missing_required_params(self):
        for action, missing in [
            ("open", "url"),
            ("click", "selector"),
            ("fill", "selector"),
            ("press", "key"),
            ("screenshot", "path"),
        ]:
            out = self.session.run(action)
            self.assertIn("ERROR", out, action)
            self.assertIn(missing, out, action)

    def test_fill_requires_text(self):
        out = self.session.run("fill", selector="#x")
        self.assertIn("text", out)

    def test_open_refuses_schemes_that_are_not_pages(self):
        for url in ("javascript:alert(1)", "chrome://settings", "view-source:https://x",
                    "example.com"):
            out = self.session.run("open", url=url)
            self.assertTrue(out.startswith("ERROR"), url)
            self.assertIn("协议", out)

    def test_dialog_choice_is_validated(self):
        out = self.session.run("snapshot", dialog="maybe")
        self.assertTrue(out.startswith("ERROR"), out)

    def test_falls_back_to_a_system_browser_when_the_bundled_one_is_missing(self):
        attempts: list = []

        class Launcher:
            def launch(self, **kwargs):
                attempts.append(kwargs.get("channel"))
                if kwargs.get("channel") != "msedge":
                    raise RuntimeError("Executable doesn't exist at /x/chromium")
                return "edge"

        self.session._pw = mock.Mock(chromium=Launcher())
        self.assertEqual(self.session._launch(), "edge")
        self.assertEqual(attempts, [None, "chrome", "msedge"])
        self.assertEqual(self.session._launched_with, "msedge")
        self.session._pw = None

    def test_other_launch_failures_are_not_masked_by_the_fallback(self):
        class Launcher:
            def launch(self, **kwargs):
                raise RuntimeError("sandbox 起不来")

        self.session._pw = mock.Mock(chromium=Launcher())
        with self.assertRaises(RuntimeError):
            self.session._launch()
        self.session._pw = None

    def test_close_without_start_is_fine(self):
        """没启动过就 close 不该炸——模型完全可能上来先 close。"""
        self.assertIn("关闭", self.session.run("close"))


class RegistrationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = Config(
            base_url="http://unused",
            model="m",
            workspace=Path(self.tmp.name).resolve(),
            enable_skills=False,
            enable_agents=False,
            enable_hooks=False,
            enable_plugins=False,
        )
        self.box = Toolbox(self.config)

    def test_local_files_outside_the_workspace_are_refused(self):
        """file:// 加 read 能把任何本机文件读出来，绕过文件工具的全部护栏。"""
        with mock.patch.object(browser, "session") as session:
            out = self.box._browser("open", url="file:///etc/passwd")
            self.assertTrue(out.startswith("ERROR"), out)
            self.assertIn("工作区之外", out)
            session.assert_not_called()
            inside = self.box.config.workspace / "page.html"
            inside.write_text("<title>ok</title>", encoding="utf-8")
            session.return_value.run.return_value = "已打开"
            self.assertEqual(self.box._browser("open", url=inside.as_uri()), "已打开")

    def test_file_url_with_a_drive_letter_is_read_as_a_path(self):
        """Windows 上 file:///C:/x 的 path 段是 "/C:/x"：直接当路径用会多出开头的
        斜杠，工作区里的文件也被判成在外面。"""
        import urllib.parse
        import urllib.request

        inside = self.box.config.workspace / "sub dir" / "页面.html"
        inside.parent.mkdir()
        inside.write_text("<title>ok</title>", encoding="utf-8")
        url = inside.as_uri()
        recovered = Path(urllib.request.url2pathname(urllib.parse.urlsplit(url).path))
        self.assertEqual(recovered.resolve(), inside.resolve())
        with mock.patch.object(browser, "session") as session:
            session.return_value.run.return_value = "已打开"
            self.assertEqual(self.box._browser("open", url=url), "已打开")

    def test_relative_screenshot_path_lands_in_the_workspace(self):
        with mock.patch.object(browser, "session") as session:
            session.return_value.run.return_value = "ok"
            self.box._browser("screenshot", path="shots/a.png")
            sent = session.return_value.run.call_args.kwargs["path"]
        self.assertEqual(Path(sent), (self.box.config.workspace / "shots" / "a.png").resolve())

    def test_registered_with_gating(self):
        tool = self.box.get("browser")
        self.assertIsNotNone(tool)
        #  能点按钮就是能以用户身份做任何事，必须过审批
        self.assertTrue(tool.requires_approval)
        #  没装 playwright 时工具必须消失；行为断言而非身份断言——
        #  check_fn 必须晚绑定（lambda），mock 掉 available 门控要跟着变
        with mock.patch.object(browser, "available", return_value=False):
            self.assertFalse(tool.available())
        with mock.patch.object(browser, "available", return_value=True):
            self.assertTrue(tool.available())

    def test_enable_browser_off_hides_tool(self):
        #  宿主装了 playwright 也要关得掉：e2e golden 的工具表密封性靠它
        box = Toolbox(replace(self.config, enable_browser=False))
        with mock.patch.object(browser, "available", return_value=True):
            self.assertFalse(box.get("browser").available())

    def test_unavailable_refuses_execution(self):
        with mock.patch.object(browser, "available", return_value=False):
            out = self.box.run("browser", {"action": "snapshot"})
        self.assertIn("不可用", out)


@unittest.skipUnless(REAL, "playwright/chromium 未就绪")
class RealBrowserTest(unittest.TestCase):
    """真开 chromium 验证端到端行为（data: URL，不打网络）。"""

    @classmethod
    def setUpClass(cls):
        cls.session = browser.BrowserSession()

    @classmethod
    def tearDownClass(cls):
        cls.session.close()

    def test_the_very_first_call_of_a_session_works(self):
        """事件监听是在建页时挂上的：挂不上的话，每个会话的第一次调用就报错。"""
        fresh = browser.BrowserSession()
        self.addCleanup(fresh.close)
        out = fresh.run("open", url=_PAGE)
        self.assertNotIn("ERROR", out)
        self.assertIn("probe", out)

    def test_open_returns_snapshot(self):
        out = self.session.run("open", url=_PAGE)
        self.assertIn("probe", out)
        self.assertIn("Add", out)  # 快照里能看到按钮，模型才有的点

    def test_click_and_read(self):
        self.session.run("open", url=_PAGE)
        self.session.run("click", selector="text=Add")
        text = self.session.run("read")
        self.assertIn("clicked", text)

    def test_fill(self):
        self.session.run("open", url=_PAGE)
        out = self.session.run("fill", selector="#inp", text="hello")
        self.assertIn("5", out)

    def test_selector_miss_is_error_text(self):
        """点不到的元素要返回 ERROR 文本给模型自愈，不能抛异常炸主循环。"""
        self.session.run("open", url=_PAGE)
        out = self.session.run("click", selector="text=不存在的按钮")
        self.assertIn("ERROR", out)

    def page(self, name: str, body: str) -> str:
        """测试页落成文件再打开：data: 地址不声明字符集，中文会乱码；浏览器也
        不许从链接跳到 data: 地址。"""
        directory = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(directory, ignore_errors=True))
        target = Path(directory) / name
        target.write_text(
            f"<!doctype html><meta charset='utf-8'><title>{name}</title>{body}", encoding="utf-8"
        )
        return target.as_uri()

    CONFIRM = (
        "<button onclick=\"document.getElementById('o').textContent="
        "confirm('确定删除？') ? 'deleted' : 'kept'\">Del</button><div id='o'></div>"
    )

    def test_confirm_is_dismissed_by_default_and_the_model_is_told(self):
        self.session.run("open", url=self.page("confirm.html", self.CONFIRM))
        out = self.session.run("click", selector="text=Del")
        self.assertIn("confirm", out)
        self.assertIn("确定删除？", out)
        self.assertIn("已取消", out)
        self.assertIn("dialog=accept", out)
        self.assertIn("kept", self.session.run("read"))

    def test_confirm_can_be_accepted_on_request(self):
        self.session.run("open", url=self.page("confirm.html", self.CONFIRM))
        out = self.session.run("click", selector="text=Del", dialog="accept")
        self.assertIn("已确认", out)
        self.assertIn("deleted", self.session.run("read"))

    def test_alert_does_not_block_and_is_reported(self):
        body = (
            "<button onclick=\"alert('保存成功'); "
            "document.getElementById('o').textContent='after'\">Go</button><div id='o'></div>"
        )
        self.session.run("open", url=self.page("alert.html", body))
        out = self.session.run("click", selector="text=Go")
        self.assertIn("保存成功", out)
        self.assertIn("已确认", out)
        self.assertIn("after", self.session.run("read"))

    def test_link_opening_a_new_tab_is_followed(self):
        second = self.page("second-page.html", "<p>arrived</p>")
        first = self.page("first.html", f"<a target='_blank' href='{second}'>Next</a>")
        self.session.run("open", url=first)
        out = self.session.run("click", selector="text=Next")
        self.assertIn("新标签页", out)
        self.assertIn("second-page", out)
        self.assertIn("arrived", self.session.run("read"))
        #  之后的动作落在新标签页上
        self.assertIn("second-page", self.session.run("snapshot"))

    def test_ordinary_click_stays_on_the_same_tab(self):
        self.session.run("open", url=_PAGE)
        out = self.session.run("click", selector="text=Add")
        self.assertNotIn("新标签页", out)

    def test_screenshot_saves_file(self):
        self.session.run("open", url=_PAGE)
        with tempfile.TemporaryDirectory() as tmp:
            target = str(Path(tmp) / "shot.png")
            out = self.session.run("screenshot", path=target)
            self.assertIn("shot.png", out)
            self.assertTrue(Path(target).stat().st_size > 0)


if __name__ == "__main__":
    unittest.main()
