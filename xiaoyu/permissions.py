"""权限规则：allow / deny 规则 + 会话内授权（按小羽体量收敛的权限管线）。

三条要点：
1. **deny 规则是 bypass-immune**：用户显式配置的 deny 连 --yolo 都不放行——
   与 tools.py 的硬拦截（hardline）同级，是"绝不"而不是"想不想"。
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

from . import bash_ast, command_check
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
#  它们全是"任意代码执行"的入口（shell、解释器 -c/-e、env/sudo 包装、npm run
#  跑 package.json 里的任意脚本、强制 rm）。判定方式不是黑名单前缀比对，
#  而是拿探针去 fnmatch 试规则本身：`allow bash(python *)` 会命中
#  "python -c …" 探针而被拒，`allow bash(python -m pytest*)` 则不会。
_BANNED_ALLOW_PROBES = (
    "bash -c evil", "bash -lc evil", "sh -c evil", "zsh -c evil", "dash -c evil",
    "ksh -c evil", "fish -c evil", "cmd /c evil", "powershell -Command evil",
    "pwsh -Command evil",
    "python -c evil", "python3 -c evil", "py -c evil", "pypy -c evil",
    "node -e evil", "deno eval evil", "bun -e evil",
    "perl -e evil", "ruby -e evil", "php -r evil", "lua -e evil",
    "julia -e evil", "Rscript -e evil", "osascript -e evil",
    "env evil", "sudo evil", "doas evil", "xargs evil",
    "npm run evil", "pnpm run evil", "yarn run evil", "npx evil", "bunx evil",
    "rm -rf /",
)


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
    return None


#  「总是允许」建议规则里按子命令粒度放行的 CLI：第二个词是子命令，
#  git status* 比 git * 更贴近用户刚刚批准的那件事（don't-ask-again 的
#  前缀推导原则：按你确认的这条命令的"命令类"放行，不扩大）。
_MULTIWORD_PREFIXES = frozenset({
    "git", "npm", "pnpm", "yarn", "cargo", "docker", "kubectl", "pip", "pip3",
    "uv", "go", "poetry", "gh", "brew", "conda", "make",
})


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
    if len(argv) == 1:
        #  裸命令（ls）用精确匹配：ls* 会连 lsof 一起放行，宁窄勿宽
        spec = argv[0]
    elif argv[0] in _MULTIWORD_PREFIXES and not argv[1].startswith("-"):
        spec = f"{argv[0]} {argv[1]}*"
    else:
        spec = f"{argv[0]} *"
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
        raw = path.read_text(encoding="utf-8")
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
        #  会话内"全部允许"的工具名（确认框答 a），退出即失效
        self.session_allowed: set[str] = set()

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
        #  2. 会话授权
        if name in self.session_allowed:
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
            return any(
                fnmatch.fnmatch(part.strip(), rule.spec)
                for part in _SEGMENT_SPLIT.split(command)
                if part.strip()
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

        command = str(args.get("command", "")).strip()
        if not command:
            return False
        #  破坏性操作（强制 rm 等，含 sudo/env/bash -c/trap 包装）不吃 allow 规则：
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
        self.session_allowed.add(name)

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
        if not lines:
            lines.append("没有配置任何权限规则（写文件/执行命令走逐次确认）。")
            lines.append(f"规则文件：{user_rules_path()}")
            lines.append("行格式：allow bash(git *) / deny bash(curl *) / allow write_file")
        return "\n".join(lines)
