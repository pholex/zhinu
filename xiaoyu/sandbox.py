"""bash 命令沙箱：macOS Seatbelt + Linux bubblewrap。

给 bash 工具执行的命令套一层内核级强制访问控制，包一层即可，策略对
**所有子孙进程**生效（模型跑的 `npm install` 里再启动的进程也跑不出去），
无需 root、无需容器：

- macOS：`/usr/bin/sandbox-exec -p <策略>`（Seatbelt 策略语言）
- Linux：`bwrap --ro-bind / /` 把整棵文件系统只读挂进 mount namespace，
  再逐个 `--bind` 可写根目录开洞。两个后端策略语义刻意对齐：
  只管"写"、读全放、断网另有开关——调用方（tools.py/cli.py）不感知平台。

威胁模型（决定了策略的松紧）：小羽要防的是**不可逆的文件系统破坏**——
误删家目录、覆写 `~/.zshrc`、把包装进系统 Python。所以：

- **写**：默认只允许工作区 + 临时目录 + 若干缓存目录（见 `default_writable_roots`），
  其余一律拒绝。这是这层沙箱的全部价值所在。
- **读**：全盘放行。收紧读会踩不完的坑（动态链接、locale、各语言 runtime 的
  配置发现），而读本身不造成不可逆损失。⚠️ 代价是模型仍读得到 `~/.ssh`、
  `~/.aws` 这类凭据——这一层不解决凭据泄露，那靠的是 bash 默认逐次确认。
- **网络**：默认放行。断网会静默打断 `pip install` / `npm install` / `git push`
  这些编码工作的日常动作，是"让人恨上这个功能"的最快方式。要断网设
  `XIAOYU_SANDBOX_NETWORK=0`——那才是完整的防外传姿势。
- **mach 服务**：整体放行。逐个白名单适合"跑更不可信的云端任务"那类
  场景，对"防写坏磁盘"这个目标没有额外收益，却会连累 DNS、日志、
  时区这些基础能力。

失败要看得见：沙箱不可用（Windows、没装 bwrap 的 Linux、被内核/AppArmor 禁掉
unprivileged user namespace 的发行版）时 `available()` 返回 False，调用方原样
执行——沙箱是纵深防御的一层，不是执行前提。Windows 没有等价的轻量原语
（AppContainer 配置重且对"限写路径"支持别扭），不做原生支持，WSL 里即是 Linux。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from . import config

#  只认固定绝对路径：走 PATH 会被 PATH 上的同名程序顶替，
#  那就等于沙箱可以被自己要沙箱化的东西关掉。
SANDBOX_EXEC = "/usr/bin/sandbox-exec"
BWRAP_CANDIDATES = (
    "/usr/bin/bwrap",
    "/usr/local/bin/bwrap",
    "/run/current-system/sw/bin/bwrap",  # NixOS
)

#  策略模板。`(deny default)` 起步，逐条开洞；写权限的根目录由 -D 参数传入，
#  **不内联进策略文本**——路径里有引号/空格/换行也破坏不了策略语法。
_POLICY_HEADER = """(version 1)
(deny default)

; 子进程继承本策略
(allow process-exec)
(allow process-fork)
(allow signal (target same-sandbox))
(allow process-info* (target same-sandbox))

; 读：全盘放行（见模块 docstring 的取舍说明）
(allow file-read*)
(allow file-test-existence)
(allow file-map-executable)
(allow file-read-metadata)

; 系统信息查询：各语言 runtime 启动时都要读，收紧只会徒增故障
(allow sysctl-read)
(allow sysctl-write (sysctl-name "kern.grade_cputype"))
(allow mach-lookup)
(allow iokit-open (iokit-registry-entry-class "RootDomainUserClient"))
(allow user-preference-read)

; Python multiprocessing 的 SemLock
(allow ipc-posix-sem)
; PyTorch / libomp 注册 OpenMP runtime
(allow ipc-posix-shm-read* ipc-posix-shm-write*)

