"""出网代理：小羽**自身** HTTP 客户端的代理策略，全仓只此一处。

为什么不交给 httpx / urllib 各自读环境变量（它们默认就读）：
1. **回环地址会被绕进代理**。两家都只认 NO_PROXY，用户开着 HTTPS_PROXY 又没配
   NO_PROXY 时，连 localhost:8000 的本机模型端点也发给代理——代理多半不转发回环，
   表现是本机端点"时通时不通"，最难查。小羽连回环一律直连，不看 NO_PROXY。
2. **不支持 / 缺依赖的 scheme 直接炸 traceback**。httpx 在构造 client 时就解析代理：
   socks5 没装 socksio 抛 ImportError、socks4 抛 ValueError——第一次调模型就崩，
   而 curl、git 这些工具明明认这个变量。这里把它降级成一条人话诊断（只打一次），
   该变量对小羽自身客户端视同未设置，其余照常。
3. **urllib 的默认 opener 在首次 urlopen 时就把代理配置冻住**，之后改环境不生效；
   且它不认 ALL_PROXY、不认 SOCKS。统一从这里取 opener，与 httpx 一路同一份判定。

边界（刻意不做的）：
- **不改写 os.environ**。bash 工具、MCP stdio server 等子进程继承用户原值——
  它们里面跑的 curl / npm / pip 有自己的代理实现，socks4 在那里可能正好是对的。
- 变量读取语义与标准库 getproxies_environment 保持一致：小写优先于大写、CGI 环境
  （有 REQUEST_METHOD）忽略大写 HTTP_PROXY（httpoxy）。Windows 的环境变量本就
  大小写不敏感，同一套逻辑两边都对。
- 环境里一个代理变量都没有时，退回标准库的系统代理设置（macOS 网络偏好 /
  Windows 注册表）——这是 httpx / urllib 原本的行为，不能因为收口而丢掉。
"""

from __future__ import annotations

import functools
import ipaddress
import os
import sys
import threading
import urllib.request
from dataclasses import dataclass
from typing import Any, Mapping
from urllib.parse import urlsplit

import httpx

#  httpx 认的代理 scheme；socks5 系还要 socksio
_HTTP_SCHEMES = ("http", "https")
_SOCKS5_SCHEMES = ("socks5", "socks5h")
#  只关心这几种：别的 *_PROXY（ftp_proxy、TRAVIS_APT_PROXY…）与小羽的出网无关
_KINDS = ("http", "https", "all", "no")
_SYSTEM_LABEL = "系统代理设置"
SOCKS_REMEDY = 'pip install "httpx[socks]"'


@dataclass(frozen=True)
class Entry:
    """一条生效的代理：作用于哪类请求（http / https / all）、出自哪个变量。"""

    kind: str
    var: str
    #  已补全 scheme（`127.0.0.1:7890` → `http://127.0.0.1:7890`），可能含凭据——
    #  给人看一律走 shown
    url: str

    @property
    def scheme(self) -> str:
        return urlsplit(self.url).scheme.lower()

    @property
    def shown(self) -> str:
        return redact(self.url)


@dataclass(frozen=True)
class Rejected:
    """一条没生效的代理变量：为什么、怎么修。url 已脱敏。"""

    var: str
    url: str
    reason: str
    remedy: str

    def notice(self) -> str:
        return (
            f"[代理变量 {self.var}={self.url} 对小羽自身的网络请求不生效：{self.reason}。"
            f"按未设置处理（子进程仍继承原值）。修复：{self.remedy}]"
        )


@dataclass(frozen=True)
class ProxyPlan:
    """一次解析的结果。不可变：环境变了就整份重建（见 current）。"""

    entries: tuple[Entry, ...] = ()
    rejected: tuple[Rejected, ...] = ()
    no_proxy: tuple[str, ...] = ()
    #  "env" / "system" / ""（没有任何代理）
    source: str = ""

    def entry_for(self, scheme: str, host: str, port: int | None = None) -> Entry | None:
        """这次请求走哪条代理；None = 直连。具体 scheme 的变量优先于 ALL_PROXY。"""
        if self.bypass(host, port):
            return None
        by_kind = {entry.kind: entry for entry in self.entries}
        return by_kind.get(scheme.lower()) or by_kind.get("all")

    def bypass(self, host: str, port: int | None = None) -> bool:
        return is_loopback(host) or _no_proxy_matches(self.no_proxy, host, port)

    def urllib_proxies(self) -> dict[str, str]:
        """urllib 能用的那部分：只有 http(s) 代理（它不会说 SOCKS），ALL_PROXY 兜底。"""
        found: dict[str, str] = {}
        by_kind = {entry.kind: entry for entry in self.entries}
        for scheme in _HTTP_SCHEMES:
            entry = by_kind.get(scheme) or by_kind.get("all")
            if entry is not None and entry.scheme in _HTTP_SCHEMES:
                found[scheme] = entry.url
        return found

    @functools.cached_property
    def opener(self) -> urllib.request.OpenerDirector:
        #  frozen dataclass 上 cached_property 照样可用（它直写实例 __dict__）
        return urllib.request.build_opener(_ProxyHandler(self))


