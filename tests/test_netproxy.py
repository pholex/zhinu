"""出网代理：回环强制直连 / 不支持的代理 scheme 降级直连 / 子进程保留原值。

全部走真 HTTP 往返（127.0.0.1 上起假端点与假代理），不打外网：
- 假端点记下自己被打到了几次；
- 假代理是一个 HTTP 转发代理的最小替身——明文 HTTP 经代理时请求行是绝对 URI
  （`GET http://host/path`），收到就记下并直接回 200，从不真的往外转发。
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from xiaoyu import diagnostics, mcp, mcp_guard, messages, netproxy, providers

#  各平台所有可能出现的代理变量写法：用例一律从"一个都没有"起步，
#  开发机上平时开着的代理不能漏进来
_PROXY_VARS = (
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
    "http_proxy", "https_proxy", "all_proxy", "no_proxy", "REQUEST_METHOD",
)


def proxy_env(**values: str):
    """清掉全部代理变量后再按需设置（其余环境原样保留）。"""
    cleared = {key: value for key, value in os.environ.items() if key not in _PROXY_VARS}
    return mock.patch.dict(os.environ, {**cleared, **values}, clear=True)


class _Recorder(BaseHTTPRequestHandler):
    """假端点 / 假代理共用：记请求行，回一段固定 JSON。"""

    protocol_version = "HTTP/1.1"
    hits: list[str]
    reply: dict

    def log_message(self, *args):  # 别把请求日志打进测试输出
        pass

    def _answer(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        type(self).hits.append(f"{self.command} {self.path}")
        body = json.dumps(type(self).reply).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = do_POST = do_DELETE = _answer


def serve(testcase: unittest.TestCase, reply: dict) -> tuple[str, list[str]]:
    """起一个 127.0.0.1 上的记录型 HTTP server，返回 (http://127.0.0.1:port, hits)。"""
    handler = type("Handler", (_Recorder,), {"hits": [], "reply": reply})
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    def stop() -> None:
        httpd.shutdown()
        httpd.server_close()

    testcase.addCleanup(stop)
    return f"http://127.0.0.1:{httpd.server_address[1]}", handler.hits


_MODELS = {"object": "list", "data": [{"id": "m1", "object": "model", "created": 0, "owned_by": "t"}]}


class LoopbackBypassTest(unittest.TestCase):
    """设了代理，小羽自己连本机端点也不能被绕进代理。"""

    def setUp(self) -> None:
        self.endpoint, self.endpoint_hits = serve(self, _MODELS)
        self.proxy, self.proxy_hits = serve(self, {**_MODELS, "vulns": []})
        env = proxy_env(HTTP_PROXY=self.proxy, HTTPS_PROXY=self.proxy, ALL_PROXY=self.proxy)
        env.start()
        self.addCleanup(env.stop)

    def test_openai_client_on_loopback_goes_direct(self) -> None:
        #  XIAOYU_PROVIDER_<NAME>_MODELS=auto 的探测走的就是 OpenAI SDK
        got = providers._discover_models(f"{self.endpoint}/v1", "local", "T")
        self.assertEqual(got, ("m1",))
        self.assertEqual(self.proxy_hits, [])
        self.assertEqual(self.endpoint_hits, ["GET /v1/models"])

    def test_registry_client_on_localhost_goes_direct(self) -> None:
        base = self.endpoint.replace("127.0.0.1", "localhost") + "/v1"
        registry = providers.Registry([providers.Provider("local", base, "k", ("m1",), "local")])
        page = registry.client("local").models.list()
        self.assertEqual([m.id for m in page], ["m1"])
        self.assertEqual(self.proxy_hits, [])

    def test_anthropic_client_on_loopback_goes_direct(self) -> None:
        client = messages.client(f"{self.endpoint}/v1", "k", 5.0)
        response = client._client.get(f"{self.endpoint}/v1/models")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.proxy_hits, [])

    def test_mcp_http_channel_on_loopback_goes_direct(self) -> None:
        channel = mcp._HttpChannel(mcp.ServerSpec(name="r", command="", url=f"{self.endpoint}/mcp"))
        channel.post({"jsonrpc": "2.0", "id": 1, "method": "ping"}, timeout=5.0)
        self.assertEqual(self.endpoint_hits, ["POST /mcp"])
        self.assertEqual(self.proxy_hits, [])

    def test_remote_host_still_goes_through_proxy(self) -> None:
        #  回环绕行不能把正常代理一起绕掉：远端地址照走代理（假代理直接应答，不出网）
        registry = providers.Registry(
            [providers.Provider("remote", "http://models.example.invalid/v1", "k", ("m1",), "r")]
        )
        page = registry.client("remote").models.list()
        self.assertEqual([m.id for m in page], ["m1"])
        self.assertEqual(self.proxy_hits, ["GET http://models.example.invalid/v1/models"])

    def test_urllib_remote_host_still_goes_through_proxy(self) -> None:
        mcp_guard._osv_cache.clear()
        with mock.patch.dict(os.environ, {"XIAOYU_OSV_ENDPOINT": "http://osv.example.invalid/v1/query"}):
            self.assertIsNone(mcp_guard.osv_malware_check("npx", ["-y", "some-pkg"]))
        self.assertEqual(self.proxy_hits, ["POST http://osv.example.invalid/v1/query"])


