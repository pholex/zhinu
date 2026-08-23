"""bash 命令的白名单式语法分析。

用途：权限判定里"这条命令形状是否简单到可以吃 allow 规则"的解析层。

为什么是白名单而不是黑名单：shell 里能藏命令的写法是无限的
（`${x:-$(…)}`、`$'\\x24(…)'`、`<(…)`、heredoc、变量赋值前缀…），
黑名单永远列不全，漏一个就被绕过；白名单只认 20 来种"看得懂"的节点，
遇到任何白名单外的节点整条命令判"不可静态分析"——**漏判的代价从
"被绕过"变成"多弹一次确认框"**，这是安全的失败方向。

约束：
- 只能证明"命令是若干纯字面量的简单命令用 && || ; | 连接"，
  **绝不能反过来用它证明某条复杂命令是安全的**（那是另一套宽松解析的事，
  见 command_check.dangerous_command）。
- tree-sitter 只认 bash 语法；Windows 上命令由 PowerShell 执行，调用方
  （permissions）负责在 Windows 上不走这条路。
- 缺包 / 解析器初始化失败时 available() 返回 False，调用方回退旧的
  字符串黑名单路径——降级仍然是保守方向。
"""

from __future__ import annotations

from functools import lru_cache

#  允许出现的命名节点：顶层容器 + 简单命令 + 纯字面量词。
#  刻意不含：redirect、command_substitution、expansion（$VAR）、subshell、
#  heredoc、variable_assignment、background（&）、comment、escape_sequence——
#  任何一个出现都意味着这条命令有静态看不清的行为。
_ALLOWED_NAMED = frozenset(
    {
        "program",
        "list",
        "pipeline",
        "command",
        "command_name",
        "word",
        "string",
        "string_content",
        "raw_string",
        "number",
        "concatenation",
    }
)

#  允许出现的匿名标点：命令连接符与引号本身
_ALLOWED_PUNCT = frozenset({"&&", "||", ";", "|", '"', "'"})


@lru_cache(maxsize=1)
def _get_parser():
    """惰性初始化解析器；缺包或 API 不兼容返回 None（回退由调用方决定）。"""
    try:
        import tree_sitter_bash
        from tree_sitter import Language, Parser

        return Parser(Language(tree_sitter_bash.language()))
    except Exception:  # noqa: BLE001 - 任何初始化问题都按"不可用"处理
        return None


def available() -> bool:
    return _get_parser() is not None


def parse_plain_commands(script: str) -> list[list[str]] | None:
    """把脚本解析成若干纯字面量命令的 argv 列表。

    返回 None 表示"看不懂"（含语法错误、白名单外节点、解析器不可用）——
    调用方必须往保守方向处理，不能当作空列表。
    整条脚本只有全部由白名单节点构成时才返回结果；引号内的 && ; | 不会被
    错当成连接符（这是字符串切分做不到的）。
    """
    parser = _get_parser()
    if parser is None:
        return None
    try:
        tree = parser.parse(script.encode("utf-8"))
    except Exception:  # noqa: BLE001 - 解析崩溃按"看不懂"处理
        return None
    root = tree.root_node
    if root.has_error:
        return None

    commands: list[list[str]] = []
    #  显式栈做先序遍历（children 逆序入栈保持文档顺序）
    stack = [root]
    while stack:
        node = stack.pop()
        if node.is_named:
            if node.type not in _ALLOWED_NAMED:
                return None
            if node.type == "command":
                argv = _argv_from_command(node)
                if argv is None:
                    return None
                commands.append(argv)
        elif node.type not in _ALLOWED_PUNCT:
            return None
        stack.extend(reversed(node.children))
    return commands


def _argv_from_command(node) -> list[str] | None:
    """把 command 节点还原成 argv。任何组成部分还原不了就整条放弃。"""
    argv: list[str] = []
    for child in node.children:
        text = _word_text(child)
        if text is None:
            return None
        argv.append(text)
    return argv or None


#  裸词里这些字符意味着 shell 会在执行前改写它：brace 展开 `{a,b}`、glob
#  `* ? [ ]`、转义 `\\`、tilde `~`、zsh 扩展 glob `^ #`、展开 `$` 与反引号。
#  tree-sitter 把 `-{delete,print}`、`-del*`、`-de\\lete`、`~` 都标成普通
#  word——源文本的拼写不是运行期 argv 的证明。
_DYNAMIC_WORD_CHARS = frozenset("{}*?[]\\~^#$`")


def _word_text(node) -> str | None:
    """把一个"词"节点还原成它的字面量值（去引号、拼接 concatenation）。

    裸 word / number 额外要求**源文本本身是字面量**：含 `_DYNAMIC_WORD_CHARS`
    或以 `=`（zsh 的 `=cmd` 展开）开头即放弃。否则 `allow bash(rg *)` 会被
    `rg --pre{=,=sh} …` 穿透——规则匹配的是改写前的拼写，执行的是改写后的
    `--pre=sh`。
    """
    kind = node.type
    if kind in ("word", "number"):
        text = node.text.decode("utf-8", errors="replace")
        if text.startswith("=") or _DYNAMIC_WORD_CHARS.intersection(text):
            return None
        return text
    if kind == "raw_string":
        #  'literal'：整个 token 含引号，剥掉首尾
        return node.text.decode("utf-8", errors="replace")[1:-1]
    if kind == "string":
        #  "…"：children 是引号标点 + string_content；出现 expansion 等其它
        #  子节点说明内容不是纯字面量，放弃。
        #  string_content 里出现反斜杠也放弃：本版 tree-sitter-bash 把 \" 之类
        #  转义原样留在 string_content 里，还原值会失真——拿失真的 argv 去匹配
        #  allow 规则不可靠，宁可退回人工确认。
        parts: list[str] = []
        for child in node.children:
            if child.type == '"':
                continue
            if child.type != "string_content":
                return None
            text = child.text.decode("utf-8", errors="replace")
            if "\\" in text:
                return None
            parts.append(text)
        return "".join(parts)
    if kind == "concatenation":
        #  -g"*.py" 这类粘连：逐段还原再拼接
        parts = []
        for child in node.children:
            text = _word_text(child)
            if text is None:
                return None
            parts.append(text)
        return "".join(parts)
    if kind == "command_name":
        return _word_text(node.children[0]) if node.children else None
    return None
