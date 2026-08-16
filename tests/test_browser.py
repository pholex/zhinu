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

    def test_screenshot_saves_file(self):
        self.session.run("open", url=_PAGE)
        with tempfile.TemporaryDirectory() as tmp:
            target = str(Path(tmp) / "shot.png")
            out = self.session.run("screenshot", path=target)
            self.assertIn("shot.png", out)
            self.assertTrue(Path(target).stat().st_size > 0)


if __name__ == "__main__":
    unittest.main()
