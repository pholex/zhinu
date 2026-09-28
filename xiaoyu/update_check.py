"""新版本提示：交互式启动时告诉用户 PyPI 上有了新版。

发版频繁，而此前没有任何渠道让用户知道——`xiaoyu update` 要人自己想起来去跑。

几条边界，都是"提示不该变成打扰或负担"这一个意思：

- **只在交互式启动时**出现（横幅之后的一行）。`-p` 一次性模式、`--wire`、serve、
  ACP、库层嵌入一律不查也不提：那些场景的输出是给程序读的，宿主自己管升级。
- **不拖慢启动**：联网放在后台线程里，结果写进缓存；提示用的是**上一次**查到的
  结果。头一回知道有新版，是查到之后的下一次启动。
- **不在会话中途冒出来**：后台线程只写缓存、不打印（线程抢终端的风险大于
  早几分钟知道的收益）。
- 每 24 小时最多查一次；同一个新版本每 24 小时最多提一次——不打算升级的人
  不该每次启动都被提醒一遍。
- 查的是 PyPI 的简单索引（pip 实际读的那份，JSON API 有缓存、两个方向都会骗人）。
  请求只带版本号当 User-Agent，没有任何身份标识。
- 从源码目录直接跑、或可编辑安装的不提示：那是在开发小羽，升级靠 git。
- `XIAOYU_UPDATE_CHECK=0` 整个关掉。

任何一步失败（断网、索引格式变了、缓存写不进去）都安静地当作"没有新版"：
这是便利，出了错不该让人看见，更不该拦住启动。
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from . import __version__, fsguard
from .config import user_config_dir

ENABLE_ENV = "XIAOYU_UPDATE_CHECK"
DISTRIBUTION = "xiaoyu-agent"
INDEX_URL = f"https://pypi.org/simple/{DISTRIBUTION}/"
#  两次联网之间、同一版本两次提示之间的最短间隔（秒）
CHECK_INTERVAL = 24 * 3600.0
NOTICE_INTERVAL = 24 * 3600.0
#  后台线程里的请求超时。线程是 daemon，进程要退时不等它
TIMEOUT = 5.0
#  索引页的读取上限：正常几十 KB，给足余量但不无限读
MAX_BYTES = 4 * 1024 * 1024

_OFF_VALUES = ("0", "false", "no", "off")
#  只认纯数字的正式版本号：预发布（0.53.0rc1）、本地版本（+local）不拿来提示
_VERSION = re.compile(r"\d+(?:\.\d+)*")
#  发布物文件名里的版本段：xiaoyu_agent-0.52.0-py3-none-any.whl / xiaoyu_agent-0.52.0.tar.gz
_FILENAME = re.compile(r"^xiaoyu[-_.]agent-([^-]+?)(?:-|\.tar\.gz$|\.zip$)", re.IGNORECASE)


def enabled() -> bool:
    return os.environ.get(ENABLE_ENV, "").strip().lower() not in _OFF_VALUES


def state_path() -> Path:
    return user_config_dir() / "update_check.json"


def parse_version(text: str) -> tuple[int, ...] | None:
    """`0.52.0` → (0, 52, 0)；不是纯数字的正式版本号返回 None。"""
    text = text.strip()
    if not _VERSION.fullmatch(text):
        return None
    return tuple(int(part) for part in text.split("."))


def latest_in_index(payload: Any) -> str | None:
    """简单索引（PEP 691 JSON）里能装到的最高正式版本；看不懂返回 None。

    只数没被撤回（yanked）的发布物：撤回的版本 pip 默认不装，提示它等于指路到
    一个装不上的版本。
    """
    files = payload.get("files") if isinstance(payload, dict) else None
    if not isinstance(files, list):
        return None
    best: tuple[int, ...] | None = None
    best_text: str | None = None
    for item in files:
        if not isinstance(item, dict) or item.get("yanked"):
            continue
        found = _FILENAME.match(str(item.get("filename", "")))
        if found is None:
            continue
        version = parse_version(found.group(1))
        if version is not None and (best is None or version > best):
            best, best_text = version, found.group(1)
    return best_text


def fetch_latest(timeout: float = TIMEOUT) -> str | None:
    """联网查一次最高版本。任何失败都返回 None。"""
    from . import netproxy

    request = urllib.request.Request(
        INDEX_URL,
        headers={
            "Accept": "application/vnd.pypi.simple.v1+json",
            "User-Agent": f"xiaoyu/{__version__}",
        },
    )
    try:
        with netproxy.urlopen(request, timeout=timeout) as response:
            raw = response.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            return None
        return latest_in_index(json.loads(raw.decode("utf-8", errors="replace")))
    except urllib.error.HTTPError as exc:
        #  4xx / 5xx 的异常对象本身就是一个开着的响应：不关就是把连接留给垃圾回收
        exc.close()
        return None
    except Exception:  # noqa: BLE001 - 断网、代理、坏 JSON……一律当作没查到
        return None


def _load_state() -> dict[str, Any]:
    try:
        data = json.loads(state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_state(state: dict[str, Any]) -> None:
    try:
        fsguard.write_atomic(state_path(), json.dumps(state, ensure_ascii=False))
    except (OSError, TypeError, ValueError):
        pass


def _number(value: Any) -> float:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0


def source_checkout() -> bool:
    """正在跑的代码是不是一份源码检出（包目录旁边就是 pyproject.toml）。

    装进 site-packages 的包旁边不会有它。比看安装记录可靠：源码目录里留着的
    egg-info 会被当成一份普通安装，可编辑安装的记录也可能早就过期。
    """
    return (Path(__file__).resolve().parent.parent / "pyproject.toml").is_file()


def upgrade_command() -> str | None:
    """这份安装该怎么升级；不该提示升级的（开发中的源码）返回 None。"""
    from importlib import metadata

    from . import diagnostics

    if source_checkout():
        return None
    try:
        dist = metadata.distribution(DISTRIBUTION)
    except metadata.PackageNotFoundError:
        return None
    form = diagnostics.install_form(dist)
    if form.startswith("可编辑"):
        return None
    if form == "pipx":
        return f"pipx upgrade {DISTRIBUTION}"
    if form == "uv tool":
        return f"uv tool upgrade {DISTRIBUTION}"
    return "xiaoyu update"


def pending_notice(current: str = __version__, now: float | None = None) -> str | None:
    """按缓存判断这次启动要不要提一句；要提就返回那句话，并记下提过了。"""
    running = parse_version(current)
    if running is None:
        return None
    state = _load_state()
    latest_text = state.get("latest")
    latest = parse_version(latest_text) if isinstance(latest_text, str) else None
    if latest is None or latest <= running:
        return None
    now = time.time() if now is None else now
    if state.get("notified_version") == latest_text:
        since = now - _number(state.get("notified_at"))
        #  时钟被往回拨过（since < 0）就当作没提过，别因此永远不提
        if 0 <= since < NOTICE_INTERVAL:
            return None
    command = upgrade_command()
    if command is None:
        return None
    state["notified_version"] = latest_text
    state["notified_at"] = now
    _save_state(state)
    return (
        f"有新版本 {latest_text}（当前 {current}）：{command} 升级；"
        f"不想看到这条提示设 {ENABLE_ENV}=0"
    )


def check_due(now: float | None = None) -> bool:
    checked_at = _load_state().get("checked_at")
    if not isinstance(checked_at, (int, float)) or isinstance(checked_at, bool):
        return True  # 从没查过（或缓存坏了）
    since = (time.time() if now is None else now) - float(checked_at)
    #  时钟被往回拨过（since < 0）也算到点，别因此永远不查
    return since < 0 or since >= CHECK_INTERVAL


def refresh(now: float | None = None) -> None:
    """联网查一次并写进缓存。查没查到都记下时间：断着网不该每次启动都去试。"""
    latest = fetch_latest()
    state = _load_state()
    state["checked_at"] = time.time() if now is None else now
    if latest is not None:
        state["latest"] = latest
    _save_state(state)


def refresh_in_background() -> threading.Thread | None:
    """到点了就在后台线程里查；没到点返回 None。线程只写缓存，不碰终端。"""
    if not check_due():
        return None
    thread = threading.Thread(target=refresh, name="xiaoyu-update-check", daemon=True)
    thread.start()
    return thread


def startup_notice(interactive: bool | None = None) -> str | None:
    """交互式启动时调用：返回要打印的那一行（没有就 None），并按需触发后台检查。"""
    if interactive is None:
        interactive = sys.stdin.isatty() and sys.stdout.isatty()
    if not interactive or not enabled():
        return None
    try:
        notice = pending_notice()
        refresh_in_background()
    except Exception:  # noqa: BLE001 - 提示出了错不该让人看见，更不该拦住启动
        return None
    return notice
