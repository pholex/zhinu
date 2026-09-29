"""权限规则：allow / deny 规则 + 会话内授权（按小羽体量收敛的权限管线）。

三条要点：
1. **deny 规则是 bypass-immune**：用户显式配置的 deny 连 --yolo 都不放行——
   与 tools.py 的硬拦截（hardline）同级，是"绝不"而不是"想不想"。
   bash 的 deny 既配原文段、也配剥开之后的每一层（command_check.any_layer）：
   换路径、加引号、套 env / bash -c 跑的还是同一条命令。
2. **allow 规则免确认**：bash 前缀（如 ``bash(git *)``）、文件路径 glob
   （如 ``write_file(src/*)``）、或整个工具（如 ``read_file``）。
3. **fail-closed**：规则解析不了就当不存在；复合命令（含 ; && || 等）和含
   命令替换 / 重定向的命令不吃 allow 前缀规则——allow 只放行形状简单、
   看得清楚的命令，看不清就退回人工确认。

规则文件（一行一条，# 注释）：
- 用户级：<用户配置目录>/permissions.txt
- 工作区级：<workspace>/.xiaoyu/permissions.txt（项目可入库共享）
行格式：``allow bash(git *)`` / ``deny bash(curl *)`` / ``allow write_file``
"""

from __future__ import annotations

import fnmatch
import os
import re
import shlex
import sys
from dataclasses import dataclass, field
from pathlib import Path

from . import bash_ast, command_check, fsguard
from .config import user_config_dir

#  复合命令的分隔符：按这些切开后逐段判定。
#  引号里的分隔符也会被切开——对 allow 是保守方向（多问一次），
#  对 deny 是误伤方向（可能多拦），两边的代价都可接受。
_SEGMENT_SPLIT = re.compile(r"(?:\|\||&&|;|\||&|\n)+")

#  命中这些标记的命令不吃 allow 前缀规则：
#  命令替换能把任意命令藏进"看起来被允许"的前缀里（git commit -m "$(...)"），
#  重定向能借允许的命令写任意文件（echo x > ~/.zshrc）。
_ALLOW_UNSAFE_MARKERS = ("$(", "`", ">")

#  「绝不允许被 allow 的 bash 规则」探针：
#  一条持久 allow 规则若能放行其中任意一条，就等于永久废掉整套权限系统——
#  它们全是"任意代码执行"的入口（shell、解释器 -c/-e、env/sudo/nice 等 wrapper
#  包装、npm run 跑 package.json 里的任意脚本、强制 rm）。判定方式不是黑名单前缀比对，
#  而是拿探针去 fnmatch 试规则本身：`allow bash(python *)` 会命中
#  "python -c …" 探针而被拒，`allow bash(python -m pytest*)` 则不会。
#  wrapper 带选项值的写法（`nice -n 5 *`）探针枚举不完，另由 _wrapped_pattern_reason 按结构判。
_BANNED_ALLOW_PROBES = (
    "bash -c evil", "bash -lc evil", "sh -c evil", "zsh -c evil", "dash -c evil",
    "ksh -c evil", "fish -c evil", "cmd /c evil", "powershell -Command evil",
    "pwsh -Command evil",
    "python -c evil", "python3 -c evil", "py -c evil", "pypy -c evil",
    "node -e evil", "deno eval evil", "bun -e evil",
    "perl -e evil", "ruby -e evil", "php -r evil", "lua -e evil",
    "julia -e evil", "Rscript -e evil", "osascript -e evil",
    "eval evil",
    #  透传 wrapper（env/sudo/nice/timeout/setsid/xargs…）：`nice *` 等于放行 `nice bash -c …`。
    #  名单取自 command_check 的 wrapper 选项表，那边加一个 wrapper 这里自动跟上
    *(f"{name} evil" for name in sorted(command_check.WRAPPER_NAMES)),
    "npm run evil", "pnpm run evil", "yarn run evil", "npx evil", "bunx evil",
    "rm -rf /",
)

#  剥 wrapper 模式时的递归上限（`nice sudo timeout 5 *` 这类层层包的模式），超限按拒绝处理
_MAX_PATTERN_DEPTH = 8


def banned_allow_reason(rule: "Rule") -> str | None:
    """持久 allow 规则若会放行任意代码执行入口，返回拒绝原因；否则 None。

    只管 allow（deny 越宽越安全），只管 bash（文件工具没有执行语义）。
    整个工具级的 `allow bash`（无 spec）等于放行一切，同样拒绝——
    要免确认请用会话内授权（确认框答 a）或 --yolo，它们退出即失效。
    """
    if rule.behavior != "allow" or rule.tool != "bash":
        return None
    if rule.spec is None:
        return ("allow bash（不带模式）等于永久放行任意命令。"
                "临时放开请在确认框答 a（本会话）或用 --yolo。")
    for probe in _BANNED_ALLOW_PROBES:
        if fnmatch.fnmatch(probe, rule.spec):
            return (f"该模式会放行「{probe.split(' evil')[0].strip()}」这类任意代码执行入口，"
                    "等于永久绕过整套权限系统。请写更窄的规则"
                    "（如 allow bash(python -m pytest*)），或在确认框答 a 做会话级放行。")
    return _wrapped_pattern_reason(rule.spec, 0)


