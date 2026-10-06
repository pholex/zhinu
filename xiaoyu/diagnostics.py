"""进程级自诊断：自注册计量器（gauge）+ 进程快照 + `xiaoyu doctor` 体检。

动机：serve 跑久了"是不是在漏会话 / MCP 连接 / 后台任务"没有任何可观测面，
只能 ps 看 RSS 猜。接一套 metrics 依赖（prometheus_client 之类）对单机档不值——
一个 `Gauge` 在首次使用时把自己登记进进程级清单，`snapshot()` 把所有登记过的
计量器一把读出来，零依赖、零配置，谁引入谁可见。

约定：
- 计量器在模块顶层声明成常量（`SESSIONS_LIVE = Gauge("serve.sessions.live")`），
  import 不登记、首次 inc/track 才登记——没用到的面不出现在快照里，
  快照天然只含本进程真正在跑的子系统。
- `track()` 是 with 语句：进入 +1、退出 -1，异常路径也不会漏减。
- 减到 0 就停，不允许负数：配对漏了是 bug，但计量器不该因此变成噪音。

`doctor` 部分只回答"这台机器能不能把小羽跑顺"：Python 版本、配置目录、磁盘、
provider 凭据**有无**（永不回显值）、出网代理解析结果、沙箱、命令解析器、MCP 配置、会话目录。
每项 ok / warn / fail 三档，任一 fail 退出码非零，`--json` 给脚本用。

两个按需的扩展，默认都不跑：
- `--probe`：对默认模型发一条最小真实请求（会出网、会花一点点钱），记耗时，
  失败按 errors.classify 的分类报——"配置看着都对但就是不通"只有真发一次才知道；
- `--bundle`：把体检结果、脱敏后的生效配置、一场会话日志的尾部、崩溃日志尾部和
  版本/平台信息打成一个 JSON，给报 issue 用。密钥一律脱敏，但路径与命令历史
  会原样带着——分享前自己过一遍。
"""

from __future__ import annotations

import contextlib
import dataclasses
import json
import os
import shutil
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Iterator

try:
    import resource
except ImportError:  # pragma: no cover - Windows 没有 resource 模块
    resource = None  # type: ignore[assignment]

# ---------- 计量器 ----------

_registry_lock = threading.Lock()
_registry: dict[str, "Gauge"] = {}


class Gauge:
    """进程级计量器：首次使用自注册，线程安全，不会减成负数。"""

    __slots__ = ("name", "_value", "_lock", "_registered")

    def __init__(self, name: str) -> None:
        self.name = name
        self._value = 0
        self._lock = threading.Lock()
        self._registered = False

    def _register(self) -> None:
        if self._registered:
            return
        with _registry_lock:
            #  同名重复声明（测试里 reload 模块）：后者顶替前者，快照里只有一条
            _registry[self.name] = self
            self._registered = True

    def inc(self, delta: int = 1) -> None:
        self._register()
        with self._lock:
            self._value += delta

    def dec(self, delta: int = 1) -> None:
        self._register()
        with self._lock:
            self._value = max(0, self._value - delta)

    def set(self, value: int) -> None:
        self._register()
        with self._lock:
            self._value = max(0, int(value))

    @property
    def value(self) -> int:
        with self._lock:
            return self._value

    @contextlib.contextmanager
    def track(self) -> Iterator[None]:
        """with 块存活期间 +1，退出（含异常）-1。"""
        self.inc()
        try:
            yield
        finally:
            self.dec()


def snapshot() -> dict[str, int]:
    """所有已登记计量器的当前值，按名字排序（输出稳定、好 diff）。"""
    with _registry_lock:
        gauges = list(_registry.values())
    return {gauge.name: gauge.value for gauge in sorted(gauges, key=lambda g: g.name)}


def _reset_registry_for_tests() -> None:
    with _registry_lock:
        for gauge in _registry.values():
            gauge._registered = False
        _registry.clear()


# ---------- 进程快照 ----------

_STARTED_AT = time.monotonic()


