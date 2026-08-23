"""小羽的命令行入口。"""

from __future__ import annotations

import argparse
import atexit
import importlib.util
import json
import locale
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from . import __version__, command_check, keys, media, modes, peers, providers, skills, terminal, ui
from .agent import Agent
from .banner import build_banner
from .session_log import (
    SessionInfo,
    SessionLog,
    check_session_id,
    install_exit_logging,
    list_sessions,
    load_messages,
    open_named,
    turn_starts,
)
from .config import (
    DEFAULT_MODEL,
    DEFAULT_SUMMARY_MODEL,
    Config,
    MissingConfig,
    _parse_dotenv,
    GATEWAY_KEY_ENVS,
    find_api_key,
    key_fallback_sources,
    load_api_key,
    load_dotenv,
    save_user_env,
    user_env_path,
)
from .permissions import Permissions, parse_rule
from .render import REPLAY_TURNS, JsonlSink, NullSink, replay_transcript
from .tools import Toolbox

#  斜杠命令 → 一行描述。单一事实源：/help 文案与 TUI 补全菜单的 meta 都从这里生成
SLASH_COMMANDS: dict[str, str] = {
    "/help": "显示这个帮助",
    "/keys": "按键与输入前缀速查",
    "/tools": "列出已注册的工具",
    "/tasks": "后台任务列表（run_in_background 的命令 / monitor）",
    "/mcp": "MCP server 状态；/mcp approve <名> 批准变更工具",
    "/skills": "列出可用技能；/skills reload 重扫磁盘并刷新索引",
    "/model": "查看或切换模型（/model 名字）",
    "/usage": "本次会话的 token 统计",
    "/context": "当前上下文占用与压缩状态",
    "/compact": "立刻压缩历史（不等阈值）",
    "/mode": "切换模式（/mode default|auto|plan），TUI 里 Shift-Tab 同效",
    "/plan": "只读规划态开关（/plan on|off）：/mode plan 的别名",
    "/perm": "查看权限规则与会话授权",
    "/allow": "持久允许，如 /allow bash(git *)、/allow write_file",
    "/deny": "持久拒绝（任何模式下都拦，包括 --yolo）",
    "/resume": "切到本工作区的历史会话（当前对话被清空；/resume <序号> 直接选）",
    "/rewind": "回滚到某轮开始前（对话和/或文件；/undo 同义）",
    "/clear": "清空对话历史（保留 system prompt）",
    "/exit": "退出",
    "/quit": "退出",
}

SLASH_HELP = "可用命令：\n" + "\n".join(
    f"  {command:<10} {description}" for command, description in SLASH_COMMANDS.items()
) + "\n"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="xiaoyu",
        description="小羽 — 一个 harness coding agent",
        epilog=(
            "子命令：xiaoyu config  初始化/查看配置（详见 xiaoyu config --help）；"
            "xiaoyu resume  恢复历史会话（详见 xiaoyu resume --help）；"
            "xiaoyu sessions  列出本机在跑的会话；"
            "xiaoyu send <会话> <消息>  给另一个会话发一条消息；"
            "xiaoyu mcp add|list|remove  管理 MCP server 声明（详见 xiaoyu mcp --help）；"
            "xiaoyu plugin add|list|update|remove  装卸插件包（skills + MCP，"
            "详见 xiaoyu plugin --help）；"
            "xiaoyu acp  以 ACP 协议 server 启动，供编辑器客户端驱动（等价 --acp）；"
            "xiaoyu doctor  体检环境（凭据有无 / 沙箱 / 磁盘 / MCP 配置）；"
            "xiaoyu terminal-setup  给 VS Code 系编辑器配 Shift+Enter 换行；"
            "xiaoyu update  升级到最新版（未装 TUI 时自动补上；已装 serve 时一并升级）；"
            "xiaoyu uninstall  卸载（--purge 连配置目录一起删）"
        ),
    )
    parser.add_argument("prompt", nargs="*", help="直接执行一条指令后退出（不进交互模式）")
    add_prompt_flag(parser)
    parser.add_argument("--version", action="version", version=f"xiaoyu {__version__}")
    parser.add_argument("--model", help="模型名，默认 deepseek-v4-pro")
    parser.add_argument("--base-url", dest="base_url", help="OpenAI 兼容端点")
    parser.add_argument("--workspace", help="工作区根目录，默认当前目录")
    parser.add_argument(
        "-s",
        "--session-id",
        dest="session_id",
        metavar="ID",
        help="给会话起个固定名字：同名会话已存在就接着聊，不存在就新建"
        "（脚本/CI 里反复调同一个会话用；交互着聊用 xiaoyu resume 更顺手）",
    )
    parser.add_argument(
        "--append-system-prompt",
        dest="append_system_prompt",
        help="追加到内置 system prompt 末尾，用于宿主进程嵌入 xiaoyu 时注入身份/人格",
    )
    parser.add_argument(
        "--env-file",
        dest="env_file",
        help="指定 .env 路径，默认依次读当前目录、项目根、用户配置目录",
    )
    parser.add_argument(
        "--mode",
        choices=list(modes.CYCLE),
        default=None,
        help="起始模式：default=逐条确认；auto=工作区内改文件与沙箱内命令免确认；plan=只读规划态",
    )
    parser.add_argument(
        "--yolo",
        action="store_true",
        help="不再逐个确认写文件和执行命令（危险：等于让模型直接在你机器上跑任意命令）",
    )
    parser.add_argument(
        "--no-sandbox",
        dest="sandbox",
        action="store_false",
        default=None,
        help="关掉 macOS 沙箱（默认开启：bash 命令只能写工作区/临时目录/构建缓存）",
    )
    parser.add_argument(
        "--no-network",
        dest="sandbox_network",
        action="store_false",
        default=None,
        help="沙箱内禁用网络（默认允许，否则 pip/npm/git push 都会失败）",
    )
    parser.add_argument(
        "--no-tui",
        dest="no_tui",
        action="store_true",
        help="禁用增强交互界面（补全/历史/粘贴折叠），用明文 REPL",
    )
    parser.add_argument(
        "--trust",
        action="store_true",
        help="信任本工作区（记入信任表）：启用仓库级 .mcp.json / permissions.txt / .env；"
        "headless（-p / --wire）下这些配置默认不生效，此旗标一次性解除",
    )
    parser.add_argument(
        "--wire",
        action="store_true",
        help="wire 模式：stdin/stdout 走 JSON-RPC 协议（headless，给外部 UI/编排器驱动）",
    )
    parser.add_argument(
        "--acp",
        action="store_true",
        help="ACP 模式：stdin/stdout 走 Agent Client Protocol"
        "（agentclientprotocol.com，给 Zed/Neovim 等编辑器客户端驱动）",
    )
    add_output_format(parser)
    return parser


def add_prompt_flag(parser: argparse.ArgumentParser) -> None:
    """`-p`：一次性执行的行业惯例拼写，主命令与 resume 共用。

    小羽的指令本来就是位置参数（`xiaoyu "干活"`），加 `-p` 纯为肌肉记忆与
    抄来的脚本——但**业界的 `-p` 语义并不统一**，得同时接住两种形态：
    - 有的 CLI 把 `-p` 当布尔开关，指令走位置参数或管道
      （`tool -p "x"`、`cat f | tool -p`）；
    - 有的 CLI 用 `-p` 吃一个值当指令。
    所以用 `nargs="?"`：带值就是指令，不带值就只表态"这是一次性模式"。
    两种写法都不用改，行为也都对。

    长名取 `--prompt` 而不是 `--print`，因为**命名要跟着 arity 走**：
    `--print` 命名的是输出行为，适合布尔开关；我们的 `-p` 吃值，
    `--print PROMPT` 读起来就成了"打印这条提示词"。
    """
    parser.add_argument(
        "-p",
        "--prompt",
        #  dest 不能叫 prompt——那是位置参数的名字。这里存的是 `-p` 的取值：
        #  None=没写、''=写了但没给值（两者必须分得开，见调用处）
        dest="prompt_opt",
        nargs="?",
        const="",
        default=None,
        metavar="PROMPT",
        help="一次性执行（行业惯例拼写，等价于把指令写成位置参数）："
        "`-p '干活'` 或 `cat 材料 | xy -p`",
    )


def prompt_words(args: argparse.Namespace) -> list[str]:
    """把 `-p` 的值与位置参数拼成一串词。

    两边都有内容时（`xy -p '总结' 这个仓库`）**`-p` 的值恒排在最前，与它写在
    命令行哪个位置无关**——`xy 这个仓库 -p 总结` 同样拼成"总结 这个仓库"。
    argparse 不保留跨 action 的书写次序，要保序就得自己扫 argv，不值；
    而"两边各写一半"本身就是罕见写法，正常写法只用其中一种。
    """
    value = getattr(args, "prompt_opt", None)
    return [value, *args.prompt] if value else list(args.prompt)


def add_output_format(parser: argparse.ArgumentParser) -> None:
    """--output-format：主命令与 resume 共用（都只在一次性模式下生效）。"""
    parser.add_argument(
        "--output-format",
        dest="output_format",
        choices=("text", "json", "stream-json"),
        default="text",
        help="一次性模式的输出：text=明文；json=末尾一个 JSON 对象（result/usage）；"
        "stream-json=每个事件一行 JSON（NDJSON），末行 kind=result",
    )


_CONFIG_VARS = (
    "XIAOYU_BASE_URL",
    "XIAOYU_MODEL",
    "XIAOYU_FALLBACK_MODELS",
    "XIAOYU_SUMMARY_MODEL",
    "XIAOYU_EXPLORE_MODEL",
)


def config_command(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="xiaoyu config",
        description=f"配置小羽：写入用户级 .env（{user_env_path()}），"
        "免去 pip/pipx 安装后到处找 .env。不带参数进交互向导。",
    )
    parser.add_argument("--show", action="store_true", help="显示当前生效配置与来源（key 永不回显）")
    parser.add_argument("--path", action="store_true", help="打印用户级配置文件路径")
    parser.add_argument(
        "--set",
        dest="pairs",
        action="append",
        metavar="KEY=VALUE",
        help="非交互写入一项配置，可重复（如 --set XIAOYU_MODEL=deepseek-v4-pro）",
    )
    args = parser.parse_args(argv)

    if args.path:
        print(user_env_path())
        return 0
    if args.pairs:
        values: dict[str, str] = {}
        for pair in args.pairs:
            key, sep, value = pair.partition("=")
            if not sep or not key.strip():
                print(ui.error(f"格式应为 KEY=VALUE：{pair}"), file=sys.stderr)
                return 2
            values[key.strip()] = value.strip()
        print(ui.success(f"已写入 {save_user_env(values)}"))
        return 0
    if args.show:
        return show_config()
    return config_wizard()


def show_config() -> int:
    #  load_dotenv 之前先记下哪些来自真实环境变量，之后就分不清了
    from_env = {name for name in (*_CONFIG_VARS, "XIAOYU_API_KEY") if name in os.environ}
    loaded = load_dotenv()
    print(ui.heading("生效配置") + ui.secondary("（优先级：环境变量 > 当前目录 .env > 项目根 .env > 用户级 .env）"))
    for name in _CONFIG_VARS:
        value = os.environ.get(name) or ui.secondary("（未设置，用内置默认）")
        source = "环境变量" if name in from_env else _dotenv_source(name, loaded)
        print(f"  {name} = {value}" + (ui.secondary(f"  · 来自 {source}") if source else ""))
    try:
        load_api_key()
        key_state = "已设置"
    except MissingConfig:
        key_state = "未设置"
    print(f"  XIAOYU_API_KEY：{key_state}" + ui.secondary("（永不回显）"))
    #  key 是靠环境变量（厂商原生名，如 DEEPSEEK_API_KEY）静默启用直连的，
    #  --show 不把生效的 provider 列出来，用户没法判断请求实际走哪条路。
    try:
        registry = providers.build(Config.from_env())
    except MissingConfig as exc:
        print(ui.warning("  当前没有可用端点：\n  " + str(exc).replace("\n", "\n  ")))
    else:
        print(ui.heading("生效的 provider") + ui.secondary("（按优先级，同名模型先出现者赢）"))
        for index, provider in enumerate(registry.providers, start=1):
            scope = "、".join(provider.models) if provider.models else "任意模型名（转发）"
            print(f"  {index}. {provider.display}  {ui.secondary(scope)}")
    print(ui.secondary(f"用户级配置文件：{user_env_path()}"))
    return 0


def model_label(agent: Agent) -> str:
    """横幅上的模型名 + 来源。只有一家 provider 时不加后缀，界面保持原样。

    多 provider 时必须标出来：直连是靠环境变量（厂商原生名，如 DEEPSEEK_API_KEY）
    静默启用的，不显示的话用户不知道请求已经改道了。
    """
    if len(agent.registry.providers) < 2:
        return agent.config.model
    route = agent.registry.resolve(agent.config.model)
    provider = agent.registry.get(route.provider)
    return f"{route.model}（{provider.display if provider else route.provider}）"


def _dotenv_source(name: str, loaded: list[Path]) -> str | None:
    #  load_dotenv 用 setdefault 合并，所以列表里第一个含该键的文件就是生效来源
    for path in loaded:
        if name in _parse_dotenv(path):
            return str(path)
    return None


def config_wizard() -> int:
    if not sys.stdin.isatty():
        print(
            ui.error("交互向导需要终端；非交互环境请用 xiaoyu config --set KEY=VALUE"),
            file=sys.stderr,
        )
        return 2
    loaded = load_dotenv()
    print(ui.heading("小羽配置向导") + ui.secondary(f"  → {user_env_path()}"))
    if loaded:
        print(ui.secondary("已读到 " + ", ".join(str(p) for p in loaded) + "，直接回车即保留现值"))

    def ask(name: str, tip: str, fallback: str = "") -> str:
        current = os.environ.get(name, "") or fallback
        suffix = ui.secondary(f"（回车保留：{current}）") if current else ""
        try:
            answer = input(f"{tip}{suffix}: ").strip()
        except EOFError:
            answer = ""
        return answer or current

    #  明文输入 key：这是用户自己的终端，看得见才知道粘贴对了没有。
    #  不回显只针对**已存储**的 key（config --show）。
    def ask_key(tip: str) -> str:
        try:
            return input(f"{tip}: ").strip()
        except EOFError:
            return ""

    #  两条路径各自都能单独跑通，所以都不强制填；但一个都不填就没有端点可用。
    print(ui.secondary("直连与网关可以同时配：直连优先，网关自动作为同名模型的兜底。"))
    values: dict[str, str] = {}
    #  直连 key 可能早已配好——三处同名（.env / 环境变量 / Keychain），
    #  先按运行时同一套逻辑探测，已有的不必重输；值永不回显，只报来源。
    #  厂商清单直接遍历 PRESETS：补一家 preset，向导自动多问一家，两处不会脱节。
    has_direct = False
    for preset in providers.PRESETS.values():
        existing = bool(find_api_key(preset.key_envs))
        if existing:
            source = (
                "环境变量或 .env"
                if any(os.environ.get(name, "").strip() for name in preset.key_envs)
                else "Keychain"
            )
            print(ui.secondary(f"已检测到 {preset.name} 直连 key（来自{source}，永不回显）。"))
            prompt = f"{preset.name} 直连 key（回车 = 沿用现有，输入新值则覆盖）"
        else:
            prompt = f"{preset.name} 直连 key（模型 {'、'.join(preset.models)}；留空 = 跳过）"
        if key := ask_key(prompt):
            #  写到厂商主键名（key_envs[0]）；别名（如 DASHSCOPE_API_KEY）只用于探测
            values[preset.key_envs[0]] = key
            existing = True
        has_direct = has_direct or existing

    base_url = ask("XIAOYU_BASE_URL", "OpenAI 兼容网关端点（留空 = 不用网关）")
    while not base_url and not has_direct:
        print(ui.warning("直连 key 与网关端点至少要有一个。"))
        base_url = ask("XIAOYU_BASE_URL", "OpenAI 兼容网关端点（留空 = 不用网关）")
    if base_url:
        values["XIAOYU_BASE_URL"] = base_url

    values["XIAOYU_MODEL"] = ask("XIAOYU_MODEL", "主模型", DEFAULT_MODEL)
    values["XIAOYU_SUMMARY_MODEL"] = ask(
        "XIAOYU_SUMMARY_MODEL", "摘要/检索用便宜模型", DEFAULT_SUMMARY_MODEL
    )
    if fallback := ask("XIAOYU_FALLBACK_MODELS", "备用模型降级链（逗号分隔，留空=不降级）"):
        values["XIAOYU_FALLBACK_MODELS"] = fallback
    if base_url:
        #  网关 key 同样先按运行时逻辑探测（XIAOYU_API_KEY / LITELLM_API_KEY），
        #  已有的不必重输
        if find_api_key(GATEWAY_KEY_ENVS):
            print(ui.secondary("已检测到网关 API key（XIAOYU_API_KEY 或 LITELLM_API_KEY，永不回显）。"))
            prompt = "网关 API key（回车 = 沿用现有，输入新值则覆盖）"
        else:
            sources = " 或 ".join(short for short, _ in key_fallback_sources())
            prompt = f"网关 API key（留空 = 不改，之后也可用{sources} 提供）"
        if key := ask_key(prompt):
            values["XIAOYU_API_KEY"] = key
    path = save_user_env(values)
    print(ui.success(f"已写入 {path}"))
    print(ui.secondary("注意：环境变量和当前目录 .env 里的同名项会优先于这份配置。"))
    return 0