def _has_glob(token: str) -> bool:
    return any(char in token for char in "*?[")


def _wrapped_pattern_reason(spec: str, depth: int) -> str | None:
    """wrapper 开头的模式（`nice -n 5 *`、`sudo -u root *`、`timeout 30 *`）按结构判。

    探针只写得出 `nice evil` 这种最短形态，wrapper 的选项值却千变万化（-n 5 / -n5 /
    --adjustment=5），枚举不完。所以用 command_check 同一张选项表剥开 wrapper，
    看里面放行的是什么：
    - 里面的命令模式能匹配任意命令（纯通配）或命中探针 → 借 wrapper 放行任意代码执行；
    - 通配符落在选项值 / 位置参数里（`timeout -s KILL *`）→ 它能连命令本身一起吞掉；
    - 剥出来的还是 wrapper（`nice sudo *`）→ 接着剥。
    """
    tokens = spec.split()
    head = tokens[0].rsplit("/", 1)[-1] if tokens else ""
    if _has_glob(head):
        #  wrapper 名本身带通配（`*nice -n 5 *`、`n?ce …`、`[s]udo …`）：unwrap 认不出名字，
        #  不拦就原样放行。逐个代入它能匹配上的 wrapper 名再判，任一代入被拒即拒
        for name in sorted(command_check.WRAPPER_NAMES):
            if fnmatch.fnmatch(name, head) and _wrapped_pattern_reason(
                " ".join([name, *tokens[1:]]), depth + 1
            ):
                return (f"该模式的开头「{tokens[0]}」能匹配「{name}」，会借它放行里面的任意命令，"
                        "等于永久绕过整套权限系统。请写到具体命令（如 allow bash(timeout 60 pytest*)），"
                        "或在确认框答 a 做会话级放行。")
        return None
    try:
        inners = command_check.unwrap_argv(tokens)
    except ValueError:
        inners = [["*"]]  # 选项分叉多到剥不完：按最宽的情况处理
    if inners is None:
        return None
    reason = (f"该模式会借「{tokens[0]}」放行里面的任意命令，等于永久绕过整套权限系统。"
              "请写到具体命令（如 allow bash(timeout 60 pytest*)），或在确认框答 a 做会话级放行。")
    if depth >= _MAX_PATTERN_DEPTH:
        return reason
    if not inners and any(_has_glob(token) for token in tokens[1:]):
        return reason
    for inner in inners:
        #  候选是模式的尾巴时，前面被当成选项/位置参数吃掉的部分不许带通配；
        #  不是尾巴（env -S 拆出来的、su -c 的脚本）就整条模式都不许带通配
        is_tail = 0 < len(inner) < len(tokens) and tokens[len(tokens) - len(inner):] == inner
        consumed = tokens[1:len(tokens) - len(inner)] if is_tail else tokens[1:]
        if any(_has_glob(token) for token in consumed):
            return reason
        text = " ".join(inner)
        if not text:
            continue
        if fnmatch.fnmatch("evil", text) or any(
            fnmatch.fnmatch(probe, text) for probe in _BANNED_ALLOW_PROBES
        ):
            return reason
        if _wrapped_pattern_reason(text, depth + 1):
            return reason
    return None


#  「总是允许」建议规则里按子命令粒度放行的 CLI：第二个词是子命令，
#  git status* 比 git * 更贴近用户刚刚批准的那件事（don't-ask-again 的
#  前缀推导原则：按你确认的这条命令的"命令类"放行，不扩大）。
_MULTIWORD_PREFIXES = frozenset({
    "git", "npm", "pnpm", "yarn", "cargo", "docker", "kubectl", "pip", "pip3",
    "uv", "go", "poetry", "gh", "brew", "conda", "make",
})

#  会话授权不给键的命令头：透传 wrapper（含批量执行器 xargs）/ 会把参数当脚本跑的
#  shell 与 eval/trap。它们"放行一次"时用户看的是里面那条命令，下次里面换成别的
#  就不是同一件事了——这类头永远逐次确认，不进会话授权。
_NO_SESSION_KEY = frozenset(
    command_check.WRAPPER_NAMES
    | {"bash", "sh", "zsh", "dash", "ksh", "fish"}
    | {"eval", "trap"}
)