def process_stats() -> dict[str, Any]:
    """RSS / 线程数 / 打开的 fd 数（拿不到的项给 None，绝不抛）。"""
    rss: int | None = None
    try:
        if resource is None:
            raise AttributeError("no resource module")
        maxrss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        #  macOS 的 ru_maxrss 是字节，Linux 是 KiB——同一个字段两种单位
        rss = int(maxrss) if sys.platform == "darwin" else int(maxrss) * 1024
    except (OSError, ValueError, AttributeError):
        pass
    #  Linux 上 statm 给的是**当前**常驻页数，比 ru_maxrss（峰值）更贴近"现在"
    if sys.platform == "linux":
        with contextlib.suppress(OSError, ValueError, IndexError):
            pages = int(Path("/proc/self/statm").read_text().split()[1])
            rss = pages * os.sysconf("SC_PAGE_SIZE")
    open_fds: int | None = None
    for fd_dir in ("/proc/self/fd", "/dev/fd"):
        with contextlib.suppress(OSError):
            open_fds = len(os.listdir(fd_dir))
            break
    return {
        "pid": os.getpid(),
        "rss_bytes": rss,
        "threads": threading.active_count(),
        "open_fds": open_fds,
        "uptime_s": round(time.monotonic() - _STARTED_AT, 1),
    }


def report() -> dict[str, Any]:
    """serve `/diagnostics` 的响应体；CLI 也能直接 dump。"""
    from . import __version__

    return {"version": __version__, "process": process_stats(), "gauges": snapshot()}


# ---------- doctor ----------

GIB = 1024**3
DISK_WARN = 5 * GIB
DISK_FAIL = 1 * GIB
MIN_PYTHON = (3, 10)

_ORDER = {"ok": 0, "warn": 1, "fail": 2}


@dataclasses.dataclass
class Check:
    id: str
    status: str  # ok | warn | fail
    summary: str
    details: list[str] = dataclasses.field(default_factory=list)
    remedy: str = ""

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


def _worse(a: str, b: str) -> str:
    return a if _ORDER[a] >= _ORDER[b] else b


def format_bytes(size: int) -> str:
    if size >= GIB:
        return f"{size / GIB:.1f} GiB"
    return f"{size / (1024 * 1024):.1f} MiB"


def _free_space(path: Path) -> int | None:
    """沿祖先找到第一个存在的目录量可用空间；目录不存在也能给出所在卷的数字。"""
    for ancestor in (path, *path.parents):
        if ancestor.is_dir():
            with contextlib.suppress(OSError):
                return shutil.disk_usage(ancestor).free
            return None
    return None


def check_python() -> Check:
    version = ".".join(str(part) for part in sys.version_info[:3])
    if sys.version_info[:2] < MIN_PYTHON:
        return Check(
            "python", "fail", f"Python {version} 过旧",
            remedy=f"需要 Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+",
        )
    return Check("python", "ok", f"Python {version}", [sys.executable])


#  PyPI 上的发行名（`xiaoyu` 这个名字是别人的包）
_DISTRIBUTION = "xiaoyu-agent"


def install_form(dist: Any) -> str:
    """这份安装是怎么来的：可编辑安装 / pipx / uv tool / pip。认不出就只报安装器名。"""
    try:
        direct = json.loads(dist.read_text("direct_url.json") or "{}")
    except (OSError, ValueError):
        direct = {}
    if isinstance(direct, dict) and (direct.get("dir_info") or {}).get("editable"):
        return "可编辑安装（指向源码目录）"
    prefix = sys.prefix.replace("\\", "/").lower()
    if "/pipx/" in prefix:
        return "pipx"
    if "/uv/tools/" in prefix:
        return "uv tool"
    try:
        installer = (dist.read_text("INSTALLER") or "").strip()
    except OSError:
        installer = ""
    return installer or "未知安装器"


