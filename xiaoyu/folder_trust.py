"""folder trust 门：工作区级"可执行配置"的一次性信任门。

堵的洞：`.mcp.json`（启动即拉起进程）、`.xiaoyu/permissions.txt`（allow 规则免
确认）、工作区 `.env`（能改写端点/开关——把用户的 key 引到别人的网关）都是
**clone 即生效**的配置。mcp_guard 的准入/OSV/指纹只覆盖"配置内容像不像攻击"，
覆盖不了"这份配置本来就不该被信"。这道门补的是后者：陌生仓库第一次要启用
这类配置时问一次，答案按 git 根记进用户级信任表，之后不再问。

六条优先级（顺序即语义，测试锁死）：
1. 功能开关关闭 → 信任（保持旧行为）；
2. 信任表里自身或祖先记为 trusted → 信任；
3. 信任键不可记录（过宽的根：文件系统根 / 家目录 / 相对路径）→ 直接信任
   ——这类键在信任表的读写两侧都被拒绝，此处若拦就是"每次启动都问一个
   永远存不下来的问题"（无限重问），所以与功能关闭同样放行；
4. 工作区没有任何可执行配置 → 信任（没东西可管）；
5. 交互式终端 → 问用户；
6. 其余（headless / --wire / 管道）→ 不信任（配置被忽略并告警）。

规则 3、4 的放行是临时判定、不落盘：git pull 之后冒出来的 .mcp.json
下次启动照样会被检查，不靠一条陈旧的放行记录长期蒙混。

嵌入宿主（库层调用方）不经这道门：门是 CLI 启动期的关卡，库层 Config 默认
workspace_trusted=True，宿主要门自己调 evaluate() 再传进来。
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .config import _parse_dotenv, home_dir, user_config_dir, user_env_path

#  功能开关。只认真实环境变量与**用户级** .env——工作区 .env 是被门管的对象，
#  它自己不能把门关掉（否则恶意仓库放一行 XIAOYU_FOLDER_TRUST=0 即绕过）。
ENABLE_ENV = "XIAOYU_FOLDER_TRUST"

_OFF_VALUES = ("0", "false", "no", "off")


def enabled() -> bool:
    flag = os.environ.get(ENABLE_ENV)
    if flag is None:
        flag = _parse_dotenv(user_env_path()).get(ENABLE_ENV)
    if flag is None:
        return True
    return flag.strip().lower() not in _OFF_VALUES


def trust_store_path() -> Path:
    return user_config_dir() / "trusted_folders.json"


# ---------- 可执行配置探测 ----------

#  kind → 给用户看的一句话（问询与告警共用，不各写一份）
KIND_LABELS = {
    "mcp": ".mcp.json（启动时拉起 MCP server 进程）",
    "permission": ".xiaoyu/permissions.txt（allow 规则可免确认执行命令）",
    "env": ".env（可改写小羽的端点与开关配置）",
}


def _present_or_uncertain(probe) -> bool:
    """探测出错按"存在"处理（fail-secure：拿不准就当有，宁多问不漏问）。"""
    try:
        return bool(probe())
    except OSError:
        return True


def _has_effective_lines(path: Path) -> bool:
    """文件里有任何非空、非注释行。不存在 → False。"""
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return False
    return any(line.strip() and not line.strip().startswith("#") for line in raw.splitlines())


def repo_config_kinds(workspace: Path) -> list[str]:
    """工作区里存在的可执行配置种类（探测顺序即报告顺序，去重）。

    探测与消费必须对齐：这里列的每一项，不信任时都真的会被跳过
    （mcp.load_server_specs / permissions.Permissions.load / config.load_dotenv），
    否则就是"问了却没管住"或"管住了却没问"。
    """
    kinds: list[str] = []
    if _present_or_uncertain(lambda: (workspace / ".mcp.json").is_file()):
        kinds.append("mcp")
    if _present_or_uncertain(
        lambda: _has_effective_lines(workspace / ".xiaoyu" / "permissions.txt")
    ):
        kinds.append("permission")
    if _present_or_uncertain(lambda: _has_effective_lines(workspace / ".env")):
        kinds.append("env")
    return kinds


# ---------- 信任键 ----------


def unsafe_trust_root(path: Path) -> bool:
    """过宽的信任根：相对路径（是一切路径的"前缀"）、文件系统根、家目录。

    这类键写进信任表等于信任半个世界，所以 record_decision 拒写、
    stored_verdict 读时跳过；decide 对它们直接放行（理由见模块 docstring 第 3 条）。
    """
    if not path.is_absolute():
        return True
    if path.parent == path:  # 文件系统根：/ 或 C:\
        return True
    home = home_dir()
    if home is not None:
        try:
            if path.resolve() == home.resolve():
                return True
        except OSError:
            return True
    return False


def workspace_key(workspace: Path) -> Path:
    """信任决定记在哪个路径名下：git 根（clone 是按仓库为单位信任的）。

    git 根过宽（家目录本身是仓库的 dotfiles 场景）→ 收窄回工作区自己；
    工作区自己仍过宽 → 原样返回，交由 decide 按"不可记录"放行。
    """
    try:
        resolved = workspace.resolve()
    except OSError:
        return workspace
    root = resolved
    for candidate in (resolved, *resolved.parents):
        if (candidate / ".git").exists():
            root = candidate
            break
    if unsafe_trust_root(root) and not unsafe_trust_root(resolved):
        return resolved
    return root


# ---------- 信任表读写 ----------


def _load_store() -> dict:
    import json

    try:
        data = json.loads(trust_store_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def stored_verdict(key: Path, store: dict | None = None) -> bool | None:
    """查信任表：最长前缀匹配（最具体的记录获胜），没有记录返回 None。

    同深度的并列记录（手改文件造出的别名）须全部 trusted 才算 trusted
    （fail-closed）；过宽根的记录读时跳过——手改文件也造不出全局放行。
    """
    if store is None:
        store = _load_store()
    folders = store.get("folders")
    if not isinstance(folders, dict):
        return None
    best_depth = -1
    verdict: bool | None = None
    for raw, record in folders.items():
        if not isinstance(record, dict):
            continue
        entry = Path(raw)
        if unsafe_trust_root(entry):
            continue
        if entry != key and entry not in key.parents:
            continue
        depth = len(entry.parts)
        trusted = bool(record.get("trusted"))
        if depth > best_depth:
            best_depth, verdict = depth, trusted
        elif depth == best_depth and verdict is not None:
            verdict = verdict and trusted
    return verdict


def record_decision(key: Path, trusted: bool) -> Path | None:
    """把决定写进信任表（原子写、0600）。过宽的根拒写，返回 None。"""
    if unsafe_trust_root(key):
        return None
    from .mcp_guard import save_json_atomic

    store = _load_store()
    folders = store.setdefault("folders", {})
    if not isinstance(folders, dict):
        folders = store["folders"] = {}
    folders[str(key)] = {
        "trusted": trusted,
        "decided_at": datetime.now().isoformat(timespec="seconds"),
    }
    path = trust_store_path()
    save_json_atomic(path, store)
    return path


# ---------- 判定 ----------


@dataclass(frozen=True)
class TrustDecision:
    verdict: str  # "trusted" | "prompt" | "untrusted"
    key: Path
    kinds: tuple[str, ...]

    @property
    def trusted(self) -> bool:
        return self.verdict == "trusted"


def decide(
    *,
    feature_enabled: bool,
    store_trusted: bool | None,
    key_recordable: bool,
    configs_present: bool,
    interactive: bool,
) -> str:
    """六条优先级的纯函数形态（顺序即语义，见模块 docstring）。

    store_trusted 是三值：True=记录信任、False=记录不信任、None=没记录。
    记录过"不信任"的目录不再重问（跳过第 5 条直接不信任）——用户已经表过态，
    每次启动都追问同一个问题是骚扰；反悔用 `xiaoyu --trust` 一次改写。
    """
    if not feature_enabled:
        return "trusted"
    if store_trusted is True:
        return "trusted"
    if not key_recordable:
        return "trusted"
    if not configs_present:
        return "trusted"
    if store_trusted is False:
        return "untrusted"
    if interactive:
        return "prompt"
    return "untrusted"


def evaluate(workspace: Path, interactive: bool) -> TrustDecision:
    """CLI 启动期的入口：算出 verdict（"prompt" 留给调用方去问）。"""
    key = workspace_key(workspace)
    kinds = tuple(repo_config_kinds(workspace))
    verdict = decide(
        feature_enabled=enabled(),
        store_trusted=stored_verdict(key),
        key_recordable=not unsafe_trust_root(key),
        configs_present=bool(kinds),
        interactive=interactive,
    )
    return TrustDecision(verdict, key, kinds)


def ask_user(decision: TrustDecision) -> bool:
    """终端问询（stderr + stdin，刻意的最简形态）。

    空输入、EOF、任何非 yes 一律按"不信任"——fail-closed。
    y → 记录信任（之后不再问）；n → 记录不信任（之后也不再问，静默降级；
    反悔用 `xiaoyu --trust`）。
    """
    found = "、".join(KIND_LABELS.get(kind, kind) for kind in decision.kinds)
    print(
        f"\n该目录带有仓库级可执行配置，启动即生效：\n"
        f"  目录：{decision.key}\n"
        f"  发现:{found}\n"
        f"信任这个目录的作者并启用这些配置吗？[y/N] ",
        end="",
        file=sys.stderr,
        flush=True,
    )
    try:
        answer = input().strip().lower()
    except (EOFError, KeyboardInterrupt):
        print(file=sys.stderr)
        answer = ""
    trusted = answer in ("y", "yes")
    record_decision(decision.key, trusted)
    return trusted


def untrusted_note(decision: TrustDecision) -> str:
    """不信任时给用户的一行说明（CLI 打到 stderr / banner 下方）。"""
    names = {"mcp": ".mcp.json", "permission": ".xiaoyu/permissions.txt", "env": ".env"}
    found = " / ".join(names.get(kind, kind) for kind in decision.kinds)
    return (
        f"工作区未受信任：仓库级 {found} 本次不生效"
        "（信任请运行 xiaoyu --trust，或删除信任表里的记录后重答）"
    )
