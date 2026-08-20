"""命令风险分析：参数注入识别 + 危险命令识别。

两个函数、两个方向（核心设计观念——方向相反的两套判定不能共用一个解析器）：

- ``injection_risk``：回答"这条命令能不能被 allow 规则**免确认放行**"。
  前缀规则只看命令开头，而很多常见命令有"参数级逃逸口"——任何能指定
  「外部程序」或「输出文件」的 flag 都能把 `git *` 这类 allow 变成任意代码执行
  （`git -c core.pager='!sh …' log`）。识别到就不吃 allow 规则，退回人工确认。
  **保守方向**：拿不准就报风险（多问一次的代价可接受）。

- ``dangerous_command``：回答"这条命令是不是破坏性操作"（强制 rm 等）。
  攻击面在包装层：`sudo rm -rf`、`env X=1 rm -rf`、`bash -c 'rm -rf …'`、
  `trap 'rm -rf …' EXIT` 都包着同一个 rm。识别时要**宽松解析、递归剥 wrapper**，
  尽量从复杂写法里挖出字面量命令。识别不出来不要紧——识别不出的命令本来
  就进不了 allow 通道（injection_risk / 前缀不匹配兜底），最终仍会走人工确认。

- ``privileged_command``：回答"这条命令要不要提权"（sudo/doas/su/pkexec）。
  与 ``dangerous_command`` 同方向（宽松解析、剥同一批 wrapper），只是命中的
  是另一类东西，所以剥法抽成了共用的 ``_peel_wrapper`` / ``_inner_scripts``。
  给 auto 档用：沙箱能拦住越权写盘，但提权是唯一可能捅穿它的动作，得有人看着。
"""

from __future__ import annotations

import re
import shlex

#  递归剥 wrapper 的深度上限：
#  防止构造出的嵌套命令把解析拖死
_MAX_WRAPPER_DEPTH = 8


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


# ---------- 危险命令（递归剥 wrapper） ----------

#  透传型 wrapper：剥掉第一个 token 继续看真正的命令
_PASSTHROUGH_WRAPPERS = {"sudo", "doas", "nice", "nohup", "time", "timeout", "stdbuf", "command"}
#  会把一段 shell 源码当参数的 shell：-c / -lc 后面那坨要重新按脚本解析
_SHELLS = {"bash", "sh", "zsh", "dash", "ksh", "fish"}
#  提权入口。sudo/doas 同时也在 _PASSTHROUGH_WRAPPERS 里——两张表各答各的问题：
#  查危险命令时要剥开 sudo 看里面包着什么，查提权时看见 sudo 本身就已经命中。
_PRIVILEGE_ESCALATORS = {"sudo", "doas", "su", "pkexec", "runas"}

#  脚本再切分用的连接符（与 permissions._SEGMENT_SPLIT 同源，避免循环引用手动内联）
_CONNECTORS = ("&&", "||", ";", "|", "\n")


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
    """
    for segment in _split_script(command):
        if reason := _dangerous_segment(_split(segment), depth=0):
            return reason
    return None


def privileged_command(command: str) -> str | None:
    """命令里出现提权入口（sudo/doas/su/pkexec）时返回原因，否则 None。

    与 `dangerous_command` 同样是宽松方向：`bash -c 'sudo …'`、`xargs sudo …`
    这类包起来的写法也要挖出来。识别不出不代表安全——沙箱与逐次确认仍在。
    """
    for segment in _split_script(command):
        if reason := _scan_segment(_split(segment), 0, _privileged_hit):
            return reason
    return None


def _privileged_hit(name: str, argv: list[str]) -> str | None:
    if name in _PRIVILEGE_ESCALATORS:
        return f"提权命令（{name}）"
    return None


def _split_script(script: str) -> list[str]:
    """按连接符把脚本粗切成段。不处理引号——宽松方向宁可多切错切。"""
    segments = [script]
    for connector in _CONNECTORS:
        parts: list[str] = []
        for piece in segments:
            parts.extend(piece.split(connector))
        segments = parts
    return [segment.strip() for segment in segments if segment.strip()]


def _peel_wrapper(name: str, argv: list[str]) -> list[str] | None:
    """透传型 wrapper（sudo/env/xargs/timeout…）剥掉外层，返回里面真正的 argv。

    不是 wrapper 则返回 None（注意与"是 wrapper 但里面是空"的 `[]` 区分）。
    剥法只写这一处：危险命令与提权两套判定共用，加一个 wrapper 两边同时受益。
    """
    if name in _PASSTHROUGH_WRAPPERS or name == "xargs":
        #  timeout/stdbuf/xargs 带自己的选项参数，粗剥即可：跳过所有 -开头 token
        rest = argv[1:]
        while rest and rest[0].startswith("-"):
            rest = rest[1:]
        #  timeout 的第一个位置参数是秒数
        if name == "timeout" and rest:
            rest = rest[1:]
        return rest
    if name == "env":
        rest = argv[1:]
        while rest and (rest[0] in ("-i", "--ignore-environment", "-u", "--")
                        or "=" in rest[0] and not rest[0].startswith("-")):
            if rest[0] in ("-u",):
                rest = rest[2:]
            else:
                rest = rest[1:]
        return rest
    return None


def _inner_scripts(name: str, argv: list[str]) -> list[str]:
    """参数里被当作 shell 源码的那部分：`bash -c '…'`、`trap '…' EXIT`。

    返回的每一项都要重新走一遍 `_split_script` + 对应的段判定。
    """
    if name in _SHELLS:
        #  bash -c 'script' / bash -lc 'script'
        for index, arg in enumerate(argv[1:], start=1):
            if arg.startswith("-") and "c" in arg.lstrip("-"):
                if index + 1 < len(argv):
                    return [argv[index + 1]]
                break
        return []
    if name == "trap" and len(argv) >= 2:
        #  trap 'action' SIGNAL：action 是一段 shell 源码
        return [argv[1]]
    return []


def _scan_segment(argv: list[str], depth: int, hit) -> str | None:
    """通用的段扫描：剥 wrapper、下钻 shell 源码，每层拿 `hit` 问一次。

    `hit(name, argv) -> str | None` 是两套判定唯一不同的地方。
    """
    if not argv or depth > _MAX_WRAPPER_DEPTH:
        return None
    name = _base_name(argv[0])
    if reason := hit(name, argv):
        return reason
    if (inner := _peel_wrapper(name, argv)) is not None:
        return _scan_segment(inner, depth + 1, hit)
    for script in _inner_scripts(name, argv):
        for segment in _split_script(script):
            if reason := _scan_segment(_split(segment), depth + 1, hit):
                return reason
    return None


def _dangerous_hit(name: str, argv: list[str]) -> str | None:
    if name == "rm" and _rm_has_force(argv[1:]):
        return "强制删除（rm -f/-rf）"
    return None


def _dangerous_segment(argv: list[str], depth: int) -> str | None:
    return _scan_segment(argv, depth, _dangerous_hit)


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