def vanished_tools(messages: list[dict[str, Any]], names: list[str]) -> list[str]:
    """历史里调用过、现已不在注册表里的工具名（resume 预警用）。

    OpenAI 兼容端点不校验历史里的工具名，这种历史发得出去，不必造哨兵工具
    （那是服务端强校验才会逼出来的形态）；真正的风险是模型
    照着历史再调一次——Toolbox.run 对未知工具会报错并列出可用工具，
    resume 时再用这份清单提前给用户一句显式预警。
    """
    used = {
        call.get("function", {}).get("name", "")
        for message in messages
        for call in message.get("tool_calls") or []
    }
    return sorted(used - set(names) - {""})


def split_resume_positionals(first: str | None, rest: list[str]) -> tuple[int | None, list[str]]:
    """`resume` 位置参数消歧：首个是纯数字就是会话序号，否则是指令的第一个词。"""
    if first is None:
        return None, list(rest)
    if first.isdigit():
        return int(first), list(rest)
    return None, [first, *rest]


def _session_label(info: SessionInfo) -> str:
    """会话在行内菜单里的一行标签（截到终端宽度，长了菜单高度就不准了）。"""
    place = Path(info.workspace).name or info.workspace
    return ui.fit(f"{info.started_at}  {info.model}  {place}  {_named(info)}{info.preview}", 12)


def _named(info: SessionInfo) -> str:
    """命名会话在列表里的前缀标记：让人看得出这个会话还会被脚本续写。"""
    return f"[{info.session_id}] " if info.session_id else ""


def choose_session(
    sessions: list[SessionInfo],
    select: Any = None,
    title: str = "恢复哪个会话？",
) -> SessionInfo | None:
    """从列表选一个会话：有 select（行内菜单）用菜单，否则编号输入。

    select 签名同 tui.inline_select（options 每项 (值, 标签, 快捷键)）；
    菜单起不来（异常）退回编号输入，用户取消（返回 None / 非法序号）返回 None。
    """
    if select is not None:
        try:
            value = select(title, [(i, _session_label(info), "") for i, info in enumerate(sessions)])
        except Exception:  # noqa: BLE001 - 非常规终端起不了菜单：退回编号输入，选择永远可用
            pass
        else:
            if isinstance(value, tuple):  # 通用件的 Tab 形态兜底：当普通确认
                value = value[1]
            return sessions[value] if isinstance(value, int) else None
    for number, info in enumerate(sessions, start=1):
        place = Path(info.workspace).name or info.workspace
        print(
            f"  {number:>2}. {info.started_at}  {ui.secondary(info.model)}  "
            f"{ui.secondary(place)}  {_named(info)}{info.preview}"
        )
    try:
        answer = input(ui.prompt("恢复哪个？（序号，回车取消）: ")).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return None
    if not answer or not answer.isdigit() or not 1 <= int(answer) <= len(sessions):
        return None
    return sessions[int(answer) - 1]


def _tui_select() -> Any:
    """子命令场景的行内菜单：TUI 依赖可用且在真终端上才给，否则 None（编号输入）。"""
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return None
    try:
        from . import terminal
        from .tui import inline_select
    except ImportError:
        return None
    #  菜单配色跟终端深浅走；make_frontend 稍后会再探一次，代价是有界的（150ms 超时）
    terminal.autodetect()
    return lambda title, options: inline_select(title, options, amend=False)


def replay_recent(agent: Agent, loaded: list[dict[str, Any]]) -> None:
    """恢复后把最近几轮补进 scrollback；没有可回放内容退回一行「上次说到」。"""
    from .agent import SYNTHETIC_USER_TEXTS

    starts = turn_starts(loaded, SYNTHETIC_USER_TEXTS)
    replayed = 0
    if starts:
        count = min(len(starts), REPLAY_TURNS)
        replayed = replay_transcript(
            loaded[starts[-count] :],
            agent.sink,
            SYNTHETIC_USER_TEXTS,
            header=f"── 回放最近 {count} 轮 ──",
        )
    if not replayed and (tail := agent.last_assistant_text()):
        print(ui.secondary(f"上次说到：{ui.fit(tail, 6)}"))


#  /resume 行内菜单最多列几个：数字直选与行内高度都到 9 为止，更早的用子命令
_SLASH_RESUME_LIMIT = 9


def slash_resume(agent: Agent, rest: list[str], select: Any = None) -> None:
    """`/resume`：REPL 内切到历史会话，不必退出重进。

    切换 = 清空当前对话再接回所选会话（reset 记 clear 事件、restore 逐条复制，
    当前会话文件仍自包含、可再次 resume）。只列当前工作区的会话——REPL 里
    跨工作区接上下文十有八九是接错；要跨就退出用 `xiaoyu resume --all`。
    """
    current = agent.session_log.path if agent.session_log else None
    sessions = [
        info
        for info in list_sessions(workspace=str(agent.config.workspace))
        if info.path != current
    ][:_SLASH_RESUME_LIMIT]
    if not sessions:
        print(ui.secondary("  当前工作区没有其它会话；跨工作区请退出后用 xiaoyu resume --all"))
        return
    if rest and rest[0].isdigit():
        index = int(rest[0])
        if not 1 <= index <= len(sessions):
            print(ui.warning(f"  序号超出范围（1-{len(sessions)}）"))
            return
        chosen: SessionInfo | None = sessions[index - 1]
    else:
        chosen = choose_session(sessions, select, title="切到哪个会话？（当前对话将被清空）")
    if chosen is None:
        return
    try:
        loaded = load_messages(chosen.path)
    except (OSError, ValueError) as exc:
        print(ui.error(f"无法恢复：{exc}"))
        return
    if not loaded:
        print(ui.warning("  该会话没有可恢复的消息。"))
        return
    agent.reset()
    agent.restore(loaded, source=str(chosen.path))
    if vanished := vanished_tools(loaded, agent.toolbox.names()):
        print(
            ui.warning(
                f"[注意] 历史会话用过的工具现已不可用：{', '.join(vanished)}"
                "（插件/技能配置可能变了）。模型若再调用会收到明确报错并自行改道。"
            )
        )
    print(ui.secondary(f"已切到 {chosen.path.name}（{len(loaded)} 条消息，原对话已清空）"))
    replay_recent(agent, loaded)


def register_peer(config: Config, interactive: bool) -> "peers.Registration | None":
    """把本会话登记进本机会话表（见 peers.py）。

    只登记交互式会话：一次性执行活不过几秒，登记进去只是噪音。登记失败返回
    None，会话照常跑——同 session_log 的纪律，辅助设施不能拖垮主体。
    """
    if not interactive or not config.enable_peers:
        return None
    reg = peers.Registration.create(str(config.workspace), config.model)
    if reg is not None:
        #  正常退出走 run_repl 的 finally；atexit 兜住 SIGTERM 这类绕过它的路径。
        #  close() 幂等，两条路都走到也无妨
        atexit.register(reg.close)
    return reg


def build_toolbox(config: Config, peer: "peers.Registration | None") -> Toolbox:
    """工具箱 + （登记了才有的）跨会话两件套。

    挂载放在这里而不是 `Agent.__init__`：`Agent` 认的是 `PeerLink` 协议，
    宿主注入自己的消息总线时，不该凭空多出两个指向本机 peers 目录的工具。
    谁登记谁挂载。
    """
    toolbox = Toolbox(config)
    if peer is not None:
        for tool in peer.tools():
            toolbox.register(tool)
    return toolbox


def run_repl(repl_fn: Any, agent: Agent) -> int:
    """跑 REPL，退出时抹掉会话登记（抹不掉也无妨：心跳一停别人自会清理）。"""
    try:
        return repl_fn(agent)
    finally:
        if agent.peer is not None:
            agent.peer.close()


def sessions_command(argv: list[str]) -> int:
    """`xiaoyu sessions`：列出本机在跑的小羽会话。

    一行一个、`·` 分隔（不排表格：inline TUI 的
    调性是紧凑）。列的是**可寻址性**——名字就是地址，`[ref]` 只在重名时才用得上。
    """
    parser = argparse.ArgumentParser(
        prog="xiaoyu sessions",
        description="列出本机在跑的小羽会话（可作为 `xiaoyu send` 的收件人）。",
    )
    parser.parse_args(argv)
    live = peers.list_peers()
    if not live:
        print(ui.secondary("没有在跑的小羽会话。"))
        return 0
    self_ref = os.environ.get(peers.REF_ENV, "")
    print(ui.heading(f"可用会话（{len(live)} 个）："))
    for peer in live:
        fields = [
            peers.KIND_LABELS.get(peer.kind, peer.kind),
            peers.STATE_LABELS.get(peer.state, peer.state),
            peers.ago(peer.started),
            shorten_home(peer.workspace),
        ]
        if peer.ref == self_ref:
            fields.append("本会话")
        head = f"  {ui.accent(peer.name)} {ui.secondary('[' + peer.ref + ']')}"
        print(head + ui.secondary("  ·  " + "  ·  ".join(fields)))
    return 0


def shorten_home(path: str) -> str:
    """`/Users/me/x` → `~/x`。列表里工作区是关键信息，但不值得占满一行。"""
    home = str(Path.home())
    return f"~{path[len(home):]}" if home != "/" and path.startswith(home) else path


def send_command(argv: list[str]) -> int:
    """`xiaoyu send <会话> <消息>`：给本机另一个会话投一条消息。

    投递即算送达——对方在下一个 step 边界收进上下文（空闲时躺在信箱里等他
    下次开口，不抢他的终端）。在会话内经 `!` 执行时会自动自报家门，对方可回信。
    """
    parser = argparse.ArgumentParser(
        prog="xiaoyu send",
        description="给本机另一个小羽会话发一条消息（收件人见 xiaoyu sessions）。",
    )
    parser.add_argument("target", help="会话名；重名时写成 `名字 [ref]`")
    parser.add_argument("message", nargs="*", help="消息内容；省略则从管道读")
    args = parser.parse_args(argv)
    load_dotenv()
    text = compose_prompt(args.message, read_piped_stdin())
    if not text:
        print(ui.error("消息是空的（给一段文字，或从管道输入）"), file=sys.stderr)
        return 2
    try:
        peer = peers.deliver(args.target, text, peers.self_name() or "命令行")
    except peers.PeerError as exc:
        print(ui.error(str(exc)), file=sys.stderr)
        return 2
    print(ui.success(f"已投给 {peer.address}"))
    print(ui.secondary("对方会在下一个步骤边界收到；它空闲时，等下一次开口才进上下文。"))
    return 0