#  子命令式 CLI 的全局选项：出现在子命令**之前**、不改变"这是哪一类操作"的选项。
#  推导命令类时跳过它们再取子命令——`kubectl -n prod get pods` 与 `kubectl get pods`
#  是同一类。只收确知无害的；表里没有的前导选项一律推不出范围（逐次确认），
#  绝不退回"整个命令头"：那会让批准一条只读查询变成放行该 CLI 的全部子命令。
#  flags = 不带值；valued = 下一个词是它的值（--opt=value 粘连形态按选项名查同一张表）。
_COMMON_FLAGS = frozenset({"-q", "--quiet", "-v", "--verbose", "--no-color"})
_GLOBAL_OPTIONS: dict[str, tuple[frozenset[str], frozenset[str]]] = {
    "git": (frozenset({"--no-pager", "--no-replace-objects", "--no-optional-locks",
                       "--literal-pathspecs", "--bare"}), frozenset()),
    "kubectl": (frozenset(), frozenset({"-n", "--namespace", "--context", "--cluster",
                                        "--user", "--request-timeout"})),
    "docker": (frozenset({"-D", "--debug"}),
               frozenset({"-c", "--context", "-l", "--log-level"})),
    "gh": (frozenset(), frozenset({"-R", "--repo"})),
    "npm": (_COMMON_FLAGS | {"-s", "--silent"}, frozenset({"--prefix"})),
    "pnpm": (_COMMON_FLAGS | {"-s", "--silent", "-r", "--recursive", "-w", "--workspace-root"},
             frozenset({"-F", "--filter", "-C", "--dir"})),
    "yarn": (_COMMON_FLAGS | {"-s", "--silent"}, frozenset({"--cwd"})),
    "cargo": (_COMMON_FLAGS | {"--locked", "--offline", "--frozen"}, frozenset({"--color"})),
    "pip": (_COMMON_FLAGS | {"--no-cache-dir", "--disable-pip-version-check", "--no-input",
                             "--isolated"}, frozenset()),
    "uv": (_COMMON_FLAGS | {"--offline", "--no-cache", "--no-progress"}, frozenset({"--color"})),
    "poetry": (_COMMON_FLAGS | {"-vv", "-vvv", "-n", "--no-interaction", "--no-ansi", "--ansi",
                                "--no-plugins"}, frozenset()),
    "brew": (_COMMON_FLAGS | {"-d", "--debug"}, frozenset()),
    "make": (frozenset({"-s", "--silent", "-k", "--keep-going", "-B", "--always-make"}),
             frozenset()),
    "go": (frozenset(), frozenset()),
    "conda": (frozenset(), frozenset()),
}
_GLOBAL_OPTIONS["pip3"] = _GLOBAL_OPTIONS["pip"]
_MAKE_JOBS = re.compile(r"-j\d+$")
_CARGO_TOOLCHAIN = re.compile(r"\+[\w.-]+$")

#  脚本运行器：第三个词才是"跑哪个脚本"——`npm run build` 与 `npm run deploy` 不是一件事
_SCRIPT_RUNNERS = frozenset({("npm", "run"), ("pnpm", "run"), ("yarn", "run"), ("bun", "run"),
                             ("npm", "run-script")})

#  透传运行器：后面跟的是另一条命令，范围按里面那条命令算（`uv run pytest` 放行后
#  `uv run python -c …` 照问）。运行器自己的选项不解析——带选项就推不出范围。
_PASSTHROUGH_RUNNERS = frozenset({
    ("uv", "run"), ("poetry", "run"), ("conda", "run"),
    ("npm", "exec"), ("pnpm", "exec"), ("pnpm", "dlx"), ("yarn", "exec"), ("yarn", "dlx"),
})
_PASSTHROUGH_HEADS = frozenset({"npx", "bunx", "uvx"})

#  名词-动词式子命令：第二个词只是对象类别，第三个词才是操作——`gh repo view` 与
#  `gh repo delete`、`git stash list` 与 `git stash drop` 不是一类
_NESTED_SUBCOMMANDS = frozenset({
    *(("gh", noun) for noun in (
        "pr", "issue", "repo", "release", "run", "workflow", "secret", "variable", "gist",
        "label", "cache", "codespace", "project", "ruleset", "extension", "auth", "config",
        "alias", "ssh-key", "gpg-key", "search")),
    *(("docker", noun) for noun in (
        "compose", "container", "image", "volume", "network", "system", "buildx", "context",
        "builder", "plugin")),
    *(("git", noun) for noun in ("stash", "remote", "worktree", "submodule", "bisect", "notes")),
    *(("kubectl", noun) for noun in ("config", "rollout", "auth")),
    *(("uv", noun) for noun in ("pip", "tool", "python")),
    ("go", "mod"), ("poetry", "env"), ("poetry", "self"), ("brew", "services"),
    ("conda", "env"), ("conda", "config"), ("npm", "config"), ("npm", "cache"),
})