def check_install() -> Check:
    """小羽自身：跑的是哪个版本、代码在哪、怎么装的。

    报 bug 时最先要对的就是这三样。顺带抓一种安静的错位：安装记录的版本和
    实际加载的代码不是同一版（可编辑安装之后只 git pull 没重装、或 sys.path
    上有另一份源码盖住了装好的那份）——`xiaoyu update` 读的是安装记录，
    错位时它报的"已是最新版本"说的不是正在跑的这份代码。
    """
    from importlib import metadata

    from . import __version__

    code = Path(__file__).resolve().parent
    details = [f"代码位置：{code}"]
    try:
        dist = metadata.distribution(_DISTRIBUTION)
    except metadata.PackageNotFoundError:
        return Check(
            "install", "ok", f"xiaoyu {__version__}（未安装，直接从源码目录运行）", details
        )
    form = install_form(dist)
    details.append(f"安装方式：{form}")
    recorded = dist.version
    if recorded != __version__:
        details.append(f"安装记录：{recorded}（{getattr(dist, '_path', '位置未知')}）")
        return Check(
            "install", "warn",
            f"xiaoyu {__version__}，但安装记录是 {recorded}",
            details,
            remedy=(
                "在源码目录重跑 pip install -e . 刷新安装记录"
                if form.startswith("可编辑")
                else f"重装一次：pip install --force-reinstall {_DISTRIBUTION}=={__version__}"
            ),
        )
    return Check("install", "ok", f"xiaoyu {__version__}（{form}）", details)