class UnsupportedSchemeTest(unittest.TestCase):
    """不支持 / 缺依赖的代理 scheme：不抛 traceback，诊断一次，该变量对自身客户端不生效。"""

    def setUp(self) -> None:
        self.endpoint, self.endpoint_hits = serve(self, _MODELS)
        #  不管测试机装没装 socksio，一律按"没装"跑
        blocker = mock.patch.dict(sys.modules, {"socksio": None})
        blocker.start()
        self.addCleanup(blocker.stop)

    def test_socks5_without_socksio_degrades_with_one_notice(self) -> None:
        err = io.StringIO()
        with proxy_env(ALL_PROXY="socks5://127.0.0.1:1"), contextlib.redirect_stderr(err):
            for _ in range(2):
                registry = providers.Registry(
                    [providers.Provider("local", f"{self.endpoint}/v1", "k", ("m1",), "l")]
                )
                self.assertEqual([m.id for m in registry.client("local").models.list()], ["m1"])
            #  远端 provider 构造也不能炸（不发请求，不打外网）
            providers.Registry(
                [providers.Provider("remote", "https://api.example.invalid/v1", "k", ("m",), "r")]
            ).client("remote")
            messages.client("https://api.example.invalid/v1", "k", 5.0)
        text = err.getvalue()
        self.assertEqual(text.count("ALL_PROXY"), 1, text)
        self.assertIn('pip install "httpx[socks]"', text)

    def test_socks4_is_rejected_without_value_error(self) -> None:
        err = io.StringIO()
        with proxy_env(HTTPS_PROXY="socks4://127.0.0.1:2"), contextlib.redirect_stderr(err):
            messages.client("https://api.example.invalid/v1", "k", 5.0)
            providers.Registry(
                [providers.Provider("remote", "https://api.example.invalid/v1", "k", ("m",), "r")]
            ).client("remote")
        text = err.getvalue()
        self.assertIn("HTTPS_PROXY", text)
        self.assertIn("SOCKS4", text)

    def test_subprocess_env_keeps_user_values(self) -> None:
        with proxy_env(ALL_PROXY="socks5://127.0.0.1:3", HTTPS_PROXY="socks4://127.0.0.1:4"), \
                contextlib.redirect_stderr(io.StringIO()):
            providers.Registry(
                [providers.Provider("local", f"{self.endpoint}/v1", "k", ("m1",), "l")]
            ).client("local").models.list()
            self.assertEqual(os.environ["ALL_PROXY"], "socks5://127.0.0.1:3")
            child = subprocess.run(
                [sys.executable, "-c", "import os;print(os.environ['ALL_PROXY'], os.environ['HTTPS_PROXY'])"],
                capture_output=True, text=True, encoding="utf-8", check=True,
            )
        self.assertEqual(child.stdout.split(), ["socks5://127.0.0.1:3", "socks4://127.0.0.1:4"])