; 终端：openpty()、交互式程序检测 TTY
(allow pseudo-tty)
(allow file-read* file-write* file-ioctl (literal "/dev/ptmx"))
(allow file-read* file-write* file-ioctl (regex #"^/dev/ttys[0-9]+$"))
(allow file-read* file-write* (literal "/dev/tty"))
(allow file-read* file-write* (literal "/dev/null"))
(allow file-read* file-write* (literal "/dev/zero"))
(allow file-read* file-write* (subpath "/dev/fd"))
(allow file-write-data
  (require-all (path "/dev/dtracehelper") (vnode-type CHARACTER-DEVICE)))

; 系统日志 socket（不受 network 开关影响：它是本地 unix socket）
(allow network-outbound (literal "/private/var/run/syslog"))
"""

_NETWORK_ALLOW = """
; 网络放行（XIAOYU_SANDBOX_NETWORK=0 可关掉这一段）
(allow network*)
"""

_NETWORK_DENY = """
; 网络已断：策略里不出现 network 放行即为全禁
"""


def policy_text(writable_count: int, allow_network: bool) -> str:
    """按可写根目录的数量生成策略文本。根目录本身由 -D WRITABLE_n 传值。"""
    parts = [_POLICY_HEADER, _NETWORK_ALLOW if allow_network else _NETWORK_DENY]
    for index in range(writable_count):
        parts.append(
            f'\n(allow file-write* (subpath (param "WRITABLE_{index}")))'
            f'\n(allow file-read* (subpath (param "WRITABLE_{index}")))\n'
        )
    return "".join(parts)


# ---------- Linux bubblewrap ----------


def bwrap_args(writable_roots: list[str], allow_network: bool) -> list[str]:
    """bwrap 参数（不含二进制路径和目标命令）。纯函数，全平台可测。

    - `--ro-bind / /`：整棵文件系统只读挂进来，之后逐条开洞——对应 seatbelt
      的 `(deny default)` + `(allow file-read*)`
    - `--dev-bind /dev /dev`：/dev 恢复读写（tty / ptmx / null / shm）
    - `--unshare-pid` 必须和 `--proc /proc` 成对：只读的 /proc 会打断
      `/proc/self/*` 写入，而挂新 procfs 内核要求持有对应 pid namespace
    - `--die-with-parent`：小羽进程死，沙箱内整棵进程树跟着死，不留孤儿
    - 断网 = `--unshare-net`（空 network namespace，连 localhost 都没有）
    - 可写根目录逐个 `--bind`。⚠️ 与 seatbelt 的语义差异：bind 不了不存在的
      目录，只能过滤掉——所以 Linux 上"允许写一个尚不存在的缓存目录"不成立，
      目录要先存在才可写（workspace/tmp 恒存在，不受影响）。
    """
    args = [
        "--ro-bind", "/", "/",
        "--dev-bind", "/dev", "/dev",
        "--unshare-pid",
        "--proc", "/proc",
        "--die-with-parent",
    ]
    if not allow_network:
        args.append("--unshare-net")
    for root in writable_roots:
        if Path(root).exists():
            args += ["--bind", root, root]
    return args


def _bwrap_path() -> str | None:
    for candidate in BWRAP_CANDIDATES:
        if Path(candidate).is_file():
            return candidate
    return None


#  探针结果缓存（进程级）。None = 还没探过。
_bwrap_verdict: bool | None = None


def _bwrap_works(bwrap: str) -> bool:
    """bwrap 在不代表能跑：不少发行版禁了 unprivileged user namespace
    （Ubuntu 24.04 的 AppArmor 限制、各种 hardened 内核）。真跑一次探针定生死，
    否则每条 bash 命令都会撞同一堵墙——"沙箱让 bash 全废了"是最坏的失败方式。
    探针参数必须与真实执行完全一致：参数更少的探针可能通过而真实调用失败。"""
    global _bwrap_verdict
    if _bwrap_verdict is None:
        try:
            probe = subprocess.run(
                [bwrap, *bwrap_args([], allow_network=True), "--", "/bin/true"],
                capture_output=True,
                timeout=10,
            )
            _bwrap_verdict = probe.returncode == 0
        except (OSError, subprocess.SubprocessError):
            _bwrap_verdict = False
    return _bwrap_verdict


def available() -> bool:
    """当前平台是否有可用沙箱：macOS Seatbelt，或 Linux bubblewrap。"""
    if sys.platform == "darwin":
        return Path(SANDBOX_EXEC).is_file()
    if sys.platform == "linux":
        bwrap = _bwrap_path()
        return bwrap is not None and _bwrap_works(bwrap)
    return False


def _worktree_git_common(workspace: Path) -> Path | None:
    """workspace 若是 linked worktree，返回主仓的 .git 目录；否则 None。

    指针文件形如 `gitdir: /主仓/.git/worktrees/<名>`，公共目录取其上两级。
    """
    pointer = workspace / ".git"
    if not pointer.is_file():
        return None
    try:
        raw = pointer.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    for line in raw.splitlines():
        if line.startswith("gitdir:"):
            gitdir = Path(line.split(":", 1)[1].strip())
            if not gitdir.is_absolute():
                gitdir = workspace / gitdir
            common = gitdir.parent.parent
            return common if common.name == ".git" else None
    return None


def default_writable_roots(workspace: Path) -> list[Path]:
    """默认可写根目录：工作区 + 临时目录 + 常见构建缓存。

    缓存目录（`~/.cache`、`~/.npm`、`~/Library/Caches`…）必须放开，否则
    `npm install` / `pip` / `cargo` 会因为写不了缓存而失败——它们不是珍贵的
    用户数据，放开的代价远小于"这功能天天坏"。
    """
    roots = [workspace, Path("/tmp"), Path("/private/tmp"), Path("/var/tmp")]
    #  workspace 是 linked worktree（.git 为指针文件）时，git 的真实写路径在
    #  主仓 .git 下（对象库/refs/worktrees/<n>/index）——不放开则 worktree 里
    #  git add/commit 必败（子 agent 隔离与宸枢 mission 分支都靠它）。放开的
    #  只是 git 元数据目录，主工作区的文件本身仍不可写。
    if (common := _worktree_git_common(workspace)) is not None:
        roots.append(common)
    if tmpdir := os.environ.get("TMPDIR"):
        roots.append(Path(tmpdir))
    #  Linux 桌面会话的 /run/user/<uid>：dbus / keyring / 各种 socket 都在这，
    #  只读会让一批工具莫名其妙坏，而它是会话级临时数据、不珍贵
    if runtime_dir := os.environ.get("XDG_RUNTIME_DIR"):
        roots.append(Path(runtime_dir))
    #  容器随机 UID（无 pwd 条目）推不出 home：跳过 home 系缓存根即可，
    #  沙箱少几个可写目录是降级，起不了沙箱才是事故（见 config.home_dir）
    if (home := config.home_dir()) is not None:
        roots += [
            home / ".cache",
            home / ".npm",
            home / ".yarn",
            home / ".cargo" / "registry",
            home / "Library" / "Caches",
        ]
    return roots


def extra_writable_roots() -> list[Path]:
    """`XIAOYU_SANDBOX_WRITABLE`（冒号分隔）追加的可写根目录。"""
    raw = os.environ.get("XIAOYU_SANDBOX_WRITABLE", "").strip()
    if not raw:
        return []
    return [Path(item).expanduser() for item in raw.split(":") if item.strip()]


def _normalize(roots: list[Path]) -> list[str]:
    """去重 + 绝对化。不存在的路径照样传：策略允许它，之后被创建也能写。"""
    seen: dict[str, None] = {}
    for root in roots:
        try:
            resolved = root.expanduser().resolve()
        except OSError:
            continue
        seen.setdefault(str(resolved), None)
    return list(seen)


def wrap(argv: list[str], workspace: Path, allow_network: bool = True) -> list[str]:
    """把命令 argv 包进当前平台的沙箱调用。不可用时原样返回。"""
    if not available():
        return argv
    roots = _normalize(default_writable_roots(workspace) + extra_writable_roots())
    if sys.platform == "darwin":
        command = [SANDBOX_EXEC, "-p", policy_text(len(roots), allow_network)]
        for index, root in enumerate(roots):
            command += ["-D", f"WRITABLE_{index}={root}"]
        return command + ["--", *argv]
    return [_bwrap_path(), *bwrap_args(roots, allow_network), "--", *argv]


def enabled(config_flag: bool) -> bool:
    """本次执行是否套沙箱：配置开 + 平台支持。"""
    return bool(config_flag) and available()


# ---------- 拒绝识别与 runner 失败识别（两类正交） ----------

#  被沙箱拒绝时内核只回 EPERM，程序打印的措辞五花八门，只能按特征猜。
#  宁可多报（提示是给模型看的、无副作用），
#  也别让模型对着 "Operation not permitted" 原地打转。
#
#  按**当前后端的方言**匹配，不做跨后端并集：
#  并集会在某个后端上声称它根本不会产生的拒绝——例如 macOS 上没有 ro-bind，
#  "read-only file system" 只能来自命令自身的语境；把它算成沙箱拒绝，
#  模型就会被"疑似沙箱拦截"的提示带偏。
_DENIAL_MARKERS_DARWIN = (
    #  Seatbelt 的写拒绝是 EPERM；程序方言两种拼法都有
    "operation not permitted",
    "permission denied",
    "deny file-write",
    "sandbox",
)
_DENIAL_MARKERS_LINUX = (
    #  bwrap ro-bind 的写拒绝是 EROFS/EACCES
    "read-only file system",
    "permission denied",
    "operation not permitted",
)
#  断网后最常见的两种措辞（连不上 + DNS 解析不了）。⚠️ 只有真的断了网时
#  才算沙箱方言——网络放行时它们就是普通的网络故障，与沙箱无关。
_NETWORK_DENIAL_MARKERS = (
    "network is unreachable",
    "temporary failure in name resolution",
)


def looks_denied(output: str, network_disabled: bool = False) -> bool:
    """输出是否像被**当前平台的沙箱**拒绝。exit code 不参与判断。"""
    lowered = output.lower()
    markers = _DENIAL_MARKERS_DARWIN if sys.platform == "darwin" else _DENIAL_MARKERS_LINUX
    if any(marker in lowered for marker in markers):
        return True
    return network_disabled and any(marker in lowered for marker in _NETWORK_DENIAL_MARKERS)


#  runner 自身失败（沙箱没起来，命令**根本没跑**）与"命令被沙箱挡了"是
#  两回事，必须分开报：前者是沙箱的问题，
#  提示模型重试命令或升权都是南辕北辙。识别只认 runner 自己的报错前缀，
#  **exit code 单独永远不能证明 runner 失败**——成功 exec 之后子进程也可能
#  返回任何退出码。前缀要求出现在行首：命令输出里引用 "bwrap:" 字样的
#  正文行（如 grep 到的源码）不在行首时不会误判。
_RUNNER_PREFIXES = ("bwrap: ", "sandbox-exec: ")


def runner_failure(output: str) -> str | None:
    """识别沙箱 runner 自身的失败行；没有则返回 None。"""
    for line in output.splitlines():
        stripped = line.strip()
        for prefix in _RUNNER_PREFIXES:
            if stripped.startswith(prefix):
                return stripped
    return None


def denial_hint(workspace: Path, allow_network: bool, escalation: bool = False) -> str:
    """疑似被沙箱拦截时附给模型的说明——要告诉它边界在哪、下一步怎么办。

    escalation=True 时改为广告升权协议（"escalation available" 结果
    标记）：模型可以带着理由原样重试这一条命令，
    是否放行由用户在确认框里裁决。
    """
    lines = [
        "",
        "[沙箱提示] 这次失败疑似是被沙箱拦截的，不一定是命令本身有问题。",
        f"当前策略：只有工作区（{workspace}）、临时目录和构建缓存可写；其余路径只读。",
    ]
    if not allow_network:
        lines.append("网络当前是**禁用**的，联网命令（pip/npm/curl/git push）都会失败。")
    if escalation:
        lines += [
            "[沙箱提示] 可升权重试：若这次操作确实需要更高权限，"
            "用**最窄的足够档位**带上 sandbox_permissions 和 justification"
            "（说明为什么需要）原样重试这一条命令，等待用户批准，仅本次生效。"
            "同一条命令只升权重试一次；被拒就换做法或问用户。",
        ]
    else:
        lines += [
            "如果这次操作确实需要写工作区之外的路径或联网，不要反复重试——"
            "向用户说明需要什么权限，由用户决定是放开沙箱（XIAOYU_SANDBOX=0）"
            "还是自己在终端里执行。",
        ]
    return "\n".join(lines)