# ---------- 解析 ----------


def redact(url: str) -> str:
    """去掉 URL 里的凭据：代理地址常带 user:pass，诊断和 doctor 里绝不回显。"""
    try:
        parts = urlsplit(url)
    except ValueError:
        return "<无法解析的地址>"
    if "@" not in parts.netloc:
        return url
    return parts._replace(netloc="***@" + parts.netloc.rsplit("@", 1)[1]).geturl()


def _env_proxies(environ: Mapping[str, str]) -> dict[str, tuple[str, str]]:
    """kind → (原始变量名, 值)。逐行对照标准库 getproxies_environment 的语义。"""
    found: dict[str, tuple[str, str]] = {}
    #  第一遍不分大小写收
    for name, value in environ.items():
        lower = name.lower()
        if value and lower.endswith("_proxy") and lower[:-6] in _KINDS:
            found[lower[:-6]] = (name, value)
    #  CGI 下 HTTP_PROXY 可能是请求头 `Proxy:` 注入进来的（httpoxy），不信
    if "REQUEST_METHOD" in environ:
        found.pop("http", None)
    #  第二遍只看小写后缀的写法：它优先，且空值等于显式取消
    for name, value in environ.items():
        if name.endswith("_proxy") and name.lower()[:-6] in _KINDS:
            kind = name.lower()[:-6]
            if value:
                found[kind] = (name, value)
            else:
                found.pop(kind, None)
    return found


def _system_proxies() -> dict[str, str]:
    """系统级代理设置（macOS 网络偏好 / Windows 注册表）。拿不到就当没有。"""
    try:
        return dict(urllib.request.getproxies())
    except Exception:  # noqa: BLE001 - 系统配置读取失败不该拦启动
        return {}


def socks_available() -> bool:
    try:
        import socksio  # noqa: F401
    except ImportError:
        return False
    return True


def _check(kind: str, var: str, raw: str, socks_ok: bool) -> Entry | Rejected:
    url = raw.strip()
    if "://" not in url:
        url = "http://" + url  # 与 curl / httpx / urllib 同一约定：裸 host:port 当 http
    shown = redact(url)
    try:
        parts = urlsplit(url)
        parts.port  # 端口非数字在这里才抛
        host = parts.hostname
    except ValueError:
        host = None
    if not host:
        return Rejected(var, shown, "地址解析不出主机名", f"检查 {var} 的写法，形如 http://127.0.0.1:7890")
    scheme = parts.scheme.lower()
    if scheme in _SOCKS5_SCHEMES and not socks_ok:
        return Rejected(var, shown, "SOCKS5 代理需要 socksio 依赖，当前环境没装", SOCKS_REMEDY)
    if scheme in ("socks4", "socks4a"):
        return Rejected(
            var, shown, "小羽的 HTTP 客户端（httpx）不支持 SOCKS4",
            "改用 socks5:// 或 http:// 代理地址（多数本地代理软件同端口都支持）",
        )
    if scheme not in _HTTP_SCHEMES + _SOCKS5_SCHEMES:
        return Rejected(
            var, shown, f"不支持的代理协议 {scheme}://",
            "改用 http://、https:// 或 socks5:// 代理地址",
        )
    try:
        httpx.Proxy(url)  # 让 httpx 自己再验一遍，构造 client 时就不会再有意外
    except Exception as exc:  # noqa: BLE001 - 任何解析异常都降级成诊断
        return Rejected(var, shown, f"httpx 拒绝这个地址（{exc}）", f"检查 {var} 的写法")
    return Entry(kind, var, url)


def build_plan(
    environ: Mapping[str, str],
    *,
    socks_ok: bool,
    system: Mapping[str, str] | None = None,
) -> ProxyPlan:
    """纯函数：环境 → 策略。system=None 表示需要时去读系统设置。"""
    raw = _env_proxies(environ)
    source = "env"
    if not raw:
        system = _system_proxies() if system is None else system
        raw = {
            kind: (_SYSTEM_LABEL, value)
            for kind, value in system.items()
            if kind in _KINDS and value
        }
        source = "system"
    entries: list[Entry] = []
    rejected: list[Rejected] = []
    for kind in ("http", "https", "all"):
        if kind not in raw:
            continue
        var, value = raw[kind]
        verdict = _check(kind, var, value, socks_ok)
        (entries if isinstance(verdict, Entry) else rejected).append(verdict)  # type: ignore[arg-type]
    no_proxy = tuple(
        item.strip().lower() for item in raw.get("no", ("", ""))[1].split(",") if item.strip()
    )
    if not entries and not rejected:
        source = ""
    return ProxyPlan(tuple(entries), tuple(rejected), no_proxy, source)


