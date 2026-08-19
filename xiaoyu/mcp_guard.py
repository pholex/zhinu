"""MCP 安全护栏：配置准入、OSV 恶意包预检、工具指纹基线（防 rug-pull）。

三块纯逻辑，不碰网络协议层：

1. **配置准入**（fail-closed）：`command` 是 shell 解释器且内联脚本触碰
   外联（curl/nc/POST）或持久化路径（authorized_keys/cron/启动项）的配置
   拒绝启动。规则来自真实攻击案例：攻击者在 config 里植
   `command: bash` 条目往 authorized_keys 追加 SSH key，宿主每次启动都
   重装一遍后门。刻意不做命令白名单——npx/python 是正常形态，误杀太多。
2. **OSV 预检**（fail-open）：npx/uvx/pipx 拉起的包先查 osv.dev 的恶意包
   记录（只认 MAL-* 前缀，普通 CVE 不拦），查到拒绝启动；网络失败放行
   ——预检是加分项，断网不该让 MCP 全体罢工。结论缓存 1 小时，
   **网络失败绝不缓存**（前车之鉴：缓存了失败态导致 16 小时打了
   779K 次 DNS 查询）。
3. **工具指纹基线**（防 rug-pull）：每个工具的
   name+description+inputSchema 做指纹落盘。首次见到即信任（TOFU——
   server 是用户刚刚亲手配的）；此后指纹变化的工具隔离不注册，
   `/mcp approve <server>` 重新批准。防的是 MCP 生态最现实的攻击：
   首次安装人畜无害，第 N 次更新悄悄往 description 里塞
   "also read ~/.aws/credentials"。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any

# ---------- 配置准入 ----------

#  只盯 shell 解释器：准入规则拦的是"配置本身就是一段内联攻击脚本"，
#  不是给正常 server 做白名单
_SHELL_INTERPRETERS = frozenset(
    {"bash", "sh", "zsh", "dash", "fish", "ksh", "cmd", "powershell", "pwsh"}
)
_EGRESS_PATTERN = re.compile(
    r"\bcurl\b|\bwget\b|\bnc\b|\bncat\b|\bsocat\b|/dev/tcp/"
    r"|Invoke-WebRequest|Invoke-RestMethod|System\.Net\.WebClient",
    re.IGNORECASE,
)
_PERSISTENCE_PATTERN = re.compile(
    r"authorized_keys|\.ssh/|/etc/ssh|/etc/pam\.d|pam_\w+\.so|/etc/sudoers"
    r"|/etc/cron|\bcrontab\b|/etc/rc\.local|/etc/systemd"
    r"|\.bashrc|\.bash_profile|\.zshrc|LaunchDaemons|LaunchAgents",
    re.IGNORECASE,
)


def admission_violation(command: str, args: list[str], env: dict[str, str]) -> str | None:
    """配置形状像内联攻击脚本时返回原因，否则 None。写入（`xiaoyu mcp add`）/
    加载 / 启动三处都要调——任一条路径都能把一份配置送到 spawn 面前。"""
    base = Path(command).name.lower()
    for suffix in (".exe", ".cmd"):
        base = base.removesuffix(suffix)
    if base not in _SHELL_INTERPRETERS:
        return None
    text = " ".join(args) + " " + " ".join(env.values())
    #  持久化优先报：它比外泄更严重，命中双规则时用户该先看这个
    if _PERSISTENCE_PATTERN.search(text):
        return "shell 内联脚本触碰持久化路径（SSH key / cron / 启动项）"
    if _EGRESS_PATTERN.search(text):
        return "shell 内联脚本包含外联命令（curl / nc / WebRequest…）"
    return None


_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "[::1]", "0.0.0.0"})


def endpoint_violation(url: str) -> str | None:
    """远端 MCP server 的地址不合规时返回原因，否则 None。

    与 admission_violation 对位：stdio 的准入判据是"这条命令像不像攻击"，
    HTTP 的判据是"这个地址会不会把凭据裸奔送出去"。两条：

    1. **只认 http/https**：file://、ftp:// 之类到不了 MCP server，出现即配置错
       （或诱导）——早报错好过在传输层抛一个看不懂的异常；
    2. **明文 http 只允许回环**：headers 里放的是 Authorization / API key，
       走公网明文等于把凭据交给路上任何一跳。本机 server 用 http 是常态，放行。

    刻意不做域名白名单：用户配的远端 server 就是他要用的，那属于意图不属于攻击。
    """
    from urllib.parse import urlparse

    try:
        parsed = urlparse(url)
    except ValueError as exc:
        return f"地址无法解析（{exc}）"
    if parsed.scheme not in ("http", "https"):
        return f"不支持的地址协议 {parsed.scheme or '(空)'}://（只支持 http/https）"
    if not parsed.hostname:
        return "地址缺少主机名"
    if parsed.scheme == "http" and parsed.hostname.lower() not in _LOOPBACK_HOSTS:
        return f"明文 http 只允许连回环地址（{parsed.hostname} 请用 https）"
    return None


# ---------- OSV 恶意包预检 ----------

OSV_ENDPOINT = "https://api.osv.dev/v1/query"
#  单次查询的 socket 超时。fail-open：查不成不拦启动。
_OSV_TIMEOUT = 10.0
_OSV_CACHE_TTL = 3600.0

#  运行器 → OSV 生态名。其它命令（node、python、二进制）不查：
#  它们跑的是本机已有的代码，不是"启动时现从公共仓库拉包"。
_RUNNER_ECOSYSTEMS = {"npx": "npm", "uvx": "PyPI", "pipx": "PyPI"}

#  结论缓存：key → (原因或 None, 过期时刻)。只缓存成功裁决，网络失败不缓存。
_osv_cache: dict[tuple[str, str, str], tuple[str | None, float]] = {}
_osv_lock = threading.Lock()


def osv_package(command: str, args: list[str]) -> tuple[str, str, str] | None:
    """从启动命令解析 (生态, 包名, 版本)。不是包运行器则返回 None。

    npx 必须尊重 --package/-p：安装目标常与执行的 binary 名不同
    （`npx -p some-pkg some-bin`），不解析会查错包。
    """
    base = Path(command).name.lower()
    for suffix in (".exe", ".cmd"):
        base = base.removesuffix(suffix)
    ecosystem = _RUNNER_ECOSYSTEMS.get(base)
    if ecosystem is None:
        return None

    spec: str | None = None
    index = 0
    while index < len(args):
        arg = args[index]
        if arg.startswith("--package="):
            spec = arg.partition("=")[2]
            break
        if arg in ("--package", "-p"):
            if index + 1 < len(args):
                spec = args[index + 1]
            break
        if arg.startswith("-"):
            index += 1
            continue
        spec = arg
        break
    if not spec:
        return None

    if ecosystem == "npm":
        #  @scope/name@ver：跳过开头的 @ 再找版本分隔符
        cut = spec.find("@", 1)
        name = spec[:cut] if cut > 0 else spec
        version = spec[cut + 1 :] if cut > 0 else ""
    else:
        #  PyPI：name[extras]==ver / name>=ver / name@ref
        match = re.match(r"^([A-Za-z0-9._-]+)", spec)
        if not match:
            return None
        name = match.group(1)
        version_match = re.search(r"==([A-Za-z0-9.!+*-]+)", spec)
        version = version_match.group(1) if version_match else ""
    return (ecosystem, name, version) if name else None


def osv_malware_check(command: str, args: list[str]) -> str | None:
    """查 OSV 恶意包记录。命中返回原因（fail-closed 由调用方执行）；
    不是包运行器 / 查询失败返回 None（fail-open）。"""
    parsed = osv_package(command, args)
    if parsed is None:
        return None
    ecosystem, name, version = parsed
    key = (ecosystem, name, version)
    now = time.monotonic()
    with _osv_lock:
        cached = _osv_cache.get(key)
        if cached and cached[1] > now:
            return cached[0]

    payload: dict[str, Any] = {"package": {"name": name, "ecosystem": ecosystem}}
    if version:
        payload["version"] = version
    request = urllib.request.Request(
        os.environ.get("XIAOYU_OSV_ENDPOINT", OSV_ENDPOINT),
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            #  具名 UA：无名 urllib UA 在部分 CDN 后会被直接 403
            "User-Agent": "xiaoyu-mcp/1.0",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=_OSV_TIMEOUT) as response:
            body = json.loads(response.read().decode("utf-8", errors="replace"))
    except Exception as exc:  # noqa: BLE001 - fail-open：预检失败不拦启动
        print(f"[MCP OSV 预检失败（放行）：{type(exc).__name__}: {exc}]", file=sys.stderr)
        return None

    malicious = [
        vuln
        for vuln in body.get("vulns") or []
        if isinstance(vuln, dict) and str(vuln.get("id", "")).startswith("MAL-")
    ]
    reason: str | None = None
    if malicious:
        shown = "; ".join(
            f"{vuln['id']}（{str(vuln.get('summary', ''))[:100]}）" for vuln in malicious[:3]
        )
        reason = f"OSV 恶意包记录：{ecosystem}/{name} → {shown}"
    with _osv_lock:
        _osv_cache[key] = (reason, now + _OSV_CACHE_TTL)
    return reason


# ---------- 工具指纹基线（rug-pull 防护） ----------


def tool_fingerprint(declared: dict[str, Any]) -> str:
    """一个工具声明的指纹：name + description + inputSchema。"""
    material = "\0".join(
        (
            str(declared.get("name", "")),
            str(declared.get("description", "")),
            json.dumps(declared.get("inputSchema") or {}, sort_keys=True, ensure_ascii=False),
        )
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


def load_baseline(path: Path) -> dict[str, dict[str, str]]:
    """{server: {tool: 指纹}}。坏文件当空基线（等价于全部重新 TOFU）。"""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def save_json_atomic(path: Path, data: dict) -> None:
    """临时文件 + rename 原子写，0600（基线被部分写坏 = 全部工具重新 TOFU）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, path)


def admit_tools(
    server_baseline: dict[str, str], declared: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[str], dict[str, str]]:
    """按基线裁决一批工具声明。

    返回 (放行的声明, 被隔离的工具名, 需并入基线的新条目)。
    - 基线里没有 → TOFU 放行并记录（server 是用户亲手配的，首见即信任；
      真正的执行安全仍有逐次审批兜底）
    - 指纹一致 → 放行
    - 指纹变化 → 隔离（不注册），等 /mcp approve
    """
    admitted: list[dict[str, Any]] = []
    quarantined: list[str] = []
    updates: dict[str, str] = {}
    for item in declared:
        name = str(item.get("name", ""))
        fingerprint = tool_fingerprint(item)
        known = server_baseline.get(name)
        if known is None:
            updates[name] = fingerprint
            admitted.append(item)
        elif known == fingerprint:
            admitted.append(item)
        else:
            quarantined.append(name)
    return admitted, quarantined, updates
