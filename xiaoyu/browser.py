"""浏览器工具（Playwright 引擎）。

给模型一双能碰网页的手：打开页面、读快照、点击、填表。设计取舍：

- **单工具 + action 参数**，不拆成七八个工具——工具 schema 每轮随请求发送，
  光是挂着就要付 token（explore 实验实测 +5%/工具），browser 这种低频能力
  必须收敛成一个入口。
- **纯文本交互**：定位靠 aria 快照（YAML，交互元素带 ref），不依赖视觉模型——
  国产便宜主模型都能用。screenshot 只存文件给人看，不回灌模型。
- **playwright 是可选依赖**（extra `[browser]`）：核心依赖极简的原则不破，
  没装则 check_fn 探测不过、工具不进 schemas，模型根本看不见它。
- **两种模式**：默认无头启动独立 Chromium（干净环境、无登录态，适合抓公开页）；
  设 `XIAOYU_BROWSER_CDP=http://127.0.0.1:9222` 则接管本机已登录的 Chrome
  （Chrome 需以 `--remote-debugging-port=9222` 启动）——操作需要登录态的
  控制台类页面（PyPI/GitHub 设置页）只能走这条路。接管模式下只新开标签页、
  绝不动用户已有的标签，关闭时也只收走自己开的。

会话是进程级单例、惰性启动：不用不付启动成本，用了跨多次调用保持状态
（登录、导航历史都在），atexit 兜底关闭。

⚠️ **所有 playwright 调用都必须走专用工作线程**：sync API 在调用线程里挂一个
常驻 asyncio loop（greenlet 驱动，会话存续期间 running 标志一直在），若跑在
主线程，TUI 的 prompt_toolkit（内部 `asyncio.run()`）会立刻炸
"cannot be called from a running event loop"——模型开过一次浏览器，确认框就废了。
单线程 executor 同时满足 playwright 的线程亲和要求（对象只能在创建线程使用）。

安全边界：browser 工具 requires_approval=True，每次动作过人工确认——
它能以用户身份点任何按钮，审批就是它的沙箱。
"""

from __future__ import annotations

import atexit
import importlib.util
import os
from concurrent.futures import ThreadPoolExecutor
from typing import Any

#  单次返回的快照/正文粗截上限。Toolbox 层还有统一的保头保尾截断兜底，
#  这里先截是为了别让一张巨型页面把那层的"头尾"都占满。
_TRUNCATE = 40_000

ACTIONS = ("open", "snapshot", "click", "fill", "press", "read", "screenshot", "close")


def available() -> bool:
    """playwright 装了没。check_fn 每次组装 schemas 都调，必须廉价。"""
    return importlib.util.find_spec("playwright") is not None


def _truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