def check_config_dir(config_dir: Path) -> Check:
    details = [str(config_dir)]
    if not config_dir.exists():
        try:
            config_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return Check(
                "config_dir", "fail", "配置目录无法创建", [*details, str(exc)],
                remedy="检查目录权限，或用 XDG_CONFIG_HOME / APPDATA 指到可写位置",
            )
    probe = config_dir / ".doctor-write-probe"
    try:
        probe.write_text("", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        return Check(
            "config_dir", "fail", "配置目录不可写", [*details, str(exc)],
            remedy="检查目录权限",
        )
    return Check("config_dir", "ok", "配置目录可写", details)


def check_disk(
    paths: dict[str, Path],
    measure: Callable[[Path], int | None] = _free_space,
) -> Check:
    status = "ok"
    details: list[str] = []
    lowest: int | None = None
    for label, path in paths.items():
        free = measure(path)
        if free is None:
            status = _worse(status, "warn")
            details.append(f"{label}：无法测量（{path}）")
            continue
        details.append(f"{label}：可用 {format_bytes(free)}（{path}）")
        lowest = free if lowest is None else min(lowest, free)
        if free < DISK_FAIL:
            status = _worse(status, "fail")
        elif free < DISK_WARN:
            status = _worse(status, "warn")
    if status == "ok":
        summary = "磁盘空间充足" + (f"（最低 {format_bytes(lowest)}）" if lowest is not None else "")
        return Check("disk", "ok", summary, details)
    if lowest is not None and lowest < DISK_WARN:
        summary = f"磁盘空间{'严重' if status == 'fail' else ''}不足（最低 {format_bytes(lowest)}）"
    else:
        summary = "磁盘空间未能完整测量"
    return Check(
        "disk", status, summary, details,
        remedy=f"清理磁盘，或把工作区/配置目录挪到更大的卷（建议 ≥ {format_bytes(DISK_WARN)}）",
    )


def check_providers() -> Check:
    """只报"哪些 provider 有凭据"，值永不出现在任何输出里。"""
    from .config import GATEWAY_KEY_ENVS, find_api_key
    from .providers import GATEWAY, PRESETS, bedrock_region

    present: list[str] = []
    absent: list[str] = []
    for name, preset in PRESETS.items():
        if preset.region_env:
            #  区域型：有 key 报 key，没 key 报区域（IAM 路线）。AWS 凭证本身有没有、
            #  对不对，这里不探（探一次要出网），首个请求会如实报
            region = bedrock_region()
            if find_api_key(preset.key_envs):
                present.append(f"{name}（API key）")
            elif region:
                present.append(f"{name}（IAM，{region}）")
            else:
                absent.append(name)
            continue
        (present if find_api_key(preset.key_envs) else absent).append(name)
    gateway_url = os.environ.get("XIAOYU_BASE_URL", "").strip()
    gateway_key = bool(find_api_key(GATEWAY_KEY_ENVS))
    details = [
        "直连已配凭据：" + ("、".join(present) or "（无）"),
        "直连未配：" + ("、".join(absent) or "（无）"),
        f"网关：{'端点+凭据齐' if gateway_url and gateway_key else '端点 ' + ('有' if gateway_url else '无') + '，凭据 ' + ('有' if gateway_key else '无')}",
    ]
    if present or (gateway_url and gateway_key):
        return Check("providers", "ok", f"{len(present) + int(bool(gateway_url and gateway_key))} 个可用端点", details)
    if gateway_url and not gateway_key:
        return Check(
            "providers", "fail", "网关配了端点但没有凭据", details,
            remedy=f"设置 {GATEWAY_KEY_ENVS[0]}，或运行 `xiaoyu config`",
        )
    return Check(
        "providers", "fail", "没有任何可用端点", details,
        remedy=f"运行 `xiaoyu config`，或设置任一厂商的 *_API_KEY / {GATEWAY}",
    )


def check_proxy() -> Check:
    """代理变量解析成了什么：哪些生效、哪些被判不生效（小羽自身按未设置处理）。
    代理地址里的凭据一律脱敏。这里只读不打 stderr 诊断——doctor 自己就是诊断面。"""
    from . import netproxy

    plan = netproxy.current(announce=False)
    details = netproxy.describe(plan)
    if plan.rejected:
        names = "、".join(item.var for item in plan.rejected)
        return Check(
            "proxy", "warn", f"代理变量未生效：{names}（小羽自身请求按未设置处理）", details,
            remedy="；".join(dict.fromkeys(item.remedy for item in plan.rejected)),
        )
    if not plan.entries:
        return Check("proxy", "ok", "未配置代理（直连）", details)
    where = "系统代理设置" if plan.source == "system" else "环境变量"
    return Check("proxy", "ok", f"代理生效（来自{where}，回环地址直连）", details)


def check_sandbox() -> Check:
    from . import sandbox

    if sandbox.available():
        backend = "Seatbelt" if sys.platform == "darwin" else "bubblewrap"
        return Check("sandbox", "ok", f"沙箱可用（{backend}）")
    why, remedy = sandbox.unavailable_reason()
    return Check(
        "sandbox", "warn", f"沙箱不可用（{why}），bash 命令可写任意路径", remedy=remedy
    )


def check_bash_parser() -> Check:
    from . import envprobe

    try:
        from . import bash_ast  # noqa: F401

        import tree_sitter_bash  # noqa: F401
    except ImportError as exc:
        return Check(
            "bash_parser", "warn", "命令解析器缺失，allow 规则退化为逐条确认", [str(exc)],
            remedy=" && ".join(
                envprobe.install_hint(name) for name in ("tree-sitter", "tree-sitter-bash")
            ),
        )
    return Check("bash_parser", "ok", "命令解析器就绪（tree-sitter-bash）")


#  env 里值是路径的键（按后缀认）。不按"值长得像绝对路径"认：`API_PREFIX=/v1`
#  这类值也以 / 开头，却不是文件
_PATH_ENV_SUFFIXES = ("_PATH", "_FILE", "_DIR", "_HOME", "_ROOT", "_CONFIG")


def _dead_paths(entry: dict[str, Any]) -> list[str]:
    """server 声明里指向不存在位置的绝对路径（args 与 env 两处）。

    搬过目录、删过检出之后最常见的坏法：command 还在（npx / python），
    参数里的脚本或数据目录没了——server 起得来，然后立刻退出。
    占位符没兑现的值（`${VAR}`）不判，那是另一类问题。
    """
    candidates: list[str] = []
    args = entry.get("args")
    for arg in args if isinstance(args, list) else []:
        if isinstance(arg, str) and not arg.startswith("-"):
            candidates.append(arg)
    env = entry.get("env")
    for key, value in (env if isinstance(env, dict) else {}).items():
        if isinstance(value, str) and str(key).upper().endswith(_PATH_ENV_SUFFIXES):
            candidates.append(value)
    dead: list[str] = []
    for value in candidates:
        if "${" in value or os.pathsep in value:
            continue
        expanded = os.path.expanduser(value)
        if os.path.isabs(expanded) and not os.path.exists(expanded):
            dead.append(value)
    return dead


def check_mcp_config(workspace: Path) -> Check:
    from . import mcp

    details: list[str] = []
    missing: list[str] = []
    dead: list[str] = []
    status = "ok"
    total = 0
    for path in mcp.config_paths(workspace):
        if not path.is_file():
            continue
        try:
            data = mcp.read_config_file(path)
        except mcp.McpError as exc:
            status = "fail"
            details.append(f"{path}：{exc}")
            continue
        servers = data.get("mcpServers")
        count = len(servers) if isinstance(servers, dict) else 0
        total += count
        details.append(f"{path}：{count} 个 server")
        for name, entry in (servers if isinstance(servers, dict) else {}).items():
            command = entry.get("command") if isinstance(entry, dict) else None
            if not isinstance(command, str) or not command.strip() or entry.get("disabled"):
                continue
            #  只查"起不起得来"的前两步：命令在不在、声明里的路径在不在。
            #  不启动任何东西。找命令与真正启动时同一个函数（认配置里的 PATH）
            env = entry.get("env")
            declared = {
                str(key): mcp._expand(str(value))
                for key, value in (env if isinstance(env, dict) else {}).items()
            }
            if mcp.find_command(command, declared) is None:
                missing.append(f"{name} 的启动命令 {command!r} 找不到")
            dead += [f"{name} 的配置指向不存在的路径 {path!r}" for path in _dead_paths(entry)]
    if status == "fail":
        return Check("mcp_config", "fail", "MCP 配置文件损坏", details, remedy="修正 JSON 后重试")
    if not details:
        return Check("mcp_config", "ok", "未配置 MCP server")
    if missing:
        return Check(
            "mcp_config", "warn", f"{len(missing)} 个 MCP server 的启动命令找不到",
            details + missing + dead,
            remedy="装上对应的程序，或在配置的 env 里声明 PATH，或把 command 写成绝对路径",
        )
    if dead:
        return Check(
            "mcp_config", "warn", f"{len(dead)} 处 MCP 配置指向不存在的路径",
            details + dead,
            remedy="把配置里的路径改成现在的位置（目录搬过或检出被删）",
        )
    return Check("mcp_config", "ok", f"MCP 配置可解析（{total} 个 server）", details)


#  名字由别处拼出来的环境变量（按前缀放行）
_DYNAMIC_ENV_PREFIXES = ("XIAOYU_PROVIDER_",)


def known_env_names() -> set[str]:
    """小羽认的全部 XIAOYU_* 环境变量名：从包的源码里现扫。

    不维护清单：清单迟早跟不上代码。源码里出现过的名字就是认的。
    """
    import re

    names: set[str] = set()
    pattern = re.compile(r"XIAOYU_[A-Z0-9_]+")
    for path in Path(__file__).resolve().parent.glob("*.py"):
        with contextlib.suppress(OSError):
            names.update(pattern.findall(path.read_text(encoding="utf-8", errors="replace")))
    return names


def check_env() -> Check:
    """写错了被忽略的配置，和拼错了名字、压根没人读的 XIAOYU_* 变量。"""
    import difflib

    from . import config

    details = list(config.env_problems())
    known = known_env_names()
    for name in sorted(os.environ):
        if not name.startswith("XIAOYU_") or name in known:
            continue
        if name.startswith(_DYNAMIC_ENV_PREFIXES):
            continue
        close = difflib.get_close_matches(name, sorted(known), n=1, cutoff=0.8)
        hint = f"——是不是想写 {close[0]}？" if close else ""
        details.append(f"{name} 不是小羽认的变量，设了也不起作用{hint}")
    if details:
        return Check(
            "env", "warn", f"{len(details)} 处配置没有生效", details,
            remedy="对照 docs/configuration.md 改正，或删掉",
        )
    return Check("env", "ok", "环境变量里的配置都认得、都合法")


def check_sessions(sessions: Path) -> Check:
    if not sessions.is_dir():
        return Check("sessions", "ok", "还没有会话记录", [str(sessions)])
    count = 0
    size = 0
    try:
        for entry in sessions.rglob("*"):
            if entry.is_file():
                count += 1
                with contextlib.suppress(OSError):
                    size += entry.stat().st_size
    except OSError as exc:
        return Check("sessions", "warn", "会话目录无法遍历", [str(sessions), str(exc)])
    details = [f"{count} 个文件，{format_bytes(size)}（{sessions}）"]
    if size > 2 * GIB:
        return Check(
            "sessions", "warn", "会话目录偏大", details,
            #  小羽没有清理旧会话的命令：别指一条不存在的路
            remedy=(
                "旧会话的 .jsonl 可以直接删（连同同名的 .lock / .plan.md）；"
                "先用 `xiaoyu sessions` 看哪些还在跑，在跑的别动"
            ),
        )
    return Check("sessions", "ok", "会话目录正常", details)


def check_tools() -> Check:
    from . import envprobe

    present, missing = envprobe.probe_tools()
    details = ["已找到：" + ("、".join(present) or "（无）")]
    if missing:
        details.append("未找到：" + "、".join(missing))
        return Check(
            "tools", "warn", f"缺 {len(missing)} 个常用工具", details,
            remedy="缺失项会被告知模型绕开；想用就装上",
        )
    return Check("tools", "ok", "常用工具链齐全", details)


def check_shell_integration() -> Check:
    """终端集成（@x / @c）接没接上。可选功能：没接也是 ok，只在细节里指路。"""
    from . import shell_setup, term

    found = shell_setup.installed_in()
    details = [
        f"{item.path}（{'term install 写入' if item.kind == 'marked' else '手写'}，"
        f"Tab 补全{'已接' if item.completion else '未接'}）"
        for item in found
    ]
    if found and not any(item.completion for item in found):
        if any(item.kind == "marked" for item in found):
            details.append("补全：重跑 xiaoyu term install（PATH 上要有 xiaoyu 命令）")
        else:
            details.append("补全：xiaoyu term install --dry-run 会给出要加在 term init 前面的那几行")
    if os.environ.get(term.SESSION_ENV):
        return Check("shell", "ok", "当前终端已接入终端集成", details)
    if details:
        return Check("shell", "ok", "启动文件已接入终端集成，新开的终端生效", details)
    return Check(
        "shell", "ok", "未接入终端集成（可选）",
        ["在 shell 里用 @x 提问、@c 要命令：xiaoyu term install"],
    )


def run_doctor(workspace: Path | None = None) -> list[Check]:
    from .config import user_config_dir
    from .session_log import sessions_dir

    workspace = (workspace or Path.cwd()).resolve()
    config_dir = user_config_dir()
    checks = [
        check_install(),
        check_python(),
        check_config_dir(config_dir),
        check_disk({"配置目录": config_dir, "工作区": workspace}),
        check_providers(),
        check_env(),
        check_proxy(),
        check_sandbox(),
        check_bash_parser(),
        check_tools(),
        check_shell_integration(),
        check_mcp_config(workspace),
        check_sessions(sessions_dir()),
    ]
    return checks


# ---------- --probe：最小真实请求 ----------

PROBE_PROMPT = "回复 ok"


def probe_model(config: Any = None) -> Check:
    """对默认模型发一条最小真实请求，记耗时；失败按 errors 的分类报。

    默认不跑（会出网）：只在 `doctor --probe` 时调。非流式、不带工具，
    与压缩摘要走的是同一条 create 路径——三条协议的适配层都认。
    """
    from . import errors, providers
    from .config import Config, MissingConfig
    from .mcp import _redact

    try:
        config = config if config is not None else Config.from_env()
        registry = providers.build(config)
        route = registry.resolve(config.model)
    except MissingConfig as exc:
        return Check("probe", "fail", "没有可用的 provider 配置", [str(exc)], remedy="先跑 xiaoyu config")
    except Exception as exc:  # noqa: BLE001 - 装配阶段的任何失败都是体检结论，不是 traceback
        return Check("probe", "fail", f"装配 provider 失败：{type(exc).__name__}", [_redact(str(exc))[:300]])
    started = time.monotonic()
    try:
        response = route.client.chat.completions.create(
            model=route.model,
            messages=[{"role": "user", "content": PROBE_PROMPT}],
        )
    except Exception as exc:  # noqa: BLE001 - 请求失败正是要报告的东西
        elapsed = round((time.monotonic() - started) * 1000)
        verdict = errors.classify(exc)
        return Check(
            "probe", "fail",
            f"{route.qualified} 请求失败（{verdict.kind}，{elapsed} ms）",
            [f"{type(exc).__name__}: {_redact(str(exc))[:300]}"],
            remedy=verdict.hint,
        )
    elapsed = round((time.monotonic() - started) * 1000)
    text = ""
    with contextlib.suppress(Exception):
        text = str(response.choices[0].message.content or "")
    text = " ".join(text.split())[:60]
    details = [f"回复：{text or '（空）'}"]
    usage = getattr(response, "usage", None)
    if usage is not None:
        details.append(
            f"usage：in {getattr(usage, 'prompt_tokens', 0) or 0} / out {getattr(usage, 'completion_tokens', 0) or 0}"
        )
    if not text:
        return Check("probe", "warn", f"{route.qualified} 应答但正文为空（{elapsed} ms）", details)
    return Check("probe", "ok", f"{route.qualified} 应答 {elapsed} ms", details)


# ---------- --bundle：诊断包 ----------

#  会话日志尾部的上限：行数与字节数取先到的那个——报 issue 要的是"最后发生了什么"
BUNDLE_TAIL_LINES = 200
BUNDLE_TAIL_BYTES = 256 * 1024
#  崩溃日志只带尾部
BUNDLE_CRASH_BYTES = 64 * 1024
#  变量名含这些片段的一律按密钥处理，值不进包
_SECRET_NAME_MARKERS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "PASSWD", "CREDENTIAL")

