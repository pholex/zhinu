"""HTTP 错误响应用完要关。

urllib 在 4xx / 5xx 时抛出的 HTTPError 本身就是一个开着的响应，手里握着连接。
只接住异常而不关它，连接就要等垃圾回收才释放——回收的时刻不受控，恰好落在
别的代码执行 import 的过程中时，Python 3.11 上会抛出以线程号为内容的 KeyError
（import 锁不可重入），表现为不相干的用例随机变红。

两层防线：逐个调用点验证"错误响应确实被关了"；再扫一遍源码，凡是发请求的函数
都必须接住 HTTPError 并关掉它——新写的调用点漏了，这里当场变红。
"""

from __future__ import annotations

import ast
import contextlib
import io
import unittest
import urllib.error
from email.message import Message
from pathlib import Path
from unittest import mock

from xiaoyu import mcp, mcp_guard, netproxy, update_check

PACKAGE = Path(mcp.__file__).resolve().parent


def http_error(code: int, reason: str = "x") -> urllib.error.HTTPError:
    """一个带着"连接"的错误响应：body 关没关，看 fp.closed。"""
    return urllib.error.HTTPError("http://127.0.0.1:1/x", code, reason, Message(), io.BytesIO(b"{}"))


class CallSiteTest(unittest.TestCase):
    def raising(self, error: urllib.error.HTTPError):
        patcher = mock.patch.object(netproxy, "urlopen", side_effect=error)
        patcher.start()
        self.addCleanup(patcher.stop)
        #  用例结束时无论如何收掉，别让用例自己成了泄漏源
        self.addCleanup(error.close)
        return error

    def channel(self) -> mcp._HttpChannel:
        return mcp._HttpChannel(mcp.ServerSpec(name="r", command="", url="http://127.0.0.1:1/mcp"))

    def test_event_stream_the_server_does_not_offer(self) -> None:
        #  405 是常态：多数 server 不提供这条流，每连一个就走一次这里
        for code in (404, 405, 501):
            with self.subTest(code=code):
                error = self.raising(http_error(code))
                err = io.StringIO()
                with contextlib.redirect_stderr(err):
                    self.channel().open_stream(lambda message: None)
                self.assertTrue(error.fp.closed)
                self.assertEqual(err.getvalue(), "")  # 规范允许不提供，不该出声

    def test_event_stream_that_fails_for_real(self) -> None:
        error = self.raising(http_error(500))
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.channel().open_stream(lambda message: None)
        self.assertTrue(error.fp.closed)
        self.assertIn("HTTP 500", err.getvalue())

    def test_session_delete_the_server_does_not_support(self) -> None:
        channel = self.channel()
        channel.session_id = "s1"
        error = self.raising(http_error(405))
        channel.close()
        self.assertTrue(error.fp.closed)

    def test_session_delete_survives_any_other_failure(self) -> None:
        channel = self.channel()
        channel.session_id = "s1"
        with mock.patch.object(netproxy, "urlopen", side_effect=OSError("断了")):
            channel.close()  # 关会话失败不该拦住关闭

    def test_request_answered_with_an_error(self) -> None:
        error = self.raising(http_error(500, "Internal Server Error"))
        with self.assertRaises(mcp.McpError) as caught:
            self.channel().post({"jsonrpc": "2.0", "id": 1, "method": "ping"}, timeout=1.0)
        self.assertEqual(caught.exception.kind, "server")
        self.assertTrue(error.fp.closed)

    def test_malware_precheck_stays_fail_open(self) -> None:
        mcp_guard._osv_cache.clear()
        self.addCleanup(mcp_guard._osv_cache.clear)
        error = self.raising(http_error(503, "Service Unavailable"))
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            verdict = mcp_guard.osv_malware_check("npx", ["-y", "xiaoyu-test-no-such-package"])
        self.assertIsNone(verdict)  # 预检失败不拦启动
        self.assertIn("预检失败", err.getvalue())
        self.assertTrue(error.fp.closed)

    def test_update_check(self) -> None:
        error = self.raising(http_error(503))
        self.assertIsNone(update_check.fetch_latest(timeout=1.0))
        self.assertTrue(error.fp.closed)


def _mentions_http_error(node: ast.AST | None) -> bool:
    return node is not None and any(
        isinstance(item, (ast.Name, ast.Attribute))
        and getattr(item, "attr", getattr(item, "id", "")) == "HTTPError"
        for item in ast.walk(node)
    )