_lock = threading.Lock()
_cache: tuple[Any, ProxyPlan] | None = None
_announced: set[tuple[str, str]] = set()


def current(*, announce: bool = True) -> ProxyPlan:
    """当前环境下的策略。按"代理相关变量 + socksio 有无"缓存：同一环境只解析一次，
    嵌入宿主或测试中途改了环境则自动重建。announce：把未生效的变量往 stderr
    各说一次（整个进程生命周期内同一变量同一取值只说一次）。"""
    global _cache
    socks_ok = socks_available()
    key = (
        tuple(
            sorted(
                (name, value)
                for name, value in os.environ.items()
                if name.lower().endswith("_proxy") or name == "REQUEST_METHOD"
            )
        ),
        socks_ok,
    )
    with _lock:
        if _cache is not None and _cache[0] == key:
            plan = _cache[1]
        else:
            plan = build_plan(os.environ, socks_ok=socks_ok)
            _cache = (key, plan)
        fresh = []
        if announce:
            for item in plan.rejected:
                if (item.var, item.url) not in _announced:
                    _announced.add((item.var, item.url))
                    fresh.append(item)
    for item in fresh:
        print(item.notice(), file=sys.stderr)
    return plan


# ---------- 回环与 NO_PROXY ----------


def _bare_host(host: str) -> str:
    return host.strip().strip("[]").split("%", 1)[0].rstrip(".").lower()


def is_loopback(host: str) -> bool:
    """本机地址：localhost / *.localhost / 127.0.0.0/8 / ::1 / 0.0.0.0 / ::（含 v4 映射）。"""
    host = _bare_host(host)
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    #  0.0.0.0 / :: 作为目的地址时各平台都落到本机，与 providers.is_local_endpoint 同口径
    return address.is_loopback or address.is_unspecified


def _no_proxy_matches(patterns: tuple[str, ...], host: str, port: int | None) -> bool:
    """curl 口径的 NO_PROXY：`*` 全部；域名匹配自身及子域（前导点可有可无）；
    IP 精确匹配；CIDR 网段；可带 `:端口` 限定。"""
    host = _bare_host(host)
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    for pattern in patterns:
        if pattern == "*":
            return True
        pattern = pattern.split("://", 1)[-1]
        want_port = ""
        if pattern.startswith("["):  # [::1]:8080
            name, _, rest = pattern[1:].partition("]")
            want_port = rest.lstrip(":")
        elif pattern.count(":") == 1:  # host:port（裸 IPv6 冒号不止一个）
            name, _, want_port = pattern.partition(":")
        else:
            name = pattern
        if want_port and port is not None and want_port != str(port):
            continue
        name = name.lstrip("*").rstrip(".")
        if "/" in name:
            try:
                if address is not None and address in ipaddress.ip_network(name, strict=False):
                    return True
            except ValueError:
                pass
            continue
        try:
            literal = ipaddress.ip_address(name)
        except ValueError:
            literal = None
        if literal is not None:  # IP 条目只做精确匹配，不走域名后缀
            if address == literal:
                return True
            continue
        name = name.lstrip(".")
        if name and (host == name or host.endswith("." + name)):
            return True
    return False


# ---------- httpx（openai / anthropic SDK） ----------


class _RoutingTransport(httpx.BaseTransport):
    """按请求逐条判定直连还是走哪条代理。

    为什么不用 httpx 的 mounts：URLPattern 表达不了 127.0.0.0/8、CIDR 形式的
    NO_PROXY，两套判定写两遍迟早分叉；而且只要 client 不传 transport，httpx
    （以及 anthropic SDK 的默认 client）就会先按环境变量把代理 transport 建出来——
    正是那一步对 socks4 / 缺 socksio 抛异常。显式传 transport 让它们完全不碰
    环境里的代理变量；trust_env 仍保持默认开（SSL_CERT_FILE / SSL_CERT_DIR 照认）。

    代理 transport 首次用到才建：每个 HTTPTransport 都要建一份 SSLContext，
    没被路由到的代理不付这份成本。
    """

    def __init__(self, plan: ProxyPlan, **transport_kwargs: Any) -> None:
        self._plan = plan
        self._kwargs = transport_kwargs
        self._direct = httpx.HTTPTransport(**transport_kwargs)
        self._proxied: dict[str, httpx.HTTPTransport] = {}
        self._lock = threading.Lock()

    def _transport_for(self, url: httpx.URL) -> httpx.BaseTransport:
        entry = self._plan.entry_for(url.scheme, url.host, url.port)
        if entry is None:
            return self._direct
        with self._lock:
            if entry.url not in self._proxied:
                self._proxied[entry.url] = httpx.HTTPTransport(
                    proxy=httpx.Proxy(entry.url), **self._kwargs
                )
            return self._proxied[entry.url]

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        return self._transport_for(request.url).handle_request(request)

    def close(self) -> None:
        self._direct.close()
        with self._lock:
            proxied, self._proxied = list(self._proxied.values()), {}
        for transport in proxied:
            transport.close()