BUNDLE_NOTICE = "诊断包含工作区路径与命令历史（密钥已脱敏），分享前请自查。"


def redact_value(name: str, value: str) -> str:
    """配置值脱敏：名字像密钥的整个换掉，其余过凭据正则（sk-… / Bearer / URL 账号密码）。"""
    from .mcp import _redact

    upper = name.upper()
    if any(marker in upper for marker in _SECRET_NAME_MARKERS):
        return "[REDACTED]" if value else ""
    return _redact(value)


def effective_config() -> dict[str, Any]:
    """脱敏后的生效配置：XIAOYU_* 环境变量（含 .env 合并后的）+ 生效 provider 清单。"""
    from . import providers
    from .config import Config, MissingConfig, load_dotenv, user_config_dir

    with contextlib.suppress(Exception):
        load_dotenv()
    variables = {
        name: redact_value(name, value)
        for name, value in sorted(os.environ.items())
        if name.startswith("XIAOYU_")
    }
    result: dict[str, Any] = {"env": variables, "config_dir": str(user_config_dir())}
    try:
        registry = providers.build(Config.from_env())
    except MissingConfig as exc:
        result["providers_error"] = str(exc)
    except Exception as exc:  # noqa: BLE001 - 配置坏了也是诊断信息
        result["providers_error"] = f"{type(exc).__name__}: {exc}"
    else:
        result["providers"] = [
            {"name": provider.name, "display": provider.display, "models": list(provider.models)}
            for provider in registry.providers
        ]
    return result