def resume_command(argv: list[str]) -> int:
    """`xiaoyu resume`：从会话日志恢复历史对话继续聊。

    模型/端点等配置照常从环境读取（会话里记录的模型只作展示）；
    恢复的消息会重新写入新的会话文件，让每个文件都自包含、可再次 resume。
    """
    parser = argparse.ArgumentParser(
        prog="xiaoyu resume",
        description="恢复历史会话。默认列出当前工作区的最近会话供选择。",
    )
    parser.add_argument(
        "index", nargs="?", help="列表里的序号；给的不是数字就当指令（此时默认恢复最近会话）"
    )
    parser.add_argument(
        "prompt",
        nargs="*",
        help="恢复后直接执行一条指令并退出（等价 claude -p --continue），不进交互模式",
    )
    parser.add_argument("--last", action="store_true", help="直接恢复最近一个，不出列表")
    parser.add_argument("--all", action="store_true", help="不按当前工作区过滤")
    parser.add_argument(
        "--turns", action="store_true", help="列出该会话的轮次（fork 截断点预览），不恢复"
    )
    parser.add_argument(
        "--fork",
        type=int,
        metavar="K",
        help="分叉：只保留前 K 轮接进新会话（原会话文件不动；配合 --turns 先看轮次）",
    )
    parser.add_argument(
        "--mode",
        choices=list(modes.CYCLE),
        default=None,
        help="起始模式：default=逐条确认；auto=沙箱兜得住的免确认；plan=只读规划态",
    )
    parser.add_argument("--yolo", action="store_true", help="不再逐个确认写文件和执行命令")
    parser.add_argument("--no-tui", dest="no_tui", action="store_true", help="用明文 REPL")
    add_prompt_flag(parser)
    add_output_format(parser)
    args = parser.parse_args(argv)
    #  folder trust 门（与 main 同一道，先于 load_dotenv；resume 的工作区就是 cwd）
    trust = resolve_folder_trust(
        Path.cwd(),
        grant=getattr(args, "trust", False),
        interactive=sys.stdin.isatty() and sys.stderr.isatty(),
    )
    load_dotenv(untrusted_dir=None if trust.trusted else Path.cwd())

    #  位置参数消歧：`resume 3 "继续"` 里 3 是序号，`resume "继续跑测试"` 里
    #  首个词是指令的开头——argparse 分不出来，按"纯数字=序号"判
    index, words = split_resume_positionals(args.index, args.prompt)
    #  `-p` 的值不参与序号消歧：写在 -p 后面的一定是指令，哪怕它是纯数字
    if args.prompt_opt:
        words = [args.prompt_opt, *words]
    prompt = compose_prompt(words, read_piped_stdin())
    if args.prompt_opt is not None and not prompt:
        print(
            ui.error("-p 是一次性执行：指令写在 -p 后面，或从管道给"),
            file=sys.stderr,
        )
        return 2
    if args.output_format != "text" and not prompt:
        print(
            ui.error("--output-format json/stream-json 只用于一次性执行（跟一条指令或从管道输入）"),
            file=sys.stderr,
        )
        return 2

    workspace = Path.cwd().resolve()
    sessions = list_sessions(workspace=None if args.all else str(workspace))
    if not sessions and not args.all:
        #  当前工作区没有就退回全量列表，别让用户空手而归
        sessions = list_sessions()
    if not sessions:
        print(ui.warning("没有可恢复的会话。"))
        return 1

    if args.last:
        chosen = sessions[0]
    elif index is not None:
        if not 1 <= index <= len(sessions):
            print(ui.error(f"序号超出范围（1-{len(sessions)}）"), file=sys.stderr)
            return 2
        chosen = sessions[index - 1]
    elif prompt:
        #  一次性执行不进交互列表：默认最近一个（"继续上一场"的惯例语义）
        chosen = sessions[0]
    else:
        #  行内菜单选择（复用 inline_select，不进 alt-screen 全屏 App）；
        #  起不来自动退回编号输入
        picked = choose_session(sessions, select=None if args.no_tui else _tui_select())
        if picked is None:
            return 0
        chosen = picked

    try:
        loaded = load_messages(chosen.path)
    except (OSError, ValueError) as exc:
        print(ui.error(f"无法恢复：{exc}"), file=sys.stderr)
        return 2
    if not loaded:
        print(ui.warning("该会话没有可恢复的消息。"))
        return 1

    #  session fork（按轮枚举分叉）：restore 本就复制进新文件，
    #  fork 只是"复制前先按轮截断"，原会话文件永远不动
    from .agent import SYNTHETIC_USER_TEXTS

    starts = turn_starts(loaded, SYNTHETIC_USER_TEXTS)
    if args.turns:
        if not starts:
            print(ui.warning("该会话没有可识别的轮次。"))
            return 1
        for number, at in enumerate(starts, start=1):
            preview = " ".join(media.text_of(loaded[at].get("content")).split())[:60]
            print(f"  {number:>2}. {preview}")
        print(ui.secondary(f"  用 xiaoyu resume … --fork <K> 保留前 K 轮分叉继续"))
        return 0
    if args.fork is not None:
        if not starts or not 1 <= args.fork <= len(starts):
            print(
                ui.error(f"--fork 超出范围（1-{len(starts)}；--turns 可先看轮次）"),
                file=sys.stderr,
            )
            return 2
        if args.fork < len(starts):
            loaded = loaded[: starts[args.fork]]

    resume_workspace = Path(chosen.workspace) if Path(chosen.workspace).is_dir() else workspace
    #  会话记录的工作区可能不是 cwd：换了目录就按新目录重新过一遍门
    if resume_workspace.resolve() != Path.cwd().resolve():
        trust = resolve_folder_trust(
            resume_workspace,
            grant=False,
            interactive=sys.stdin.isatty() and sys.stderr.isatty(),
        )
    try:
        config = Config.from_env(
            workspace=resume_workspace,
            auto_approve=args.yolo or None,
            mode=args.mode,
            workspace_trusted=trust.trusted,
        )
        permissions = Permissions.load(config.workspace, include_workspace=trust.trusted)
        if prompt:
            approver, sink = oneshot_frontend(permissions, args.output_format)
            repl_fn, note, asker = repl, None, None
        else:
            approver, sink, repl_fn, note, asker = make_frontend(permissions, args.no_tui)
        peer = register_peer(config, interactive=not prompt)
        agent = Agent(
            config,
            build_toolbox(config, peer),
            approver=approver,
            session_log=SessionLog.create(config.model, str(config.workspace)),
            permissions=permissions,
            sink=sink,
            asker=asker,
            peer=peer,
        )
    except MissingConfig as exc:
        print(ui.error(str(exc)), file=sys.stderr)
        return 2
    install_exit_logging(agent.session_log)

    #  接回上下文并复制进新会话文件（新文件自包含，可再次 resume）
    agent.restore(loaded, source=str(chosen.path))

    if vanished := vanished_tools(loaded, agent.toolbox.names()):
        print(
            ui.warning(
                f"[注意] 历史会话用过的工具现已不可用：{', '.join(vanished)}"
                "（插件/技能配置可能变了）。模型若再调用会收到明确报错并自行改道。"
            ),
            #  json/stream-json 的 stdout 只准出现结构化输出，人读的警告挪去 stderr
            file=sys.stderr if prompt and args.output_format != "text" else sys.stdout,
        )

    forked = f"，已按前 {args.fork} 轮分叉" if args.fork is not None else ""
    if prompt:
        if args.output_format == "text":
            print(ui.secondary(f"已恢复会话（{len(loaded)} 条消息，来自 {chosen.path.name}{forked}）"))
        return run_once(agent, prompt, args.output_format)
    print(build_banner(model_label(agent), str(config.workspace)))
    if budget_note := skills.budget_warning():
        print(ui.secondary(budget_note))
    print(ui.secondary(f"已恢复会话（{len(loaded)} 条消息，来自 {chosen.path.name}{forked}）"))
    #  回放最近几轮补进 scrollback（重建 turn 喂同一个
    #  渲染器），恢复后不再两眼一抹黑（/resume 切会话共用同一条路径）
    replay_recent(agent, loaded)
    if note:
        print(ui.secondary(note))
    return run_repl(repl_fn, agent)


def doctor_command(argv: list[str]) -> int:
    """体检：这台机器能不能把小羽跑顺。任一 FAIL 退出码 1；--json 给脚本。"""
    from . import diagnostics

    parser = argparse.ArgumentParser(
        prog="xiaoyu doctor",
        description="检查 Python / 配置目录 / 磁盘 / 凭据有无 / 沙箱 / 命令解析器 / MCP 配置 / 会话目录",
    )
    parser.add_argument("--json", action="store_true", help="机器可读输出（含进程快照）")
    parser.add_argument("-w", "--workspace", default="", help="按哪个工作区检查（默认当前目录）")
    args = parser.parse_args(argv)

    workspace = Path(args.workspace).expanduser() if args.workspace else None
    checks = diagnostics.run_doctor(workspace)
    if args.json:
        print(diagnostics.to_json(checks))
    else:
        paint = {"ok": ui.success, "warn": ui.warning, "fail": ui.error}
        for line in diagnostics.render(checks):
            mark = line[:4].strip().lower()
            if mark in paint:
                print(paint[mark](line[:4]) + line[4:])
            else:
                print(ui.secondary(line))
    return 1 if diagnostics.overall(checks) == "fail" else 0


def terminal_setup_command(argv: list[str]) -> int:
    """配 VS Code 系编辑器的 Shift+Enter。

    这条命令会改工作区之外的用户配置，所以默认先把计划打出来让人过目再动手。
    """
    from . import editor_setup

    parser = argparse.ArgumentParser(
        prog="xiaoyu terminal-setup",
        description=(
            "让 VS Code / Cursor / Windsurf 的内置终端支持 Shift+Enter 换行"
            "（默认只有 Alt-Enter 能换行，因为这些终端把 Shift+Enter 发成普通回车）"
        ),
    )
    parser.add_argument("--yes", action="store_true", help="不询问，直接写入")
    parser.add_argument("--dry-run", action="store_true", help="只看计划，不写任何文件")
    args = parser.parse_args(argv)

    plans = editor_setup.make_plans()
    if not plans:
        print(ui.warning("没找到 VS Code / Cursor / Windsurf 的用户配置目录。"))
        print(ui.secondary("其它终端多数原生支持 Alt-Enter 换行，无需配置。"))
        return 0

    marks = {"install": ui.success("将写入"), "already": ui.secondary("已配好"),
             "conflict": ui.warning("跳过"), "unreadable": ui.error("跳过")}
    for plan in plans:
        print(f"  {marks[plan.action]}  {plan.editor.name}：{plan.detail}")
        print(ui.secondary(f"        {plan.path}"))
    todo = [plan for plan in plans if plan.action == "install"]
    if not todo:
        print(ui.secondary("没有需要改动的。"))
        return 0
    if args.dry_run:
        return 0
    if not args.yes:
        print(ui.secondary("  会先留一份 .bak 备份；已有的 shift+enter 绑定不会被覆盖。"))
        try:
            answer = input(ui.prompt(f"写入这 {len(todo)} 个文件？[y/N] ")).strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return 1
        if answer not in ("y", "yes"):
            print(ui.secondary("没有改动。"))
            return 1
    for plan in todo:
        try:
            print(ui.success("  " + editor_setup.apply(plan)))
        except OSError as exc:
            print(ui.error(f"  {plan.editor.name}：写入失败 {exc}"), file=sys.stderr)
            return 1
    print(ui.secondary("重启编辑器（或重开终端面板）后生效。"))
    return 0


def _tui_available() -> bool:
    """装没装 TUI 可选依赖。/keys 用它决定要不要提示"这些键当前不生效"。"""
    try:
        from . import tui  # noqa: F401
    except ImportError:
        return False
    return True


def _serve_available() -> bool:
    """装没装 [serve] 可选依赖（fastapi + uvicorn）。update 用它决定要不要把
    serve 一并升级：serve 锁精确版本，只升本体会把已 opt-in 的用户无声留在
    旧 pin 上（0.31.6 就动过 pin）。用 find_spec 不真 import——fastapi 一
    import 就是几百毫秒，这里只需要"在不在"。

    和 _tui_available 一样是"可导入"启发式：fastapi 也可能是同环境里别的项目
    装的，这时多带 [serve] 会把它钉到我们的 pin 上——与本体锁版本是同一套
    取舍，接受。
    """
    return all(
        importlib.util.find_spec(name) is not None for name in ("fastapi", "uvicorn")
    )


#  自己动不了自己时给的兜底命令：不经 xiaoyu.exe 就没有自锁
_WINDOWS_MANUAL_UPGRADE = "python -m pip install --upgrade xiaoyu-agent"
_WINDOWS_MANUAL_UNINSTALL = "python -m pip uninstall xiaoyu-agent"


#  这个 helper 走 os.path 而非 Path：Path() 按调用时的 os.name 分派，测试里要
#  伪造 Windows 就只能 patch os.name，那会连带把 Path 变成 WindowsPath（在
#  POSIX 上一构造就抛 UnsupportedOperation）。纯字符串路径没有这层耦合。
def _running_launcher() -> str | None:
    """本次是不是从 Scripts\\xiaoyu.exe 这类启动器跑起来的；是则给出它的路径。

    只在 Windows 上有意义：那里正在运行的 .exe 被系统锁着，pip 卸旧版时移不走
    它，升级/卸载断在最后一步（WinError 32）。别的平台随便覆盖，无需操心。

    **别指望 argv[0] 还带着 .exe**：pip 塞进那个 exe 的 `__main__.py` 是
    distlib 的 SCRIPT_TEMPLATE 生成的，进我们的 main() 之前就先削了后缀——
    `sys.argv[0] = re.sub(r'(-script\\.pyw|\\.exe)?$', '', sys.argv[0])`。
    所以这里拿到的是 `...\\Scripts\\xiaoyu`，得把 `.exe` 补回去再验存在性。
    （v0.30.1/v0.30.2 就是栽在这上面：判据永远为假，整套处理从没被执行过。）
    """
    if os.name != "nt":
        return None
    raw = sys.argv[0] if sys.argv else ""
    if not raw:
        return None
    base = raw[:-4] if raw.lower().endswith(".exe") else raw
    try:
        path = os.path.realpath(f"{base}.exe")
    except OSError:
        return None
    return path if os.path.isfile(path) else None


def _detached_child_usable() -> bool:
    """先同步探一下子进程入口跑不跑得起来，再决定要不要把活儿交出去。

    交出去之后本命令立刻返回，子进程要是起不来就"说了稍后输出、然后什么都没
    发生"——比直接报错还难查。不带参数调用固定返回 2（打印用法），正好当探针。
    """
    try:
        probe = subprocess.run(
            [sys.executable, "-P", "-m", "xiaoyu._winpip"], capture_output=True
        )
    except OSError:
        return False
    return probe.returncode == 2


def _defer_pip_to_detached(mode: str, spec: str) -> bool:
    """把 pip 交给一个脱离的子进程，等本进程退出后再跑；拉起成功返回 True。

    只有"从 Scripts\\xiaoyu.exe 启动"这一种情形需要（见 _running_launcher）。
    为什么不能在本进程里硬跑、也不能靠改名绕开，见 xiaoyu._winpip 的模块注释。

    子进程刻意**不**加 DETACHED_PROCESS：那会连控制台一起脱掉，用户就看不见
    pip 的输出了。只加 CREATE_NEW_PROCESS_GROUP，让它不被这个控制台的 Ctrl+C
    带走。拉不起来就返回 False，调用方照旧在本进程里硬跑——不比以前差。
    """
    if _running_launcher() is None or not _detached_child_usable():
        return False
    argv = [
        sys.executable,
        #  别把 CWD 塞进 sys.path：在小羽源码目录里跑会 import 到工作树
        "-P",
        "-m",
        "xiaoyu._winpip",
        str(os.getpid()),
        str(os.getppid()),  # python.exe 之上还有启动器 stub，它也得退干净
        mode,
        spec,
        __version__,
    ]
    try:
        subprocess.Popen(
            argv, creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        )
    except OSError as exc:
        print(ui.warning(f"没能把 pip 交给后台进程（{exc}），改在本进程里执行"))
        return False
    return True


def update_command(argv: list[str]) -> int:
    """升级 xiaoyu-agent 本体；没装 TUI 可选依赖时借这次升级一并补上。

    pip 一律走 `sys.executable -m pip`：PATH 里的 pip 可能属于另一个解释器，
    升到别的环境里等于没升。pipx/uv tool 装的环境没有 pip 模块，只能给出提示。

    Windows 上从 xiaoyu.exe 启动时不能在本进程里升——正在运行的启动器锁着自己，
    pip 卸旧版必然撞 WinError 32。这种情形整段交给脱离的子进程去跑（见
    _defer_pip_to_detached / xiaoyu._winpip），本命令打完招呼就退出。
    """
    parser = argparse.ArgumentParser(
        prog="xiaoyu update",
        description="升级小羽到最新版（pip install --upgrade）；"
        "未装 TUI 增强界面时自动带上 [tui] 可选依赖，"
        "已装 serve（HTTP API）时一并升级其锁定依赖",
    )
    parser.parse_args(argv)

    probe = subprocess.run(
        [sys.executable, "-m", "pip", "--version"], capture_output=True
    )
    if probe.returncode != 0:
        print(ui.error("当前 Python 环境里没有 pip，无法自动升级。"), file=sys.stderr)
        print(ui.secondary("  pipx 安装的话：pipx upgrade xiaoyu-agent"))
        print(ui.secondary("  uv 安装的话：  uv tool upgrade xiaoyu-agent"))
        return 1

    #  tui 与 serve 的方向刻意相反：tui 缺了才补（默认体验人人该有）；serve
    #  装了才跟（opt-in 的少数派，但已 opt-in 就得跟上新 pin）。browser 有意
    #  不跟——playwright 换版本还得重跑 playwright install，别替用户做主。
    extras = []
    if not _tui_available():
        extras.append("tui")
        print(ui.secondary("未检测到 TUI 增强界面（补全/历史/粘贴折叠），本次升级一并安装"))
    if _serve_available():
        extras.append("serve")
        print(ui.secondary("检测到 serve（HTTP API）依赖，一并升级到本版锁定版本"))
    spec = "xiaoyu-agent" + (f"[{','.join(extras)}]" if extras else "")
    print(ui.secondary(f"当前 xiaoyu {__version__}，执行 pip install --upgrade {spec}"))
    if _defer_pip_to_detached("update", spec):
        print(ui.secondary("Windows 不让程序覆盖正在运行的自己，升级改在本进程退出后继续。"))
        print(ui.secondary("pip 输出会接着打在这个窗口里，跑完按一次 Enter 回到命令提示符。"))
        return 0
    #  spec 作为独立 argv 传入、不经 shell，[] 不会被展开，无需引号
    result = subprocess.run([sys.executable, "-m", "pip", "install", "--upgrade", spec])
    if result.returncode != 0:
        print(ui.error("升级失败，原因见上方 pip 输出。"), file=sys.stderr)
        if os.name == "nt":
            print(
                ui.secondary(
                    "  若报 WinError 32（文件被占用），是别的进程锁住了要覆盖的文件；"
                    f"关掉其它 xiaoyu 窗口后另开终端执行：{_WINDOWS_MANUAL_UPGRADE}"
                )
            )
        return 1

    #  本进程还载着旧代码，新版本号得开个新解释器去读
    fresh = subprocess.run(
        [
            sys.executable,
            "-c",
            "from importlib.metadata import version; print(version('xiaoyu-agent'))",
        ],
        capture_output=True,
        text=True,
        #  输出只是版本号，但 text=True 按 locale 严格解码，Windows 上一个
        #  意外字节就能把升级流程炸在最后一步——显式 UTF-8 + replace
        encoding="utf-8",
        errors="replace",
    )
    new_version = fresh.stdout.strip() if fresh.returncode == 0 else ""
    if new_version and new_version != __version__:
        print(ui.success(f"已升级：{__version__} → {new_version}"))
    else:
        print(ui.success(f"已是最新版本（{new_version or __version__}）"))
    return 0


