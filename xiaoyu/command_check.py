"""命令风险分析：参数注入识别 + 危险命令识别。

两个函数、两个方向（核心设计观念——方向相反的两套判定不能共用一个解析器）：

- ``injection_risk``：回答"这条命令能不能被 allow 规则**免确认放行**"。
  前缀规则只看命令开头，而很多常见命令有"参数级逃逸口"——任何能指定
  「外部程序」或「输出文件」的 flag 都能把 `git *` 这类 allow 变成任意代码执行
  （`git -c core.pager='!sh …' log`）。识别到就不吃 allow 规则，退回人工确认。
  **保守方向**：拿不准就报风险（多问一次的代价可接受）。

- ``dangerous_command``：回答"这条命令是不是破坏性操作"（强制 rm 等）。
  攻击面在包装层：`sudo -u root rm -rf`、`env -S 'rm -rf …'`、`bash -c 'rm -rf …'`、
  `echo "$(rm -rf …)"` 都包着同一个 rm。识别时要**宽松解析、递归剥 wrapper**，
  尽量从复杂写法里挖出字面量命令。识别不出来不要紧——识别不出的命令本来
  就进不了 allow 通道（injection_risk / 前缀不匹配兜底），最终仍会走人工确认。

- ``privileged_command``：回答"这条命令要不要提权"（sudo/doas/su/pkexec）。
  与 ``dangerous_command`` 同方向（宽松解析、剥同一批 wrapper），只是命中的
  是另一类东西，所以扫描骨架抽成了共用的 ``_scan_script`` / ``_scan_segment``。
  给 auto 档用：沙箱能拦住越权写盘，但提权是唯一可能捅穿它的动作，得有人看着。

剥 wrapper 的关键是**认得每个 wrapper 的选项语法**：`sudo -u root rm` 里的 root
是选项值不是命令名，`timeout 5 rm` 的 5 是 DURATION。只跳过 `-` 开头的 token 会把
选项值当成命令，里面真正的 rm 就漏了。选项表见 ``_WRAPPERS``；表外的选项按
fail-safe 处理（带值 / 不带值两种解释都扫），扫不完（嵌套过深、分叉过多）按有风险报。
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field

#  递归剥 wrapper 的深度上限：
#  防止构造出的嵌套命令把解析拖死
_MAX_WRAPPER_DEPTH = 8
#  嵌套层里被扫描的节点总数上限：表外选项会分叉出两种解释，层层嵌套时分叉相乘，
#  靠它兜底。只数嵌套层——几百行的扁平脚本（heredoc 写文件）不该因为长而被判风险
_MAX_SCAN_NODES = 512
_TOO_COMPLEX = "命令嵌套过深或写法分叉过多，无法完整分析（按有风险处理）"


def _split(segment: str) -> list[str]:
    """shlex 分词；解析不了（引号不闭合等）返回空列表，由调用方决定方向。"""
    try:
        return shlex.split(segment, posix=True)
    except ValueError:
        return []


def _base_name(token: str) -> str:
    """argv[0] 归一：取 basename、去 Windows 后缀、小写。

    没有这一步，`/usr/bin/git` / `git.exe` 就绕过了所有按名字写的检查。
    """
    name = token.replace("\\", "/").rsplit("/", 1)[-1].lower()
    for suffix in (".exe", ".cmd", ".bat", ".com"):
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return name


# ---------- 注入风险（allow 规则的防绕过） ----------

#  git 的全局选项黑名单：这些出现在 subcommand 之前就能注入任意执行/改仓库位置。
#  -c core.pager=… / -p 走 pager 执行、--exec-path 劫持子命令二进制目录。
#  三种书写形态都要覆盖：独立值（-c foo=x）、粘连值（-cfoo=x）、等号（--git-dir=x）。
_GIT_UNSAFE_GLOBAL_EXACT = {"-c", "-C", "-p", "--paginate", "--config-env",
                            "--exec-path", "--git-dir", "--namespace",
                            "--super-prefix", "--work-tree"}
_GIT_UNSAFE_GLOBAL_PREFIX = ("--config-env=", "--exec-path=", "--git-dir=",
                             "--namespace=", "--super-prefix=", "--work-tree=")
_GIT_UNSAFE_GLOBAL_INLINE = ("-c", "-C")  # -cfoo.bar=x / -C/path 粘连形态
#  子命令级：能写任意文件或执行外部程序的选项
_GIT_UNSAFE_SUB_EXACT = {"--output", "--ext-diff", "--textconv", "--exec", "--upload-pack"}
_GIT_UNSAFE_SUB_PREFIX = ("--output=", "--exec=", "--upload-pack=")

#  find：执行/删除/写文件类动作
_FIND_UNSAFE = {"-exec", "-execdir", "-ok", "-okdir", "-delete",
                "-fls", "-fprint", "-fprint0", "-fprintf"}

#  rg：--pre 对每个文件执行任意命令，-z/--search-zip 调外部解压器
_RG_UNSAFE_EXACT = {"--pre", "--hostname-bin", "-z", "--search-zip"}
_RG_UNSAFE_PREFIX = ("--pre=", "--hostname-bin=")

#  tar / rsync / ssh 系：能指定外部程序的选项
_TAR_UNSAFE_PREFIX = ("--to-command", "--use-compress-program", "--rmt-command", "--rsh-command")

#  sed 家族：脚本里的 e 标志/命令会执行 shell（GNU 扩展）
_SED_NAMES = {"sed", "gsed", "ssed"}
#  vim/ex 家族：-c/--cmd/+cmd 跑 ex 命令，可 :!shell 逃逸；-S 直接 source 脚本
_VIM_NAMES = {"vim", "vi", "view", "nvim", "ex", "rvim", "gvim", "vimdiff", "evim"}
_VIM_EXEC_EXACT = {"-c", "--cmd", "-S", "--source"}
_VIM_EXEC_PREFIX = ("-c", "--cmd=", "-S", "--source=", "+")


def _sed_script_executes(script: str) -> bool:
    """sed 脚本里有没有会执行 shell 的构造：`s///e` 标志 或 独立 `e` 命令。

    关键是把 `s/a/b/e`（e 是**替换后的标志**=执行）和 `s/e/x/`（e 只是被替换的
    字面量）分开——前者危险、后者无害。做法：定位每个 s 命令，越过它的三个
    定界符（转义的 `\\<定界符>` 不算），只在**定界符之后的标志段**里找 e。
    """
    n = len(script)
    i = 0
    while i < n:
        if script[i] == "s" and i + 1 < n:
            delim = script[i + 1]
            #  定界符是紧跟 s 的那个字符，通常 / 也可以是别的；字母数字/空白/
            #  反斜杠不能当定界符（那是普通命令/转义，不是 s 命令）
            if not delim.isalnum() and delim not in (" ", "\\", "\n"):
                #  开定界符是 script[i+1]，完整 s 命令后面还有 2 个（中、闭）
                j, seen = i + 2, 0
                while j < n and seen < 2:
                    if script[j] == "\\" and j + 1 < n:
                        j += 2  # 转义序列整体跳过，`\<定界符>` 不计入定界符
                        continue
                    if script[j] == delim:
                        seen += 1
                    j += 1
                if seen == 2:
                    #  闭定界符之后是标志段（字母数字连续串），含 e 即执行
                    flags = ""
                    while j < n and script[j].isalnum():
                        flags += script[j]
                        j += 1
                    if "e" in flags:
                        return True
                    i = j
                    continue
        i += 1
    #  独立 e 命令（GNU：`e` 或 `e 命令`）——出现在命令位（脚本开头或 ; / 换行后）。
    #  带地址的花式形态（`/re/e cmd`）不强求覆盖：over-flag 只是多问一次人，
    #  漏掉的少数形态由前缀不匹配/人工确认兜底。
    if re.search(r"(?:^|[;\n])\s*e(?:\s|$)", script):
        return True
    return False


def injection_risk(segment: str) -> str | None:
    """单段命令（不含 && ; | 等连接符）里发现参数级逃逸口时返回原因，否则 None。

    只用于收窄 allow 规则的放行范围——返回原因意味着"这条命令不吃 allow 规则、
    退回人工确认"，不是拒绝执行。
    """
    argv = _split(segment)
    if not argv:
        #  解析不了的命令看不清楚，保守方向：按有风险处理（allow 不放行）
        return "命令无法安全解析（引号不闭合等）" if segment.strip() else None
    return injection_risk_argv(argv)


def injection_risk_argv(argv: list[str]) -> str | None:
    """injection_risk 的 argv 版：调用方已有可靠分词（bash_ast）时直接用，
    不再经 shlex 二次解析（二次解析会把引号里的内容又拆开）。"""
    try:
        return _injection_risk_argv(argv, 0)
    except _TooComplex:
        return _TOO_COMPLEX


def _injection_risk_argv(argv: list[str], depth: int) -> str | None:
    if not argv:
        return None
    name = _base_name(argv[0])

    if name == "git":
        return _git_risk(argv[1:])
    if name == "find":
        for arg in argv[1:]:
            if arg in _FIND_UNSAFE:
                return f"find {arg} 可执行命令/删除/写文件"
        return None
    if name == "rg":
        for arg in argv[1:]:
            if arg in _RG_UNSAFE_EXACT or arg.startswith(_RG_UNSAFE_PREFIX):
                return f"rg {arg} 可调用外部程序"
        return None
    if name == "tar":
        for arg in argv[1:]:
            if arg.startswith(_TAR_UNSAFE_PREFIX):
                return f"tar {arg} 可执行外部程序"
        return None
    if name in ("ssh", "scp", "sftp"):
        for index, arg in enumerate(argv[1:], start=1):
            lowered = arg.lower()
            if "proxycommand" in lowered or "localcommand" in lowered:
                return f"{name} 的 ProxyCommand/LocalCommand 可执行任意命令"
            if arg == "-o" and index + 1 < len(argv):
                follow = argv[index + 1].lower()
                if "proxycommand" in follow or "localcommand" in follow:
                    return f"{name} -o {argv[index + 1].split('=')[0]} 可执行任意命令"
        return None
    if name in ("awk", "gawk", "mawk", "nawk"):
        if any("system(" in arg for arg in argv[1:]):
            return "awk system() 可执行任意命令"
        return None
    if name in _SED_NAMES:
        #  脚本可能是位置参数，也可能跟在 -e/--expression 后；-f 是脚本文件不检查
        #  （文件内容用户自控，属意图）。逐 token 扫（文件名不会误命中 s///e 结构）。
        for arg in argv[1:]:
            if _sed_script_executes(arg):
                return "sed 的 e 标志/命令会执行 shell"
        return None
    if name in _VIM_NAMES:
        for arg in argv[1:]:
            if arg in _VIM_EXEC_EXACT or arg.startswith(_VIM_EXEC_PREFIX):
                return f"{name} {arg.split('=')[0]} 可执行 ex 命令（:!shell 逃逸）"
        return None
    if name == "xargs":
        return "xargs 会执行任意后续命令"
    #  wrapper 不能替里面的命令洗白：`timeout 60 git -c core.pager=sh log` 被
    #  `timeout 60 git*` 放行的话，git 的注入口就绕过去了。剥开逐个候选再查
    if (peeled := _peel_wrapper(name, argv)) is not None:
        commands, scripts = peeled
        if scripts:
            return f"{name} 的选项里带着一段脚本，前缀规则看不见里面跑什么"
        for inner in commands:
            if depth + 1 > _MAX_WRAPPER_DEPTH:
                raise _TooComplex
            if reason := _injection_risk_argv(_strip_shell_prefix(inner), depth + 1):
                return reason
    return None


def _git_risk(args: list[str]) -> str | None:
    """先定位 subcommand（跳过全局选项），路上撞到注入口就报。

    「第一个非选项 token 就是 subcommand」——不这样定位的话，
    `git checkout status` 会被误判成安全的 status（经典反例）。
    """
    skip_next = False
    for arg in args:
        if skip_next:
            skip_next = False
            continue
        if arg in _GIT_UNSAFE_GLOBAL_EXACT or arg.startswith(_GIT_UNSAFE_GLOBAL_PREFIX):
            return f"git 全局选项 {arg.split('=')[0]} 可注入任意命令（如 -c core.pager）"
        #  粘连形态：-cfoo.bar=x / -C/path
        if any(arg.startswith(opt) and len(arg) > len(opt) for opt in _GIT_UNSAFE_GLOBAL_INLINE):
            return f"git 全局选项 {arg[:2]} 可注入任意命令（如 -c core.pager）"
        #  带独立值的无害全局选项：跳过它的值
        if arg in ("--git-dir", "--work-tree", "--namespace"):  # 已在黑名单，防御性保留
            skip_next = True
            continue
        if arg == "--" or arg.startswith("-"):
            continue
        #  到达 subcommand：往后只查子命令级黑名单
        break
    for arg in args:
        if arg in _GIT_UNSAFE_SUB_EXACT or arg.startswith(_GIT_UNSAFE_SUB_PREFIX):
            return f"git {arg.split('=')[0]} 可执行外部程序或写任意文件"
    return None


# ---------- wrapper 选项表 ----------

#  选项的五种语法（剥法各不相同）：
#  - value：必带值，`-u root` / `-uroot` / `--user root` / `--user=root`，值不是命令；
#  - optional：值可选，只认粘连（`-m/proc/1/ns/mnt`、`--mount=…`），不吃下一个 token；
#  - flag：不带值；
#  - script：值是一段 shell 源码（`su -c '…'`），要重新当脚本扫；
#  - split：值按空白拆成参数、插回原位继续当本 wrapper 的参数（`env -S '…'`）。
#  **拿不准的选项宁可不写进表**：表外选项两种解释都扫，最多多报；把带值选项
#  错写成 flag 则会把选项值当命令、真命令漏掉——这正是本表要修的那类洞。


@dataclass(frozen=True)
class _WrapperSpec:
    """一个 wrapper 的命令行语法。options：选项名 → 语法；单字母是短选项，多字母是长选项（不写 --）。"""

    options: dict[str, str] = field(default_factory=dict)
    positionals: int = 0  # 命令之前的位置参数个数：timeout 的 DURATION、chroot 的 NEWROOT、flock 的锁文件
    assignments: bool = False  # env：命令之前可以有 NAME=VALUE
    numeric: bool = False  # nice 的老写法 `nice -10 cmd`
    post_script: bool = False  # flock：锁文件之后可以是 `-c '脚本'`
    shell_tail: bool = False  # su：选项可以出现在用户名之后（GNU 重排），用户名之后的参数整体交给 shell


def _spec(*, value: str = "", optional: str = "", flag: str = "", script: str = "",
          split: str = "", **extra) -> _WrapperSpec:
    """用空格分隔的选项串建表，免得每个选项写一遍语法名。"""
    options: dict[str, str] = {}
    for kind, names in (("value", value), ("optional", optional), ("flag", flag),
                        ("script", script), ("split", split)):
        for option in names.split():
            options[option] = kind
    return _WrapperSpec(options, **extra)


_SU_OPTIONS = dict(
    value="g G s w group supp-group shell whitelist-environment",
    flag="f l m p P fast login preserve-environment pty help version",
    script="c command session-command",
)

#  wrapper → 语法（元组：一个命令有两种调用形态时两种都扫，如 runuser）。
#  依据各自 man 手册（GNU coreutils / util-linux / sudo / systemd / BSD 变体取并集）。
_WRAPPERS: dict[str, tuple[_WrapperSpec, ...]] = {
    #  -h 既是 --help 又是 --host=host，-c 各版本含义不一：故意不写，走 fail-safe
    "sudo": (_spec(
        value="C D R T U g p r t u chdir chroot close-from command-timeout group host "
              "login-class other-user prompt role type user",
        optional="preserve-env",
        flag="A B E H K N P S V b e i k l n s v askpass background bell edit help list login "
             "non-interactive preserve-groups remove-timestamp reset-timestamp set-home shell "
             "stdin validate version",
    ),),
    "doas": (_spec(value="C u", flag="L n s"),),
    "run0": (_spec(
        value="D g u area background chdir description group lightweight machine nice property "
              "setenv shell-prompt-prefix slice unit user",
        flag="no-ask-password pipe pty slice-inherit help version",
    ),),
    "pkexec": (_spec(value="u user", flag="disable-internal-agent keep-cwd help version"),),
    #  GNU env 与 BSD env 取并集：-P altpath / -L|-U user 是 BSD 的带值选项
    "env": (_spec(
        value="C L P U a u argv0 chdir unset",
        optional="block-signal default-signal ignore-signal",
        flag="0 i v debug ignore-environment list-signal-handling null help version",
        split="S split-string",
        assignments=True,
    ),),
    "nice": (_spec(value="n adjustment", flag="help version", numeric=True),),
    "ionice": (_spec(value="P c n p u class classdata pgid pid uid",
                     flag="t ignore help version"),),
    "nohup": (_spec(flag="help version"),),
    "timeout": (_spec(value="k s kill-after signal",
                      flag="f p v foreground preserve-status verbose help version",
                      positionals=1),),
    "stdbuf": (_spec(value="e i o error input output", flag="help version"),),
    "command": (_spec(flag="p v V"),),
    "exec": (_spec(value="a", flag="c l"),),
    #  GNU time 与 BSD time 取并集（bash 关键字 time 只认 -p）
    "time": (_spec(value="f o format output",
                   flag="a h l p q v V append portability quiet verbose help version"),),
    "setsid": (_spec(flag="c f w ctty fork wait help version"),),
    "chroot": (_spec(value="G g u groups userspec", flag="n skip-chdir help version",
                     positionals=1),),
    "flock": (_spec(
        value="E w conflict-exit-code timeout wait",
        flag="F e n o s u x close exclusive nb no-fork nonblock shared unlock verbose help version",
        script="c command",
        positionals=1, post_script=True,
    ),),
    "su": (_spec(**_SU_OPTIONS, shell_tail=True),),
    #  runuser 两种形态：`runuser -u user -- cmd …`（argv 形态）与 su 兼容形态
    "runuser": (
        _spec(value="u user " + _SU_OPTIONS["value"], flag=_SU_OPTIONS["flag"],
              script=_SU_OPTIONS["script"]),
        _spec(value="u user " + _SU_OPTIONS["value"], flag=_SU_OPTIONS["flag"],
              script=_SU_OPTIONS["script"], shell_tail=True),
    ),
    "nsenter": (_spec(
        value="G S W t setgid setuid target wdns",
        optional="C T U i m n p r u w cgroup ipc mount net pid root time user uts wd",
        flag="F N Z a c e all env follow-context join-cgroup keep-caps no-fork "
             "preserve-credentials user-parent help version",
    ),),
    "unshare": (_spec(
        value="G R S l w boottime load-interp map-group map-groups map-user map-users monotonic "
              "propagation root setgid setgroups setuid wd",
        optional="C T U i m n p u cgroup ipc kill-child mount mount-binfmt mount-proc net pid "
                 "time user uts",
        flag="c f r fork keep-caps map-auto map-current-user map-root-user help version",
    ),),
    "setpriv": (_spec(
        value="ambient-caps apparmor-profile bounding-set egid euid groups inh-caps "
              "landlock-access landlock-rule pdeathsig regid reuid rgid ruid securebits "
              "seccomp-filter selinux-label",
        flag="d clear-groups dump init-groups keep-groups nnp no-new-privs reset-env help version",
    ),),
    "systemd-run": (_spec(
        value="C E H M p u capsule description gid host job-mode machine nice on-active on-boot "
              "on-calendar on-startup on-unit-active on-unit-inactive path-property property "
              "service-type setenv slice socket-property timer-property uid unit "
              "working-directory",
        flag="G P S d q r t collect no-ask-password no-block on-clock-change "
             "on-timezone-change pipe pty quiet remain-after-exit same-dir scope send-sighup "
             "shell slice-inherit system user wait help version",
    ),),
    #  GNU xargs 与 BSD xargs 取并集（-J/-R/-S 是 BSD 的带值选项）
    "xargs": (_spec(
        value="E I J L P R S a d n s arg-file delimiter max-args max-chars max-procs "
              "process-slot-var",
        optional="e i l eof max-lines replace",
        flag="0 o p r t x exit interactive no-run-if-empty null open-tty show-limits verbose "
             "help version",
    ),),
    "strace": (_spec(value="E I O P S U X a b e o p s u output",
                     flag="C D F T V Z c d f h i k n q r t v w x y z"),),
    "ltrace": (_spec(value="A D F a e l n o p s u x output",
                     flag="C L S T V b c f h i r t w"),),
    "chrt": (_spec(
        value="D P T sched-deadline sched-period sched-runtime",
        flag="R a b d e f i m o p r v all-tasks batch deadline ext fifo idle max other pid "
             "reset-on-fork rr verbose help version",
        positionals=1,
    ),),
    "taskset": (_spec(flag="a c p all-tasks cpu-list pid help version", positionals=1),),
}

#  所有 wrapper 名：permissions 的会话授权与 allow 探针都从这里取，加一个 wrapper 三处同时受益
WRAPPER_NAMES = frozenset(_WRAPPERS)

_NICE_NUMERIC = re.compile(r"-[+-]?\d+")


class _TooComplex(ValueError):
    """扫描超出深度 / 工作量上限：调用方按"有风险"处理，绝不当作安全。"""


def unwrap_argv(argv: list[str]) -> list[list[str]] | None:
    """剥一层 wrapper，返回里面可能被执行的命令 argv（选项里带的脚本包成 `sh -c 脚本`）。

    不是 wrapper 返回 None；表外选项分叉过多时抛 ValueError（调用方按有风险处理）。
    给 permissions 判 allow 规则模式用——它要看 `nice -n 5 *` 剥开以后放行的是什么。
    """
    if not argv:
        return None
    peeled = _peel_wrapper(_base_name(argv[0]), argv)
    if peeled is None:
        return None
    commands, scripts = peeled
    return commands + [["sh", "-c", script] for script in scripts]


def _long_kind(spec: _WrapperSpec, name: str) -> str | None:
    """长选项的语法。GNU getopt 接受无歧义缩写（`su --comm '…'`），按前缀匹配；
    缩写能对上脚本类选项就当脚本（宁可多扫），其余有歧义的返回 None（表外）。"""
    if kind := spec.options.get(name):
        return kind if len(name) > 1 else None
    if not name:
        return None
    kinds = {kind for option, kind in spec.options.items()
             if len(option) > 1 and option.startswith(name)}
    for preferred in ("script", "split"):
        if preferred in kinds:
            return preferred
    return kinds.pop() if len(kinds) == 1 else None


def _option_steps(argv: list[str], index: int,
                  spec: _WrapperSpec) -> list[tuple[int, list[str], list[str]]]:
    """解析 argv[index] 这个选项，返回每种可能解释的 (下一个 token 下标, 脚本值, 拆分值)。

    表内选项只有一种解释；表外选项两种都给（带值吃掉下一个 token / 不带值）。
    """
    token = argv[index]
    following = argv[index + 1] if index + 1 < len(argv) else None
    after_value = index + 2 if following is not None else index + 1

    def carried(kind: str, value: str | None, step: int) -> tuple[int, list[str], list[str]]:
        found = [] if value is None else [value]
        return (step, found if kind == "script" else [], found if kind == "split" else [])

    if token.startswith("--"):
        name, equals, attached = token[2:].partition("=")
        kind = _long_kind(spec, name)
        if kind in ("script", "split"):
            return [carried(kind, attached if equals else following,
                            index + 1 if equals else after_value)]
        if kind == "value":
            return [(index + 1 if equals else after_value, [], [])]
        if kind in ("flag", "optional") or equals:
            return [(index + 1, [], [])]
        #  表外长选项：带值、不带值两种解释都走
        return [(index + 1, [], []), (after_value, [], [])]

    steps: list[tuple[int, list[str], list[str]]] = []
    #  短选项聚合（-Eu root）：逐字母走，遇到带值字母为止；它的值是本 token 剩余部分或下一个 token
    for offset in range(1, len(token)):
        letter, rest = token[offset], token[offset + 1:]
        kind = spec.options.get(letter)
        if kind == "flag":
            continue
        if kind == "optional":
            steps.append((index + 1, [], []))
            return steps
        if kind in ("value", "script", "split"):
            steps.append(carried(kind, rest or following, index + 1 if rest else after_value))
            return steps
        #  表外字母：先记"它带值"这种解释，再当不带值继续往后走
        steps.append((index + 1 if rest else after_value, [], []))
    steps.append((index + 1, [], []))
    return steps


def _split_env_payload(payload: str) -> list[str]:
    """`env -S` 的值拆成参数。GNU 有自己的转义（`\\_` 是分词空格、`\\c` 截断其后），
    近似成 shlex；shlex 解析不了就按空白粗切（宽松方向）。"""
    payload = payload.split("\\c", 1)[0].replace("\\_", " ")
    return _split(payload) or payload.split()


def _peel_wrapper(name: str, argv: list[str]) -> tuple[list[list[str]], list[str]] | None:
    """wrapper 剥掉外层，返回 (里面可能被执行的命令 argv 列表, 选项里携带的脚本列表)。

    不是 wrapper 则返回 None（注意与"是 wrapper 但里面是空"的 `([], [])` 区分）。
    剥法只写这一处：危险命令、提权、注入三套判定共用，加一个 wrapper 三边同时受益。

    做法是在 (下标, 还差几个位置参数, 是否已过 --, 已收集的位置参数) 状态上走一遍：
    表外选项分叉成两条路，每条路走到命令位就记一个候选——宁可多扫几个候选，不漏真命令。
    """
    specs = _WRAPPERS.get(name)
    if specs is None:
        return None
    commands: list[list[str]] = []
    scripts: list[str] = []
    for spec in specs:
        seen: set[tuple[int, int, bool, tuple[str, ...]]] = set()
        pending = [(1, spec.positionals, False, ())]
        while pending:
            state = pending.pop()
            if state in seen:
                continue
            seen.add(state)
            if len(seen) > _MAX_SCAN_NODES:
                raise _TooComplex
            index, need, ended, tail = state
            if index >= len(argv):
                #  su 形态：第一个位置参数是用户名，其后的参数原样交给 shell（`su root -- -c '…'`）
                if spec.shell_tail and len(tail) > 1:
                    commands.append(["sh", *tail[1:]])
                continue
            token = argv[index]
            if not ended and token == "--":
                pending.append((index + 1, need, True, tail))
            elif not ended and token == "-":
                #  env 的 `-` 等于 -i，su 的 `-` 等于 -l：都是开关
                pending.append((index + 1, need, ended, tail))
            elif not ended and token.startswith("-"):
                if spec.numeric and _NICE_NUMERIC.fullmatch(token):
                    pending.append((index + 1, need, ended, tail))
                    continue
                for step, found_scripts, found_splits in _option_steps(argv, index, spec):
                    scripts.extend(found_scripts)
                    for payload in found_splits:
                        #  拆出来的参数插回原位，连同其后的参数重新当 env 的参数剥一遍
                        commands.append([argv[0], *_split_env_payload(payload), *argv[step:]])
                    pending.append((step, need, ended, tail))
            elif spec.assignments and "=" in token.lstrip("="):
                pending.append((index + 1, need, ended, tail))
            elif spec.shell_tail:
                pending.append((index + 1, need, ended, (*tail, token)))
            elif need > 0:
                pending.append((index + 1, need - 1, ended, tail))
            elif spec.post_script and token in ("-c", "--command"):
                if index + 1 < len(argv):
                    scripts.append(argv[index + 1])
            else:
                commands.append(argv[index:])
    return commands, scripts


# ---------- 危险命令 / 提权（递归剥 wrapper） ----------

#  会把一段 shell 源码当参数的 shell：-c / -lc 后面那坨要重新按脚本解析
_SHELLS = {"bash", "sh", "zsh", "dash", "ksh", "fish"}
#  提权入口。sudo/doas/run0 同时也在 _WRAPPERS 里——两张表各答各的问题：
#  查危险命令时要剥开 sudo 看里面包着什么，查提权时看见 sudo 本身就已经命中。
_PRIVILEGE_ESCALATORS = {"sudo", "doas", "su", "pkexec", "runas", "run0"}

#  脚本再切分用的连接符（与 permissions._SEGMENT_SPLIT 同源，避免循环引用手动内联）。
#  单个 & 是后台符，同样隔开两条命令；但 2>&1、&> 里的 & 属于重定向，不切
_CONNECTORS = ("&&", "||", ";", "|", "&", "\n")

#  命令名之前的 shell 语法外壳：子 shell/分组、流程关键字、变量赋值、前置重定向
_SHELL_KEYWORDS = frozenset({"!", "{", "if", "then", "else", "elif", "do", "while", "until"})
_ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\+?=")
_REDIRECTION = re.compile(r"\d*(?:&>>?|>>?|>&|>\||<<<|<<-?|<>|<&|<)")


def command_risk(command: str) -> str | None:
    """给确认框用的汇总判定：危险操作优先，其次参数注入口。返回原因或 None。"""
    if reason := dangerous_command(command):
        return reason
    for segment in _split_script(command):
        if reason := injection_risk(segment):
            return reason
    return None


def dangerous_command(command: str) -> str | None:
    """从命令里挖出破坏性操作（当前主要是强制 rm）。返回原因或 None。

    宽松方向：尽量识别，识别不出不代表安全（安全判定另有 allow/确认兜底）。
    扫不完（嵌套过深、分叉过多）返回原因——宁可多问一次，不能当安全放行。
    """
    return _scan_command(command, _dangerous_hit)


def privileged_command(command: str) -> str | None:
    """命令里出现提权入口（sudo/doas/su/pkexec）时返回原因，否则 None。

    与 `dangerous_command` 同样是宽松方向：`bash -c 'sudo …'`、`env -S 'sudo …'`、
    `echo "$(sudo …)"` 这类包起来的写法也要挖出来。识别不出不代表安全——
    沙箱与逐次确认仍在。
    """
    return _scan_command(command, _privileged_hit)


def _privileged_hit(name: str, argv: list[str]) -> str | None:
    if name in _PRIVILEGE_ESCALATORS:
        return f"提权命令（{name}）"
    return None


def _split_script(script: str) -> list[str]:
    """按连接符把脚本粗切成段——**认引号**：单/双引号内的 && || ; | 属于参数，
    不当连接符切。裸切（旧实现）会把 `grep 'a|b|c'`、`echo "x && y"` 里引号中的
    连接符也切开，段内引号被劈成两半 → 下游 shlex 判成"引号不闭合"而误报风险。

    引号规则贴 bash：单引号内无转义、只认闭合的 `'`；双引号内 `\\` 转义下一字符
    （`\\"` 不算闭合）；引号外 `\\` 也转义下一字符（`\\|` 是字面量不是连接符）。
    真正不闭合的引号会一直吃到末尾成一段，下游 shlex 仍会如实报"无法解析"——
    该报的没漏。方向仍宽松：认引号只是别切错，不追求完整 shell 语法。"""
    segments: list[str] = []
    buf: list[str] = []
    quote = ""  # "'" 或 '"'；空 = 不在引号内
    i, n = 0, len(script)
    while i < n:
        ch = script[i]
        if quote == "'":
            buf.append(ch)
            if ch == "'":
                quote = ""
            i += 1
        elif quote == '"':
            if ch == "\\" and i + 1 < n:
                buf.append(ch)
                buf.append(script[i + 1])
                i += 2
                continue
            buf.append(ch)
            if ch == '"':
                quote = ""
            i += 1
        elif ch == "\\" and i + 1 < n:
            buf.append(ch)
            buf.append(script[i + 1])
            i += 2
        elif ch in ("'", '"'):
            quote = ch
            buf.append(ch)
            i += 1
        elif (connector := next((c for c in _CONNECTORS if script.startswith(c, i)), None)) and not (
            connector == "&" and ((i > 0 and script[i - 1] in "<>") or script.startswith("&>", i))
        ):
            segments.append("".join(buf))
            buf = []
            i += len(connector)
        else:
            buf.append(ch)
            i += 1
    segments.append("".join(buf))
    return [segment.strip() for segment in segments if segment.strip()]


def _closing_paren(script: str, start: int) -> int:
    """从 start（`$(` 之后）找配对的 `)`，找不到返回 len(script)（取到末尾，宽松方向）。

    用显式栈而不是递归：病态的深层嵌套不能把 Python 调用栈打爆。
    双引号内的括号是字面量，但双引号里的 `$(` 照样开新一层。"""
    stack = ["("]
    i, n = start, len(script)
    while i < n:
        ch, top = script[i], stack[-1]
        if top == "'":
            if ch == "'":
                stack.pop()
        elif ch == "\\":
            i += 1
        elif top == '"':
            if ch == '"':
                stack.pop()
            elif script.startswith("$(", i):
                stack.append("(")
                i += 1
        elif ch in ("'", '"'):
            stack.append(ch)
        elif ch == "(":
            stack.append("(")
        elif ch == ")":
            stack.pop()
            if not stack:
                return i
        i += 1
    return n


def _substitutions(script: str) -> list[str]:
    """脚本里会被执行的命令替换体：`$(…)`、反引号、进程替换 `<(…)` / `>(…)`。

    单引号内不展开（`echo '$(rm -rf /)'` 只是字面量），双引号内照样展开，
    `\\$(` 是转义的字面量。只取最外层——里面再嵌套的由递归扫描接着挖。
    """
    bodies: list[str] = []
    quote = ""
    i, n = 0, len(script)
    while i < n:
        ch = script[i]
        if quote == "'":
            if ch == "'":
                quote = ""
            i += 1
        elif ch == "\\":
            i += 2
        elif ch == "`":
            end = i + 1
            while end < n and script[end] != "`":
                end += 2 if script[end] == "\\" else 1
            bodies.append(script[i + 1:end])
            i = end + 1
        elif script.startswith("$(", i) or (not quote and ch in "<>" and script.startswith("(", i + 1)):
            end = _closing_paren(script, i + 2)
            bodies.append(script[i + 2:end])
            i = end + 1
        else:
            if ch == '"':
                quote = "" if quote else '"'
            elif ch == "'" and not quote:
                quote = "'"
            i += 1
    return bodies


def _strip_shell_prefix(argv: list[str]) -> list[str]:
    """剥掉命令名之前的 shell 语法外壳：`(rm …)`、`then rm …`、`FOO=1 rm …`、`2>/dev/null rm …`。

    不剥的话 argv[0] 是 `(rm` / `then` / `FOO=1`，按名字写的检查全部落空。
    """
    while argv:
        head = argv[0]
        if head.startswith("("):
            head = head.lstrip("(")
            argv = [head, *argv[1:]] if head else argv[1:]
        elif head in _SHELL_KEYWORDS or _ASSIGNMENT.match(head):
            argv = argv[1:]
        elif match := _REDIRECTION.match(head):
            #  `>/dev/null` 目标粘在一起只占一个 token；`> /dev/null` 要连目标一起跳
            argv = argv[1:] if match.end() < len(head) else argv[2:]
        else:
            break
    return argv


def _inner_scripts(name: str, argv: list[str]) -> list[str]:
    """参数里被当作 shell 源码的那部分：`bash -c '…'`、`trap '…' EXIT`、`eval '…'`。

    返回的每一项都要重新当脚本扫一遍。
    """
    if name in _SHELLS:
        #  -c 只是开关，脚本是它之后的第一个非选项参数，中间可能夹着带值选项
        #  （`bash -o pipefail -c …`、`bash --rcfile x -c …`）。分不清哪个才是脚本，
        #  就把 -c 之后的非选项参数都当脚本扫（宽松方向，多扫的是 $0/$1 这类参数）
        scripts: list[str] = []
        seen_c = False
        for arg in argv[1:]:
            if arg.startswith("--command="):
                scripts.append(arg.split("=", 1)[1])  # fish --command=…
            elif arg == "--command" or (
                arg.startswith("-") and not arg.startswith("--") and "c" in arg[1:]
            ):
                seen_c = True
            elif seen_c and not arg.startswith(("-", "+")):
                scripts.append(arg)
        return scripts
    if name == "trap" and len(argv) >= 2:
        #  trap 'action' SIGNAL：action 是一段 shell 源码
        return [argv[1]]
    if name == "eval" and len(argv) >= 2:
        #  eval 把全部参数拼成一段源码执行
        return [" ".join(argv[1:])]
    return []


class _Budget:
    """一次判定的扫描工作量：每下钻一层记一个节点，超深度或超总量抛 _TooComplex。"""

    def __init__(self) -> None:
        self.nodes = 0

    def descend(self, depth: int) -> int:
        self.nodes += 1
        if depth + 1 > _MAX_WRAPPER_DEPTH or self.nodes > _MAX_SCAN_NODES:
            raise _TooComplex
        return depth + 1


def _scan_command(command: str, hit) -> str | None:
    """危险命令与提权的共用入口；扫不完按有风险返回（fail-safe）。"""
    try:
        return _scan_script(command, 0, hit, _Budget())
    except _TooComplex:
        return _TOO_COMPLEX


def _scan_script(script: str, depth: int, hit, budget: _Budget) -> str | None:
    """扫一段 shell 源码：先挖命令替换体（递归当脚本扫），再逐段扫。"""
    for body in _substitutions(script):
        if reason := _scan_script(body, budget.descend(depth), hit, budget):
            return reason
    for segment in _split_script(script):
        if reason := _scan_segment(_split(segment), depth, hit, budget):
            return reason
    return None


def _scan_segment(argv: list[str], depth: int, hit, budget: _Budget) -> str | None:
    """通用的段扫描：剥语法外壳、剥 wrapper、下钻 shell 源码，每层拿 `hit` 问一次。

    `hit(name, argv) -> str | None` 是两套判定唯一不同的地方。
    """
    argv = _strip_shell_prefix(argv)
    if not argv:
        return None
    name = _base_name(argv[0])
    if reason := hit(name, argv):
        return reason
    if (peeled := _peel_wrapper(name, argv)) is not None:
        commands, scripts = peeled
        for inner in commands:
            if reason := _scan_segment(inner, budget.descend(depth), hit, budget):
                return reason
        for script in scripts:
            if reason := _scan_script(script, budget.descend(depth), hit, budget):
                return reason
        return None
    for script in _inner_scripts(name, argv):
        if reason := _scan_script(script, budget.descend(depth), hit, budget):
            return reason
    return None


def _dangerous_hit(name: str, argv: list[str]) -> str | None:
    if name == "rm" and _rm_has_force(argv[1:]):
        return "强制删除（rm -f/-rf）"
    return None


def _rm_has_force(args: list[str]) -> bool:
    """rm 是否带 force。`--` 之后是文件名（`rm -- -f` 是删一个叫 -f 的文件）。"""
    for arg in args:
        if arg == "--":
            return False
        if arg == "--force":
            return True
        #  短选项聚合：-rf / -fr / -f；排除长选项（--foo 里的 f 不算）
        if arg.startswith("-") and not arg.startswith("--") and "f" in arg[1:]:
            return True
    return False