class BrowserSession:
    """惰性启动的浏览器会话。所有方法返回给模型看的文本，错误也是文本。"""

    def __init__(self) -> None:
        self._pw: Any = None
        self._browser: Any = None
        self._page: Any = None
        #  自己 launch 的浏览器关闭时要整个关掉；CDP 接管的只关自己开的页
        self._owns_browser = False
        #  max_workers=1：所有 playwright 操作固定在同一条线程（见模块 docstring）
        self._executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="xiaoyu-browser"
        )

    def _call(self, fn: Any, *args: Any) -> Any:
        """把 playwright 操作提交到工作线程并同步等结果。
        180s 是兜底（playwright 各操作自带更短超时），防工作线程死锁挂死主循环。"""
        return self._executor.submit(fn, *args).result(timeout=180)

    # ---------- 生命周期 ----------

    def _ensure_page(self) -> Any:
        if self._page is not None and not self._page.is_closed():
            return self._page
        from playwright.sync_api import sync_playwright

        if self._pw is None:
            self._pw = sync_playwright().start()
        if self._browser is None or not self._browser.is_connected():
            cdp = os.environ.get("XIAOYU_BROWSER_CDP", "").strip()
            if cdp:
                self._browser = self._pw.chromium.connect_over_cdp(cdp)
                self._owns_browser = False
            else:
                self._browser = self._pw.chromium.launch(
                    headless=not _truthy("XIAOYU_BROWSER_HEADED")
                )
                self._owns_browser = True
        context = (
            self._browser.contexts[0]
            if self._browser.contexts
            else self._browser.new_context()
        )
        self._page = context.new_page()
        return self._page

    def close(self) -> str:
        """关闭会话。CDP 接管模式只关自己开的页，不碰用户的浏览器。"""
        try:
            self._call(self._close_on_worker)
        except Exception:  # noqa: BLE001 - 关闭失败没有下一步可做，静默即可
            pass
        return "浏览器已关闭"

    def _close_on_worker(self) -> None:
        try:
            if self._page is not None and not self._page.is_closed():
                self._page.close()
            if self._browser is not None and self._owns_browser:
                self._browser.close()
            if self._pw is not None:
                self._pw.stop()
        finally:
            #  半关状态也要归零：下次调用从头启动，而不是抱着坏对象重试
            self._pw = self._browser = self._page = None
            self._owns_browser = False

    # ---------- 动作 ----------

    def run(
        self,
        action: str,
        url: str | None = None,
        selector: str | None = None,
        text: str | None = None,
        key: str | None = None,
        path: str | None = None,
    ) -> str:
        if action not in ACTIONS:
            return f"ERROR: 未知 action {action!r}。可用：{', '.join(ACTIONS)}"
        #  参数校验放在启动浏览器之前：参数都没给对，不值得付启动成本
        required = {"open": ("url", url), "click": ("selector", selector),
                    "fill": ("selector", selector), "press": ("key", key),
                    "screenshot": ("path", path)}
        if action in required and not required[action][1]:
            return f"ERROR: action={action} 需要参数 {required[action][0]}"
        if action == "fill" and text is None:
            return "ERROR: action=fill 需要参数 text"
        if action == "close":
            return self.close()
        try:
            return self._call(self._dispatch, action, url, selector, text, key, path)
        except Exception as exc:  # noqa: BLE001 - 浏览器错误回给模型自愈
            return f"ERROR: {type(exc).__name__}: {self._with_hint(exc)}"

    def _dispatch(
        self,
        action: str,
        url: str | None,
        selector: str | None,
        text: str | None,
        key: str | None,
        path: str | None,
    ) -> str:
        page = self._ensure_page()
        if action == "open":
            page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            return self._state(page, f"已打开 {page.url}")
        if action == "snapshot":
            return self._state(page, "当前页面")
        if action == "click":
            page.locator(selector).first.click(timeout=8_000)
            #  点击常触发跳转/DOM 变化，直接带回新快照省一轮往返
            return self._state(page, f"已点击 {selector}")
        if action == "fill":
            page.locator(selector).first.fill(text, timeout=8_000)
            return f"已在 {selector} 填入 {len(text)} 个字符"
        if action == "press":
            page.keyboard.press(key)
            return self._state(page, f"已按 {key}")
        if action == "read":
            return page.inner_text("body")[:_TRUNCATE]
        if action == "screenshot":
            page.screenshot(path=path)
            return f"截图已存到 {os.path.abspath(path)}"
        raise AssertionError(f"未分派的 action: {action}")  # ACTIONS 已校验，到不了这

    # ---------- 输出 ----------

    def _state(self, page: Any, headline: str) -> str:
        return (
            f"{headline}\n标题: {page.title()}\nURL: {page.url}\n\n"
            f"{self._snapshot(page)}"
        )

    def _snapshot(self, page: Any) -> str:
        """aria 快照。新版 playwright 支持 ref=True（交互元素带 [ref=eN]，
        可用 `aria-ref=eN` 选择器直点）；老版本没有该参数则退回纯快照，
        模型改用 role/text 选择器即可。"""
        body = page.locator("body")
        try:
            snap = body.aria_snapshot(ref=True)
        except TypeError:
            snap = body.aria_snapshot()
        return snap[:_TRUNCATE]

    @staticmethod
    def _with_hint(exc: Exception) -> str:
        """给常见环境错误接一句"下一步怎么办"，别让模型对着堆栈空转。"""
        message = str(exc)
        if "Executable doesn't exist" in message or "playwright install" in message:
            message += (
                "\n[提示] 浏览器内核未安装：运行一次 `playwright install chromium`"
                "（一次性下载，之后离线可用）。"
            )
        if "connect_over_cdp" in message or "ECONNREFUSED" in message:
            message += (
                "\n[提示] 接管模式连不上 Chrome：确认 Chrome 以"
                " --remote-debugging-port=9222 启动，且 XIAOYU_BROWSER_CDP 指向该端口。"
            )
        return message


#  进程级单例：会话状态（登录、当前页）要跨多次工具调用存活
_session: BrowserSession | None = None


def session() -> BrowserSession:
    global _session
    if _session is None:
        _session = BrowserSession()
        atexit.register(_session.close)
    return _session