def _error_names(function: ast.AST) -> set[str]:
    """函数里被认出是 HTTPError 的变量名：`except HTTPError as e` 与
    `isinstance(e, HTTPError)` 两种写法都算。"""
    names: set[str] = set()
    for node in ast.walk(function):
        if isinstance(node, ast.ExceptHandler) and node.name and _mentions_http_error(node.type):
            names.add(node.name)
        if (
            isinstance(node, ast.Call)
            and getattr(node.func, "id", "") == "isinstance"
            and len(node.args) == 2
            and isinstance(node.args[0], ast.Name)
            and _mentions_http_error(node.args[1])
        ):
            names.add(node.args[0].id)
    return names


def _disposed(function: ast.AST, name: str) -> bool:
    """这个错误响应有没有被处置：自己 `.close()`，或交给别的函数去关。"""
    for node in ast.walk(function):
        if not isinstance(node, ast.Call):
            continue
        target = node.func
        if (
            isinstance(target, ast.Attribute)
            and target.attr == "close"
            and isinstance(target.value, ast.Name)
            and target.value.id == name
        ):
            return True
        if any(isinstance(arg, ast.Name) and arg.id == name for arg in node.args):
            #  isinstance(e, …) 只是认类型，不算交出去
            if getattr(target, "id", "") != "isinstance":
                return True
    return False


def problems_in(source: str, where: str) -> list[str]:
    found: list[str] = []
    for function in ast.walk(ast.parse(source)):
        if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        sends = [
            node for node in ast.walk(function)
            if isinstance(node, ast.Call)
            and getattr(node.func, "attr", getattr(node.func, "id", "")) == "urlopen"
        ]
        if not sends:
            continue
        names = _error_names(function)
        if not names:
            found.append(f"{where}:{function.lineno} {function.name}() 发了请求却没接 HTTPError")
        for name in sorted(names):
            if not _disposed(function, name):
                found.append(f"{where}:{function.lineno} {function.name}() 接住了 {name} 却没关")
    return found


class SentinelTest(unittest.TestCase):
    def test_every_request_site_closes_its_error_response(self) -> None:
        sites = 0
        problems: list[str] = []
        for path in sorted(PACKAGE.glob("*.py")):
            source = path.read_text(encoding="utf-8")
            sites += source.count("urlopen(")
            problems += problems_in(source, path.name)
        #  一处都没扫到 = 判据失效（调用方式变了），不能当成全部通过
        self.assertGreaterEqual(sites, 5, "没扫到发请求的调用点——检查判据")
        self.assertEqual(problems, [])

    def test_sentinel_catches_the_mistakes_it_is_there_for(self) -> None:
        """哨兵自己要经得起验证：把修之前的三种写法喂给它，必须都报出来。"""
        swallowed = '''
def open_stream(self):
    try:
        stream = netproxy.urlopen(request)
    except urllib.error.HTTPError as exc:
        if exc.code == 405:
            return
'''
        suppressed = '''
def close(self):
    with contextlib.suppress(Exception):
        netproxy.urlopen(request).close()
'''
        broad = '''
def check():
    try:
        with netproxy.urlopen(request) as response:
            body = response.read()
    except Exception as exc:
        print(exc)
'''
        only_recognised = '''
def check():
    try:
        netproxy.urlopen(request)
    except Exception as exc:
        if isinstance(exc, urllib.error.HTTPError):
            print("http")
'''
        for label, source in (("吞掉", swallowed), ("suppress", suppressed),
                              ("宽接", broad), ("只认不关", only_recognised)):
            with self.subTest(case=label):
                self.assertTrue(problems_in(source, "sample.py"), "该报未报")

    def test_sentinel_accepts_the_correct_shapes(self) -> None:
        closed = '''
def fetch():
    try:
        with netproxy.urlopen(request) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        exc.close()
        return None
'''
        delegated = '''
def post(self):
    try:
        response = netproxy.urlopen(request)
    except urllib.error.HTTPError as exc:
        raise self._http_error(exc) from exc
'''
        recognised_then_closed = '''
def check():
    try:
        netproxy.urlopen(request)
    except Exception as exc:
        if isinstance(exc, urllib.error.HTTPError):
            exc.close()
'''
        no_request = '''
def helper():
    try:
        work()
    except Exception:
        pass
'''
        for label, source in (("自己关", closed), ("交出去", delegated),
                              ("认出后关", recognised_then_closed), ("不发请求", no_request)):
            with self.subTest(case=label):
                self.assertEqual(problems_in(source, "sample.py"), [])


if __name__ == "__main__":
    unittest.main()
