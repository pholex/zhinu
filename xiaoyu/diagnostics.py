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
provider 凭据**有无**（永不回显值）、沙箱、命令解析器、MCP 配置、会话目录。
每项 ok / warn / fail 三档，任一 fail 退出码非零，`--json` 给脚本用。
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
    from .providers import GATEWAY, PRESETS

    present: list[str] = []
    absent: list[str] = []
    for name, preset in PRESETS.items():
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


def check_sandbox() -> Check:
    from . import sandbox

    if sys.platform == "darwin":
        ok = sandbox.available()
        return Check(
            "sandbox", "ok" if ok else "warn",
            "沙箱可用（Seatbelt）" if ok else "sandbox-exec 不存在，bash 不受沙箱约束",
        )
    if sys.platform == "linux":
        if sandbox.available():
            return Check("sandbox", "ok", "沙箱可用（bubblewrap）")
        return Check(
            "sandbox", "warn", "沙箱不可用，bash 命令可写任意路径",
            remedy="安装 bubblewrap，并确认内核允许 unprivileged user namespace",
        )
    return Check("sandbox", "warn", "本平台无沙箱（建议在 WSL 里用）")


def check_bash_parser() -> Check:
    try:
        from . import bash_ast  # noqa: F401

        import tree_sitter_bash  # noqa: F401
    except ImportError as exc:
        return Check(
            "bash_parser", "warn", "命令解析器缺失，allow 规则退化为逐条确认", [str(exc)],
            remedy="pip install tree-sitter tree-sitter-bash",
        )
    return Check("bash_parser", "ok", "命令解析器就绪（tree-sitter-bash）")


def check_mcp_config(workspace: Path) -> Check:
    from . import mcp

    details: list[str] = []
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
    if status == "fail":
        return Check("mcp_config", "fail", "MCP 配置文件损坏", details, remedy="修正 JSON 后重试")
    if not details:
        return Check("mcp_config", "ok", "未配置 MCP server")
    return Check("mcp_config", "ok", f"MCP 配置可解析（{total} 个 server）", details)


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
            remedy="`xiaoyu sessions` 清理旧会话",
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


def run_doctor(workspace: Path | None = None) -> list[Check]:
    from .config import user_config_dir
    from .session_log import sessions_dir

    workspace = (workspace or Path.cwd()).resolve()
    config_dir = user_config_dir()
    checks = [
        check_python(),
        check_config_dir(config_dir),
        check_disk({"配置目录": config_dir, "工作区": workspace}),
        check_providers(),
        check_sandbox(),
        check_bash_parser(),
        check_tools(),
        check_mcp_config(workspace),
        check_sessions(sessions_dir()),
    ]
    return checks


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