def serve_command(argv: list[str]) -> int:
    """`xiaoyu serve`：HTTP API server（见 serve.py 模块 docstring）。

    与 `--acp` / `--wire` 并列的第三条协议面，驱动方是工作流编排器
    （n8n / Dify / 自研调度）。fastapi/uvicorn 是可选额外，缺包只影响这条命令。
    """
    from .serve import ServeConfig, ServeUnavailable, print_openapi, serve

    parser = argparse.ArgumentParser(
        prog="xiaoyu serve",
        description="起 HTTP API server，把小羽接给工作流编排器（n8n / Dify / 自研调度）。",
    )
    parser.add_argument("--host", default="127.0.0.1", help="监听地址，默认只绑回环")
    parser.add_argument("--port", type=int, default=8420, help="监听端口，默认 8420")
    parser.add_argument(
        "--workspace",
        help="root 工作区，默认当前目录。会话只能落在它或它的子目录里",
    )
    parser.add_argument(
        "--token",
        default=os.environ.get("XIAOYU_SERVE_TOKEN", ""),
        help="Bearer token（也可用 XIAOYU_SERVE_TOKEN）。绑非回环地址时必填",
    )
    parser.add_argument("--model", help="默认模型名，会话可覆盖")
    parser.add_argument("--base-url", dest="base_url", help="OpenAI 兼容端点")
    parser.add_argument("--mode", choices=modes.CYCLE, help="默认交互模式，会话可覆盖")
    parser.add_argument(
        "--approval",
        choices=("ask", "allow_all"),
        default="ask",
        help="ask=需要放行的工具调用挂起等 /permissions（默认）；allow_all=等价 --yolo，无人值守但没有闸门",
    )
    parser.add_argument(
        "--approval-timeout",
        type=float,
        default=300.0,
        help="审批等多久算超时（秒）。超时按拒绝处理，默认 300",
    )
    parser.add_argument(
        "--max-sessions",
        dest="max_sessions",
        type=int,
        default=32,
        help="能同时跑的会话数（= 工作线程池大小）。等审批期间线程也被占着，"
        "所以它同时是'能同时挂起等审批的会话数'上限，默认 32",
    )
    parser.add_argument(
        "--no-mcp",
        dest="mcp",
        action="store_false",
        help="不挂 /mcp（MCP server 面，给 LangChain/LangGraph 等 MCP client 用；默认挂）",
    )
    parser.add_argument(
        "--state-dir",
        dest="state_dir",
        help="agent 对象 / 会话清单 / 会话日志的落盘目录，默认 ~/.xiaoyu/serve/<root slug>/",
    )
    parser.add_argument(
        "--no-persist",
        dest="persist",
        action="store_false",
        help="不落盘：agent 与会话只在内存里，重启即失（一次性跑 / 临时调试）",
    )
    parser.add_argument(
        "--print-openapi",
        action="store_true",
        help="把 OpenAPI schema 打到 stdout 后退出（贴给 Dify 自定义工具用），不起服务",
    )
    parser.add_argument(
        "--public-url",
        dest="public_url",
        default="",
        help="写进 schema servers 的地址。编排器在容器里时必填"
        "（Docker Desktop 常用 http://host.docker.internal:8420）",
    )
    args = parser.parse_args(argv)

    root = (Path(args.workspace).expanduser() if args.workspace else Path.cwd()).resolve()
    if not root.is_dir():
        print(ui.error(f"工作区不存在：{root}"), file=sys.stderr)
        return 2
    #  与主命令同一道门，且必须在 load_dotenv 之前：工作区 .env 是被门管的对象。
    #  服务端不可能弹窗问人，所以非交互判定（headless 纪律，与 --acp 一致）
    trust = resolve_folder_trust(root, grant=False, interactive=False)
    load_dotenv(None, untrusted_dir=None if trust.trusted else root)

    cfg = ServeConfig(
        root=root,
        host=args.host,
        port=args.port,
        token=args.token,
        model=args.model or "",
        base_url=args.base_url or "",
        mode=args.mode or "",
        approval=args.approval,
        approval_timeout=args.approval_timeout,
        max_sessions=max(1, args.max_sessions),
        mcp=args.mcp,
        state_dir=Path(args.state_dir).expanduser().resolve() if args.state_dir else None,
        persist=args.persist,
    )
    try:
        if args.print_openapi:
            return print_openapi(cfg, args.public_url)
        if args.host in ("127.0.0.1", "::1", "localhost") or args.token:
            print(ui.success(f"xiaoyu serve → http://{args.host}:{args.port}  (root: {root})"))
            extra = " · MCP /mcp" if args.mcp else ""
            print(ui.secondary(f"  文档 /docs · schema /openapi.json{extra} · Ctrl+C 停"))
        return serve(cfg)
    except ServeUnavailable:
        print(
            ui.error("serve 需要 fastapi 和 uvicorn，当前环境没装。"),
            file=sys.stderr,
        )
        print('  pip install "xiaoyu-agent[serve]"', file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


def uninstall_command(argv: list[str]) -> int:
    """卸载小羽：pip uninstall 本体 + 收拾装完之后留下的东西。

    裸 `pip uninstall` 只删包，不会碰用户配置目录和 terminal-setup 写进
    编辑器的键绑定；这条命令把"从装上到用过"的全过程反向走一遍。
    默认保留配置目录（用户可能只是换环境重装），--purge 才连它一起删。
    pip 的讲究与 update_command 相同：走 `sys.executable -m pip`，
    pipx/uv tool 环境没有 pip 模块，只能给出对应命令让用户自己跑。
    """
    from . import editor_setup
    from .config import user_config_dir

    parser = argparse.ArgumentParser(
        prog="xiaoyu uninstall",
        description="卸载小羽（pip uninstall xiaoyu-agent），并移除 terminal-setup "
        "写入的编辑器键绑定；--purge 连配置目录（会话记录、用户级 .env、MCP 配置等）一起删",
    )
    parser.add_argument("--purge", action="store_true", help="连配置目录一起删（默认保留，重装可复用）")
    parser.add_argument("--yes", action="store_true", help="不询问，直接执行")
    parser.add_argument("--dry-run", action="store_true", help="只看计划，不动任何东西")
    args = parser.parse_args(argv)

    #  先把要动的东西全部打出来，确认后才动手——和 terminal-setup 同一姿势
    plans = editor_setup.removal_plans()
    config_dir = user_config_dir()
    purge_target = config_dir if args.purge and config_dir.is_dir() else None
    pip_ok = (
        subprocess.run([sys.executable, "-m", "pip", "--version"], capture_output=True).returncode == 0
    )

    for plan in plans:
        print(f"  {ui.success('将移除')}  {plan.editor.name}：shift+enter 绑定（留 .bak 备份）")
        print(ui.secondary(f"        {plan.path}"))
    if purge_target is not None:
        print(f"  {ui.success('将删除')}  配置目录（会话记录、用户级 .env、MCP 配置等）")
        print(ui.secondary(f"        {purge_target}"))
    elif args.purge:
        print(ui.secondary(f"  配置目录不存在，无需删除：{config_dir}"))
    else:
        print(ui.secondary(f"  保留配置目录（--purge 可连它一起删）：{config_dir}"))
    if pip_ok:
        print(f"  {ui.success('将执行')}  pip uninstall xiaoyu-agent")
    else:
        print(ui.warning("  当前 Python 环境里没有 pip，包本体需要你自己卸："))
        print(ui.secondary("    pipx 安装的话：pipx uninstall xiaoyu-agent"))
        print(ui.secondary("    uv 安装的话：  uv tool uninstall xiaoyu-agent"))

    if args.dry_run:
        return 0
    if not args.yes:
        try:
            answer = input(ui.prompt("确认卸载？[y/N] ")).strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return 1
        if answer not in ("y", "yes"):
            print(ui.secondary("没有改动。"))
            return 1

    #  先收拾附属物，最后才卸包——包一卸掉，本进程就不该再干活了
    for plan in plans:
        try:
            print(ui.success("  " + editor_setup.apply_removal(plan)))
        except OSError as exc:
            print(ui.error(f"  {plan.editor.name}：写入失败 {exc}"), file=sys.stderr)
            return 1
    if purge_target is not None:
        try:
            shutil.rmtree(purge_target)
        except OSError as exc:
            print(ui.error(f"  删除配置目录失败：{exc}"), file=sys.stderr)
            return 1
        print(ui.success(f"  已删除 {purge_target}"))
        _hint_keychain_leftover()

    if not pip_ok:
        return 1
    #  附属物已经收拾完，剩下卸包这一步整段交给脱离的子进程（同 update）
    if _defer_pip_to_detached("uninstall", "xiaoyu-agent"):
        print(ui.secondary("Windows 不让程序删掉正在运行的自己，卸包改在本进程退出后继续。"))
        print(ui.secondary("pip 输出会接着打在这个窗口里，跑完按一次 Enter 回到命令提示符。"))
        return 0
    result = subprocess.run([sys.executable, "-m", "pip", "uninstall", "-y", "xiaoyu-agent"])
    if result.returncode != 0:
        print(ui.error("卸载失败，原因见上方 pip 输出。"), file=sys.stderr)
        if os.name == "nt":
            print(
                ui.secondary(
                    "  Windows 下 xiaoyu.exe 正在运行时删不掉自己；关掉其它 xiaoyu "
                    f"窗口后另开终端执行：{_WINDOWS_MANUAL_UNINSTALL}"
                )
            )
        return 1
    print(ui.success("小羽已卸载。后会有期。"))
    return 0


def _hint_keychain_leftover() -> None:
    """--purge 后提醒 macOS Keychain 里的 key（不自动删：删了就找不回来）。"""
    from .config import KEYCHAIN_SERVICE, _read_from_keychain

    if sys.platform != "darwin" or _read_from_keychain() is None:
        return
    account = os.environ.get("USER", "")
    print(
        ui.secondary(
            f"  Keychain 里还存着 {KEYCHAIN_SERVICE}（不自动删），要清的话："
            f'security delete-generic-password -a "{account}" -s "{KEYCHAIN_SERVICE}"'
        )
    )


#  server 名会拼进工具名 mcp__<名字>__<工具>：限死 ASCII 安全字符，
#  否则名字要经消毒改写（见 mcp._sanitize_name），用户在 /tools 里认不出自己配的东西
MCP_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")

#  `xiaoyu mcp add` 认得的选项 → 是否再吃一个值。用来在 argv 里切出
#  "选项段 | 启动命令段"（见 split_mcp_command）。
_MCP_ADD_FLAGS = {
    "--scope": True,
    "-s": True,
    "--env": True,
    "-e": True,
    "--timeout": True,
    "--force": False,
    "-f": False,
    "--help": False,
    "-h": False,
}


def split_mcp_command(argv: list[str]) -> tuple[list[str], list[str]]:
    """把 `<名字> [选项…] <命令> [参数…]` 切成（交给 argparse 的段, 启动命令段）。

    argparse 干不了这活：nargs=REMAINDER 从第一个位置参数之后就通吃，
    `add x --scope user npx …` 里的 `--scope user` 会被卷进启动命令。所以自己扫一遍——
    第二个"裸词"就是启动命令的开头。显式 `--` 分隔也认（启动命令本身像选项时用）。
    """
    seen_name = False
    index = 0
    while index < len(argv):
        token = argv[index]
        if token == "--":
            return argv[:index], argv[index + 1 :]
        if token.startswith("-") and token != "-":
            #  未知选项按"不吃值"处理，留给 argparse 去报错
            takes_value = _MCP_ADD_FLAGS.get(token.split("=", 1)[0], False)
            index += 2 if takes_value and "=" not in token else 1
            continue
        if not seen_name:
            seen_name = True
            index += 1
            continue
        return argv[:index], argv[index:]
    return argv, []


MCP_USAGE = (
    "用法：\n"
    "  xiaoyu mcp add <名字> [选项] <命令> [参数…]   添加一个 stdio MCP server\n"
    "  xiaoyu mcp list                              列出已声明的 server\n"
    "  xiaoyu mcp remove <名字> [--scope …]          删除一个声明\n"
    "例：xiaoyu mcp add chrome-devtools --scope user npx -y chrome-devtools-mcp@latest"
)


def mcp_command(argv: list[str]) -> int:
    """`xiaoyu mcp`：增删查 MCP server 声明。

    写的就是 `.mcp.json` / `mcp.json` 本身（多家客户端通用的格式），
    不是另起一套注册表——手写和命令行两条路随时可以互相接管。
    """
    action = argv[0] if argv else ""
    if action == "add":
        return mcp_add_command(argv[1:])
    if action in ("list", "ls"):
        return mcp_list_command(argv[1:])
    if action in ("remove", "rm"):
        return mcp_remove_command(argv[1:])
    if action in ("", "-h", "--help", "help"):
        print(MCP_USAGE)
        return 0
    print(ui.error(f"未知的 mcp 子命令：{action}"), file=sys.stderr)
    print(MCP_USAGE, file=sys.stderr)
    return 2


def _mcp_scope_argument(parser: argparse.ArgumentParser, help_text: str) -> None:
    from . import mcp

    parser.add_argument("-s", "--scope", choices=mcp.SCOPES, help=help_text)


def mcp_add_command(argv: list[str]) -> int:
    from . import mcp, mcp_guard

    parser = argparse.ArgumentParser(
        prog="xiaoyu mcp add",
        #  启动命令段不是 argparse 的位置参数（见 split_mcp_command），
        #  自动生成的 usage 里不会有——手写一行，否则 -h 看不出命令写哪
        usage="xiaoyu mcp add <名字> [-s {project,user}] [-e KEY=VALUE] "
        "[--timeout 秒] [-f] <命令> [参数…]\n"
        "       xiaoyu mcp add <名字> --url https://… [-H 头:值] [-s …] [--timeout 秒] [-f]",
        description="添加一个 MCP server 声明。两种传输：本地 stdio（给启动命令）"
        "与远端 Streamable HTTP（给 --url）。老式 SSE 传输不支持。",
        epilog="例：xiaoyu mcp add chrome-devtools --scope user npx -y chrome-devtools-mcp@latest\n"
        "例：xiaoyu mcp add 网关 --url https://mcp.example.com/mcp -H 'Authorization: Bearer ${env:TOKEN}'",
    )
    parser.add_argument("name", help="server 名字，工具会挂成 mcp__<名字>__<工具>")
    _mcp_scope_argument(
        parser, "project=工作区 .mcp.json（默认，随仓库走）；user=用户级 mcp.json（全工作区生效）"
    )
    parser.add_argument(
        "-e",
        "--env",
        action="append",
        metavar="KEY=VALUE",
        default=[],
        help="传给 server 的环境变量，可重复。值里写 ${env:VAR} 可留到启动时再展开，"
        "密钥不必落进配置文件",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        metavar="秒",
        help=f"tools/call 超时，默认 {int(mcp.CALL_TIMEOUT)} 秒",
    )
    parser.add_argument(
        "--url",
        help="远端 server 地址（Streamable HTTP）。给了 --url 就不要再给启动命令；"
        "明文 http 只允许连回环地址——头里通常放着凭据",
    )
    parser.add_argument(
        "-H",
        "--header",
        action="append",
        metavar="名:值",
        default=[],
        help="远端 server 每次请求都带的头，可重复。值里同样支持 ${env:VAR} 延迟展开",
    )
    parser.add_argument("-f", "--force", action="store_true", help="同名声明已存在时覆盖")
    head, command_argv = split_mcp_command(argv)
    args = parser.parse_args(head)
    scope = args.scope or "project"

    if not MCP_NAME_PATTERN.match(args.name):
        print(ui.error(f"server 名字只能用字母/数字/下划线/连字符：{args.name}"), file=sys.stderr)
        return 2
    if args.url and command_argv:
        print(
            ui.error("--url 与启动命令只能给一个（远端 server 没有本地命令）"),
            file=sys.stderr,
        )
        return 2
    if not args.url and not command_argv:
        print(
            ui.error("缺少启动命令或 --url，如：xiaoyu mcp add 名字 npx -y 某个包"),
            file=sys.stderr,
        )
        return 2
    if not args.url and args.header:
        print(ui.error("-H/--header 只对 --url 的远端 server 有意义"), file=sys.stderr)
        return 2
    env: dict[str, str] = {}
    for pair in args.env:
        key, sep, value = pair.partition("=")
        if not sep or not key.strip():
            print(ui.error(f"--env 格式应为 KEY=VALUE：{pair}"), file=sys.stderr)
            return 2
        env[key.strip()] = value
    if args.timeout is not None and args.timeout <= 0:
        print(ui.error("--timeout 得是正数"), file=sys.stderr)
        return 2

    entry: dict[str, Any] = {}
    if args.url:
        #  准入的"保存点"，远端版：判据是地址会不会让凭据裸奔（见 endpoint_violation）
        if reason := mcp_guard.endpoint_violation(args.url):
            print(ui.error(f"这条声明被安全规则拦下：{reason}"), file=sys.stderr)
            return 2
        headers: dict[str, str] = {}
        for pair in args.header:
            key, sep, value = pair.partition(":")
            if not sep or not key.strip():
                print(ui.error(f"--header 格式应为 名:值：{pair}"), file=sys.stderr)
                return 2
            headers[key.strip()] = value.strip()
        entry["type"] = "http"
        entry["url"] = args.url
        if headers:
            entry["headers"] = headers
    else:
        command, *command_args = command_argv
        #  准入检查的"保存点"（启动点在 mcp.load_server_specs / ensure_started）：
        #  形状像内联攻击脚本的配置在落盘前就拦掉，别等到某次启动才炸
        if reason := mcp_guard.admission_violation(command, command_args, env):
            print(ui.error(f"这条声明被安全规则拦下：{reason}"), file=sys.stderr)
            return 2
        entry["command"] = command
        if command_args:
            entry["args"] = command_args
        if env:
            entry["env"] = env
    if args.timeout is not None:
        entry["timeout"] = args.timeout

    path = mcp.scope_path(scope, Path.cwd())
    try:
        data = mcp.read_config_file(path)
    except mcp.McpError as exc:
        print(ui.error(str(exc)), file=sys.stderr)
        return 1
    servers = data.get("mcpServers")
    if not isinstance(servers, dict):
        servers = {}
    if args.name in servers and not args.force:
        print(
            ui.error(f"{path} 里已经有 {args.name} 了，要覆盖加 --force"),
            file=sys.stderr,
        )
        return 2
    servers[args.name] = entry
    data["mcpServers"] = servers
    try:
        mcp.write_config_file(path, data)
    except OSError as exc:
        print(ui.error(f"写不进 {path}：{exc}"), file=sys.stderr)
        return 1

    print(ui.success(f"已写入 {path}"))
    print(f"  {ui.accent(args.name)}  {ui.secondary('·')}  {' '.join(command_argv)}")
    #  Windows 上 npx 是 .cmd、pipx/uvx 常不在 PATH 里——现在提示比启动后翻日志便宜
    if shutil.which(command) is None:
        print(ui.warning(f"  提示：当前 PATH 里找不到 {command}，装好之前这个 server 起不来"))
    print(ui.secondary("  下次启动小羽时后台连上；/mcp 看状态与日志路径。"))
    return 0


def mcp_list_command(argv: list[str]) -> int:
    from . import mcp

    parser = argparse.ArgumentParser(
        prog="xiaoyu mcp list",
        description="列出两级配置文件里声明的 MCP server（工作区级覆盖用户级同名项）。",
    )
    _mcp_scope_argument(parser, "只看某一级")
    args = parser.parse_args(argv)

    workspace = Path.cwd()
    shown = 0
    failed = False
    project_names: set[str] = set()
    for scope in mcp.SCOPES:
        path = mcp.scope_path(scope, workspace)
        try:
            data = mcp.read_config_file(path)
        except mcp.McpError as exc:
            print(ui.error(str(exc)), file=sys.stderr)
            failed = True
            continue
        servers = data.get("mcpServers")
        servers = servers if isinstance(servers, dict) else {}
        if scope == "project":
            project_names = set(servers)
        if args.scope and args.scope != scope:
            continue
        if not servers:
            continue
        shown += len(servers)
        print(ui.heading(f"{scope}  ") + ui.secondary(shorten_home(str(path))))
        for name, raw in servers.items():
            raw = raw if isinstance(raw, dict) else {}
            argv_text = " ".join([str(raw.get("command", "?")), *(str(a) for a in raw.get("args") or [])])
            notes = []
            if raw.get("disabled"):
                notes.append("已停用")
            if scope == "user" and name in project_names:
                notes.append("被工作区同名声明覆盖")
            if raw.get("env"):
                notes.append("env: " + ", ".join(raw["env"]))
            line = f"  {ui.accent(name)}  {ui.secondary('·')}  {argv_text}"
            print(line + (ui.secondary("  ·  " + "  ·  ".join(notes)) if notes else ""))
    if failed:
        #  读坏了的那一级里有什么无从得知，别拿"没有声明"糊弄过去
        return 1
    if not shown:
        print(ui.secondary("没有声明任何 MCP server。"))
        print(ui.secondary(MCP_USAGE.splitlines()[-1]))
    return 0


def mcp_remove_command(argv: list[str]) -> int:
    from . import mcp

    parser = argparse.ArgumentParser(
        prog="xiaoyu mcp remove",
        description="删除一个 MCP server 声明。",
    )
    parser.add_argument("name", help="server 名字")
    _mcp_scope_argument(parser, "在哪一级删；不给则自动找（两级都有时要求指定）")
    args = parser.parse_args(argv)

    workspace = Path.cwd()
    scopes = [args.scope] if args.scope else list(mcp.SCOPES)
    found: list[tuple[str, Path, dict[str, Any]]] = []
    for scope in scopes:
        path = mcp.scope_path(scope, workspace)
        try:
            data = mcp.read_config_file(path)
        except mcp.McpError as exc:
            print(ui.error(str(exc)), file=sys.stderr)
            return 1
        servers = data.get("mcpServers")
        if isinstance(servers, dict) and args.name in servers:
            found.append((scope, path, data))
    if not found:
        where = f"{args.scope} 级" if args.scope else "两级配置里都"
        print(ui.error(f"{where}没有叫 {args.name} 的 server"), file=sys.stderr)
        return 2
    if len(found) > 1:
        #  两处都有时不替用户选：删错哪个都得手工恢复
        print(
            ui.error(f"{args.name} 在两级配置里都有声明，用 --scope project|user 指定删哪个"),
            file=sys.stderr,
        )
        return 2
    scope, path, data = found[0]
    del data["mcpServers"][args.name]
    try:
        mcp.write_config_file(path, data)
    except OSError as exc:
        print(ui.error(f"写不进 {path}：{exc}"), file=sys.stderr)
        return 1
    print(ui.success(f"已从 {path} 删除 {args.name}"))
    return 0


PLUGIN_USAGE = (
    "用法：\n"
    "  xiaoyu plugin add <owner/repo|URL|路径> [选项]   装一个插件包（skills + MCP 声明）\n"
    "  xiaoyu plugin list                              列出已装的插件包\n"
    "  xiaoyu plugin update [名字…]                    从记录的来源拉新（不给名字则全部）\n"
    "  xiaoyu plugin remove <名字>                     卸掉一个插件包\n"
    "例：xiaoyu plugin add aws/agent-toolkit-for-aws --name aws-core"
)


def plugin_command(argv: list[str]) -> int:
    """`xiaoyu plugin`：插件包（agent-plugins.org 那套 bundle 格式）的装卸更新。

    与 `XIAOYU_ENABLE_PLUGINS` 管的**插件工具**（entry point 组 `xiaoyu.tools`）
    不是一回事：那是代码级工具通道，这里装的是技能文本和 MCP 声明。
    """
    action = argv[0] if argv else ""
    if action == "add":
        return plugin_add_command(argv[1:])
    if action in ("list", "ls"):
        return plugin_list_command(argv[1:])
    if action == "update":
        return plugin_update_command(argv[1:])
    if action in ("remove", "rm", "uninstall"):
        return plugin_remove_command(argv[1:])
    if action in ("", "-h", "--help", "help"):
        print(PLUGIN_USAGE)
        return 0
    print(ui.error(f"未知的 plugin 子命令：{action}"), file=sys.stderr)
    print(PLUGIN_USAGE, file=sys.stderr)
    return 2


def _print_bundle_summary(bundle) -> None:
    from . import plugins

    label = f"{bundle.name}" + (f" {bundle.version}" if bundle.version else "")
    print(ui.heading(label) + ("  " + ui.secondary(bundle.manifest_file or "无元数据，按目录识别")))
    if bundle.description:
        print("  " + ui.secondary(ui.fit(bundle.description, reserve=2)))
    print(f"  技能 {len(bundle.skills)} 个" + (f"：{'、'.join(bundle.skills)}" if bundle.skills else ""))
    if bundle.mcp_servers:
        print(f"  MCP server {len(bundle.mcp_servers)} 个：")
        for server, entry in bundle.mcp_servers.items():
            name = plugins.mcp_server_name(bundle.name, server)
            print(f"    {ui.accent(name)}  {ui.secondary('·')}  {plugins.command_line(entry)}")
    #  识别到但不装的东西逐条报出来：hooks 静默丢掉比不装更危险
    for note in bundle.notes:
        print(ui.warning(f"  未安装：{note}"))


def _confirm_mcp(bundle, accept: bool) -> bool:
    """装 MCP 声明前的那道门。非交互场景一律不装，而不是默默替用户点头。

    插件包是从网上拉来的，`.mcp.json` 里那行 command 会在下次启动时被 spawn。
    这跟手敲一条 `xiaoyu mcp add` 的区别只在于：用户没看过那行命令。
    """
    if not bundle.mcp_servers:
        return False
    if accept:
        return True
    if not (sys.stdin.isatty() and sys.stderr.isatty()):
        print(
            ui.warning(
                "  非交互环境：MCP server 声明未安装（技能照装）。"
                "确认上面的命令行没问题后，加 --accept-mcp 重装一次即可。"
            )
        )
        return False
    try:
        answer = input(ui.prompt("  把上面这些 MCP server 声明也装上？[y/N] ")).strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    return answer in ("y", "yes")


def plugin_add_command(argv: list[str]) -> int:
    from . import plugins

    parser = argparse.ArgumentParser(
        prog="xiaoyu plugin add",
        description="装一个插件包：包里的 skills/ 挂进技能索引，mcp.json 里的 server "
        "声明经确认后合进用户级 mcp.json。hooks 不装（会报出来）。",
        epilog="例：xiaoyu plugin add aws/agent-toolkit-for-aws --name aws-core",
    )
    parser.add_argument("source", help="owner/repo、git 仓库 URL，或本地目录")
    parser.add_argument("--name", help="一仓多包时指定装哪个（也用作安装后的包名）")
    parser.add_argument("--ref", default="", help="git 分支或 tag，默认跟随远端默认分支")
    parser.add_argument("--dir", dest="subpath", default="", help="包在仓库里的子目录")
    parser.add_argument(
        "--accept-mcp", action="store_true", help="免确认地装上包里的 MCP server 声明"
    )
    parser.add_argument("-f", "--force", action="store_true", help="同名插件包已装时覆盖")
    args = parser.parse_args(argv)

    #  名字已知就先拦一道：克隆几十兆再告诉用户"已经装过了"太不体面
    if args.name and not args.force and (plugins.plugins_root() / args.name).is_dir():
        print(
            ui.error(f"{args.name} 已经装过了，要覆盖加 --force（或用 update 拉新）"),
            file=sys.stderr,
        )
        return 2

    try:
        with plugins.staging_dir() as tmp:
            kind, _ = plugins.resolve_source(args.source)
            root, origin = plugins.fetch(
                args.source, args.ref, into=Path(tmp) / "src" if kind == "git" else None
            )
            bundle_dir, picked = plugins.pick_bundle(root, args.name, args.subpath)
            bundle = plugins.inspect_bundle(bundle_dir, fallback_name=picked or args.name)
            #  账本里的 subpath 一律用正斜杠：Windows 上 str() 出来是反斜杠，
            #  同一份账本就不跨平台了（`root / "p/a"` 在 Windows 上照样解析）
            origin = replace(
                origin,
                subpath=bundle_dir.relative_to(root).as_posix() if bundle_dir != root else "",
            )

            target = plugins.plugins_root() / bundle.name
            reinstall = target.is_dir()
            if reinstall and not args.force:
                print(
                    ui.error(f"{bundle.name} 已经装过了，要覆盖加 --force（或用 update 拉新）"),
                    file=sys.stderr,
                )
                return 2

            _print_bundle_summary(bundle)
            wanted = plugins.declared_mcp(bundle)
            installed = plugins.installed_mcp(bundle.name)
            if plugins.same_servers(installed, wanted) and installed:
                #  重装同一份声明：已经点过头了，不必再问一遍
                accepted, servers = True, sorted(installed)
            else:
                accepted = _confirm_mcp(bundle, args.accept_mcp)
                servers = sorted(wanted) if accepted else []
            #  准入检查赶在落盘之前：被拦下就该整包不装，而不是留个半装的目录
            if accepted:
                plugins.guard_servers(bundle.name, bundle.mcp_servers)

            path = plugins.materialize(bundle_dir, bundle.name)
            #  写 mcp.json 必须在包落盘**之后**：反过来的话 materialize 一失败
            #  （磁盘满、权限），声明已经写进去了、下次启动照样拉起，账本却没记
            if accepted:
                servers = plugins.sync_mcp(bundle.name, bundle.mcp_servers)
            elif reinstall and installed:
                #  覆盖安装 = 换掉这一份。上一次装的声明可能来自完全不同的来源，
                #  不点头就不能留着继续跑
                dropped = plugins.drop_mcp(bundle.name)
                print(ui.warning("  覆盖安装：上次装的 MCP 声明已摘掉（" + "、".join(dropped) + "）"))
            plugins.record(bundle.name, bundle, origin, servers)
    except plugins.PluginError as exc:
        print(ui.error(str(exc)), file=sys.stderr)
        return 2
    except OSError as exc:
        print(ui.error(f"安装失败：{exc}"), file=sys.stderr)
        return 1

    print(ui.success(f"已装到 {shorten_home(str(path))}"))
    if bundle.skills:
        from . import skills

        sample = f"{bundle.name}{skills.NAMESPACE_SEP}{bundle.skills[0]}"
        print(ui.secondary(f"  技能带包名前缀，如 {sample}；/skills 可查看全部"))
    if servers:
        print(ui.secondary("  MCP server 下次启动小羽时后台连上；/mcp 看状态。"))
        _warn_missing_commands(bundle)
    return 0


def _warn_missing_commands(bundle) -> None:
    """Windows 上 npx 是 .cmd、uvx 常不在 PATH 里——现在提示比启动后翻日志便宜。"""
    for entry in bundle.mcp_servers.values():
        command = str(entry.get("command", ""))
        if command and shutil.which(command) is None:
            print(ui.warning(f"  提示：当前 PATH 里找不到 {command}，装好之前这个 server 起不来"))


def plugin_list_command(argv: list[str]) -> int:
    from . import plugins

    parser = argparse.ArgumentParser(
        prog="xiaoyu plugin list", description="列出已安装的插件包。"
    )
    parser.parse_args(argv)

    entries = plugins.installed_dirs()
    if not entries:
        print(ui.secondary("没有安装任何插件包。"))
        print(ui.secondary(PLUGIN_USAGE.splitlines()[-1]))
        return 0
    recorded = plugins.load_registry()
    print(ui.secondary(shorten_home(str(plugins.plugins_root()))))
    for name, path in entries:
        meta = recorded.get(name) or {}
        version = meta.get("version") or ""
        source = meta.get("source") or "（来源未知，装的时候没记上或账本被删过）"
        head = ui.accent(name) + (f" {version}" if version else "")
        print(f"  {head}  {ui.secondary('·')}  {ui.secondary(source)}")
        skill_names = plugins.scan_bundle_skills(path)
        servers = sorted(plugins.installed_mcp(name))
        detail = [f"技能 {len(skill_names)}"]
        if servers:
            detail.append("MCP " + "、".join(servers))
        if commit := meta.get("commit"):
            detail.append(commit[:8])
        print(ui.secondary("    " + "  ·  ".join(detail)))
    return 0


def plugin_update_command(argv: list[str]) -> int:
    from . import plugins

    parser = argparse.ArgumentParser(
        prog="xiaoyu plugin update",
        description="按账本记的来源重新取一遍并覆盖安装。不给名字则更新全部。",
    )
    parser.add_argument("names", nargs="*", help="要更新的插件包名")
    parser.add_argument(
        "--accept-mcp", action="store_true", help="MCP 声明有变化时免确认地接受"
    )
    args = parser.parse_args(argv)

    recorded = plugins.load_registry()
    names = args.names or sorted(name for name, _ in plugins.installed_dirs())
    if not names:
        print(ui.secondary("没有安装任何插件包。"))
        return 0
    failed = 0
    for name in names:
        meta = recorded.get(name)
        if not meta or not meta.get("source"):
            print(ui.warning(f"{name}：账本里没有来源记录，更不了（重新 plugin add 一次即可）"))
            failed += 1
            continue
        try:
            changed = _update_one(name, meta, args.accept_mcp)
        except plugins.PluginError as exc:
            print(ui.error(f"{name}：{exc}"), file=sys.stderr)
            failed += 1
            continue
        except OSError as exc:
            print(ui.error(f"{name}：更新失败：{exc}"), file=sys.stderr)
            failed += 1
            continue
        print(ui.success(f"{name}：{changed}"))
    return 1 if failed else 0


def _update_one(name: str, meta: dict[str, Any], accept_mcp: bool) -> str:
    """重取一次并覆盖安装，返回一句结果描述。"""
    from . import plugins

    with plugins.staging_dir() as tmp:
        kind = meta.get("kind") or "git"
        #  优先用记下的解析结果：本地来源当初可能是相对路径，用户换个目录再
        #  update 就找不着了；git 来源用完整 URL 也比 owner/repo 简写稳
        source = meta.get("url") or meta["source"]
        root, origin = plugins.fetch(
            source, meta.get("ref") or "", into=Path(tmp) / "src" if kind == "git" else None
        )
        subpath = meta.get("subpath") or ""
        bundle_dir = root / subpath if subpath else root
        if not bundle_dir.is_dir():
            raise plugins.PluginError(f"来源里已经没有 {subpath} 了，可能上游改了目录结构")
        bundle = plugins.inspect_bundle(bundle_dir, fallback_name=name)
        if bundle.name != name:
            raise plugins.PluginError(f"来源里的包名变成了 {bundle.name!r}，不覆盖同名安装")
        #  保留用户当初敲的那串（list 里展示的就是它），只更新解析结果
        origin = replace(origin, source=meta["source"], subpath=subpath)

        before_skills = set(plugins.scan_bundle_skills(plugins.plugins_root() / name))
        installed = plugins.installed_mcp(name)
        wanted = plugins.declared_mcp(bundle)
        #  和**上次上游声明的**比，不是和已装的比：用户当初拒了 MCP 的包，
        #  拿已装的（空）去比就会每次 update 都重报一遍"有变化"。
        #  账本里没有这一项 = 本次升级之前装的，退回拿已装的比，多问一次而已。
        previous = meta.get("declared_mcp")
        if not isinstance(previous, dict):
            previous = {n: plugins.strip_owner(e) for n, e in installed.items()}

        #  更新正是 rug-pull 的着陆点：首装人畜无害、第 N 次更新悄悄换掉 command。
        #  上游声明变了就重新问一次，不点头就一个字节都不动已装的那份。
        apply_mcp = False
        servers = sorted(installed)
        if not plugins.same_servers(previous, wanted):
            print(ui.warning(f"  {name} 的 MCP 声明有变化："))
            _print_mcp_diff(previous, wanted)
            if _confirm_mcp(bundle, accept_mcp):
                plugins.guard_servers(name, bundle.mcp_servers)
                apply_mcp = True
            elif not bundle.mcp_servers:
                #  上游把 server 全撤了：跟着撤。这个方向只减不增，不需要点头
                apply_mcp = True
            else:
                print(ui.secondary(f"  {name}：已装的 MCP 声明保持原样"))
        elif installed and not plugins.same_servers(installed, wanted):
            #  上游没变，装着的却和声明对不上 = 用户手改过 mcp.json。那是他的选择
            print(ui.secondary(f"  {name}：mcp.json 里的声明与包内不一致，按你改过的算"))

        plugins.materialize(bundle_dir, name)
        if apply_mcp:
            servers = plugins.sync_mcp(name, bundle.mcp_servers)
        plugins.record(name, bundle, origin, servers)

    added = sorted(set(bundle.skills) - before_skills)
    removed = sorted(before_skills - set(bundle.skills))
    parts = [f"技能 {len(bundle.skills)} 个"]
    if added:
        parts.append(f"新增 {'、'.join(added)}")
    if removed:
        parts.append(f"移除 {'、'.join(removed)}")
    if bundle.version:
        parts.insert(0, f"版本 {bundle.version}")
    for note in bundle.notes:
        print(ui.warning(f"  未安装：{note}"))
    return "，".join(parts)


def _print_mcp_diff(before: dict[str, Any], wanted: dict[str, Any]) -> None:
    from . import plugins

    for name in sorted(set(before) | set(wanted)):
        old = plugins.command_line(before[name]) if name in before else None
        new = plugins.command_line(wanted[name]) if name in wanted else None
        if old == new:
            continue
        if old is None:
            print(f"    + {ui.accent(name)}  {new}")
        elif new is None:
            print(f"    - {ui.accent(name)}  {old}")
        else:
            print(f"    ~ {ui.accent(name)}  {old}  →  {new}")


def plugin_remove_command(argv: list[str]) -> int:
    from . import plugins

    parser = argparse.ArgumentParser(
        prog="xiaoyu plugin remove",
        description="卸掉一个插件包：删安装目录、摘掉它装的 MCP 声明、清账本。",
    )
    parser.add_argument("name", help="插件包名")
    args = parser.parse_args(argv)

    try:
        dropped = plugins.drop_mcp(args.name)
        removed = plugins.uninstall_dir(args.name)
    except plugins.PluginError as exc:
        print(ui.error(str(exc)), file=sys.stderr)
        return 2
    except OSError as exc:
        print(ui.error(f"删不掉：{exc}"), file=sys.stderr)
        return 1
    plugins.forget(args.name)
    if not removed and not dropped:
        print(ui.error(f"没有装过叫 {args.name} 的插件包"), file=sys.stderr)
        return 2
    print(ui.success(f"已卸掉 {args.name}"))
    if dropped:
        print(ui.secondary("  一并摘掉的 MCP server：" + "、".join(dropped)))
    return 0


def make_frontend(permissions: Permissions, no_tui: bool = False):
    """选择交互前端，返回 (approver, sink, repl_fn, note, asker)。

    TUI 条件：未被 --no-tui 禁用 + stdin/stdout 都是真实终端 + 装了可选依赖
    （prompt_toolkit/rich）。任一不满足退回明文 REPL——sink=None 表示用
    Agent 默认的 PlainSink。note 是给用户的一行提示（当前仅"可装 TUI"）。
    asker 是 ask_user 工具的提问通道：TUI 走行内面板，明文 REPL 走编号问答
    ——交互前端总有人在，提问永远可用；headless 才是 None（工具不进 schemas）。
    """
    interactive = sys.stdin.isatty() and sys.stdout.isatty()
    if no_tui or not interactive:
        return make_confirm(permissions), None, repl, None, text_ask_questions
    #  探背景色定深浅配色。必须赶在建 Tui 之前——RichSink 构造时就把主题挂到
    #  Console 上了；也赶在横幅之前，那是全程唯一"用户还没开始打字"的窗口
    #  （探测要临时切 raw 模式读回答，抢跑的按键会被吃掉几个字符）
    terminal.autodetect()
    try:
        from . import tui
    except ImportError:
        #  只在交互场景提示：管道/CI 里刷这行只会碍事
        return (
            make_confirm(permissions),
            None,
            repl,
            '提示：pip install "xiaoyu-agent[tui]" 可获得补全/历史/粘贴折叠',
            text_ask_questions,
        )
    front = tui.Tui(permissions)
    return front.confirm, front.sink, front.run, None, front.ask


def compose_prompt(arg_words: list[str], piped: str) -> str:
    """拼一次性指令：管道内容在前当材料、命令行参数在后当任务。

    `git diff | xiaoyu "写 commit message"` 里模型先看到 diff 再看到任务，
    和人读邮件"先材料后要求"的顺序一致。只有管道没有参数时，管道内容就是指令。
    """
    prompt = " ".join(arg_words).strip()
    if piped:
        return f"{piped}\n\n{prompt}" if prompt else piped
    return prompt


def read_piped_stdin() -> str:
    """stdin 是管道/重定向时读完并返回内容，是终端时返回空串（绝不阻塞等输入）。"""
    if sys.stdin.isatty():
        return ""
    try:
        return sys.stdin.read().strip()
    except OSError:
        return ""


def ask_one_text(item: dict[str, Any], position: str = "") -> str | None:
    """明文形态问一题：编号列表 + input()。返回答案；None = 用户收工不再答。

    也是 TUI 面板起不来时的逐题回退（与确认框的 _confirm_text 同一条纪律：
    提问永远可用）。数字=选对应项（多选可空格分隔多个），其它文本=自由回答，
    回车/Ctrl-C/EOF=不答了。
    """
    multi = bool(item.get("multi_select"))
    suffix = f"（{position}）" if position else ""
    print(ui.warning(f"  ? {item['question']}{suffix}"))
    labels = [str(option.get("label", "")) for option in item["options"]]
    for number, option in enumerate(item["options"], start=1):
        description = str(option.get("description", "") or "")
        tail = f" — {description}" if description else ""
        print(ui.secondary(f"    {number}. {labels[number - 1]}{tail}"))
    hint = "数字多选可空格分隔；" if multi else "数字选择；"
    try:
        answer = input(ui.secondary(f"    回答（{hint}其它文本=自由回答；回车=不答了）：")).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return None
    if not answer:
        return None
    tokens = answer.split()
    if all(token.isdigit() and 1 <= int(token) <= len(labels) for token in tokens):
        picked = [labels[int(token) - 1] for token in tokens]
        return ", ".join(picked if multi else picked[:1])
    return answer


def text_ask_questions(questions: list[dict[str, Any]]) -> dict[str, str]:
    """明文 REPL 的 asker（ask_user 工具）：顺序逐题问，语义与 TUI 面板对齐。"""
    answers: dict[str, str] = {}
    total = len(questions)
    for number, item in enumerate(questions, start=1):
        answer = ask_one_text(item, position=f"{number}/{total}" if total > 1 else "")
        if answer is None:
            break
        answers[item["question"]] = answer
    return answers


def make_headless_deny():
    """非交互一次性模式的 approver：没人能按 y，一律拒绝并告诉模型为什么。

    注意分工：permissions 的 allow 规则和 --yolo 在 Agent._execute 里先于
    approver 生效——无人值守要放行的工具靠预先 /allow 或 --yolo，不靠这里。
    拒绝理由回灌给模型，让它改用免确认工具或向用户交代清楚，而不是撞 EOF 静默失败。
    """

    def confirm(name: str, args: dict[str, Any]) -> str:
        return (
            "当前是非交互模式，没有人能确认这次调用，已自动拒绝。"
            "请尽量用免确认工具完成任务；确实绕不开时，在最终回复里说明需要哪个工具，"
            "并建议用户配置 allow 规则（/allow）或加 --yolo 重跑。"
        )

    return confirm


def oneshot_frontend(permissions: Permissions, output_format: str):
    """一次性模式的 (approver, sink)。sink=None 表示用 Agent 默认的 PlainSink。

    - json：stdout 只准出现最后那个 JSON 对象 → 全程静默；
    - stream-json：事件流本身就是输出 → NDJSON sink；
    - text 且 stdin 是终端：照常交互确认（原有行为）；
    - text 但 stdin 已被管道占用：input() 只会撞 EOF → 同样 headless 拒绝。
    """
    if output_format == "stream-json":
        return make_headless_deny(), JsonlSink()
    if output_format == "json":
        return make_headless_deny(), NullSink()
    if not sys.stdin.isatty():
        return make_headless_deny(), None
    return make_confirm(permissions), None


def open_session(config: Config, session_id: str | None) -> tuple[SessionLog, list[dict[str, Any]]]:
    """按 `--session-id` 决定开新会话还是续写同名会话。返回 (日志, 待接回的历史)。

    没给名字就是老行为：每次一个新文件。给了名字则"有则续、无则建"——
    脚本按固定名字反复调，上下文自然接上，盘上仍只有一个文件。
    名字不合规、或会话文件格式比本版新，都抛 ValueError（消息面向用户）。
    """
    if not session_id:
        return SessionLog.create(config.model, str(config.workspace)), []
    name = check_session_id(session_id)
    return open_named(name, config.model, str(config.workspace))


def warn_if_home_workspace(workspace: Path) -> None:
    """工作区是用户主目录时提醒一句：产物会直接撒在主目录里。

    真实会话里用户在 C:\\Users\\<名字> 下直接启动，生成的 HTML、二维码图片
    全落在主目录根上。只提醒不阻拦——一次性快问快答在哪跑都无妨。
    """
    try:
        if workspace.resolve() != Path.home().resolve():
            return
    except OSError:
        return
    print(
        ui.warning(
            "注意：当前工作区是用户主目录，小羽产出的文件会直接落在主目录里。"
            "建议 cd 到项目目录再启动，或用 --workspace 指定。"
        ),
        file=sys.stderr,
    )


def resolve_folder_trust(
    workspace: Path, *, grant: bool, interactive: bool
) -> "folder_trust.TrustDecision":
    """启动期的 folder trust 门（见 folder_trust.py 模块 docstring）。

    必须在 load_dotenv 之前调用：工作区 .env 是被门管的对象，先读了再问
    等于门形同虚设。--trust 先记后判：记完 evaluate 自然走"信任表命中"。
    """
    from . import folder_trust

    if grant:
        key = folder_trust.workspace_key(workspace)
        if folder_trust.record_decision(key, True) is None:
            print(
                ui.warning(f"--trust：{key} 过宽（家目录/文件系统根），不记录信任"),
                file=sys.stderr,
            )
    decision = folder_trust.evaluate(workspace, interactive)
    if decision.verdict == "prompt":
        trusted = folder_trust.ask_user(decision)
        decision = folder_trust.TrustDecision(
            "trusted" if trusted else "untrusted", decision.key, decision.kinds
        )
    if decision.verdict == "untrusted":
        print(ui.warning(folder_trust.untrusted_note(decision)), file=sys.stderr)
    return decision


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    #  崩溃面包屑：守护进程/后台线程/原生 segfault 的无声崩溃留一条痕迹。
    #  只在 CLI 入口装，不在 import 时装（嵌入宿主自管 excepthook）。
    from . import crash_guard

    crash_guard.install()
    #  子命令拦截：nargs="*" 的 prompt 位置参数和 subparsers 不兼容，手动分流
    if argv and argv[0] == "config":
        return config_command(argv[1:])
    if argv and argv[0] == "resume":
        return resume_command(argv[1:])
    if argv and argv[0] == "sessions":
        return sessions_command(argv[1:])
    if argv and argv[0] == "send":
        return send_command(argv[1:])
    if argv and argv[0] == "mcp":
        return mcp_command(argv[1:])
    if argv and argv[0] == "serve":
        return serve_command(argv[1:])
    if argv and argv[0] == "acp":
        #  子命令形态与 `--acp` 旗标完全等价：转写成旗标再走主解析器，两条路
        #  共用同一套参数、folder trust 门与 wire/acp 互斥检查，永不漂移。
        #  两种写法都留：编辑器/registry 的配置模板惯用子命令，`--acp` 是
        #  既有集成（含 ACP registry 提交物）的入口，属永久别名不做废弃。
        return main(["--acp", *argv[1:]])
    if argv and argv[0] in ("plugin", "plugins"):
        return plugin_command(argv[1:])
    if argv and argv[0] == "terminal-setup":
        return terminal_setup_command(argv[1:])
    if argv and argv[0] == "doctor":
        return doctor_command(argv[1:])
    if argv and argv[0] == "update":
        return update_command(argv[1:])
    if argv and argv[0] == "uninstall":
        return uninstall_command(argv[1:])
    args = build_parser().parse_args(argv)
    #  folder trust 门必须先于 load_dotenv（工作区 .env 是被门管的对象）。
    #  交互判定：stdin 与 stderr 都是 tty 才算——wire/管道/重定向都
    #  走 headless 分支（不问，直接不信任 + 告警）。
    if args.wire and args.acp:
        print(ui.error("--wire 与 --acp 是两套协议，一次只能开一个"), file=sys.stderr)
        return 2
    gate_workspace = Path(args.workspace).expanduser() if args.workspace else Path.cwd()
    trust = resolve_folder_trust(
        gate_workspace,
        grant=args.trust,
        interactive=not (args.wire or args.acp) and sys.stdin.isatty() and sys.stderr.isatty(),
    )
    env_files = load_dotenv(
        Path(args.env_file).expanduser() if args.env_file else None,
        untrusted_dir=None if trust.trusted else gate_workspace,
    )

    #  wire/acp 模式：stdin 是协议通道，绝不能被当成管道指令读掉
    if args.wire:
        return wire_main(args, workspace_trusted=trust.trusted)
    if args.acp:
        return acp_main(args)

    #  管道输入即指令：`git diff | xiaoyu "写 commit message"`
    prompt = compose_prompt(prompt_words(args), read_piped_stdin())
    if args.prompt_opt is not None and not prompt:
        #  `-p` 明确表了态"这是一次性模式"，却没给指令：掉进交互 REPL 会更让人意外
        print(
            ui.error("-p 是一次性模式：指令写在 -p 后面，或从管道给（`cat 材料 | xy -p`）"),
            file=sys.stderr,
        )
        return 2
    if args.output_format != "text" and not prompt:
        print(
            ui.error("--output-format json/stream-json 只用于一次性模式（给出指令或从管道输入）"),
            file=sys.stderr,
        )
        return 2

    workspace = Path(args.workspace).expanduser() if args.workspace else Path.cwd()
    if not workspace.is_dir():
        print(ui.error(f"工作区不存在：{workspace}"), file=sys.stderr)
        return 2
    warn_if_home_workspace(workspace)

    try:
        config = Config.from_env(
            workspace=workspace,
            model=args.model,
            base_url=args.base_url,
            auto_approve=args.yolo or None,
            mode=args.mode,
            sandbox=args.sandbox,
            sandbox_network=args.sandbox_network,
            append_system_prompt=args.append_system_prompt,
            workspace_trusted=trust.trusted,
        )
        permissions = Permissions.load(config.workspace, include_workspace=trust.trusted)
        #  一次性模式不进 TUI：输出常被管道/重定向接走，格式由 --output-format 决定
        if prompt:
            approver, sink = oneshot_frontend(permissions, args.output_format)
            repl_fn, note, asker = repl, None, None
        else:
            approver, sink, repl_fn, note, asker = make_frontend(permissions, args.no_tui)
        try:
            session_log, restored = open_session(config, args.session_id)
        except ValueError as exc:  # 会话名不合规 / 会话文件格式比本版新
            print(ui.error(str(exc)), file=sys.stderr)
            return 2
        peer = register_peer(config, interactive=not prompt)
        agent = Agent(
            config,
            build_toolbox(config, peer),
            approver=approver,
            session_log=session_log,
            permissions=permissions,
            sink=sink,
            asker=asker,
            peer=peer,
        )
    except MissingConfig as exc:
        print(ui.error(str(exc)), file=sys.stderr)
        return 2
    install_exit_logging(agent.session_log)
    #  copy=False：续写的就是历史所在那个文件，再抄一遍等于每次调用翻倍
    agent.restore(restored, copy=False)
    resumed = f"已接上会话 {args.session_id}（{len(restored)} 条消息）" if restored else ""

    if prompt:
        if resumed and args.output_format == "text":
            print(ui.secondary(resumed))
        return run_once(agent, prompt, args.output_format)
    print(build_banner(model_label(agent), str(config.workspace)))
    if env_files:
        print(ui.secondary("已加载 " + ", ".join(str(p) for p in env_files)))
    print(ui.secondary(sandbox_status(config)))
    if budget_note := skills.budget_warning():
        print(ui.secondary(budget_note))
    if resumed:
        #  接回上下文却不回放，人进来是两眼一抹黑——与 resume 同一条纪律
        print(ui.secondary(resumed))
        replay_recent(agent, restored)
    if note:
        print(ui.secondary(note))
    return run_repl(repl_fn, agent)


def wire_main(args: argparse.Namespace, workspace_trusted: bool = True) -> int:
    """`--wire` 入口：构造 server → agent（approver/sink 都指向 server）→ 阻塞服务。

    与一次性模式同一套 Config 装配；不打横幅、不碰 TUI——stdout 上只有协议。
    人读的错误照旧走 stderr。
    """
    from .wire import WireServer

    if prompt_words(args):
        print(ui.error("--wire 模式不接受命令行指令（用协议里的 prompt 方法）"), file=sys.stderr)
        return 2
    workspace = Path(args.workspace).expanduser() if args.workspace else Path.cwd()
    if not workspace.is_dir():
        print(ui.error(f"工作区不存在：{workspace}"), file=sys.stderr)
        return 2
    try:
        config = Config.from_env(
            workspace=workspace,
            model=args.model,
            base_url=args.base_url,
            auto_approve=args.yolo or None,
            mode=args.mode,
            sandbox=args.sandbox,
            sandbox_network=args.sandbox_network,
            append_system_prompt=args.append_system_prompt,
            workspace_trusted=workspace_trusted,
        )
        permissions = Permissions.load(config.workspace, include_workspace=workspace_trusted)
        try:
            session_log, restored = open_session(config, args.session_id)
        except ValueError as exc:  # 会话名不合规 / 会话文件格式比本版新
            print(ui.error(str(exc)), file=sys.stderr)
            return 2
        server = WireServer()
        agent = Agent(
            config,
            Toolbox(config),
            approver=server.approve,
            session_log=session_log,
            permissions=permissions,
            sink=server.sink,
        )
    except MissingConfig as exc:
        print(ui.error(str(exc)), file=sys.stderr)
        return 2
    server.attach(agent)
    install_exit_logging(agent.session_log)
    #  wire 侧不打招呼——stdout 只有协议；接回的条数由 initialize 的 messages 字段说
    agent.restore(restored, copy=False)
    return server.serve()


def acp_main(args: argparse.Namespace) -> int:
    """`xiaoyu acp` / `--acp` 入口：Agent Client Protocol server（见 acp.py 模块 docstring）。

    与 wire 的结构差异：ACP 的工作区随 session/new 的 cwd 来（编辑器一个
    项目一个 session），所以 Agent 不在启动期装配，而是给 server 一个工厂。
    工厂本身在 acp.build_agent_factory——嵌入宿主与 CLI 共用同一份装配链，
    这里只负责把命令行旗标翻成它的参数。
    """
    from .acp import AcpServer, build_agent_factory

    if prompt_words(args):
        print(ui.error("acp 模式不接受命令行指令（用协议里的 session/prompt）"), file=sys.stderr)
        return 2

    return AcpServer(
        build_agent_factory(
            model=args.model,
            base_url=args.base_url,
            append_system_prompt=args.append_system_prompt,
            auto_approve=args.yolo or None,
            mode=args.mode,
            sandbox=args.sandbox,
            sandbox_network=args.sandbox_network,
        )
    ).serve()


def run_once(agent: Agent, prompt: str, output_format: str = "text") -> int:
    error = ""
    try:
        agent.send(prompt)
    except KeyboardInterrupt:
        if agent.session_log:
            agent.session_log.event("interrupt")
        if output_format == "text":
            print(ui.warning("\n[已中断]"))
            return 130
        error = "interrupted"
    except Exception as exc:  # noqa: BLE001 - JSON 消费方要结构化错误，不是 traceback
        if agent.session_log:
            agent.session_log.event("error", error=f"{type(exc).__name__}: {exc}")
        if output_format == "text":
            raise
        error = f"{type(exc).__name__}: {exc}"

    if output_format == "text":
        #  一次性模式也把用量打出来：做了模型路由就得看得见每个模型花了多少
        print(ui.secondary(f"\n{agent.usage}"))
        return 0

    #  json / stream-json 的收尾对象结构相同；stream-json 带 kind 与事件流同一词汇
    payload: dict[str, Any] = {
        "result": agent.last_assistant_text(),
        "usage": agent.usage.to_dict(),
        "model": agent.config.model,
    }
    if agent.session_log:
        payload["session_log"] = str(agent.session_log.path)
    if error:
        payload["error"] = error
    if output_format == "stream-json":
        payload = {"kind": "result", **payload}
    print(json.dumps(payload, ensure_ascii=False), flush=True)
    if error:
        return 130 if error == "interrupted" else 1
    return 0


#  ! 直跑输出进入上下文的截断上限：模型只需要知道结果，不需要整卷日志
_SHELL_CONTEXT_CAP = 8000


@dataclass(frozen=True)
class ShellResult:
    """`user_shell` 的执行结果，打印归前端。"""

    command: str
    returncode: int
    stdout: str
    stderr: str


def user_shell(agent: Agent, command: str) -> ShellResult:
    """! 前缀的核心：跑用户自己敲的命令，结果灌进对话上下文。

    这是用户自己敲的命令，不过权限关卡（权限管的是模型的手，不是用户的手）、
    不进沙箱——和用户自己开个终端跑一模一样，只是结果顺手喂给了模型。
    打印归前端（TUI rich / 明文 REPL 各自渲染），Ctrl-C 也由前端接。
    """
    proc = subprocess.run(
        command,
        shell=True,
        cwd=agent.config.workspace,
        capture_output=True,
        text=True,
        #  这里刻意用 locale 编码而非全仓通用的 UTF-8：跑的是用户自己的
        #  shell，输出该按用户终端的编码理解（中文 Windows 的 cmd.exe 就是
        #  GBK）。replace 兜底，猜错顶多花屏，不会把 REPL 炸掉。
        encoding=locale.getpreferredencoding(False),
        errors="replace",
    )
    combined = proc.stdout + (("\n" + proc.stderr) if proc.stderr else "")
    combined = combined.strip()
    if len(combined) > _SHELL_CONTEXT_CAP:
        combined = combined[:_SHELL_CONTEXT_CAP] + "\n…（输出过长，进入上下文的部分已截断）"
    agent._record(  # noqa: SLF001 - 前端与 Agent 同包，历史注入专用
        {
            "role": "user",
            "content": (
                "（我刚在终端手动执行了命令，结果供你参考，不必回应）\n"
                f"$ {command}\n退出码 {proc.returncode}\n{combined or '（无输出）'}"
            ),
        }
    )
    return ShellResult(command, proc.returncode, proc.stdout, proc.stderr)


def user_memo(agent: Agent, note: str) -> str:
    """# 前缀的核心：备忘追加进项目指令文件，返回落盘文件名。

    追加到工作区第一个存在的指令文件（AGENTS.md / XIAOYU.md / CLAUDE.md），
    都没有则新建 XIAOYU.md。指令文件在会话开始时已拼进 system prompt
    （之后不重建），所以同时把这句话灌进当前对话——文件管以后的会话，
    灌话管这一个。写入失败的 OSError 上抛，由前端打印。
    """
    workspace = agent.config.workspace
    target = next(
        (workspace / name for name in Agent._PROJECT_DOC_NAMES if (workspace / name).is_file()),  # noqa: SLF001
        workspace / "XIAOYU.md",
    )
    fresh = not target.exists()
    with target.open("a", encoding="utf-8") as handle:
        if fresh:
            handle.write("# 项目备忘\n")
        handle.write(f"- {note}\n")
    agent._record(  # noqa: SLF001 - 同上
        {"role": "user", "content": f"（备忘，已写入 {target.name}，照此执行，不必回应）：{note}"}
    )
    return target.name


def _repl_shell(agent: Agent, command: str) -> None:
    """明文 REPL 的 ! 前缀外壳：调核心 + 明文打印（文案与 TUI 对齐）。"""
    print(ui.accent(f"$ {command}"))
    try:
        result = user_shell(agent, command)
    except KeyboardInterrupt:
        print(ui.warning("[命令被 Ctrl-C 中断，输出未能捕获]"))
        return
    if result.stdout:
        print(result.stdout, end="" if result.stdout.endswith("\n") else "\n")
    if result.stderr:
        print(ui.error(result.stderr.rstrip("\n")))
    print(ui.secondary(f"  （退出码 {result.returncode}，输出已进入上下文）"))


def _repl_memo(agent: Agent, note: str) -> None:
    """明文 REPL 的 # 前缀外壳：调核心 + 明文打印（文案与 TUI 对齐）。"""
    try:
        target = user_memo(agent, note)
    except OSError as exc:
        print(ui.error(f"  写入失败：{exc}"))
        return
    print(ui.secondary(f"  已记入 {target}（本会话即刻生效，之后的会话自动加载）"))


def print_mode_notice(agent: Agent) -> None:
    """非默认档时在开场打一行：这一档到底会不会问你。

    默认档不打——它就是横幅之外的基线，说了是噪音。auto 档而沙箱不可用时
    `modes.describe` 会自己改口说降级，不必在这里判。
    """
    if agent.mode == modes.DEFAULT:
        return
    print(ui.warning(modes.describe(agent.mode, sandbox_ready=agent.sandbox_ready())))


def background_status(agent: Agent) -> str:
    """轮次结束后的 still-running 状态行（inline 架构下打一行即走）。"""
    tasks = getattr(agent.toolbox, "tasks", None)
    return tasks.still_running_line() if tasks is not None else ""


def repl(agent: Agent) -> int:
    config = agent.config
    #  模型/工作区/help 提示都在启动横幅里了，这里只留必须扎眼的警告
    if config.auto_approve:
        print(ui.error("--yolo 已开启：写文件和执行命令都不会再问你"))
    print_mode_notice(agent)

    while True:
        try:
            line = input(ui.prompt(f"{modes.prompt_prefix(agent.mode)}› "))
        except (EOFError, KeyboardInterrupt):
            print()
            return 0

        #  提交路由单点在 keys.classify_input：TUI 与明文 REPL 同一张表
        action = keys.classify_input(line)
        if action.kind == "empty":
            continue
        if action.kind == "usage":
            print(ui.secondary(f"  {action.hint}"))
            continue
        if action.kind == "slash":
            if handle_slash(agent, action.args):
                return 0
            continue
        if action.kind == "shell":
            _repl_shell(agent, action.args)
            continue
        if action.kind == "memo":
            _repl_memo(agent, action.args)
            continue

        try:
            agent.send(action.args)
        except KeyboardInterrupt:
            agent.close_open_tool_calls("用户按 Ctrl-C 中断了本轮。")
            print(ui.warning("\n[已中断，可以继续输入]"))
        except Exception as exc:  # noqa: BLE001 - REPL 不该因为一次请求失败就退出
            if agent.session_log:
                agent.session_log.event("error", error=f"{type(exc).__name__}: {exc}")
            print(ui.error(f"\n请求失败：{type(exc).__name__}: {exc}"))
        if note := background_status(agent):
            print(ui.secondary(note))
        print()


def _rewind_flow(agent: Agent, rest: list[str]) -> None:
    """/rewind 的交互流程：列点 → 选点 → 选范围 → 冲突确认 → 执行。

    快照只覆盖 write_file / str_replace 的改动；bash 里改的、git 操作不在
    范围内——列表下方明说，不装作全能。
    """
    store = getattr(agent.toolbox, "rewind", None)
    points = store.points() if store is not None else []
    if not points:
        print(ui.secondary("  本会话还没有可回滚的快照点（每轮开始时自动打点）。"))
        return

    print("  可回滚的轮次（恢复到该轮**开始前**的状态）：")
    for point in reversed(points):
        stamp = time.strftime("%H:%M", time.localtime(point.started_at))
        touched = f"，改动 {len(point.files)} 个文件" if point.files else ""
        print(f"  {point.index:>3}. [{stamp}] {point.preview}{touched}")
    print(ui.secondary("  （快照只覆盖 write_file/str_replace；bash 改动与 git 操作不在内）"))

    choice = rest[0] if rest else input(ui.prompt("  回滚到第几轮前（回车取消）› ")).strip()
    if not choice:
        return
    try:
        index = int(choice)
    except ValueError:
        print(ui.warning("  要一个轮次编号。"))
        return
    if store.get(index) is None:
        print(ui.warning(f"  没有编号为 {index} 的快照点。"))
        return

    print("  回滚范围：1=对话+文件（默认） 2=仅对话 3=仅文件")
    scope = input(ui.prompt("  › ")).strip() or "1"
    conversation = scope in ("1", "2")
    files = scope in ("1", "3")
    if scope not in ("1", "2", "3"):
        print(ui.warning("  只认 1/2/3。"))
        return

    if files:
        conflicts = store.conflicts(index)
        if conflicts:
            print(ui.warning("  以下文件在轮次之外被改动过（手改/外部进程），恢复会覆盖："))
            for raw in conflicts[:8]:
                print(ui.warning(f"    {raw}"))
        skipped = store.skipped_from(index)
        if skipped:
            print(ui.warning(f"  另有 {len(skipped)} 个超大文件没有快照，无法恢复。"))
        if conflicts and input(ui.prompt("  仍要恢复文件吗？[y/N] › ")).strip().lower() not in (
            "y",
            "yes",
        ):
            files = False
            if not conversation:
                return

    result = agent.rewind_to(index, conversation=conversation, files=files)
    print(ui.success(f"  {result}"))
    if conversation:
        print(ui.secondary("  （屏幕上方的旧输出只是显示残留，模型已不记得被截掉的轮次）"))


def handle_slash(agent: Agent, line: str, select: Any = None) -> bool:
    """处理斜杠命令。返回 True 表示应该退出。

    select 是前端注入的行内单选菜单（签名同 tui.inline_select，无附言形态），
    /resume 这类要列表选择的命令用它；明文 REPL 不传，自动退回编号输入。
    """
    parts = line.split()
    command, rest = parts[0], parts[1:]

    if command in ("/exit", "/quit"):
        return True
    if command == "/help":
        print(SLASH_HELP)
    elif command == "/keys":
        #  内容全部从 keys.BINDINGS 渲染：这里不复述任何按键，避免又一处会漂移的文案
        print(keys.help_text())
        if not _tui_available():
            print(ui.secondary("  当前是明文 REPL，上表只在 TUI 前端生效"))
            print(ui.secondary('  装上可选依赖即可：pip install "xiaoyu-agent[tui]"'))
    elif command == "/tools":
        for name in agent.toolbox.names():
            tool = agent.toolbox.get(name)
            flag = ui.warning(" [需确认]") if tool and tool.requires_approval else ""
            if tool and not tool.available():
                flag += ui.error(" [不可用]")
            print(f"  {name}{flag}")
        print(ui.secondary("  " + sandbox_status(agent.config)))
    elif command in ("/rewind", "/undo"):
        _rewind_flow(agent, rest)
    elif command == "/tasks":
        tasks = getattr(agent.toolbox, "tasks", None)
        entries = tasks.all() if tasks is not None else []
        if not entries:
            print(ui.secondary("  没有后台任务（bash 的 run_in_background、monitor 工具会出现在这里）"))
        for task in entries:
            mark = "monitor " if task.kind == "monitor" else ""
            if task.done.is_set():
                state = f"{task.status}（exit {task.exit_code}）"
            else:
                state = f"running（{task.elapsed():.0f}s）"
            print(f"  {task.task_id}  {mark}{state}  {task.description}")
            print(ui.secondary(f"    日志：{task.log_path}"))
    elif command == "/mcp":
        from . import mcp

        if not agent.config.enable_mcp:
            print(ui.secondary("  MCP 已被 XIAOYU_ENABLE_MCP=0 关闭"))
        else:
            manager = mcp.launch(agent.config)
            if manager is None:
                print(ui.secondary("  " + mcp.McpManager.usage_hint().replace("\n", "\n  ")))
            elif rest and rest[0] == "approve":
                if len(rest) < 2:
                    print(ui.warning("  用法：/mcp approve <server名>（批准变更工具，解除隔离）"))
                else:
                    print(ui.secondary(f"  {manager.approve(rest[1])}"))
            else:
                print(ui.secondary("  " + manager.describe().replace("\n", "\n  ")))
    elif command == "/skills":
        if rest and rest[0] == "reload":
            #  显式全量刷新：重扫磁盘 + 重建 system prompt 索引。cache 前缀因此
            #  作废一次（下轮全价），这是用户主动要的，明说即可。被动通道
            #  （轮首差量检测）平时已自动跟进增删，这条给"想立刻看到索引"的人
            added, removed = agent.reload_skills()
            if not added and not removed:
                print(ui.secondary(f"  索引无变化（共 {len(agent.skills)} 个技能）"))
            else:
                if added:
                    print(ui.secondary(f"  新增：{'、'.join(added)}"))
                if removed:
                    print(ui.secondary(f"  移除：{'、'.join(removed)}"))
                print(ui.secondary("  索引已重建（本轮 prompt cache 前缀作废，下一轮起重新累积）"))
        else:
            if not agent.skills:
                print(ui.secondary("  没有发现技能。放到 ~/.agents/skills/<名字>/SKILL.md 即可被识别，"))
                print(ui.secondary("  或用 xiaoyu plugin add <owner/repo> 装一个插件包"))
            for skill in agent.skills:
                #  插件技能标出来源：名字里虽然带了包名前缀，但"这是装来的、能 update"
                #  和"这是我自己写的"是两回事
                origin = ui.secondary(f"  [插件 {skill.plugin}]") if skill.plugin else ""
                print(f"  {skill.name}  {ui.secondary(skill.description or str(skill.path))}{origin}")
    elif command == "/model":
        if rest:
            agent.switch_model(rest[0])
            print(ui.secondary(f"已切换到 {rest[0]}"))
        else:
            print(ui.secondary(f"当前模型 {agent.config.model}"))
            #  直连是靠环境变量静默启用的——不把来源印出来，用户根本不知道
            #  请求走的哪条路、钱花在哪家。
            print(agent.registry.describe())
            #  网关上有什么要现场问 /v1/models 才知道；只在此刻探测，启动仍零往返
            for label, models, note in agent.registry.remote_models():
                if models is None:
                    print(ui.secondary(f"  {label}清单获取失败：{note}"))
                    continue
                print(ui.secondary(f"  {label}可用模型（现场探测）："))
                for name in models:
                    print(f"    {name}")
            chain = " → ".join(route.qualified for route in agent.model_chain())
            print(ui.secondary(f"降级链：{chain}"))
    elif command == "/usage":
        print(ui.secondary(str(agent.usage)))
    elif command == "/context":
        used = agent.context_tokens()
        limit = agent.config.context_limit
        #  显示前同步：/model 切换后 compactor 里还是旧模型的上限
        agent.compactor.context_limit = limit
        budget = agent.compactor.budget()
        state = agent.compactor.state
        bar = "█" * int(20 * min(1.0, used / limit)) or "▏"
        print(
            ui.secondary(
                f"  {bar}  {used} / {limit} tok（{used / limit:.0%}）\n"
                f"  压缩阈值 {budget} tok · 已压缩 {state.count} 次"
                f"（上次省 {state.saved_tokens} tok）\n"
                f"  消息 {len(agent.messages)} 条 · 估算依据：{agent.context_source()}\n"
                f"  摘要模型链：{' → '.join(r.qualified for r in agent.summary_models())}"
            )
        )
        #  归因：上下文都花在哪，按字符降序。让用户无需自己拆解拼装逻辑即可审计。
        breakdown = sorted(agent.context_breakdown(), key=lambda kv: kv[1], reverse=True)
        total_chars = sum(chars for _, chars in breakdown) or 1
        print(ui.secondary("  ── 归因（字符）──"))
        for label, chars in breakdown:
            if chars == 0:
                continue
            print(ui.secondary(f"    {label:<16} {chars:>8}  {chars / total_chars:>4.0%}"))
    elif command == "/compact":
        note = agent.maybe_compact(force=True)
        print(ui.secondary(f"  {note or '无需压缩'}"))
    elif command == "/mode":
        if not rest:
            print(ui.secondary(f"  当前：{modes.describe(agent.mode, sandbox_ready=agent.sandbox_ready())}"))
            print(ui.secondary(modes.help_text()))
            print(ui.secondary("  /mode default|auto|plan 切换（TUI 里 Shift-Tab 同效）"))
        elif rest[0] in modes.BY_NAME:
            print(ui.secondary(f"  {agent.set_mode(rest[0])}"))
        else:
            print(ui.warning(f"  未知模式 {rest[0]}；可选：{'、'.join(modes.CYCLE)}"))
    elif command == "/plan":
        if rest and rest[0] == "on":
            print(ui.secondary(f"  {agent.enter_plan_mode()}"))
        elif rest and rest[0] == "off":
            print(ui.secondary(f"  {agent.leave_plan_mode()}"))
        elif rest:
            print(ui.warning("  用法：/plan on 开启只读规划态，/plan off 退出"))
        else:
            state = "开启（只读规划态）" if agent.plan_mode else "关闭"
            print(ui.secondary(f"  plan mode 当前{state}；/plan on|off 切换"))
    elif command == "/perm":
        print(ui.secondary(f"当前模式：{modes.describe(agent.mode, sandbox_ready=agent.sandbox_ready())}"))
        print(ui.secondary(agent.permissions.describe()))
    elif command in ("/allow", "/deny"):
        rule = parse_rule(f"{command[1:]} {' '.join(rest)}") if rest else None
        if rule is None:
            print(ui.warning(f"规则格式：{command} bash(git *) 或 {command} write_file"))
        else:
            try:
                path = agent.permissions.add_persistent(rule)
            except ValueError as exc:
                #  持久 allow 不许覆盖任意代码执行入口（banned prefixes）
                print(ui.error(f"已拒绝：{exc}"))
            else:
                print(ui.success(f"已写入 {path}：{rule}"))
    elif command == "/resume":
        slash_resume(agent, rest, select)
    elif command == "/clear":
        agent.reset()
        print(ui.secondary("对话已清空"))
    else:
        print(ui.warning(f"未知命令 {command}，/help 看可用命令"))
    return False


def sandbox_status(config: Config) -> str:
    """一行沙箱状态：默默生效的保护要看得见，不然出问题时没人想得到它。"""
    from . import sandbox

    if not config.sandbox:
        return "沙箱：已关闭（--no-sandbox）——bash 命令可写任意路径"
    if not sandbox.available():
        import sys

        if sys.platform == "linux":
            #  Linux 上"不可用"分两种：没装 bwrap，或装了但内核/AppArmor
            #  禁了 unprivileged user namespace——都指向同一条出路
            return (
                "沙箱：未生效（需安装 bubblewrap 且内核允许 unprivileged user namespace），"
                "bash 命令可写任意路径"
            )
        return "沙箱：本平台不支持（macOS/Linux 可用；Windows 建议在 WSL 里用），bash 命令可写任意路径"
    network = "允许联网" if config.sandbox_network else "禁止联网"
    return f"沙箱：已启用 · 只可写工作区/临时目录/构建缓存 · 全盘可读 · {network}"


#  确认框答 a 的哨兵。不用字符串——用户完全可能拿任意单词（哪怕就是 "grant"）
#  当拒绝理由，字符串哨兵会把理由误判成授权。
GRANT_SESSION = object()


def interpret_confirm_answer(answer: str) -> bool | str | object:
    """确认框输入 → 判定（明文与 TUI 两个前端共用的单一语义源）：
    y/yes=允许；GRANT_SESSION=本会话该工具全允许（调用方负责落会话授权并提示）；
    空/n/no=拒绝；其它任意文本=拒绝并把原文当理由回灌模型。"""
    lowered = answer.strip().lower()
    if lowered == "a":
        return GRANT_SESSION
    if lowered in ("y", "yes"):
        return True
    if lowered in ("", "n", "no"):
        return False
    return answer.strip()


def make_confirm(permissions: Permissions):
    """构造交互式确认函数。y=允许一次，a=本次会话该工具全部允许，
    回车/n=拒绝，**其它任意文本=拒绝并把原文当理由回灌模型**
    （拒绝即改指令——用户在确认框随手打的一句话，
    比干巴巴的"被拒绝了"有用得多，模型能直接按它改道）。

    a 的记忆存进 permissions 的会话授权（v0.9 的 bug：a 只放行了一次就忘了，
    因为 confirm 是无状态函数、没有地方落这个决定——现在闭包持有权限存储）。
    """

    def confirm(name: str, args: dict[str, Any]) -> bool | str:
        if name == "write_file":
            content = str(args.get("content", ""))
            head = content.split("\n")[:12]
            print(ui.secondary("  ┌ 将写入：" + str(args.get("path", ""))))
            for row in head:
                print(ui.secondary(f"  │ {ui.fit(row, 6)}"))
            if content.count("\n") > 12:
                print(ui.secondary(f"  └ …还有 {content.count(chr(10)) - 12} 行"))
        elif name == "str_replace":
            print(ui.secondary("  ┌ 将修改：" + str(args.get("path", ""))))
            for row in str(args.get("old_str", "")).split("\n")[:8]:
                print(ui.error(f"  │ - {ui.fit(row, 6)}"))
            for row in str(args.get("new_str", "")).split("\n")[:8]:
                print(ui.success(f"  │ + {ui.fit(row, 6)}"))
            print(ui.secondary("  └"))
        elif name == "bash":
            #  破坏性操作 / 参数注入口在确认框里点名：用户看到的是"为什么要多想一下"
            if reason := command_check.command_risk(str(args.get("command", ""))):
                print(ui.warning(f"  ⚠ 注意：{reason}"))

        try:
            answer = input(
                ui.warning(f"  允许执行 {name}? [y/N/a=本会话都允许，其它输入=拒绝理由] ")
            ).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return False

        verdict = interpret_confirm_answer(answer)
        if verdict is GRANT_SESSION:
            permissions.grant_session(name)
            print(ui.secondary(f"  （本次会话内 {name} 不再逐次确认；/perm 可查看）"))
            return True
        return verdict

    return confirm


if __name__ == "__main__":
    raise SystemExit(main())