class PlanTest(unittest.TestCase):
    """策略本身：纯函数 build_plan，不碰真实环境与系统设置。"""

    def plan(self, env: dict[str, str], *, socks_ok: bool = False, system=None):
        return netproxy.build_plan(env, socks_ok=socks_ok, system=system or {})

    def test_loopback_hosts_always_bypass(self) -> None:
        plan = self.plan({"ALL_PROXY": "http://p:1"})
        for host in ("127.0.0.1", "127.8.9.10", "::1", "[::1]", "localhost", "LOCALHOST",
                     "api.localhost", "0.0.0.0", "::ffff:127.0.0.1"):
            self.assertIsNone(plan.entry_for("http", host), host)
        for host in ("localhost.example.com", "10.0.0.1", "api.example.com"):
            self.assertIsNotNone(plan.entry_for("https", host), host)

    def test_no_proxy_semantics(self) -> None:
        plan = self.plan({
            "HTTPS_PROXY": "http://p:1",
            "NO_PROXY": ".corp.example, example.org, 10.0.0.0/8, 192.168.1.5, intra:8443",
        })
        direct = [("a.corp.example", None), ("corp.example", None), ("example.org", None),
                  ("www.example.org", None), ("10.2.3.4", None), ("192.168.1.5", None),
                  ("intra", 8443)]
        for host, port in direct:
            self.assertIsNone(plan.entry_for("https", host, port), host)
        for host, port in [("badexample.org", None), ("192.168.1.6", None), ("intra", 443)]:
            self.assertIsNotNone(plan.entry_for("https", host, port), host)
        star = self.plan({"ALL_PROXY": "http://p:1", "NO_PROXY": "*"})
        self.assertIsNone(star.entry_for("https", "x.com"))

    def test_specific_scheme_wins_over_all_proxy(self) -> None:
        plan = self.plan({"HTTPS_PROXY": "http://s:1", "ALL_PROXY": "http://a:2"})
        self.assertEqual(plan.entry_for("https", "x.com").var, "HTTPS_PROXY")
        self.assertEqual(plan.entry_for("http", "x.com").var, "ALL_PROXY")

    def test_bare_host_port_means_http_proxy(self) -> None:
        entry = self.plan({"HTTPS_PROXY": "127.0.0.1:7890"}).entry_for("https", "x.com")
        self.assertEqual(entry.url, "http://127.0.0.1:7890")

    @unittest.skipIf(os.name == "nt", "Windows 环境变量大小写不敏感，两种写法无法并存")
    def test_lowercase_variable_wins_like_stdlib(self) -> None:
        plan = self.plan({"HTTPS_PROXY": "http://upper:1", "https_proxy": "http://lower:2"})
        self.assertEqual(plan.entry_for("https", "x.com").url, "http://lower:2")
        #  小写空值 = 显式取消
        cancelled = self.plan({"HTTPS_PROXY": "http://u:1", "https_proxy": ""})
        self.assertIsNone(cancelled.entry_for("https", "x.com"))

    def test_lowercase_only_is_recognized_and_named(self) -> None:
        plan = self.plan({"https_proxy": "socks4://h:1"})
        self.assertEqual([item.var for item in plan.rejected], ["https_proxy"])

    def test_cgi_environment_ignores_uppercase_http_proxy(self) -> None:
        plan = self.plan({"REQUEST_METHOD": "GET", "HTTP_PROXY": "http://injected:1"})
        self.assertIsNone(plan.entry_for("http", "x.com"))

    def test_rejected_variable_falls_back_to_remaining_ones(self) -> None:
        plan = self.plan({"HTTPS_PROXY": "socks4://h:1", "ALL_PROXY": "http://a:2"})
        self.assertEqual(plan.entry_for("https", "x.com").var, "ALL_PROXY")
        only = self.plan({"ALL_PROXY": "socks5://h:1"})
        self.assertIsNone(only.entry_for("https", "x.com"), "唯一的代理变量不生效 = 直连")
        with_socks = self.plan({"ALL_PROXY": "socks5://h:1"}, socks_ok=True)
        self.assertIsNotNone(with_socks.entry_for("https", "x.com"))

    def test_unknown_scheme_and_garbage_are_rejected(self) -> None:
        plan = self.plan({"HTTP_PROXY": "ftp://h:1", "HTTPS_PROXY": "http://:bad"})
        self.assertEqual(sorted(item.var for item in plan.rejected), ["HTTPS_PROXY", "HTTP_PROXY"])
        self.assertEqual(plan.entries, ())

    def test_system_settings_only_without_env_variables(self) -> None:
        system = {"https": "sys:8080"}
        plan = self.plan({}, system=system)
        self.assertEqual(plan.source, "system")
        self.assertEqual(plan.entry_for("https", "x.com").url, "http://sys:8080")
        self.assertIsNone(plan.entry_for("https", "127.0.0.1"))
        self.assertEqual(self.plan({"HTTP_PROXY": "http://env:1"}, system=system).source, "env")

    def test_urllib_gets_only_http_proxies(self) -> None:
        plan = self.plan({"HTTPS_PROXY": "socks5://s:1", "ALL_PROXY": "http://a:2"}, socks_ok=True)
        self.assertEqual(plan.urllib_proxies(), {"http": "http://a:2"})

    def test_credentials_never_shown(self) -> None:
        plan = self.plan({"HTTPS_PROXY": "socks4://user:s3cret@h:1", "ALL_PROXY": "http://u:pw@a:2"})
        text = "\n".join([*netproxy.describe(plan), *(item.notice() for item in plan.rejected)])
        self.assertNotIn("s3cret", text)
        self.assertNotIn(":pw@", text)
        self.assertIn("***@h:1", text)


class DoctorProxyTest(unittest.TestCase):
    def setUp(self) -> None:
        #  系统代理设置因机器而异，一律当没有
        patcher = mock.patch.object(netproxy, "_system_proxies", return_value={})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_warns_on_rejected_variable_without_stderr_noise(self) -> None:
        with proxy_env(ALL_PROXY="socks4://127.0.0.1:5"), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            check = diagnostics.check_proxy()
        self.assertEqual(check.status, "warn")
        self.assertIn("ALL_PROXY", check.summary)
        self.assertTrue(check.remedy)
        self.assertEqual(err.getvalue(), "", "doctor 自己就是诊断面，不再往 stderr 重复")

    def test_ok_states(self) -> None:
        with proxy_env(HTTPS_PROXY="http://127.0.0.1:6"):
            check = diagnostics.check_proxy()
        self.assertEqual(check.status, "ok")
        self.assertTrue(any("HTTPS_PROXY" in line for line in check.details))
        self.assertTrue(any("回环" in line for line in check.details))
        with proxy_env():
            self.assertEqual(diagnostics.check_proxy().summary, "未配置代理（直连）")

    def test_run_doctor_includes_proxy_check(self) -> None:
        sentinel = diagnostics.Check("proxy", "ok", "桩")
        with mock.patch.object(diagnostics, "check_proxy", return_value=sentinel):
            checks = diagnostics.run_doctor(Path.cwd())
        self.assertIn(sentinel, checks)


if __name__ == "__main__":
    unittest.main()