#  解释器：范围取到"跑的是哪个模块/脚本"。-c / -e 之类把参数当代码跑的形态不在
#  无害选项表里，自然推不出范围。各家同一个字母含义不同（python -E 是忽略环境变量，
#  perl -E 是执行代码），所以无害选项按解释器分别列，不共用。
_INTERPRETER = re.compile(
    r"(?:python|pypy)(?:\d+(?:\.\d+)?)?$|(?:py|node|ruby|perl|php|lua|Rscript|julia)$"
)
_PYTHON_FLAGS = frozenset({"-u", "-B", "-q", "-O", "-OO", "-s", "-S", "-E", "-I", "-b", "-d", "-P"})
_INFO_FLAGS = frozenset({"--version", "--help", "-h", "-V"})
_MAX_PREFIX_DEPTH = 4


def _option_name(token: str) -> str:
    return token.split("=", 1)[0] if token.startswith("--") and "=" in token else token


#  持久规则的模式后缀：命令头后面要隔一个空格（`ls *`，否则 `ls*` 连 lsof 一起放行），
#  子命令后面沿用粘连写法（`git status*`），整条精确的不带通配
_EXACT, _AFTER_HEAD, _AFTER_SUB = "", " *", "*"


def _exact_if_options_only(argv: list[str]) -> tuple[str, int, str] | None:
    """整条命令除了命令头全是选项（`npm -v` / `git --version`）：没有子命令可取，
    范围就是这一条命令本身。后面还跟着位置参数就说不清哪个是子命令——推不出。"""
    if all(token.startswith("-") for token in argv[1:]):
        return " ".join([Path(argv[0]).name, *argv[1:]]), len(argv), _EXACT
    return None


def _stable_prefix(argv: list[str], depth: int = 0) -> tuple[str, int, str] | None:
    """一条简单命令的"命令类"：(范围键, 原始 argv 里属于前缀的词数, 规则模式后缀)。

    范围键是归一后的（全局选项已剥掉），给会话授权做相等比较；词数给持久规则
    拼字面前缀用（规则按命令文本匹配，得保留用户实际写的那些选项）。
    推不出返回 None。调用方已经拦过危险命令、注入口与 wrapper 头。
    """
    if not argv or depth > _MAX_PREFIX_DEPTH:
        return None
    head = Path(argv[0]).name
    if head in _NO_SESSION_KEY:
        return None

    if head in _PASSTHROUGH_HEADS:
        return _through_runner(argv, 1, head, depth)

    if _INTERPRETER.match(head):
        harmless = _PYTHON_FLAGS if head.startswith(("python", "pypy")) or head == "py" else frozenset()
        index = 1
        while index < len(argv):
            token = argv[index]
            if token == "-m" and harmless is _PYTHON_FLAGS:
                #  -m 后面是"另一条命令"：python -m pip list 与 python -m pip install
                #  不是一类，范围按模块自己的子命令算
                if index + 1 >= len(argv):
                    return None
                return _through_runner(argv, index + 1, f"{head} -m", depth)
            if not token.startswith("-"):
                return f"{head} {token}", index + 1, _AFTER_SUB
            if token in _INFO_FLAGS:
                return _exact_if_options_only(argv)
            if token not in harmless:
                return None
            index += 1
        #  没给脚本的裸解释器从标准输入读代码（echo … | python）：推不出范围
        return None

    if head not in _MULTIWORD_PREFIXES:
        return head, 1, _EXACT if len(argv) == 1 else _AFTER_HEAD

    flags, valued = _GLOBAL_OPTIONS.get(head, (frozenset(), frozenset()))
    index = 1
    while index < len(argv):
        token = argv[index]
        if not token.startswith("-"):
            if head == "cargo" and index == 1 and _CARGO_TOOLCHAIN.match(token):
                index += 1
                continue
            break
        name = _option_name(token)
        if name in valued:
            index += 1 if name != token else 2
        elif token in flags or (head == "make" and _MAKE_JOBS.match(token)):
            index += 1
        else:
            return _exact_if_options_only(argv)
    if index >= len(argv):
        return _exact_if_options_only(argv)
    sub = argv[index]
    if (head, sub) in _PASSTHROUGH_RUNNERS:
        return _through_runner(argv, index + 1, f"{head} {sub}", depth)
    if (head, sub) in _SCRIPT_RUNNERS and index + 1 < len(argv):
        script = argv[index + 1]
        if script.startswith("-"):
            return None
        return f"{head} {sub} {script}", index + 2, _AFTER_SUB
    if (head, sub) in _NESTED_SUBCOMMANDS and index + 1 < len(argv):
        verb = argv[index + 1]
        if not verb.startswith("-"):
            return f"{head} {sub} {verb}", index + 2, _AFTER_SUB
    return f"{head} {sub}", index + 1, _AFTER_SUB


