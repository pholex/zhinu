"""Chrome 扩展的 native messaging host：找到或拉起本机 serve，把地址和 token 交给扩展。

扩展侧调 `chrome.runtime.sendNativeMessage("xiaoyu.chrome_host", {type: "ensure"})`，
Chrome 按清单拉起 `xiaoyu chrome host`，读一帧、回一帧、退出。之后扩展照旧用 REST /
SSE / 浏览器桥连 serve——这里只是启动器，不在 stdio 上跑会话：host → 扩展单条消息
上限 1 MB，截图和长输出会撞上，HTTP 没这个限制；一问一答也不用管长连的生命周期。

几条纪律：
- **stdout 只写帧**。进程一开始就把 fd 1 指到 stderr、帧写进私有的副本 fd：import
  期间的告警、任何库的打印都漏不进协议流（漏一个字节 Chrome 就判协议错误断开）。
- 鉴权两层：谁能连 host 由 Chrome 按清单的 `allowed_origins` 把关；HTTP 那层照旧要
  token，挡本机别的进程。token 只在 0600 的状态文件和 serve 的环境变量里，不进 argv。
- workspace root 与审批档在 `xiaoyu chrome install` 时由命令行定，扩展发来的消息改不了：
  消息里只认 `type`。
- 拉起的 serve 脱离本进程的会话组（Chrome 会收拾 host 的进程组），之后常驻；版本或
  安装配置和当前不一致时直接重启（正在跑的那一轮会断，会话清单落盘可恢复）。

本模块不 import cli / rich：Chrome 每次连接都要拉起一次，启动越轻越好
（`__main__` 在导入 cli 之前就把 `chrome host` 分流到这里）。
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import secrets
import signal
import socket
import struct
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

HOST_NAME = "xiaoyu.chrome_host"
PORT_RANGE = range(8420, 8430)
START_TIMEOUT = 25.0
STOP_GRACE = 5.0
#  扩展 → host 的消息上限是 64 MB，host → 扩展 1 MB；这里只收 {type: ...} 这种小消息
MAX_INCOMING = 64 * 1024
#  已知的扩展 id：`install` 默认登记这些，再加上 --extension-id 给的。
#  ljoc… = xiaoyu-chrome 开发版（manifest 的 key 钉死，从源码目录加载都是这个 id）；
#  上架后商店分配的 id 另加一行
KNOWN_EXTENSION_IDS: tuple[str, ...] = ("ljocofdloggcollnfkekijoncaonjnie",)


# ---------- 路径 ----------


def base_dir() -> Path:
    from .config import user_config_dir

    return user_config_dir() / "chrome"


def config_path() -> Path:
    return base_dir() / "config.json"


def state_path() -> Path:
    return base_dir() / "serve.json"


def launcher_path() -> Path:
    return base_dir() / "host"


def scratch_dir() -> Path:
    """未选目录的会话落在这里：空目录，碰不到代码。"""
    return base_dir() / "scratch"


def browser_dirs(home: Path | None = None, platform: str | None = None) -> list[tuple[str, Path]]:
    """各 Chromium 系浏览器的用户数据目录（不管存不存在），清单写进其下的 NativeMessagingHosts/。"""
    home = home or Path.home()
    platform = platform or sys.platform
    if platform == "darwin":
        support = home / "Library" / "Application Support"
        return [
            ("Chrome", support / "Google" / "Chrome"),
            ("Chrome Beta", support / "Google" / "Chrome Beta"),
            ("Chrome Canary", support / "Google" / "Chrome Canary"),
            ("Chromium", support / "Chromium"),
            ("Edge", support / "Microsoft Edge"),
            ("Brave", support / "BraveSoftware" / "Brave-Browser"),
            ("Arc", support / "Arc" / "User Data"),
        ]
    config = Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config")
    return [
        ("Chrome", config / "google-chrome"),
        ("Chrome Beta", config / "google-chrome-beta"),
        ("Chrome Dev", config / "google-chrome-unstable"),
        ("Chromium", config / "chromium"),
        ("Edge", config / "microsoft-edge"),
        ("Brave", config / "BraveSoftware" / "Brave-Browser"),
    ]


def manifest_file(browser_dir: Path) -> Path:
    return browser_dir / "NativeMessagingHosts" / f"{HOST_NAME}.json"


def render_manifest(launcher: Path, extension_ids: list[str]) -> dict[str, Any]:
    return {
        "name": HOST_NAME,
        "description": "Xiaoyu local serve launcher",
        "path": str(launcher),
        "type": "stdio",
        "allowed_origins": [f"chrome-extension://{eid}/" for eid in extension_ids],
    }


def render_launcher(python: str, workdir: Path) -> str:
    #  清单的 path 必须是绝对路径且不能带参数，所以套一层脚本。钉安装时那个 Python，
    #  不依赖 Chrome 进程的 PATH；-P 不把 cwd 放进 sys.path，cd 到自家目录再兜一层
    return (
        "#!/bin/sh\n"
        "# 由 `xiaoyu chrome install` 生成，重跑 install 会覆盖；Chrome 按 native messaging 清单调它\n"
        f"cd {_sh_quote(str(workdir))} || exit 1\n"
        f'exec {_sh_quote(python)} -P -m xiaoyu chrome host "$@"\n'
    )


def _sh_quote(text: str) -> str:
    return "'" + text.replace("'", "'\"'\"'") + "'"


# ---------- 安装配置与状态文件 ----------


@dataclass
class HostConfig:
    workspace: str
    approval: str = "ask"
    extension_ids: list[str] = field(default_factory=list)
    #  install 时 shell 的 PATH：Chrome 拉起的进程只有 GUI 环境的短 PATH，bash 工具里的
    #  git / node / brew 会找不到。只记 PATH，不记别的变量（免得把密钥落盘）
    path_env: str = ""
    python: str = ""

    def fingerprint(self) -> str:
        """影响 serve 启动参数的那几项；变了就得重启 serve。"""
        raw = json.dumps([self.workspace, self.approval, self.python], ensure_ascii=False)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def load_config() -> HostConfig | None:
    data = _read_json(config_path())
    if not isinstance(data, dict) or not isinstance(data.get("workspace"), str):
        return None
    ids = data.get("extension_ids")
    return HostConfig(
        workspace=data["workspace"],
        approval=data.get("approval") if data.get("approval") in ("ask", "allow_all") else "ask",
        extension_ids=[i for i in ids if isinstance(i, str)] if isinstance(ids, list) else [],
        path_env=data.get("path_env") if isinstance(data.get("path_env"), str) else "",
        python=data.get("python") if isinstance(data.get("python"), str) else "",
    )


def save_config(cfg: HostConfig) -> None:
    _write_private_json(config_path(), cfg.__dict__)


def load_state() -> dict[str, Any] | None:
    data = _read_json(state_path())
    if not isinstance(data, dict):
        return None
    if not isinstance(data.get("pid"), int) or not isinstance(data.get("port"), int):
        return None
    if not isinstance(data.get("token"), str):
        return None
    return data


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return None


def _write_private_json(path: Path, data: Any) -> None:
    """原子写、0600（状态文件里有 token）。"""
    from .fsguard import write_atomic

    write_atomic(path, json.dumps(data, ensure_ascii=False, indent=2), private=True)


@contextlib.contextmanager
def _locked():
    """两个窗口同时 ensure 时串行，只拉起一个 serve。"""
    import fcntl

    path = base_dir() / "lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


# ---------- serve 探测、拉起、停止 ----------


def current_version() -> str:
    from . import __version__

    return __version__


def probe(port: int, token: str, timeout: float = 2.0) -> dict[str, Any] | None:
    """带 token 打 /health。对得上 token 的 serve 会回 root；回不来或对不上返回 None。"""
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/health", headers={"Authorization": f"Bearer {token}"},
    )
    try:
        with _no_proxy().open(request, timeout=timeout) as response:
            body = json.loads(response.read().decode("utf-8", errors="replace"))
    except (OSError, ValueError, urllib.error.URLError):
        return None
    if not isinstance(body, dict) or not body.get("ok") or "root" not in body:
        return None
    return body


def _no_proxy() -> urllib.request.OpenerDirector:
    #  回环地址不走代理：GUI 环境里继承的 http_proxy 会把 127.0.0.1 也送去代理
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        #  与 uvicorn 一样带 SO_REUSEADDR：刚停的 serve 留下的 TIME_WAIT 不算占用
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def pick_port(ports=None) -> int | None:
    for port in ports or PORT_RANGE:
        if port_free(port):
            return port
    return None


def serve_argv(cfg: HostConfig, port: int, default_workspace: Path | None) -> list[str]:
    argv = [
        cfg.python or sys.executable, "-P", "-m", "xiaoyu", "serve",
        "--host", "127.0.0.1", "--port", str(port),
        "--workspace", cfg.workspace, "--approval", cfg.approval,
    ]
    if default_workspace is not None:
        argv += ["--default-workspace", str(default_workspace)]
    return argv


def default_workspace_for(cfg: HostConfig) -> Path | None:
    """scratch 在 root 之内才能当默认；root 设得比配置目录还窄时退回 root 本身。"""
    scratch = scratch_dir().resolve()
    root = Path(cfg.workspace).expanduser().resolve()
    if scratch == root or root in scratch.parents:
        return scratch
    return None


def stop(pid: int, grace: float = STOP_GRACE) -> None:
    """SIGTERM，等 grace 秒还在就 SIGKILL。"""
    with contextlib.suppress(ProcessLookupError):
        os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        if not pid_alive(pid) or _reap(pid):
            return
        time.sleep(0.1)
    with contextlib.suppress(ProcessLookupError):
        os.kill(pid, signal.SIGKILL)
    for _ in range(20):
        if not pid_alive(pid) or _reap(pid):
            return
        time.sleep(0.05)


def _reap(pid: int) -> bool:
    """本进程拉起的子进程退出后是僵尸，kill(pid, 0) 仍算活着：顺手收一下。"""
    try:
        done, _ = os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        return False
    return done == pid


def serve_deps_missing() -> bool:
    import importlib.util

    return any(importlib.util.find_spec(name) is None for name in ("fastapi", "uvicorn"))


_DETACHED: list[subprocess.Popen] = []


class HostError(Exception):
    def __init__(self, code: str, message: str, **extra: Any):
        super().__init__(message)
        self.code = code
        self.extra = extra


def _tail(path: Path, limit: int = 1200) -> str:
    try:
        data = path.read_bytes()
    except OSError:
        return ""
    return data[-limit:].decode("utf-8", errors="replace").strip()


def start(cfg: HostConfig, *, timeout: float = START_TIMEOUT,
          argv_for: Callable[[HostConfig, int, Path | None], list[str]] | None = None) -> dict[str, Any]:
    port = pick_port()
    if port is None:
        raise HostError("no_port", f"{PORT_RANGE[0]}–{PORT_RANGE[-1]} 端口都被占了")
    token = secrets.token_hex(16)
    default_workspace = default_workspace_for(cfg)
    if default_workspace is not None:
        default_workspace.mkdir(parents=True, exist_ok=True)
    workdir = default_workspace or Path(cfg.workspace).expanduser()
    env = dict(os.environ)
    if cfg.path_env:
        env["PATH"] = cfg.path_env
    env["XIAOYU_SERVE_TOKEN"] = token
    log_path = base_dir() / "serve.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "ab") as log:
        log.write(f"\n--- {time.strftime('%Y-%m-%d %H:%M:%S')} 启动 port={port}\n".encode())
        log.flush()
        #  start_new_session：Chrome 收拾 host 时连进程组一起杀，脱离出去 serve 才活得下来。
        #  cwd 放在空目录：serve 会读 cwd 下的 .env，别让 Chrome 所在目录的东西混进来
        process = subprocess.Popen(
            (argv_for or serve_argv)(cfg, port, default_workspace), cwd=str(workdir), env=env,
            stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True,
        )
    state = {
        "pid": process.pid, "port": port, "token": token, "version": current_version(),
        "workspace": str(Path(cfg.workspace).expanduser().resolve()),
        "fingerprint": cfg.fingerprint(), "started_at": time.time(),
    }
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise HostError("start_failed", "serve 启动后立刻退出了", log=str(log_path),
                            detail=_tail(log_path))
        if probe(port, token, timeout=1.0) is not None:
            _write_private_json(state_path(), state)
            #  serve 刻意比 host 活得久：留住 Popen，别让它被回收时报"子进程还在跑"
            _DETACHED.append(process)
            return state
        time.sleep(0.2)
    process.terminate()
    try:
        process.wait(timeout=1.0)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
    raise HostError("start_timeout", f"serve {int(timeout)} 秒内没起来", log=str(log_path),
                    detail=_tail(log_path))


def ensure(*, timeout: float = START_TIMEOUT, grace: float = STOP_GRACE,
           argv_for: Callable[[HostConfig, int, Path | None], list[str]] | None = None) -> dict[str, Any]:
    """复用在跑的 serve，或拉起一个；版本 / 安装配置不一致就重启。返回给扩展的回复。"""
    cfg = load_config()
    if cfg is None:
        raise HostError("not_installed", "没有安装配置：在终端跑一次 xiaoyu chrome install")
    with _locked():
        state = load_state()
        restarted = False
        if state is not None and pid_alive(state["pid"]):
            body = probe(state["port"], state["token"])
            ours = body is not None and str(body.get("root")) == state.get("workspace")
            if ours and body.get("version") == current_version() \
                    and state.get("fingerprint") == cfg.fingerprint():
                return _reply(state, started=False)
            if ours:
                #  版本或配置变了：直接重启（2026-11 定，正在跑的那一轮会断）。
                #  只停确认是自家的进程——对不上 token 的 pid 可能已被别的进程复用
                stop(state["pid"], grace=grace)
                restarted = True
        if serve_deps_missing():
            raise HostError("serve_unavailable",
                            "serve 需要 fastapi 和 uvicorn：pip install 'xiaoyu-agent[serve]'")
        state = start(cfg, timeout=timeout, argv_for=argv_for)
        return _reply(state, started=True, restarted=restarted)


def status() -> dict[str, Any]:
    """不拉起任何东西，只报状态。"""
    cfg = load_config()
    state = load_state()
    running = False
    version = ""
    if state is not None and pid_alive(state["pid"]):
        body = probe(state["port"], state["token"])
        if body is not None:
            running = True
            version = str(body.get("version") or "")
    return {
        "ok": True, "installed": cfg is not None, "running": running,
        "baseUrl": f"http://127.0.0.1:{state['port']}" if running and state else "",
        "version": version, "hostVersion": current_version(),
    }


def stop_running(grace: float = STOP_GRACE) -> bool:
    """停 host 拉起的那个 serve（确认 token 对得上才动）；停了返回 True。"""
    with _locked():
        state = load_state()
        if state is None or not pid_alive(state["pid"]):
            return False
        if probe(state["port"], state["token"]) is None:
            return False
        stop(state["pid"], grace=grace)
        with contextlib.suppress(OSError):
            state_path().unlink()
        return True


def _reply(state: dict[str, Any], *, started: bool, restarted: bool = False) -> dict[str, Any]:
    return {
        "ok": True, "baseUrl": f"http://127.0.0.1:{state['port']}", "token": state["token"],
        "version": state.get("version", ""), "started": started, "restarted": restarted,
    }


# ---------- 帧 ----------


def read_frame(stream) -> Any:
    """4 字节本机字节序长度 + UTF-8 JSON。流结束返回 None。"""
    header = _read_exact(stream, 4)
    if header is None:
        return None
    (length,) = struct.unpack("=I", header)
    if length > MAX_INCOMING:
        raise HostError("too_large", f"消息 {length} 字节，超过上限 {MAX_INCOMING}")
    body = _read_exact(stream, length)
    if body is None:
        raise HostError("truncated", "消息被截断")
    try:
        return json.loads(body.decode("utf-8", errors="replace"))
    except ValueError as exc:
        raise HostError("bad_json", f"消息不是 JSON：{exc}") from None


def _read_exact(stream, size: int) -> bytes | None:
    chunks = b""
    while len(chunks) < size:
        chunk = stream.read(size - len(chunks))
        if not chunk:
            return None
        chunks += chunk
    return chunks


def encode_frame(message: Any) -> bytes:
    body = json.dumps(message, ensure_ascii=False).encode("utf-8")
    return struct.pack("=I", len(body)) + body


def handle(message: Any) -> dict[str, Any]:
    kind = message.get("type") if isinstance(message, dict) else None
    try:
        if kind == "ensure":
            return ensure()
        if kind == "status":
            return status()
        raise HostError("unknown_type", f"不认识的消息类型：{kind!r}")
    except HostError as exc:
        return {"ok": False, "code": exc.code, "message": str(exc), **exc.extra}


def _log(text: str) -> None:
    with contextlib.suppress(OSError):
        path = base_dir() / "host.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {text}\n")


def host_main(argv: list[str]) -> int:
    """`xiaoyu chrome host [<origin>]`：只给 Chrome 调。读一帧、回一帧、退出。"""
    out_fd = os.dup(1)
    os.dup2(2, 1)
    sys.stdout = sys.stderr
    out = os.fdopen(out_fd, "wb", buffering=0)
    origin = argv[0] if argv else ""
    try:
        kind = None
        try:
            message = read_frame(sys.stdin.buffer)
        except HostError as exc:
            reply = {"ok": False, "code": exc.code, "message": str(exc)}
        else:
            if message is None:
                return 0
            kind = message.get("type") if isinstance(message, dict) else None
            reply = handle(message)
        _log(f"origin={origin} type={kind} ok={reply.get('ok')} code={reply.get('code', '')}")
        out.write(encode_frame(reply))
        return 0
    except Exception as exc:  # noqa: BLE001 — 任何意外都回一帧，别让扩展干等
        _log(f"origin={origin} crash {type(exc).__name__}: {exc}")
        with contextlib.suppress(OSError):
            out.write(encode_frame({"ok": False, "code": "internal", "message": str(exc)}))
        return 1
    finally:
        with contextlib.suppress(OSError):
            out.close()
