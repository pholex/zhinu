"""shell 集成（`xiaoyu term`）：人在自己的 shell 里干活，随时 `@x 问题`。

不进 REPL 的工作方式：shell 钩子把每条敲过的命令（命令文本与退出码，没有输出）
追加进一个 pending 文件；`@x 问题` 时 `term run` 把这些命令作为「终端上下文」
放在问题前面交给模型，并续写同一个会话，所以可以连续追问。

三条纪律，决定了这个模块的形状：

- **钩子里不起 Python 进程。** 每条命令 fork 一次解释器（百毫秒级）会让 shell
  明显发卡，所以 `term init` 输出的脚本全用 shell 内建（`print -r` /
  `printf` / `string` / `Add-Content`）追加一行 `<epoch>\\t<cwd>\\t<命令行>`，
  命令跑完再追加一行 `=<epoch>\\t<退出码>`，Python 只在 `@x` 时才启动。
  备用入口 `term log` 走 `__main__` 的快路径，同样不导入 agent / tools 这些
  重模块。
- **本模块本身要轻。** 快路径会导入它，所以模块级只碰标准库与 config；
  会话日志、脱敏正则（mcp）、`<untrusted_content>` 消毒（tools）都在函数
  里按需导入。
- **命令文本当外部内容对待。** 命令行可能是粘贴来的，拼进 prompt 时裹上
  `<untrusted_content>`，并在拼之前脱敏——pending 文件记的是原样文本，
  模型只看脱敏后的。
"""

from __future__ import annotations

import os
import re
import secrets
import shlex
import sys
import time
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path

from .config import user_config_dir

SESSION_ENV = "XIAOYU_TERM_SESSION"
#  钩子往哪个文件追加：init 时算好路径一并导出，钩子里不必再推导配置目录
PENDING_ENV = "XIAOYU_TERM_PENDING"
SESSION_PREFIX = "term-"
SHELLS = ("bash", "zsh", "fish", "powershell")
#  pending 文件上限：超了只留尾部。一次提问带几百条命令已经没有信息量，
#  更多只是在烧上下文
MAX_PENDING_LINES = 500
MAX_PENDING_BYTES = 256 * 1024