def _keepalive_socket_options() -> list[tuple[int, int, int | bool]]:
    """与 anthropic SDK 自建 client 同一套 TCP keepalive：显式传 transport 后
    SDK 不再替我们设，长时间无数据的流式响应经 NAT / 负载均衡会被静默掐断。"""
    import socket

    options: list[tuple[int, int, int | bool]] = [(socket.SOL_SOCKET, socket.SO_KEEPALIVE, True)]
    if (interval := getattr(socket, "TCP_KEEPINTVL", None)) is not None:
        options.append((socket.IPPROTO_TCP, interval, 60))
    elif sys.platform == "darwin":
        options.append((socket.IPPROTO_TCP, getattr(socket, "TCP_KEEPALIVE", 0x10), 60))
    if (count := getattr(socket, "TCP_KEEPCNT", None)) is not None:
        options.append((socket.IPPROTO_TCP, count, 5))
    if (idle := getattr(socket, "TCP_KEEPIDLE", None)) is not None:
        options.append((socket.IPPROTO_TCP, idle, 60))
    return options


def http_client(sdk: str = "openai") -> httpx.Client:
    """给 openai / anthropic SDK 传 `http_client=` 用的 httpx client。

    基类用 SDK 自己导出的 DefaultHttpxClient：超时、连接数上限、跟随重定向
    与 SDK 自建时一致。base_url / timeout / max_retries 仍由 SDK 构造参数决定
    （SDK 每个请求都显式带自己的 timeout，client 上的只是兜底）。
    """
    if sdk == "anthropic":
        import anthropic as module

        extra: dict[str, Any] = {"socket_options": _keepalive_socket_options()}
    else:
        import openai as module

        extra = {}
    transport = _RoutingTransport(current(), limits=module.DEFAULT_CONNECTION_LIMITS, **extra)
    return module.DefaultHttpxClient(transport=transport)


# ---------- urllib（MCP HTTP 传输、OSV 预检） ----------


class _ProxyHandler(urllib.request.ProxyHandler):
    """代理表来自 ProxyPlan；回环与 NO_PROXY 在交给标准库之前先拦下。"""

    def __init__(self, plan: ProxyPlan) -> None:
        self._plan = plan
        #  显式传 dict（哪怕是空的）：传 None 标准库会自己再读一遍环境
        super().__init__(plan.urllib_proxies())

    def proxy_open(self, req: Any, proxy: str, type: str) -> Any:  # noqa: A002 - 标准库签名
        try:
            parts = urlsplit("//" + req.host)
            host, port = parts.hostname or "", parts.port
        except ValueError:
            host, port = req.host, None
        if self._plan.bypass(host, port):
            return None  # 返回 None = 本 handler 不处理，落到普通直连
        return super().proxy_open(req, proxy, type)


def urlopen(request: urllib.request.Request | str, timeout: float | None = None) -> Any:
    """替代 urllib.request.urlopen：同样的返回值与异常，代理判定走 ProxyPlan。"""
    return current().opener.open(request, timeout=timeout)


# ---------- doctor ----------


def describe(plan: ProxyPlan) -> list[str]:
    """给 doctor 的明细行。凭据一律脱敏。"""
    lines: list[str] = []
    if plan.source == "system":
        lines.append("来源：系统代理设置（环境里没有代理变量）")
    for entry in plan.entries:
        scope = "其余请求" if entry.kind == "all" else f"{entry.kind} 请求"
        lines.append(f"{entry.var} → {entry.shown}（{scope}）")
    for item in plan.rejected:
        lines.append(f"未生效：{item.var}={item.url}——{item.reason}")
    if plan.no_proxy:
        lines.append("NO_PROXY：" + ", ".join(plan.no_proxy))
    if plan.entries or plan.rejected:
        lines.append("回环地址（localhost / 127.0.0.0/8 / ::1）一律直连，不看 NO_PROXY")
        if any(entry.scheme in _SOCKS5_SCHEMES for entry in plan.entries):
            lines.append("SOCKS 代理只作用于模型请求；MCP HTTP 传输与 OSV 预检（urllib）不支持 SOCKS，按直连")
        lines.append("子进程（bash、MCP stdio server）继承原始代理变量，不受上述处理影响")
    return lines