def _through_runner(argv: list[str], start: int, label: str,
                    depth: int) -> tuple[str, int, str] | None:
    """透传运行器后面那条命令的范围，拼上运行器前缀。"""
    inner = argv[start:]
    if not inner:
        return label, len(argv), _EXACT
    if inner[0].startswith("-") or command_check.injection_risk_argv(inner):
        return None
    found = _stable_prefix(inner, depth + 1)
    if found is None:
        return None
    key, used, suffix = found
    return f"{label} {key}", start + used, suffix


def _plain_argvs(command: str) -> list[list[str]] | None:
    """命令 → 各段 argv；看不懂返回 None（与 _allowed 同一保守面）。"""
    if os.name != "nt" and bash_ast.available():
        argvs = bash_ast.parse_plain_commands(command)
        return argvs or None
    if any(marker in command for marker in _ALLOW_UNSAFE_MARKERS):
        return None
    argvs = []
    for part in _SEGMENT_SPLIT.split(command):
        if not part.strip():
            continue
        try:
            argvs.append(shlex.split(part))
        except ValueError:
            return None
    return argvs or None


def command_keys(command: str) -> tuple[str, ...] | None:
    """会话授权键：命令每一段一个键（git status / rg / npm test…）。

    答"本会话允许"记的是这些键，不是工具名——`git status` 上答一次，
    后面 `git status -s` 免问，`git commit`、`curl` 照问。推不出键（看不懂 /
    危险命令 / 参数注入口 / wrapper 与 `sh -c` 这类头）返回 None：这种调用
    只能一次一批。
    """
    command = command.strip()
    if not command or command_check.dangerous_command(command):
        return None
    argvs = _plain_argvs(command)
    if argvs is None:
        return None
    keys: list[str] = []
    for argv in argvs:
        if not argv or command_check.injection_risk_argv(argv):
            return None
        found = _stable_prefix(argv)
        if found is None:
            return None
        keys.append(found[0])
    return tuple(keys)


def suggest_allow_rule(name: str, args: dict, workspace: Path) -> Rule | None:
    """从一次待确认的调用推导「总是允许」的持久规则；推不出返回 None。

    确认框的第三个选项靠它：能给用户一条"范围恰好覆盖这类调用"的规则才显示。
    保守三关（推不出就只是少一个选项，不影响允许/拒绝）：
    1. bash 只认单条、能被 bash_ast 白名单解析的简单命令——复合命令、
       重定向、命令替换统统不推导；
    2. 危险命令（强制 rm 等）与参数注入口（git -c / find -exec …）不推导；
    3. 推导结果必须过 banned_allow_reason——python * 这类任意代码执行入口
       在这里就被拦下，不会出现"界面提供了选项、落盘时才报错"。
    """
    if name != "bash":
        path = args.get("path")
        if isinstance(path, str) and path:
            candidate = Path(path).expanduser()
            if not candidate.is_absolute():
                candidate = workspace / candidate
            parent = candidate.resolve().parent
            root = workspace.resolve()
            if parent != root and parent.is_relative_to(root):
                #  目录级规则（write_file(src/*)）：与 _match_path 的
                #  工作区相对匹配对齐；根目录或工作区外退回工具级
                return Rule("allow", name, f"{parent.relative_to(root).as_posix()}/*")
        return Rule("allow", name)

    command = str(args.get("command", "")).strip()
    if not command or command_check.dangerous_command(command):
        return None
    argv: list[str] | None
    if os.name != "nt" and bash_ast.available():
        argvs = bash_ast.parse_plain_commands(command)
        if argvs is None or len(argvs) != 1:
            return None
        argv = argvs[0]
    else:
        #  回退路径（缺 tree-sitter / Windows）：与 _allowed 的回退同一保守面——
        #  复合命令、重定向、命令替换一律不推导，argv 用 shlex 切
        if any(marker in command for marker in _ALLOW_UNSAFE_MARKERS):
            return None
        if len([part for part in _SEGMENT_SPLIT.split(command) if part.strip()]) != 1:
            return None
        try:
            argv = shlex.split(command)
        except ValueError:
            return None
    if not argv or command_check.injection_risk_argv(argv):
        return None
    found = _stable_prefix(argv)
    if found is None:
        return None
    _, used, suffix = found
    literal = argv[:used]
    if any(not token or _has_glob(token) or any(ch.isspace() for ch in token)
           for token in literal):
        #  规则是命令文本上的 fnmatch 模式：前缀里的词自带通配符或空白就拼不出
        #  一条"恰好是它"的模式，宁可不给这个选项
        return None
    #  裸命令（ls）与纯选项命令（npm -v）是精确匹配，宁窄勿宽
    spec = " ".join(literal) + suffix
    rule = Rule("allow", "bash", spec)
    return None if banned_allow_reason(rule) else rule


