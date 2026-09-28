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

**信任的是内容，不是路径。** 信任表里随记录存着当时那几份配置的指纹
（按工作区分别记）；规则 2 命中之后还要比一次指纹，对不上——git pull 带来
的改动、模型上一轮悄悄写进去的一行、换了个子目录启动——就当没有记录，
落到规则 5/6 重新问。配置被删掉不算变化（少了可执行的东西，不用问）。
没带指纹的旧记录（以及 record_decision 只记了信任没给指纹的）在第一次
过门时按当前内容补记，不追问——那份信任本来就是对着当时的内容给的。

嵌入宿主（库层调用方）不经这道门：门是 CLI 启动期的关卡，库层 Config 默认
workspace_trusted=True，宿主要门自己调 evaluate() 再传进来。
"""

from __future__ import annotations

import hashlib
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from . import fsguard
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


#  写入目标按路径分量认的"可执行配置"：文件名命中，或落在这些目录之下。
#  比 repo_config_kinds 宽——那边只管工作区根上小羽自己启动时会读的三样，
#  这里管的是"模型这一笔写下去，之后会有东西被执行"：子目录里的 .mcp.json
#  （换个目录启动就生效）、.git 里的 hooks 与 config（不进 git diff，事后看不见）
_GUARDED_FILES = {
    ".mcp.json": "MCP server 声明（启动时拉起进程）",
    ".env": "环境配置（可改写端点与开关）",
}
_GUARDED_DIRS = {
    ".xiaoyu": "小羽的工作区配置（权限规则、子 agent 声明）",
    ".git": "git 内部文件（hooks 与 config 会在跑 git 时被执行）",
}


def guarded_write_reason(target: Path) -> str | None:
    """这个写入目标是不是可执行配置；是就返回给用户看的说明。

    target 应当是 resolve 过的路径——符号链接指过去的、`sub/../.mcp.json` 这种
    绕路写法都该落在真实目标上判。按分量比、不分大小写（macOS / Windows 的
    文件系统默认不分，`.MCP.JSON` 写的是同一个文件）。
    """
    parts = [part.casefold() for part in target.parts]
    if not parts:
        return None
    for name, label in _GUARDED_DIRS.items():
        if name in parts:
            return f"{name}/：{label}"
    if label := _GUARDED_FILES.get(parts[-1]):
        return f"{target.name}：{label}"
    return None


def _present_or_uncertain(probe) -> bool:
    """探测出错按"存在"处理（fail-secure：拿不准就当有，宁多问不漏问）。"""
    try:
        return bool(probe())
    except OSError:
        return True


def _has_effective_lines(path: Path) -> bool:
    """文件里有任何非空、非注释行。不存在 → False。

    信任问询之前就会读：仓库可以提交指向 /dev/zero 的符号链接，整读会读不到头。
    非普通文件抛 NotRegularFile（OSError），由 _present_or_uncertain 按"存在"处理。
    """
    try:
        fsguard.require_regular(path)
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


#  kind → 工作区内的相对路径（探测、指纹、告警三处共用）
_KIND_FILES = {
    "mcp": (".mcp.json",),
    "permission": (".xiaoyu", "permissions.txt"),
    "env": (".env",),
}
#  指纹只读这么多字节；再大的"配置文件"按尺寸记，不整读
_FINGERPRINT_MAX_BYTES = 4 * 1024 * 1024


def config_fingerprints(
    workspace: Path, kinds: tuple[str, ...] | list[str] | None = None
) -> dict[str, str]:
    """当前工作区里每份可执行配置的内容指纹（kind → 摘要）。

    读不了的（特殊文件、权限不够）记成固定的 "unreadable"：它反正不会被消费
    （消费点同样要求普通文件），不必每次启动都为它重问一遍。
    """
    if kinds is None:
        kinds = repo_config_kinds(workspace)
    result: dict[str, str] = {}
    for kind in kinds:
        parts = _KIND_FILES.get(kind)
        if parts is None:
            continue
        path = workspace.joinpath(*parts)
        try:
            fsguard.require_regular(path)
            size = path.stat().st_size
            if size > _FINGERPRINT_MAX_BYTES:
                result[kind] = f"oversize:{size}"
                continue
            result[kind] = hashlib.sha256(path.read_bytes()).hexdigest()[:32]
        except OSError:
            result[kind] = "unreadable"
    return result


_ENV_REFERENCE = re.compile(r"\$\{(?:env:)?([A-Za-z_][A-Za-z0-9_]*)\}")


def mcp_env_references(workspace: Path) -> tuple[list[str], list[str]]:
    """工作区 .mcp.json 会读哪些环境变量：(全部, 其中属于小羽自己密钥的)。

    `${VAR}` 展开与 inheritEnv 点名都算。给问询用：一份会把你的模型密钥递给
    它自己那个 server 的配置，该让人在答 y 之前看见。读不了就返回空。
    """
    path = workspace / ".mcp.json"
    try:
        fsguard.require_regular(path)
        if path.stat().st_size > _FINGERPRINT_MAX_BYTES:
            return [], []
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return [], []
    names = dict.fromkeys(_ENV_REFERENCE.findall(raw))
    try:
        import json

        servers = json.loads(raw).get("mcpServers")
    except (ValueError, AttributeError):
        servers = None
    if isinstance(servers, dict):
        for entry in servers.values():
            inherit = entry.get("inheritEnv") if isinstance(entry, dict) else None
            for name in inherit if isinstance(inherit, list) else []:
                if isinstance(name, str) and name.strip():
                    names.setdefault(name.strip())
    if not names:
        return [], []
    from .tools import non_inheritable_env_names

    secrets = non_inheritable_env_names()

    def is_secret(name: str) -> bool:
        upper = name.upper()
        if upper.endswith("*"):
            prefix = upper[:-1]
            return not prefix or any(item.startswith(prefix) for item in secrets)
        return upper in secrets

    return list(names), [name for name in names if is_secret(name)]


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


def _matching_records(key: Path, store: dict) -> list[tuple[str, dict]]:
    """信任表里管着 key 的记录：最长前缀那一层的全部条目（通常就一条）。"""
    folders = store.get("folders")
    if not isinstance(folders, dict):
        return []
    best_depth = -1
    best: list[tuple[str, dict]] = []
    for raw, record in folders.items():
        if not isinstance(record, dict):
            continue
        entry = Path(raw)
        if unsafe_trust_root(entry):
            continue
        if entry != key and entry not in key.parents:
            continue
        depth = len(entry.parts)
        if depth > best_depth:
            best_depth, best = depth, [(raw, record)]
        elif depth == best_depth:
            best.append((raw, record))
    return best


def stored_verdict(key: Path, store: dict | None = None) -> bool | None:
    """查信任表：最长前缀匹配（最具体的记录获胜），没有记录返回 None。

    同深度的并列记录（手改文件造出的别名）须全部 trusted 才算 trusted
    （fail-closed）；过宽根的记录读时跳过——手改文件也造不出全局放行。
    """
    if store is None:
        store = _load_store()
    records = _matching_records(key, store)
    if not records:
        return None
    return all(bool(record.get("trusted")) for _, record in records)


def _workspace_id(workspace: Path) -> str:
    try:
        return str(workspace.resolve())
    except OSError:
        return str(workspace)


def _known_fingerprints(
    key: Path, workspace: Path, store: dict
) -> tuple[bool, dict[str, str] | None]:
    """(记录是否还没绑过内容, 记录里这个工作区的指纹)。

    没绑过内容 = 管着 key 的记录全都没有 configs 字段（旧版本写的，或只记了
    信任没给指纹）。绑过但没有这个工作区 = 这里的配置从没被看过，得问。
    """
    records = _matching_records(key, store)
    unbound = True
    for _, record in records:
        configs = record.get("configs")
        if not isinstance(configs, dict):
            continue
        unbound = False
        known = configs.get(_workspace_id(workspace))
        if isinstance(known, dict):
            return False, {str(k): str(v) for k, v in known.items()}
    return unbound, None


def _remember_fingerprints(key: Path, workspace: Path, fingerprints: dict[str, str]) -> None:
    """把指纹记到管着 key 的那条（那几条）信任记录上。落盘失败不拦启动。"""
    from .mcp_guard import save_json_atomic

    store = _load_store()
    records = _matching_records(key, store)
    if not records:
        return
    for _, record in records:
        configs = record.get("configs")
        if not isinstance(configs, dict):
            configs = record["configs"] = {}
        configs[_workspace_id(workspace)] = dict(fingerprints)
    try:
        save_json_atomic(trust_store_path(), store)
    except OSError:
        pass


def record_decision(
    key: Path,
    trusted: bool,
    workspace: Path | None = None,
    fingerprints: dict[str, str] | None = None,
) -> Path | None:
    """把决定写进信任表（原子写、0600）。过宽的根拒写，返回 None。

    workspace + fingerprints 一起给时，信任绑到这份内容上；同一条记录下别的
    工作区已经记过的指纹原样保留。不给则只记信任，指纹留到下次过门时补。
    """
    if unsafe_trust_root(key):
        return None
    from .mcp_guard import save_json_atomic

    store = _load_store()
    folders = store.setdefault("folders", {})
    if not isinstance(folders, dict):
        folders = store["folders"] = {}
    previous = folders.get(str(key))
    record: dict = {
        "trusted": trusted,
        "decided_at": datetime.now().isoformat(timespec="seconds"),
    }
    if trusted and workspace is not None and fingerprints is not None:
        kept = previous.get("configs") if isinstance(previous, dict) else None
        configs = dict(kept) if isinstance(kept, dict) else {}
        configs[_workspace_id(workspace)] = dict(fingerprints)
        record["configs"] = configs
    folders[str(key)] = record
    path = trust_store_path()
    save_json_atomic(path, store)
    return path


def resync_after_own_write(workspace: Path, before: dict[str, str]) -> None:
    """用户自己敲命令（`xiaoyu mcp add` 之类）改了工作区配置之后，同步指纹。

    不同步的话，用户刚亲手加的 server 下次启动就被当成"配置被人动过"问一遍。
    before 是动手之前的指纹：只有那时的内容本来就对得上记录（或记录还没绑过
    内容、或那时根本没有配置）才同步——否则会顺手把别人的改动一起认下来。
    """
    if not enabled():
        return
    key = workspace_key(workspace)
    store = _load_store()
    if stored_verdict(key, store) is not True:
        return
    unbound, known = _known_fingerprints(key, workspace, store)
    if unbound or known == before or (known is None and not before):
        _remember_fingerprints(key, workspace, config_fingerprints(workspace))


# ---------- 判定 ----------


@dataclass(frozen=True)
class TrustDecision:
    verdict: str  # "trusted" | "prompt" | "untrusted"
    key: Path
    kinds: tuple[str, ...]
    #  曾经信任过、但这几样的内容与当时对不上了（空 = 头一回见，或没变）
    changed: tuple[str, ...] = ()
    #  过门的工作区（信任键是 git 根，配置却是按工作区读的）
    workspace: Path | None = None

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
    feature_enabled = enabled()
    recordable = not unsafe_trust_root(key)
    store = _load_store()
    store_trusted = stored_verdict(key, store)
    changed: tuple[str, ...] = ()
    if feature_enabled and recordable and store_trusted is True and kinds:
        current = config_fingerprints(workspace, kinds)
        unbound, known = _known_fingerprints(key, workspace, store)
        if unbound:
            _remember_fingerprints(key, workspace, current)
        else:
            changed = tuple(
                kind for kind in kinds if (known or {}).get(kind) != current.get(kind)
            )
            if changed:
                #  内容对不上：这条记录信的不是眼前这份配置
                store_trusted = None
            elif known != current:
                #  只是少了几样（配置被删）：不用问，把记录收窄到现状
                _remember_fingerprints(key, workspace, current)
    verdict = decide(
        feature_enabled=feature_enabled,
        store_trusted=store_trusted,
        key_recordable=recordable,
        configs_present=bool(kinds),
        interactive=interactive,
    )
    return TrustDecision(verdict, key, kinds, changed, workspace)


def ask_user(decision: TrustDecision) -> bool:
    """终端问询（stderr + stdin，刻意的最简形态）。

    空输入、EOF、任何非 yes 一律按"不信任"——fail-closed。
    y → 记录信任（之后不再问）；n → 记录不信任（之后也不再问，静默降级；
    反悔用 `xiaoyu --trust`）。
    """
    found = "、".join(KIND_LABELS.get(kind, kind) for kind in decision.kinds)
    if decision.changed:
        changed = "、".join(KIND_LABELS.get(kind, kind) for kind in decision.changed)
        headline = (
            f"\n该目录的可执行配置与你上次信任时不一样了：\n"
            f"  目录：{decision.key}\n"
            f"  有变化：{changed}\n"
        )
        question = "看过改动、确认仍然信任并启用这些配置吗？[y/N] "
    else:
        headline = (
            f"\n该目录带有仓库级可执行配置，启动即生效：\n"
            f"  目录：{decision.key}\n"
            f"  发现:{found}\n"
        )
        question = "信任这个目录的作者并启用这些配置吗？[y/N] "
    reads = ""
    if decision.workspace is not None and "mcp" in decision.kinds:
        names, secrets = mcp_env_references(decision.workspace)
        if names:
            shown = "、".join(names[:12]) + ("…" if len(names) > 12 else "")
            reads = f"  .mcp.json 会读取环境变量：{shown}\n"
            if secrets:
                reads += (
                    f"  ⚠ 其中 {'、'.join(secrets)} 是小羽调模型用的密钥，"
                    "会被递给它声明的 server\n"
                )
    print(headline + reads + question, end="", file=sys.stderr, flush=True)
    try:
        answer = input().strip().lower()
    except (EOFError, KeyboardInterrupt):
        print(file=sys.stderr)
        answer = ""
    trusted = answer in ("y", "yes")
    if trusted and decision.workspace is not None:
        record_decision(
            decision.key, True, decision.workspace,
            config_fingerprints(decision.workspace, decision.kinds),
        )
    else:
        record_decision(decision.key, trusted)
    return trusted


def untrusted_note(decision: TrustDecision) -> str:
    """不信任时给用户的一行说明（CLI 打到 stderr / banner 下方）。"""
    names = {"mcp": ".mcp.json", "permission": ".xiaoyu/permissions.txt", "env": ".env"}
    found = " / ".join(names.get(kind, kind) for kind in decision.kinds)
    #  工作区自带的技能跟着这道门的结论走（不单独问、不记指纹）：没加载要说一声
    from . import skills

    if decision.workspace is not None and skills.has_project_skills(decision.workspace):
        found += " 与工作区自带的技能"
    if decision.changed:
        changed = " / ".join(names.get(kind, kind) for kind in decision.changed)
        return (
            f"工作区的可执行配置与上次信任时不一样了（{changed} 有变化）："
            f"仓库级 {found} 本次不生效（看过改动、确认无误后运行 xiaoyu --trust 重新信任）"
        )
    return (
        f"工作区未受信任：仓库级 {found} 本次不生效"
        "（信任请运行 xiaoyu --trust，或删除信任表里的记录后重答）"
    )