#  自己人的调用不记：`@x …` 本身、`xiaoyu term …`。shell 脚本里有同一份判断
#  （那是第一道），这里是第二道——`term log` 入口与手改过脚本的用户都靠它
_OWN_COMMAND = re.compile(
    r"^\s*(?:@x|@xiaoyu|Ask-Xiaoyu)(?:\s|$)"
    r"|^\s*(?:xiaoyu|xy|xiaoyu-agent)\s+term(?:\s|$)"
    r"|-m\s+xiaoyu\s+term(?:\s|$)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Entry:
    ts: int
    cwd: str
    command: str
    #  退出码；None = 没记到（旧版脚本、`term log`、命令还没跑完、shell 被杀）
    status: int | None = None


# ---------------------------------------------------------------- 会话与路径


def current_session() -> str:
    """当前 shell 的会话 id（`term init` 导出的环境变量）；没 init 过回空串。"""
    return os.environ.get(SESSION_ENV, "").strip()


def session_id_for(name: str | None) -> str:
    """`--name` 给了就是确定的 `term-<name>`（多个终端共用、重启续用），
    没给就每个终端一个随机 id。名字要当文件名用，按会话名同一套规则校验。"""
    from .session_log import check_session_id

    if name:
        return check_session_id(f"{SESSION_PREFIX}{name.strip()}")
    return f"{SESSION_PREFIX}{secrets.token_hex(4)}"


def pending_dir() -> Path:
    return user_config_dir() / "term"


def pending_path(session_id: str) -> Path:
    """pending 文件位置。优先信 init 导出的路径：钩子写的就是它，这边再按
    配置目录推一遍只会在 XDG 变量中途变了时对不上。"""
    exported = os.environ.get(PENDING_ENV, "").strip()
    if exported and Path(exported).name == f"{session_id}.pending":
        return Path(exported)
    return pending_dir() / f"{session_id}.pending"


def term_sessions_dir() -> Path:
    """终端会话的会话文件目录：不按工作区分子目录——人在 shell 里 cd 来 cd 去，
    一个终端就是一段对话，不该换个目录就换个会话。"""
    from .session_log import sessions_dir

    return sessions_dir() / "term"


# ---------------------------------------------------------------- 行格式


def escape_field(text: str) -> str:
    """一行一条记录，字段用制表符分隔：字段里的反斜杠/制表符/换行转义掉。
    与各 shell 脚本里的内建替换是同一套约定（先反斜杠、再制表符、再换行）。"""
    return text.replace("\\", "\\\\").replace("\t", "\\t").replace("\n", "\\n")


_UNESCAPE = re.compile(r"\\([\\tn])")


def unescape_field(text: str) -> str:
    return _UNESCAPE.sub(lambda m: {"\\": "\\", "t": "\t", "n": "\n"}[m.group(1)], text)


def format_line(ts: int, cwd: str, command: str) -> str:
    return f"{int(ts)}\t{escape_field(cwd)}\t{escape_field(command)}"


def parse_line(line: str) -> Entry | None:
    """解析一行；格式不对（手改坏了、半行）回 None，调用方跳过即可。"""
    line = line.rstrip("\r\n")
    if not line.strip():
        return None
    parts = line.split("\t", 2)
    if len(parts) != 3:
        return None
    raw_ts, cwd, command = parts
    try:
        ts = int(float(raw_ts)) if raw_ts.strip() else 0
    except ValueError:
        ts = 0
    command = unescape_field(command)
    if not command.strip():
        return None
    return Entry(ts, unescape_field(cwd), command)


#  状态行：`=<命令行的 epoch>\t<退出码>`。命令行在命令开跑前就写了（管道里的
#  `cmd | @x …` 要在跑的当口就看得见这一行），退出码要等跑完才有，只能另起一行；
#  用开跑时刻认领它属于哪条命令。两个字段，命令行的解析读它必然回 None
STATUS_MARK = "="


def format_status(ts: int, status: int) -> str:
    return f"{STATUS_MARK}{int(ts)}\t{int(status)}"


def parse_status(line: str) -> tuple[int, int] | None:
    """解析状态行，回 (命令行的 epoch, 退出码)；不是状态行或写坏了回 None。"""
    line = line.rstrip("\r\n")
    if not line.startswith(STATUS_MARK):
        return None
    parts = line[len(STATUS_MARK):].split("\t")
    if len(parts) != 2:
        return None
    try:
        return int(parts[0]), int(parts[1])
    except ValueError:
        return None


def is_own_command(command: str) -> bool:
    return bool(_OWN_COMMAND.search(command))


# ---------------------------------------------------------------- pending 文件


def _tail(lines: list[str]) -> list[str]:
    """按命令条数与字节数两个上限截尾（留最新的）。状态行只占字节、不占条数；
    截掉了命令行的状态行留在头部也无妨，认领不到命令就被丢弃。"""
    commands = total = 0
    start = len(lines)
    for index in range(len(lines) - 1, -1, -1):
        line = lines[index]
        size = len(line.encode("utf-8", "replace")) + 1
        is_command = not line.startswith(STATUS_MARK)
        if total + size > MAX_PENDING_BYTES or (is_command and commands >= MAX_PENDING_LINES):
            break
        total += size
        commands += is_command
        start = index
    return lines[start:]


def _read_lines(path: Path) -> list[str]:
    #  utf-8-sig：Windows PowerShell 5.1 的 Add-Content -Encoding utf8 会在新建
    #  文件开头写 BOM
    try:
        text = path.read_bytes().decode("utf-8-sig", "replace")
    except OSError:
        return []
    return [line for line in text.splitlines() if line.strip()]


def _write_lines(path: Path, lines: list[str]) -> None:
    from . import fsguard

    path.parent.mkdir(parents=True, exist_ok=True)
    fsguard.write_atomic(path, "".join(f"{line}\n" for line in lines), private=True)


def append_pending(path: Path, command: str, cwd: str | None = None, ts: int | None = None) -> bool:
    """追加一条（`term log` 用）。自己人的命令不记，返回是否记了。

    追加用 O_APPEND 单次 write，与 shell 的 `>>` 同一种原子性；文件胀过上限
    两倍才截尾——截尾要整文件重写，不该每条都做。
    """
    if is_own_command(command) or not command.strip():
        return False
    line = format_line(int(time.time()) if ts is None else ts, cwd or os.getcwd(), command)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(line + "\n")
    try:
        if path.stat().st_size > MAX_PENDING_BYTES * 2:
            _write_lines(path, _tail(_read_lines(path)))
    except OSError:
        pass
    return True


def count_pending(path: Path) -> int:
    return sum(1 for line in _read_lines(path) if parse_line(line) is not None)


def drain_pending(path: Path) -> list[Entry]:
    """取走并清空 pending：先改名再读。改名之后 shell 的 `>>` 会新建文件，
    这次提问之后敲的命令归下一次，不会被这次读走又清掉。"""
    taken = path.with_name(f"{path.name}.{os.getpid()}.draining")
    try:
        os.replace(path, taken)
    except FileNotFoundError:
        return []
    except OSError:
        #  改不了名（权限/跨设备）：退回"读完再清"，窗口里的新命令可能丢一条
        entries = _entries(_read_lines(path))
        try:
            path.unlink()
        except OSError:
            pass
        return entries
    try:
        lines = _read_lines(taken)
    finally:
        try:
            taken.unlink()
        except OSError:
            pass
    return _entries(_tail(lines))


def _entries(lines: list[str]) -> list[Entry]:
    entries: list[Entry] = []
    for line in lines:
        finished = parse_status(line)
        if finished is not None:
            _claim_status(entries, *finished)
            continue
        entry = parse_line(line)
        if entry is not None:
            entries.append(entry)
    #  认领完再滤掉自己人：先滤的话，它后面那条状态行会找错主
    return [entry for entry in entries if not is_own_command(entry.command)]


def _claim_status(entries: list[Entry], ts: int, status: int) -> None:
    """状态行归最近一条同一时刻开跑、还没有退出码的命令。找不到就丢：命令行
    已被上一次提问取走（`cmd | @x …`）或被截尾截掉了。"""
    for index in range(len(entries) - 1, -1, -1):
        entry = entries[index]
        if entry.ts == ts and entry.status is None:
            entries[index] = replace(entry, status=status)
            return


def requeue(path: Path, entries: list[Entry]) -> None:
    """取走之后没能交给模型（配置错、会话被占）：放回文件头，下次提问还带着。"""
    if not entries:
        return
    lines: list[str] = []
    for entry in entries:
        lines.append(format_line(entry.ts, entry.cwd, entry.command))
        if entry.status is not None:
            lines.append(format_status(entry.ts, entry.status))
    _write_lines(path, _tail(lines + _read_lines(path)))


# ---------------------------------------------------------------- 脱敏


#  命令行特有的凭据形态，补在 mcp 那份通用脱敏之后（那份认 Authorization /
#  Bearer / sk- / ghp_ / URL 里的 user:pass / key=value）。这里只换值、留下
#  旗标名，模型还能看出"这条是在登录"：
#  - `--password x` / `--token x` 这类空格分隔的旗标值（通用那份只认 = 和 :）
#  - `AWS_SECRET_ACCESS_KEY=…` 这类关键词在中间的环境变量赋值
#  - mysql 系的 `-p密码`（紧贴写法；`-p` 单独出现是交互输入，不动）
#  - sshpass -p、curl/wget 的 -u user:pass、smbclient 的 -U user%pass
_FLAG_VALUE = re.compile(
    r"(?P<flag>(?<![\w-])--?(?:password|passwd|pass|pw|token|api[-_]?key|secret(?:[-_]key)?"
    r"|access[-_]key|auth|bearer|credentials?)(?:=|\s+))(?P<value>[^\s]+)",
    re.IGNORECASE,
)
_ENV_ASSIGN = re.compile(
    r"(?P<key>(?<![\w-])[A-Za-z_][A-Za-z0-9_]*(?:SECRET|PASSWORD|PASSWD|TOKEN|API_?KEY|CREDENTIAL)"
    r"[A-Za-z0-9_]*=)(?P<value>[^\s]+)",
    re.IGNORECASE,
)
_MYSQL_P = re.compile(r"(?P<head>(?:^|\s)(?:mysql\w*|mariadb\w*)\b[^\n]*?\s-p)(?P<value>[^\s-][^\s]*)")
_SSHPASS = re.compile(r"(?P<head>(?:^|\s)sshpass\s+-p\s*)(?P<value>[^\s]+)")
_USER_PASS = re.compile(r"(?P<head>(?<![\w-])(?:-u|--user|-U)[=\s]+[^\s:%]+[:%])(?P<value>[^\s]+)")
_KNOWN_TOKENS = re.compile(
    r"(?<![A-Za-z0-9])(?:xox[abpr]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16}|glpat-[A-Za-z0-9_-]{20,})"
)
REDACTED = "[REDACTED]"


def redact(command: str) -> str:
    from .mcp import _redact

    text = _redact(command)
    text = _FLAG_VALUE.sub(lambda m: m.group("flag") + REDACTED, text)
    text = _ENV_ASSIGN.sub(lambda m: m.group("key") + REDACTED, text)
    text = _MYSQL_P.sub(lambda m: m.group("head") + REDACTED, text)
    text = _SSHPASS.sub(lambda m: m.group("head") + REDACTED, text)
    text = _USER_PASS.sub(lambda m: m.group("head") + REDACTED, text)
    return _KNOWN_TOKENS.sub(REDACTED, text)


# ---------------------------------------------------------------- 拼 prompt


def shorten_home(path: str) -> str:
    home = str(Path.home())
    return f"~{path[len(home):]}" if home != "/" and path.startswith(home) else path


def _stamp(ts: int, now: float) -> str:
    if ts <= 0:
        return "--:--"
    moment = datetime.fromtimestamp(ts)
    today = datetime.fromtimestamp(now)
    if moment.date() == today.date():
        return moment.strftime("%H:%M")
    return moment.strftime("%m-%d %H:%M")


def build_prefix(entries: list[Entry], now: float | None = None) -> str:
    """终端上下文段落；没有命令就回空串（`term run` 不加前缀）。

    连续同目录的命令只标一次目录；每行是 `时刻  $ 命令`，记到了退出码的在
    行尾标 `→ 退出码 N`。整段裹进 <untrusted_content>：命令文本可能是粘贴来的，
    里面若有伪造的闭合标记先消毒。
    """
    if not entries:
        return ""
    from .tools import neutralize_untrusted_markers

    now = time.time() if now is None else now
    lines: list[str] = []
    last_cwd: str | None = None
    for entry in entries:
        if entry.cwd != last_cwd:
            lines.append(f"# {shorten_home(entry.cwd)}")
            last_cwd = entry.cwd
        outcome = "" if entry.status is None else f"  → 退出码 {entry.status}"
        lines.append(f"{_stamp(entry.ts, now)}  $ {redact(entry.command)}{outcome}")
    body = neutralize_untrusted_markers("\n".join(lines))
    #  一条退出码都没记到（旧版脚本还在跑）就不提它：别让模型去找不存在的东西
    recorded = "与行尾的退出码" if any(entry.status is not None for entry in entries) else ""
    return (
        "[终端上下文] 自上次提问以来，你在这些目录跑过这些命令（按时间；"
        f"只有命令文本{recorded}、没有输出，需要结果可以自己重跑）：\n"
        f"<untrusted_content>\n{body}\n</untrusted_content>"
    )


def compose(question: str, entries: list[Entry], now: float | None = None) -> str:
    prefix = build_prefix(entries, now)
    return f"{prefix}\n\n{question}" if prefix else question


# ---------------------------------------------------------------- 脚本渲染


def default_launcher(shell: str) -> str:
    """脚本里怎么调 xiaoyu：PATH 上找得到就用裸名；找不到（venv 没激活、pipx
    的 bin 目录不在 PATH）就钉死当前解释器 `-m xiaoyu`。"""
    import shutil

    if shutil.which("xiaoyu"):
        return "xiaoyu"
    if shell == "powershell":
        return f"& {_ps_quote(sys.executable)} -m xiaoyu"
    if shell == "fish":
        return f"{_fish_quote(sys.executable)} -m xiaoyu"
    return f"{shlex.quote(sys.executable)} -m xiaoyu"


def _ps_quote(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def _fish_quote(text: str) -> str:
    return "'" + text.replace("\\", "\\\\").replace("'", "\\'") + "'"


def render_script(
    shell: str,
    session_id: str,
    *,
    named: bool,
    command_not_found: bool = False,
    launcher: str | None = None,
    directory: Path | None = None,
) -> str:
    """生成 `term init <shell>` 的输出。

    named=True 时无条件导出会话 id（用户点名要共用这个名字）；匿名 id 只在
    没设时导出——重复 eval、或在子 shell 里再 eval，都接着用同一个终端的会话。
    钩子挂载有守卫，重复 eval 不会挂两次。
    """
    if shell not in SHELLS:
        raise ValueError(f"不支持的 shell：{shell}（可选 {', '.join(SHELLS)}）")
    directory = pending_dir() if directory is None else directory
    launcher = default_launcher(shell) if launcher is None else launcher
    template = _TEMPLATES[shell]
    quote = {"powershell": _ps_quote, "fish": _fish_quote}.get(shell, shlex.quote)
    if shell == "powershell":
        session_block = (
            f"$env:{SESSION_ENV} = {quote(session_id)}"
            if named
            else f"if (-not $env:{SESSION_ENV}) {{ $env:{SESSION_ENV} = {quote(session_id)} }}"
        )
    elif shell == "fish":
        session_block = (
            f"set -gx {SESSION_ENV} {quote(session_id)}"
            if named
            else f"set -q {SESSION_ENV}; or set -gx {SESSION_ENV} {quote(session_id)}"
        )
    else:
        session_block = (
            f"export {SESSION_ENV}={quote(session_id)}"
            if named
            else f'if [ -z "${{{SESSION_ENV}:-}}" ]; then export {SESSION_ENV}={quote(session_id)}; fi'
        )
    cnf = _CNF[shell] if command_not_found else ""
    #  先拼进 command-not-found 段再替换启动命令：那段里也有 @@LAUNCHER@@
    return (
        template.replace("@@CNF@@", cnf)
        .replace("@@SESSION@@", session_block)
        .replace("@@DIR@@", quote(str(directory)))
        .replace("@@LAUNCHER@@", launcher)
        .replace("@@SESSION_ENV@@", SESSION_ENV)
        .replace("@@PENDING_ENV@@", PENDING_ENV)
    )


#  zsh：preexec 拿到的 $1 就是整行命令。zsh/datetime 给 $EPOCHSECONDS（内建）。
#  precmd 进来时 $? 还是刚跑完那条命令的（zsh 在每个钩子函数之后把它复原）；
#  __xiaoyu_term_ts 只在记了命令时才有值，空回车、自己人的调用不会多写状态行。
#  `@x` 是 noglob 别名而不是函数：zsh 默认对不上通配就整行报错，问题里一个半角
#  问号就问不出去。别名展开只发生在命令位置，preexec 的 $1 仍是人敲的原文
_ZSH = r"""# xiaoyu 终端集成（zsh）——放进 ~/.zshrc：eval "$(xiaoyu term init zsh)"
@@SESSION@@
export @@PENDING_ENV@@=@@DIR@@/"$@@SESSION_ENV@@".pending
__xiaoyu_term_preexec() {
  local line=$1
  line=${line#"${line%%[![:space:]]*}"}
  [[ -z $line ]] && return 0
  case $line in
    ("@x"|"@x "*|"@xiaoyu"|"@xiaoyu "*|"xiaoyu term"*|"xy term"*) return 0 ;;
  esac
  local cwd=$PWD
  line=${line//\\/\\\\}; line=${line//$'\t'/\\t}; line=${line//$'\n'/\\n}
  cwd=${cwd//\\/\\\\}; cwd=${cwd//$'\t'/\\t}; cwd=${cwd//$'\n'/\\n}
  typeset -g __xiaoyu_term_ts=${EPOCHSECONDS:-0}
  print -r -- "$__xiaoyu_term_ts"$'\t'"$cwd"$'\t'"$line" >> "$@@PENDING_ENV@@" 2>/dev/null
  return 0
}
__xiaoyu_term_precmd() {
  local st=$?
  [[ -n ${__xiaoyu_term_ts:-} ]] || return 0
  print -r -- "=$__xiaoyu_term_ts"$'\t'"$st" >> "$@@PENDING_ENV@@" 2>/dev/null
  __xiaoyu_term_ts=
  return 0
}
__xiaoyu_term_ask() { @@LAUNCHER@@ term run "$@"; }
alias @xiaoyu='noglob __xiaoyu_term_ask'
alias @x='noglob __xiaoyu_term_ask'
if [[ -z "${__xiaoyu_term_hooked:-}" ]]; then
  zmodload zsh/datetime 2>/dev/null
  autoload -Uz add-zsh-hook
  add-zsh-hook preexec __xiaoyu_term_preexec
  add-zsh-hook precmd __xiaoyu_term_precmd
  typeset -g __xiaoyu_term_hooked=1
fi
@@CNF@@"""

#  bash：没有 preexec，用 DEBUG trap + PROMPT_COMMAND 置位的旗标，每个提示符
#  周期只记第一次触发（DEBUG 对管道/循环里的每个简单命令都会触发）。整行命令
#  从 `history 1` 取（$BASH_COMMAND 只是管道里的第一段）；history 取不到或
#  对不上（HISTCONTROL=ignorespace 时它还是上一条）就退回 $BASH_COMMAND。
#  退出码：__xiaoyu_term_status 排在 PROMPT_COMMAND 最前，那时 $? 还没被别的
#  提示符命令动过；它原样 return 回去，排在后面、同样要读 $? 的提示符命令不受影响。
#  字符串形态用换行拼接：原值以分号结尾时再接 `; ` 是语法错
_BASH = r"""# xiaoyu 终端集成（bash）——放进 ~/.bashrc：eval "$(xiaoyu term init bash)"
@@SESSION@@
export @@PENDING_ENV@@=@@DIR@@/"$@@SESSION_ENV@@".pending
__xiaoyu_term_record() {
  local line=$1
  line=${line#"${line%%[![:space:]]*}"}
  [[ -z $line ]] && return 0
  case $line in
    "@x"|"@x "*|"@xiaoyu"|"@xiaoyu "*|"xiaoyu term"*|"xy term"*) return 0 ;;
  esac
  local cwd=$PWD ts=${EPOCHSECONDS:-}
  [[ -n $ts ]] || ts=$(date +%s 2>/dev/null) || ts=0
  line=${line//\\/\\\\}; line=${line//$'\t'/\\t}; line=${line//$'\n'/\\n}
  cwd=${cwd//\\/\\\\}; cwd=${cwd//$'\t'/\\t}; cwd=${cwd//$'\n'/\\n}
  printf '%s\t%s\t%s\n' "$ts" "$cwd" "$line" >> "$@@PENDING_ENV@@" 2>/dev/null
  __xiaoyu_term_ts=$ts
  return 0
}
__xiaoyu_term_status() {
  local st=$?
  if [[ -n ${__xiaoyu_term_ts:-} ]]; then
    printf '=%s\t%s\n' "$__xiaoyu_term_ts" "$st" >> "$@@PENDING_ENV@@" 2>/dev/null
    __xiaoyu_term_ts=
  fi
  return "$st"
}
__xiaoyu_term_prompt() { __xiaoyu_term_ready=1; }
__xiaoyu_term_debug() {
  [[ ${__xiaoyu_term_ready:-0} == 1 ]] || return 0
  [[ -n ${COMP_LINE:-} ]] && return 0
  [[ ${BASH_SUBSHELL:-0} == 0 ]] || return 0
  [[ "${PROMPT_COMMAND[*]}" == *"$BASH_COMMAND"* ]] && return 0
  __xiaoyu_term_ready=0
  local hist first=${BASH_COMMAND%%[[:space:]]*}
  hist=$(HISTTIMEFORMAT= builtin history 1 2>/dev/null)
  hist=${hist#"${hist%%[![:space:]]*}"}
  hist=${hist#"${hist%%[![:digit:]]*}"}
  hist=${hist#"${hist%%[![:space:]]*}"}
  [[ -n $hist && $hist == *"$first"* ]] || hist=$BASH_COMMAND
  __xiaoyu_term_record "$hist"
  return 0
}
@xiaoyu() { @@LAUNCHER@@ term run "$@"; }
@x() { @@LAUNCHER@@ term run "$@"; }
if [ -z "${__xiaoyu_term_hooked:-}" ]; then
  case "$(declare -p PROMPT_COMMAND 2>/dev/null)" in
    "declare -a"*) PROMPT_COMMAND=(__xiaoyu_term_status "${PROMPT_COMMAND[@]}" __xiaoyu_term_prompt) ;;
    *) PROMPT_COMMAND=$'__xiaoyu_term_status\n'"${PROMPT_COMMAND:+$PROMPT_COMMAND$'\n'}__xiaoyu_term_prompt" ;;
  esac
  trap '__xiaoyu_term_debug' DEBUG
  __xiaoyu_term_hooked=1
fi
@@CNF@@"""

#  fish：fish_preexec 事件带整行命令。fish 没有内建的 epoch，`date` 这一次
#  fork 是外部进程里最便宜的一种（fish 的提示符本来就常年 fork git）。
#  `string` 从管道读时按行处理，换行要最后用 `string join` 合回一行。
#  退出码在 fish_postexec 里取：事件处理函数进来时 $status 还是那条命令的，
#  处理完 fish 会把它复原，提示符照常读得到
_FISH = r"""# xiaoyu 终端集成（fish）——放进 ~/.config/fish/config.fish：xiaoyu term init fish | source
@@SESSION@@
set -gx @@PENDING_ENV@@ @@DIR@@/$@@SESSION_ENV@@.pending
function __xiaoyu_term_preexec --on-event fish_preexec
    set -l raw "$argv[1]"
    if string match -q -r '^\s*$' -- "$raw"
        return 0
    end
    if string match -q -r '^\s*(@x|@xiaoyu)(\s|$)|^\s*(xiaoyu|xy)\s+term(\s|$)' -- "$raw"
        return 0
    end
    set -l line (string replace -a -- '\\' '\\\\' "$raw" | string replace -a -- \t '\\t' | string join -- '\\n' | string trim --left)
    set -l cwd (string replace -a -- '\\' '\\\\' "$PWD" | string replace -a -- \t '\\t' | string join -- '\\n')
    set -g __xiaoyu_term_ts (date +%s)
    printf '%s\t%s\t%s\n' "$__xiaoyu_term_ts" "$cwd" "$line" >> "$@@PENDING_ENV@@" 2>/dev/null
    return 0
end
function __xiaoyu_term_postexec --on-event fish_postexec
    set -l st $status
    if set -q __xiaoyu_term_ts
        printf '=%s\t%s\n' "$__xiaoyu_term_ts" $st >> "$@@PENDING_ENV@@" 2>/dev/null
        set -e __xiaoyu_term_ts
    end
    return 0
end
function @xiaoyu
    @@LAUNCHER@@ term run $argv
end
function @x
    @@LAUNCHER@@ term run $argv
end
set -g __xiaoyu_term_hooked 1
@@CNF@@"""

#  PowerShell：`@x` 在这里是 splatting 语法、当不了命令名，改叫 `x` / `Ask-Xiaoyu`。
#  不包 PSReadLine 的回车键（会顶掉用户自己的绑定），改在 prompt 函数里读
#  Get-History 的增量：命令跑完、下一个提示符出来之前记下来，对 `x 问题` 来说
#  一样及时。包 prompt 时保留原来的 prompt 函数并接着调它。
#  退出码：prompt 的第一句就把 $? 与 $LASTEXITCODE 取走。$? 为真记 0；为假时
#  cmdlet 失败没有数字、记 1，原生命令失败用 $LASTEXITCODE。$LASTEXITCODE 是上一个
#  原生命令留下的、cmdlet 不会清它，所以先看最近一条错误记录是不是这一行报的：
#  是就是 cmdlet 失败，不去读那个可能过期的数
_PWSH = r"""# xiaoyu 终端集成（PowerShell）——放进 $PROFILE：Invoke-Expression (xiaoyu term init powershell | Out-String)
@@SESSION@@
$env:@@PENDING_ENV@@ = Join-Path @@DIR@@ ($env:@@SESSION_ENV@@ + '.pending')
function global:__xiaoyu_term_record {
    param($ok, $code)
    $item = Get-History -Count 1
    if (-not $item) { return }
    if ($global:__xiaoyu_term_last_id -eq $item.Id) { return }
    $global:__xiaoyu_term_last_id = $item.Id
    $line = $item.CommandLine.TrimStart()
    if (-not $line) { return }
    if ($line -match '^(x|Ask-Xiaoyu)(\s|$)|^(xiaoyu|xy)\s+term(\s|$)') { return }
    $ts = [DateTimeOffset]::new($item.StartExecutionTime).ToUnixTimeSeconds()
    $cwd = (Get-Location).Path
    $line = $line -replace '\\', '\\' -replace "`t", '\t' -replace "`r?`n", '\n'
    $cwd = $cwd -replace '\\', '\\' -replace "`t", '\t' -replace "`r?`n", '\n'
    $status = 0
    if (-not $ok) {
        $status = 1
        $err = if ($global:Error.Count -gt 0) { $global:Error[0] } else { $null }
        $at = if (($err -is [System.Management.Automation.ErrorRecord]) -and $err.InvocationInfo) { "$($err.InvocationInfo.Line)".Trim() } else { '' }
        $own = $at -and $item.CommandLine.Contains($at)
        if (-not $own -and $code -is [int] -and $code -ne 0) { $status = $code }
    }
    Add-Content -LiteralPath $env:@@PENDING_ENV@@ -Value @("$ts`t$cwd`t$line", "=$ts`t$status") -Encoding utf8 -ErrorAction SilentlyContinue
}
function global:x { @@LAUNCHER@@ term run @args }
function global:Ask-Xiaoyu { @@LAUNCHER@@ term run @args }
if (-not $global:__xiaoyu_term_hooked) {
    $global:__xiaoyu_term_hooked = $true
    $global:__xiaoyu_term_last_id = (Get-History -Count 1).Id
    if (Test-Path function:prompt) { $function:global:__xiaoyu_term_prev_prompt = $function:prompt }
    function global:prompt {
        $ok = $global:?
        $code = $global:LASTEXITCODE
        __xiaoyu_term_record $ok $code
        if (Test-Path function:__xiaoyu_term_prev_prompt) { & $function:__xiaoyu_term_prev_prompt }
        else { "PS $($executionContext.SessionState.Path.CurrentLocation)> " }
    }
}
@@CNF@@"""

_TEMPLATES = {"zsh": _ZSH, "bash": _BASH, "fish": _FISH, "powershell": _PWSH}

#  command-not-found：敲错的命令整行交给模型。默认不开——每个 typo 都打一次
#  模型太费钱，要的人显式 --command-not-found
_CNF = {
    "zsh": 'command_not_found_handler() { @@LAUNCHER@@ term run "$@"; }\n',
    "bash": 'command_not_found_handle() { @@LAUNCHER@@ term run "$@"; }\n',
    "fish": "function fish_command_not_found\n    @@LAUNCHER@@ term run $argv\nend\n",
    #  CommandOrigin 不是 Runspace 的查找（脚本内部、模块自动加载探测、Get-Command）
    #  不接：那些不是人敲错了字
    "powershell": (
        "$ExecutionContext.InvokeCommand.CommandNotFoundAction = {\n"
        "    param($CommandName, $CommandLookupEventArgs)\n"
        "    if ($CommandLookupEventArgs.CommandOrigin -ne 'Runspace') { return }\n"
        "    $name = $CommandName\n"
        "    $CommandLookupEventArgs.CommandScriptBlock = { @@LAUNCHER@@ term run $name @args }.GetNewClosure()\n"
        "    $CommandLookupEventArgs.StopSearch = $true\n"
        "}\n"
    ),
}


# ---------------------------------------------------------------- 快路径子命令


def info_line(session_id: str) -> str:
    """`term info`：`会话 id · 模型 · 已用 token · 待交付命令数`，给人放进提示符。"""
    from .config import DEFAULT_MODEL
    from .session_log import find_named, last_model, last_usage

    model = os.environ.get("XIAOYU_MODEL", "").strip() or DEFAULT_MODEL
    used = 0
    path = find_named(session_id, "", term_sessions_dir())
    if path is not None:
        model = last_model(path) or model
        usage = last_usage(path) or {}
        used = int(usage.get("prompt_tokens", 0) or 0) + int(usage.get("completion_tokens", 0) or 0)
    waiting = count_pending(pending_path(session_id))
    return f"{session_id} · {model} · {used:,} tok · {waiting} 条待交付"


def fast_command(argv: list[str]) -> int:
    """`term log` / `term info`：不导入 cli（它一导就把 agent/tools 整套带进来）。

    `python -m xiaoyu term log …` 在 __main__ 里先于 cli 被拦下来走到这；
    console script 入口同样从 __main__ 进（见 pyproject）。
    """
    action = argv[0] if argv else ""
    rest = argv[1:]
    session_id = current_session()
    if action == "info":
        if session_id:
            print(info_line(session_id))
        return 0
    if action == "log":
        if not session_id:
            print(
                f"xiaoyu term log：没有 {SESSION_ENV}，先 eval \"$(xiaoyu term init <shell>)\"",
                file=sys.stderr,
            )
            return 2
        command = " ".join(rest).strip()
        if not command:
            print("xiaoyu term log <命令行>：要记什么？", file=sys.stderr)
            return 2
        append_pending(pending_path(session_id), command)
        return 0
    print(f"未知的 term 子命令：{action}", file=sys.stderr)
    return 2