@dataclass(frozen=True)
class Rule:
    behavior: str  # "allow" | "deny"
    tool: str
    #  None = 整个工具；bash 的 spec 是命令 fnmatch 模式；文件工具的 spec 是路径 glob
    spec: str | None = None

    def __str__(self) -> str:
        target = f"{self.tool}({self.spec})" if self.spec else self.tool
        return f"{self.behavior} {target}"


_RULE_LINE = re.compile(r"^(allow|deny)\s+([A-Za-z_][\w-]*)(?:\((.*)\))?\s*$")


def parse_rule(text: str) -> Rule | None:
    """解析一条规则；解析不了返回 None（fail-closed：坏规则不生效）。"""
    match = _RULE_LINE.match(text.strip())
    if not match:
        return None
    behavior, tool, spec = match.groups()
    if spec is not None:
        spec = spec.strip()
        if not spec:
            #  空括号 bash() 是笔误而不是"整个工具"：宁可无效也不猜意图
            return None
    return Rule(behavior, tool, spec)


@dataclass(frozen=True)
class RuleTest:
    """规则文件里的自测断言（match/not_match 内联测试）。

    行格式：``#test <expected> <tool> <参数原文>``，如::

        #test allow bash git status
        #test ask bash curl http://x
        #test deny write_file .env

    加载规则时立即对全量规则集断言，失败在启动时报警——规则一多就会互相打架，
    「我写的 git * 意外放行了什么」要在写规则的当下暴露，而不是事后被利用。
    """

    expected: str  # "allow" | "deny" | "ask"
    tool: str
    argument: str  # bash 是命令原文；文件工具是路径
    source: str  # 来源文件，报错用


_TEST_LINE = re.compile(r"^#test\s+(allow|deny|ask)\s+([A-Za-z_][\w-]*)\s+(.+)$")


def _parse_rules_file(path: Path) -> tuple[list[Rule], list[RuleTest]]:
    try:
        #  工作区规则文件来自仓库：指向设备的链接整读读不到头，按读不了处理
        fsguard.require_regular(path)
        #  坏字节不该让整份规则作废：里面的 deny 是用户明令禁止的事，
        #  能认出来的行照常生效
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return [], []
    rules: list[Rule] = []
    tests: list[RuleTest] = []
    for line in raw.splitlines():
        line = line.strip()
        if match := _TEST_LINE.match(line):
            expected, tool, argument = match.groups()
            tests.append(RuleTest(expected, tool, argument.strip(), str(path)))
            continue
        if not line or line.startswith("#"):
            continue
        rule = parse_rule(line)
        if rule is None:
            continue
        if reason := banned_allow_reason(rule):
            #  手改文件绕过 add_persistent 的校验也拦得住：坏规则不生效并当场报警
            print(f"[权限规则被忽略] {path}: {rule} —— {reason}", file=sys.stderr)
            continue
        rules.append(rule)
    return rules, tests


def user_rules_path() -> Path:
    return user_config_dir() / "permissions.txt"


def workspace_rules_path(workspace: Path) -> Path:
    return workspace / ".xiaoyu" / "permissions.txt"