def tail_lines(path: Path, max_lines: int, max_bytes: int) -> list[str]:
    """文件尾部的若干行（两个上限先到为准）。被字节上限切掉的首行丢弃——半行没有意义。"""
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            if size > max_bytes:
                handle.seek(size - max_bytes)
                truncated = True
            else:
                truncated = False
            raw = handle.read()
    except OSError:
        return []
    text = raw.decode("utf-8", errors="replace")
    lines = text.splitlines()
    if truncated and lines:
        lines = lines[1:]
    return lines[-max_lines:]


def session_tail(path: Path) -> dict[str, Any]:
    """一场会话日志的尾部，逐行脱敏；读不了也如实写进包。"""
    from .mcp import _redact

    lines = tail_lines(path, BUNDLE_TAIL_LINES, BUNDLE_TAIL_BYTES)
    return {
        "path": str(path),
        "tail_lines": len(lines),
        "lines": [_redact(line) for line in lines],
    }


def write_private(path: Path, text: str) -> None:
    """0600 写文件，拒绝 symlink：诊断包落在别人指过来的位置等于把配置写去别处。"""
    if path.is_symlink():
        raise ValueError(f"输出路径是符号链接，拒绝写入：{path}")
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(str(path), flags, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)


def build_bundle(
    checks: list[Check],
    workspace: Path | None = None,
    session: Path | None = None,
    out: Path | None = None,
) -> Path:
    """把体检结果与现场打成一个 JSON 文件，返回路径。

    session=None 时取当前工作区最近一场（没有就全局最近）；out=None 落在当前
    目录的 `xiaoyu-doctor-<时间戳>.json`。密钥脱敏，其余原样——见 BUNDLE_NOTICE。
    """
    import platform
    from datetime import datetime

    from . import __version__, crash_guard
    from .mcp import _redact
    from .session_log import list_sessions

    workspace = (workspace or Path.cwd()).resolve()
    if session is None:
        found = list_sessions(limit=1, workspace=str(workspace)) or list_sessions(limit=1)
        session = found[0].path if found else None
    payload: dict[str, Any] = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "notice": BUNDLE_NOTICE,
        "version": __version__,
        "platform": {
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "os": os.name,
            "executable": sys.executable,
        },
        "workspace": str(workspace),
        "doctor": {"status": overall(checks), "checks": [check.to_dict() for check in checks]},
        "diagnostics": report(),
        "config": effective_config(),
        "session": session_tail(session) if session is not None else {"note": "没有会话记录"},
    }
    crash = crash_guard._resolve_path()
    if crash.is_file():
        crash_lines = tail_lines(crash, 400, BUNDLE_CRASH_BYTES)
        payload["crash_log"] = {"path": str(crash), "lines": [_redact(line) for line in crash_lines]}
    else:
        payload["crash_log"] = {"path": str(crash), "note": "没有崩溃记录"}
    if out is None:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        out = Path.cwd() / f"xiaoyu-doctor-{stamp}.json"
    out = out.expanduser()
    write_private(out, json.dumps(payload, ensure_ascii=False, indent=2))
    return out


def overall(checks: list[Check]) -> str:
    status = "ok"
    for check in checks:
        status = _worse(status, check.status)
    return status


def render(checks: list[Check]) -> list[str]:
    """纯文本行（着色由 CLI 层做，这里不碰 ui）。"""
    marks = {"ok": "OK  ", "warn": "WARN", "fail": "FAIL"}
    lines: list[str] = []
    for check in checks:
        lines.append(f"{marks[check.status]}  {check.id:<12} {check.summary}")
        for detail in check.details:
            lines.append(f"      {detail}")
        if check.remedy and check.status != "ok":
            lines.append(f"      → {check.remedy}")
    return lines


def to_json(checks: list[Check]) -> str:
    payload = {
        "status": overall(checks),
        "checks": [check.to_dict() for check in checks],
        "diagnostics": report(),
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)
