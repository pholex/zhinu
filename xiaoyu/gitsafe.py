"""宿主侧自动 git 调用的加固：不吃仓库自带的 .git/config 与 hooks。

威胁：连同 .git 目录分发的仓库（zip 包、同步盘）能在 .git/config 里写
core.fsmonitor、filter/diff/merge 驱动、gpg.program，或往 .git/hooks 放钩子。
xiaoyu 自己起的 git——@ 补全的 ls-files、子 agent worktree 的建/查/删、
宸枢的 diff/merge、插件取包——不经信任门、用户无感知，一跑就等于替仓库
执行了代码。模型经 bash 发起的 git 不走这里：那条归审批管线管。

三层：

1. **环境**：隔离 system/global 配置（宿主的管道命令行为可预测），再用
   GIT_CONFIG_COUNT 注入命令级配置钉住执行面——命令级优先于仓库级，恶意
   .git/config 盖不回来。继承来的 GIT_CONFIG_PARAMETERS 在 COUNT 之后解析、
   会反盖钉值，丢掉；调用方的 `-c` 同理，挪进 COUNT 排在钉值前面。
2. **驱动**：filter/diff/merge 驱动挂在仓库自定的名字下，钉不了固定键——先
   列出本次会生效的配置键，把驱动命令逐个钉空。filter 钉空 = 不过滤；
   merge 驱动钉空 = 跑不起来退回冲突（fail-closed，不会悄悄取一边）。
3. **argv**：diff 家族补 `--no-ext-diff --no-textconv`（diff.external 不能
   钉空：空值会让 diff 直接报"cannot run"）。

隔离全局配置的两处代价已补回：safe.directory 只认 system/global/命令级，
从用户配置读出来经命令级转回；会产生提交的子命令（宸枢 merge）按 git 自己
的优先级查出生效身份、经 GIT_AUTHOR_*/GIT_COMMITTER_* 传入——不补的话
git 会静默退回 user@主机名 往用户仓库里写提交。
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from typing import Mapping, Sequence

#  辅助查询（列配置键、查身份、读 safe.directory）的超时：都是读本地配置文件
_LOOKUP_TIMEOUT = 10.0

#  执行面钉值：本地与联网两种模式共用
_EXEC_PINS: tuple[tuple[str, str], ...] = (
    ("core.fsmonitor", "false"),
    #  os.devnull 下建不出子路径，钩子一个都找不到。刻意不用临时空目录：
    #  沙箱内进程若能写临时区，就能往里放钩子等宿主来跑
    ("core.hooksPath", os.devnull),
    ("core.pager", "cat"),
    ("core.editor", ":"),
    ("sequence.editor", ":"),
    #  签名/验签会起 gpg.program（仓库可写成任意命令）
    ("commit.gpgSign", "false"),
    ("tag.gpgSign", "false"),
    ("merge.verifySignatures", "false"),
    ("log.showSignature", "false"),
    ("submodule.recurse", "false"),
    #  gc --auto / maintenance 是宿主在用户仓库里额外的写动作，还会再起子进程
    ("gc.auto", "0"),
    ("maintenance.auto", "false"),
)

#  传输面钉值：只给本地模式。本地动作本不该碰网络——partial clone 的懒取回
#  会借仓库里的 remote/credential/ssh 配置起进程；联网模式反而要靠用户配置
#  里的凭据与代理，不能钉
_LOCAL_PINS: tuple[tuple[str, str], ...] = (
    ("protocol.allow", "never"),
    ("credential.helper", ""),
    ("core.askPass", ""),
    ("core.sshCommand", "false"),
)

_DIFF_FAMILY = frozenset({"diff", "show", "log", "whatchanged", "format-patch"})
_NO_DRIVER_FLAGS = ("--no-ext-diff", "--no-textconv")

#  不做内容转换、碰不到驱动的子命令：省掉一次配置扫描（@ 补全每次按键都可能走到）
_NO_CONTENT = frozenset({"rev-parse", "branch", "ls-files", "config"})

#  会写提交对象、需要身份的子命令
_COMMITTING = frozenset({"commit", "merge", "cherry-pick", "revert", "rebase", "am", "stash", "notes", "tag"})

#  仓库自命名的驱动命令键（git 输出里节名与键名恒小写，子节保留原样）
_DRIVER_KEY = re.compile(
    r"^(?:filter\..+\.(?:clean|smudge|process)"
    r"|diff\..+\.(?:command|textconv)"
    r"|merge\..+\.driver)$"
)

_IDENTITY_SOURCES = {
    "GIT_AUTHOR_NAME": ("author.name", "user.name"),
    "GIT_AUTHOR_EMAIL": ("author.email", "user.email"),
    "GIT_COMMITTER_NAME": ("committer.name", "user.name"),
    "GIT_COMMITTER_EMAIL": ("committer.email", "user.email"),
}

_safe_directory_cache: dict[tuple[str | None, ...], list[tuple[str, str]]] = {}


def prepare(
    args: Sequence[str],
    cwd: os.PathLike[str] | str | None,
    *,
    env: Mapping[str, str] | None = None,
    network: bool = False,
) -> tuple[list[str], dict[str, str]]:
    """把 subcommand 在前的 git 参数（不含 "git"）加工成加固后的 (argv, env)。

    env 缺省取 os.environ；调用方已有自定义环境就传进来，在其上叠加。
    network=True 给联网取包：保留 system/global 配置（凭据、代理、证书都在
    那里）、不钉传输面、不补身份；执行面钉值与驱动扫描照旧——cwd 指向的
    仓库不一定是刚克隆出来的那个。
    """
    base = dict(os.environ if env is None else env)
    dash_c, rest = _split_dash_c(args)
    index = _subcommand_index(rest)
    subcommand = rest[index] if index is not None else None
    if subcommand in _DIFF_FAMILY:
        rest = [*rest[: index + 1], *_NO_DRIVER_FLAGS, *rest[index + 1 :]]

    pins = list(dash_c)
    if not network:
        pins += _safe_directories(base)
    pins += _EXEC_PINS
    if not network:
        pins += _LOCAL_PINS
    hardened = _compose(base, isolate=not network, pins=pins)

    if cwd is not None and subcommand not in _NO_CONTENT:
        _append_pins(hardened, _driver_pins(cwd, hardened))
    #  联网模式不隔离用户配置，git 自己就读得到身份
    if not network and cwd is not None and subcommand in _COMMITTING:
        _fill_identity(hardened, base, cwd, dash_c)
    return ["git", *rest], hardened


def _compose(base: Mapping[str, str], *, isolate: bool, pins: Sequence[tuple[str, str]]) -> dict[str, str]:
    env = dict(base)
    #  PARAMETERS 在 COUNT 之后解析，同键会反盖钉值
    env.pop("GIT_CONFIG_PARAMETERS", None)
    if isolate:
        env["GIT_CONFIG_NOSYSTEM"] = "1"
        env["GIT_CONFIG_SYSTEM"] = os.devnull
        env["GIT_CONFIG_GLOBAL"] = os.devnull
        env["GIT_NO_LAZY_FETCH"] = "1"
    env["GIT_TERMINAL_PROMPT"] = "0"
    _append_pins(env, pins)
    return env


def _append_pins(env: dict[str, str], pins: Sequence[tuple[str, str]]) -> None:
    """续编号追加命令级配置。继承的计数不是合法整数时 git 会整条拒跑，从 0 重编。"""
    if not pins:
        return
    raw = env.get("GIT_CONFIG_COUNT", "")
    count = int(raw) if raw.isdigit() else 0
    for key, value in pins:
        env[f"GIT_CONFIG_KEY_{count}"] = key
        env[f"GIT_CONFIG_VALUE_{count}"] = value
        count += 1
    env["GIT_CONFIG_COUNT"] = str(count)


def _split_dash_c(args: Sequence[str]) -> tuple[list[tuple[str, str]], list[str]]:
    """摘出 subcommand 之前的 `-c k=v`（git 同法：首个 = 切分，无 = 即 true）。"""
    pins: list[tuple[str, str]] = []
    rest: list[str] = []
    items = list(args)
    i = 0
    while i < len(items):
        token = items[i]
        if token == "-c" and i + 1 < len(items):
            key, sep, value = items[i + 1].partition("=")
            pins.append((key, value if sep else "true"))
            i += 2
            continue
        if token == "-C" and i + 1 < len(items):
            rest += items[i : i + 2]
            i += 2
            continue
        if token.startswith("-"):
            rest.append(token)
            i += 1
            continue
        rest += items[i:]
        break
    return pins, rest


def _subcommand_index(args: Sequence[str]) -> int | None:
    i = 0
    while i < len(args):
        if args[i] == "-C":
            i += 2
            continue
        if not args[i].startswith("-"):
            return i
        i += 1
    return None


def _lookup(args: list[str], cwd: os.PathLike[str] | str, env: Mapping[str, str]) -> str | None:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd,
            env=dict(env),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_LOOKUP_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return result.stdout


def _driver_pins(cwd: os.PathLike[str] | str, env: Mapping[str, str]) -> list[tuple[str, str]]:
    """本次会生效的驱动命令键逐个钉空。列不出来就不钉：真命令多半也跑不起来。"""
    listing = _lookup(["config", "--list", "--name-only", "-z"], cwd, env)
    if not listing:
        return []
    seen: dict[str, None] = {}
    for key in listing.split("\0"):
        if _DRIVER_KEY.match(key):
            seen.setdefault(key)
    return [(key, "") for key in seen]


def _safe_directories(base: Mapping[str, str]) -> list[tuple[str, str]]:
    """用户在 system/global 里配的 safe.directory，经命令级转回。

    git 只从这几级认它（仓库级自己给自己放行无效），隔离之后不转回，属主
    不一致的仓库（网络盘、共享 checkout）上宿主的 git 会全部拒跑。按决定读哪
    些配置文件的环境变量缓存，会话中途改全局配置要重启才生效。
    """
    cache_key = tuple(
        base.get(name)
        for name in ("HOME", "USERPROFILE", "XDG_CONFIG_HOME", "GIT_CONFIG_GLOBAL",
                     "GIT_CONFIG_SYSTEM", "GIT_CONFIG_NOSYSTEM", "PATH")
    )
    cached = _safe_directory_cache.get(cache_key)
    if cached is not None:
        return cached
    #  中立 cwd：只读 system/global 两级，别让所在仓库的配置混进来
    output = _lookup(
        ["config", "--show-scope", "--get-all", "safe.directory"],
        tempfile.gettempdir(),
        _compose(base, isolate=False, pins=_EXEC_PINS),
    )
    values: list[tuple[str, str]] = []
    for line in (output or "").splitlines():
        scope, _, value = line.partition("\t")
        if scope in ("system", "global"):
            values.append(("safe.directory", value))
    _safe_directory_cache[cache_key] = values
    return values


def _fill_identity(
    env: dict[str, str],
    base: Mapping[str, str],
    cwd: os.PathLike[str] | str,
    dash_c: Sequence[tuple[str, str]],
) -> None:
    """按 git 自己的优先级（命令级 > 仓库 > 全局 > 系统）查出生效身份，经环境变量传入。

    查询用未隔离的配置、带执行面钉值：`git config --list` 本身不起任何外部命令。
    用户环境里已有的 GIT_AUTHOR_* 等保持不动；哪项都查不到就不填，
    与用户自己跑 git 的回退行为一致。
    """
    missing = [name for name in _IDENTITY_SOURCES if name not in env]
    if not missing:
        return
    listing = _lookup(
        ["config", "--list", "-z"],
        cwd,
        _compose(base, isolate=False, pins=[*dash_c, *_EXEC_PINS]),
    )
    if not listing:
        return
    values: dict[str, str] = {}
    for entry in listing.split("\0"):
        key, _, value = entry.partition("\n")
        values[key] = value
    for name in missing:
        for source in _IDENTITY_SOURCES[name]:
            if values.get(source):
                env[name] = values[source]
                break