class Permissions:
    """规则 + 会话授权的判定器。判定顺序（编号固定的有序管线）：

    1. deny 规则 → "deny"（调用方必须无视 auto_approve 执行拦截）
    2. 会话授权（用户在确认框答过 a）→ "allow"
    3. allow 规则 → "allow"
    4. 都没命中 → "ask"（交回 requires_approval / auto_approve 的常规流程）
    """

    def __init__(self, workspace: Path, rules: list[Rule] | None = None) -> None:
        self.workspace = workspace
        self.rules: list[Rule] = list(rules or [])
        #  会话内"全部允许"的工具名（非命令类工具答 a），退出即失效
        self.session_allowed: set[str] = set()
        #  会话内已放行的命令键（见 command_keys）：bash 的会话授权按命令头记，
        #  不按工具名——答一次 a 不该把整个 shell 都交出去
        self.session_commands: set[str] = set()

    @classmethod
    def load(cls, workspace: Path, include_workspace: bool = True) -> "Permissions":
        """从用户级 + 工作区级规则文件加载。deny 永远优先，来源不分先后。

        加载完立即跑规则文件里的 #test 自测断言，失败打到 stderr——
        坏规则要在启动时暴露，不能等到被模型撞上。

        include_workspace=False = 工作区未通过 folder trust 门：工作区级规则
        文件整个不读——allow 规则能免确认执行命令，是"clone 即生效"的口子。
        （deny 规则也一并不读：宁可多问，不能让不受信文件参与任何判定。）
        """
        user_rules, user_tests = _parse_rules_file(user_rules_path())
        if include_workspace:
            workspace_rules, workspace_tests = _parse_rules_file(workspace_rules_path(workspace))
        else:
            workspace_rules, workspace_tests = [], []
            if workspace_rules_path(workspace).is_file():
                print(
                    f"[工作区未受信任：{workspace_rules_path(workspace)} 的规则不生效]",
                    file=sys.stderr,
                )
        permissions = cls(workspace, user_rules + workspace_rules)
        for failure in permissions.run_self_tests(user_tests + workspace_tests):
            print(f"[权限规则自测失败] {failure}", file=sys.stderr)
        return permissions

    def run_self_tests(self, tests: list[RuleTest]) -> list[str]:
        """对全量规则集执行 #test 断言，返回失败描述列表。"""
        failures: list[str] = []
        for test in tests:
            args = {"command": test.argument} if test.tool == "bash" else {"path": test.argument}
            actual = self.decide(test.tool, args)
            if actual != test.expected:
                failures.append(
                    f"{test.source}: 期望 {test.expected}，实际 {actual} —— "
                    f"#test {test.expected} {test.tool} {test.argument}"
                )
        return failures

    # ---------- 判定 ----------

    def decide(self, name: str, args: dict) -> str:
        """返回 "deny" / "allow" / "ask"。"""
        decision, _ = self.explain(name, args)
        return decision

    def explain(self, name: str, args: dict) -> tuple[str, Rule | None]:
        """decide 的带出处版本：返回 (判定, 命中的 deny 规则)。

        deny 时携带命中的规则原文——"为什么被拒"要让用户和模型都看得见，否则模型只能瞎猜
        换写法，用户也不知道该去改哪条规则。
        allow / ask 不带规则：allow 可能来自会话授权或多条规则联合判定，
        没有单一出处，硬给一条反而误导。
        """
        #  1. deny：任一规则命中即拦，bypass-immune
        for rule in self.rules:
            if rule.behavior == "deny" and self._deny_matches(rule, name, args):
                return "deny", rule
        #  2. 会话授权：工具名（非命令类）或命令键（bash，每段都要已放行）
        if name in self.session_allowed:
            return "allow", None
        if name == "bash" and self.session_commands:
            keys = command_keys(str(args.get("command", "")))
            if keys and all(key in self.session_commands for key in keys):
                return "allow", None
        #  3. allow 规则（bash 是多条规则联合判定：每一段命中任一条即可）
        if self._allowed(name, args):
            return "allow", None
        #  4. 常规确认流程
        return "ask", None

    def _deny_matches(self, rule: Rule, name: str, args: dict) -> bool:
        if rule.tool != name:
            return False
        if rule.spec is None:
            return True
        if name == "bash":
            #  deny：任一段命中即拦
            command = str(args.get("command", ""))
            spec = rule.spec
            if any(
                fnmatch.fnmatch(part.strip(), spec)
                for part in _SEGMENT_SPLIT.split(command)
                if part.strip()
            ):
                return True
            #  原文对不上再看剥开之后的每一层：`/usr/bin/curl …`、`env curl …`、
            #  `"curl" …`、`FOO=1 curl …`、`bash -c 'curl …'` 跑的都是同一个 curl。
            #  两种写法都配——归一过命令名的，和保留原样 argv[0] 的（规则本身
            #  可能就写了路径）
            return command_check.any_layer(
                command,
                lambda base, argv: fnmatch.fnmatch(" ".join([base, *argv[1:]]), spec)
                or fnmatch.fnmatch(" ".join(argv), spec),
            )
        return self._match_path(rule.spec, args.get("path"))

    def _allowed(self, name: str, args: dict) -> bool:
        rules = [r for r in self.rules if r.behavior == "allow" and r.tool == name]
        if not rules:
            return False
        if any(rule.spec is None for rule in rules):
            return True
        if name != "bash":
            return any(self._match_path(rule.spec, args.get("path")) for rule in rules)

        #  会放行任意代码执行入口的模式（nice * / python * …）不参与放行：文件加载与
        #  add_persistent 已经拦过一道，这里兜住直接构造 Permissions 传进来的规则
        rules = [rule for rule in rules if banned_allow_reason(rule) is None]
        if not rules:
            return False
        command = str(args.get("command", "")).strip()
        if not command:
            return False
        #  破坏性操作（强制 rm 等，含 sudo -u/env -S/bash -c/$(…) 包装）不吃 allow 规则：
        #  用户批准的是"这类命令"，不是"关掉所有确认"。仍可执行，但要问过人。
        if command_check.dangerous_command(command):
            return False
        #  主路径：tree-sitter-bash 白名单解析。
        #  只认"纯字面量简单命令 + && || ; | 连接"这一种形状，重定向/命令替换/
        #  变量展开/子 shell 等任何白名单外节点 → 看不懂 → 不吃 allow。
        #  相比字符串切分：引号里的连接符不会被错切（git commit -m "a && b" 是一段），
        #  且每段有可靠 argv 供注入检查。
        #  Windows 例外：命令由 PowerShell 执行，bash 语法树对它无意义，走回退路径。
        if os.name != "nt" and bash_ast.available():
            argvs = bash_ast.parse_plain_commands(command)
            if argvs is None or not argvs:
                #  看不懂 ≠ 不能跑，只是不免确认
                return False
            for argv in argvs:
                #  参数级逃逸口（git -c core.pager / find -exec / rg --pre …）：
                #  前缀规则看不见参数，命中注入风险的段不吃 allow
                if command_check.injection_risk_argv(argv):
                    return False
                #  复合命令的每一段都要被"某条" allow 规则覆盖（多规则联合：
                #  git add . && ls 需要 git * 和 ls 两条规则一起放行）
                if not any(fnmatch.fnmatch(" ".join(argv), rule.spec) for rule in rules):
                    return False
            return True
        #  回退路径（缺 tree-sitter / Windows）：字符串黑名单 + 正则切段。
        #  含命令替换/重定向的命令不吃前缀规则：
        #  $(…) 能把任意命令藏进允许的前缀里，> 能借允许的命令写任意文件
        if any(marker in command for marker in _ALLOW_UNSAFE_MARKERS):
            return False
        segments = [part.strip() for part in _SEGMENT_SPLIT.split(command) if part.strip()]
        if not segments:
            return False
        for part in segments:
            if command_check.injection_risk(part):
                return False
            if not any(fnmatch.fnmatch(part, rule.spec) for rule in rules):
                return False
        return True

    def _match_path(self, spec: str, path: object) -> bool:
        if not isinstance(path, str) or not path:
            return False
        candidate = Path(path).expanduser()
        if not candidate.is_absolute():
            candidate = self.workspace / candidate
        resolved = candidate.resolve()
        #  工作区内用相对路径匹配（规则写 src/* 就够）；外面的用绝对路径匹配
        if resolved.is_relative_to(self.workspace):
            shown = resolved.relative_to(self.workspace).as_posix()
        else:
            shown = resolved.as_posix()
        return fnmatch.fnmatch(shown, spec)

    # ---------- 变更 ----------

    def grant_session(self, name: str) -> None:
        """按工具名放行整个会话——只给非命令类工具用；bash 走 grant_session_call。"""
        self.session_allowed.add(name)

    def session_grant_label(self, name: str, args: dict) -> str | None:
        """确认框"本会话允许"选项的文案主体；None = 这次调用推不出会话授权范围，
        选项不该出现。一处定义，三个前端（TUI / 明文 / ACP）同用。"""
        if name != "bash":
            return name
        keys = command_keys(str(args.get("command", "")))
        return " 与 ".join(keys) if keys else None

    def grant_session_call(self, name: str, args: dict) -> str | None:
        """落地一次"本会话允许"：bash 记命令键，其它工具记工具名。
        返回放行范围的文案；推不出范围时不记任何东西并返回 None。"""
        label = self.session_grant_label(name, args)
        if label is None:
            return None
        if name == "bash":
            self.session_commands.update(command_keys(str(args.get("command", ""))) or ())
        else:
            self.session_allowed.add(name)
        return label

    def add_persistent(self, rule: Rule) -> Path:
        """把规则追加到用户级规则文件并立即生效。

        持久 allow 不许覆盖任意代码执行入口（ValueError）——这类口子一开就是
        永久的；会话级授权（确认框答 a）不受此限，因为退出即失效。
        """
        if reason := banned_allow_reason(rule):
            raise ValueError(reason)
        path = user_rules_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(f"{rule}\n")
        self.rules.append(rule)
        return path

    def describe(self) -> str:
        """给 /perm 用的可读描述。"""
        lines: list[str] = []
        if self.rules:
            lines.append("持久规则：")
            lines += [f"  {rule}" for rule in self.rules]
        if self.session_allowed:
            lines.append("会话内已全部允许：" + ", ".join(sorted(self.session_allowed)))
        if self.session_commands:
            lines.append("会话内已放行的命令：" + ", ".join(sorted(self.session_commands)))
        if not lines:
            lines.append("没有配置任何权限规则（写文件/执行命令走逐次确认）。")
            lines.append(f"规则文件：{user_rules_path()}")
            lines.append("行格式：allow bash(git *) / deny bash(curl *) / allow write_file")
        return "\n".join(lines)
