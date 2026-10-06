"""小羽的命令行入口。"""

from __future__ import annotations

import argparse
import atexit
import importlib
import importlib.util
import json
import locale
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import (
    __version__, attention, command_check, envprobe, errors, keys, media, modes, peers,
    providers, skills, terminal, ui,
)
from .agent import Agent
from .banner import build_banner
from .term import SESSION_PREFIX as TERM_SESSION_PREFIX
from .session_log import (
    LoadedMessages,
    SessionInfo,
    SessionLockedError,
    SessionLog,
    check_session_id,
    export_markdown,
    export_messages,
    find_by_id,
    find_by_name,
    find_session,
    install_exit_logging,
    list_sessions,
    load_messages,
    load_system_prompt,
    looks_like_session_id,
    open_named,
    rename_session,
    turn_starts,
    usage_digest,
)
from .config import (
    DEFAULT_MODEL,
    DEFAULT_SUMMARY_MODEL,
    EFFORT_LEVELS,
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
from .permissions import Permissions, parse_rule, rule_lint
from .render import REPLAY_TURNS, JsonlSink, NullSink, replay_transcript
from .tools import Toolbox

#  斜杠命令 → 一行描述。单一事实源：/help 文案与 TUI 补全菜单的 meta 都从这里生成
SLASH_COMMANDS: dict[str, str] = {
    "/help": "显示这个帮助",
    "/keys": "按键与输入前缀速查",
    "/tools": "列出已注册的工具",
    "/tasks": "后台任务列表（run_in_background 的命令 / monitor）",
    "/mcp": "MCP server 状态；/mcp diff [名] 看变更工具的差异；/mcp approve [名] 批准（无参 = 全部）；/mcp reconnect [名] 修好后重读配置热恢复（无参 = 全部失败的）",
    "/skills": "列出可用技能；/skills reload 重扫磁盘并刷新索引",
    "/<技能名>": "把技能展开成本轮提示（/<技能名> 参数…，正文里的 $ARGUMENTS / $1 / $名字 用参数填）；与内建命令撞名时写 /skill:<技能名>",
    "/model": "查看或切换模型（/model 名字）",
    "/search": "查看或切换搜索后端（/search deepseek|xai|bedrock，仅当前会话）",
    "/usage": "本次会话的 token 统计",
    "/effort": "查看或设置推理深度（/effort low|medium|high|xhigh|max）",
    "/context": "当前上下文占用与压缩状态",
    "/compact": "立刻压缩历史（不等阈值）",
    "/mode": "切换模式（/mode default|auto|plan），TUI 里 Shift-Tab 同效",
    "/plan": "只读规划态开关（/plan on|off）：/mode plan 的别名",
    "/goal": "本会话的验收目标（/goal <一句话> 设定、/goal 看当前、/goal clear 清除）：模型收尾前会被要求核对是否达成",
    "/perm": "查看权限规则与会话授权",
    "/allow": "持久允许，如 /allow bash(git *)、/allow write_file",
    "/deny": "持久拒绝（任何模式下都拦，包括 --yolo）",
    "/resume": "切到本工作区的历史会话（当前对话被清空；/resume <序号> 直接选）",
    "/rewind": "回滚到某轮开始前（对话和/或文件；/undo 同义）",
    "/clear": "清空对话历史（保留 system prompt）",
    "/exit": "退出",
    "/quit": "退出",
}

def effort_mismatch(agent: Agent, level: str) -> str:
    """当前模型登记过认哪几档推理深度、而这一档不在其中时，返回一句提醒；否则空串。

    只提醒不拦：用户给自己点名的模型配的档位是原样发的（不认就让上游报错，
    不替他改）。但登记表就在手边，当场说一声好过等第一次请求才收到一句 400。
    没登记过的模型不知道它认什么，不猜。
    """
    if not level:
        return ""
    try:
        route = agent.registry.resolve(agent.config.model)
    except providers.UnknownModel:
        return ""
    accepted = providers.accepted_efforts(route.provider, route.model)
    if not accepted or level in accepted:
        return ""
    return (
        f"  {route.qualified} 登记的档位是 {' / '.join(accepted)}，{level} 多半会被上游拒绝"
        "——仍按你的设置发送，被拒就换一档"
    )


SLASH_HELP = "可用命令：\n" + "\n".join(
    f"  {command:<10} {description}" for command, description in SLASH_COMMANDS.items()
) + "\n"


#  子命令表：分发（main）与 --help 的清单共用这一张。各写各的时候 serve 能分发、
#  帮助里却没有这一项。每行：(名字与别名, 处理函数, 帮助里的写法, 一句话说明)。
#  处理函数按名字在分发时再取（见 subcommand_handler）：裸名字取本模块的，
#  "模块.函数" 取同包别的模块——住在自己模块里的子命令族（mcp / plugin / term）
#  到分发那一刻才导入，启动不为用不到的子命令付代价，它们回头 import 本模块也不成环
SUBCOMMANDS: tuple[tuple[tuple[str, ...], str, str, str], ...] = (
    (("config",), "config_command", "config", "初始化/查看配置"),
    (("resume",), "resume_command", "resume", "恢复历史会话"),
    (("sessions",), "sessions_command", "sessions [digest|export|rename|inspect]",
     "列出本机会话；digest 汇总用量、export 导出、rename 起名、inspect 诊断日志"),
    (("send",), "send_command", "send <会话> <消息>", "给另一个会话发一条消息"),
    (("mcp",), "cli_mcp.mcp_command", "mcp add|list|remove|probe", "管理 MCP server 声明；probe 不经模型直接探测一个 server"),
    (("term",), "cli_term.term_command", "term install|init|run|command|log|info",
     "shell 集成：term install 一条命令接入（连同 Tab 补全）；之后在自己的 shell 里 @x 提问，带上刚跑过的命令；@c 一句话换一条命令"),
    (("plugin", "plugins"), "cli_plugin.plugin_command", "plugin add|list|update|remove",
     "装卸插件包（skills + MCP）"),
    (("serve",), "serve_command", "serve", "以 HTTP API 服务启动（需 [serve] 可选依赖）"),
    (("acp",), "_acp_command", "acp", "以 ACP 协议 server 启动，供编辑器客户端驱动（等价 --acp）"),
    (("doctor",), "doctor_command", "doctor [--probe] [--bundle]",
     "体检环境（凭据有无 / 配置 / 代理 / 沙箱 / 磁盘 / MCP 配置）；--probe 真发一条请求，--bundle 打诊断包"),
    (("completion",), "completion_command", "completion bash|zsh|fish",
     "输出 shell 补全脚本（eval \"$(xiaoyu completion zsh)\"；term install 会一并写好）"),
    (("terminal-setup",), "terminal_setup_command", "terminal-setup",
     "给 VS Code 系编辑器配 Shift+Enter 换行"),
    (("update",), "update_command", "update",
     "升级到最新版（未装 TUI 时自动补上；已装 serve 时一并升级）"),
    (("uninstall",), "uninstall_command", "uninstall", "卸载（--purge 连配置目录一起删）"),
)


def subcommand_handler(handler: str):
    """SUBCOMMANDS 里的处理函数名 → 可调用对象（裸名字在本模块，"模块.函数" 在同包）。"""
    module_name, _, name = handler.rpartition(".")
    if not module_name:
        return globals()[name]
    return getattr(importlib.import_module(f".{module_name}", __package__), name)


def subcommand_help() -> str:
    """--help 末尾的子命令清单（从 SUBCOMMANDS 渲染，对齐按可见宽度算）。"""
    width = max(ui.display_width(usage) for _, _, usage, _ in SUBCOMMANDS)
    lines = ["子命令（各自带 --help）："]
    lines += [
        f"  xiaoyu {ui.pad(usage, width)}  {summary}" for _, _, usage, summary in SUBCOMMANDS
    ]
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="xiaoyu",
        description="小羽 — 一个 harness coding agent",
        epilog=subcommand_help(),
        #  清单是排好版的：默认的格式化会把它重新折成一段
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("prompt", nargs="*", help="直接执行一条指令后退出（不进交互模式）")
    add_prompt_flag(parser)
    parser.add_argument(
        "--image",
        dest="images",
        action="append",
        metavar="PATH",
        help="随指令附一张图片，可重复（仅一次性模式；交互模式里 Ctrl-V 直接贴）",
    )
    parser.add_argument(
        "--paste",
        action="store_true",
        help="把系统剪贴板里的图片随指令一起发（仅一次性模式）",
    )
    parser.add_argument("--version", action="version", version=f"xiaoyu {__version__}")
    parser.add_argument("--model", help="模型名，默认 deepseek-flash")
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
    add_system_prompt_flags(parser)
    parser.add_argument(
        "--effort",
        choices=list(EFFORT_LEVELS),
        help="推理深度（默认不传、随上游默认）；会话里可用 /effort 改",
    )
    parser.add_argument(
        "--budget-tokens",
        dest="budget_tokens",
        type=int,
        help="本会话 token 软预算（prompt+completion 累计）：模型会收到倒计时并在到线前收尾",
    )
    parser.add_argument(
        "--goal",
        metavar="TEXT",
        help="验收目标（一句话）：模型收尾前会被要求核对它是否完全达成，未达成继续做；会话里同 /goal",
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
        help="起始模式：auto=工作区内改文件与沙箱内命令免确认（出厂默认）；default=逐条确认；plan=只读规划态",
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
    add_guardrail_flags(parser)
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
    add_stats_flag(parser)
    return parser


def add_system_prompt_flags(parser: argparse.ArgumentParser) -> None:
    """system prompt 的四个旗标，主命令与 resume 共用。"""
    parser.add_argument(
        "--system-prompt",
        dest="system_prompt",
        metavar="TEXT",
        help="自定义 system prompt，直接给文本；与 --system-prompt-file 同义（两者只能给一个）",
    )
    parser.add_argument(
        "--system-prompt-file",
        dest="system_prompt_file",
        metavar="PATH",
        help="用文件内容作为自定义 system prompt：顶替内置的身份与回答风格，"
        "工具使用纪律、环境信息、项目指令与技能索引照常保留；续会话时不必重复给",
    )
    parser.add_argument(
        "--append-system-prompt",
        dest="append_system_prompt",
        help="追加到内置 system prompt 末尾，用于宿主进程嵌入 xiaoyu 时注入身份/人格",
    )
    parser.add_argument(
        "--append-system-prompt-file",
        dest="append_system_prompt_file",
        metavar="PATH",
        help="同 --append-system-prompt，内容从文件读（两者只能给一个）",
    )


def _read_prompt_file(flag: str, spec: str) -> str:
    """读提示词文件：块级 HTML 注释不发给模型（约定见 promptfile.py）。

    没闭合的注释、没填完的 `{{…}}` 占位符只在 stderr 提醒，不改内容也不拦启动。
    """
    from . import promptfile

    path = Path(spec).expanduser()
    try:
        loaded = promptfile.load(path)
    except (OSError, UnicodeDecodeError) as exc:
        raise ValueError(f"{flag} 读不了 {path}：{exc}") from exc
    if not loaded.text:
        raise ValueError(f"{flag} 指向的文件是空的（或只有注释）：{path}")
    for note in loaded.warnings(flag):
        print(ui.warning(note), file=sys.stderr)
    return loaded.text


def resolve_system_prompt_flags(args: argparse.Namespace) -> None:
    """把两个 -file 旗标读成文本，落到 args.system_prompt / args.append_system_prompt。

    读不了、文件为空、同一份提示词的文本与文件两种写法同时给，都抛 ValueError
    （消息面向用户）——启动期就报，别等装配完 agent 才发现提示词没生效。
    """
    if args.system_prompt_file:
        if args.system_prompt is not None:
            raise ValueError("--system-prompt 与 --system-prompt-file 只能给一个")
        args.system_prompt = _read_prompt_file("--system-prompt-file", args.system_prompt_file)
    elif args.system_prompt is not None:
        #  文本写法原样使用（不剥注释，与 --append-system-prompt 一致）；
        #  空串照文件为空处理——静默退回内置身份会让人以为顶替生效了
        if not args.system_prompt.strip():
            raise ValueError("--system-prompt 给的是空文本")
    else:
        args.system_prompt = None
    if args.append_system_prompt_file:
        if args.append_system_prompt:
            raise ValueError("--append-system-prompt 与 --append-system-prompt-file 只能给一个")
        args.append_system_prompt = _read_prompt_file(
            "--append-system-prompt-file", args.append_system_prompt_file
        )


def add_guardrail_flags(parser: argparse.ArgumentParser) -> None:
    """护栏开关：主命令与 resume 共用（表在 guardrails.py）。

    --unattended 单独放开 --yolo 下仍必问的三项；--unguarded 是预设：一次放开表里
    全部层，且只在 XIAOYU_UNGUARDED=1 时生效（见 resolve_guardrail_flags）。
    """
    from . import guardrails

    parser.add_argument(
        "--unattended",
        action="store_true",
        default=None,
        help="--yolo 之上再放开退出 plan、沙箱升权、写可执行配置这三处必问（无人值守里没人按键）",
    )
    parser.add_argument(
        guardrails.FLAG,
        dest="unguarded",
        action="store_true",
        help=f"无护栏预设：放开端侧全部可关护栏（等价 --yolo --no-sandbox --unattended "
        f"XIAOYU_HARDLINE=0 XIAOYU_MCP_TRUST_CHANGES=1 并跳过工作区信任门）。"
        f"只在环境变量 {guardrails.CONSENT_ENV}=1 时生效——由沙箱编排脚本注入，不读 .env",
    )


def resolve_guardrail_flags(args: argparse.Namespace) -> None:
    """把 --unguarded 落到 args 上（各 Config.from_env 站点照旧从 args 取值）。

    没有环境同意就抛 ValueError（消息面向用户）：给了旗标又不生效的静默失败
    是最坏的形态——用户以为在无护栏跑，其实每条命令还在等确认。
    """
    from . import guardrails

    #  resume 解析器没有 --no-sandbox / 主解析器有：两边都给字段一个落点
    for name in ("hardline", "mcp_trust_changes", "sandbox"):
        if not hasattr(args, name):
            setattr(args, name, None)
    if not getattr(args, "unguarded", False):
        return
    if not guardrails.consented():
        raise ValueError(guardrails.missing_consent())
    guardrails.apply_args(args)


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


def collect_image_parts(
    paths: list[str] | None, paste: bool
) -> tuple[list[dict[str, Any]], str]:
    """`--image`/`--paste` → 图片部件列表。(parts, 出错原因)，出错即整单失败。

    这里的报错纪律与管线内回图**刻意相反**：MCP 工具回图而模型看不了，降级成
    一行文字说明（fail-closed 的温和面，一张图不值得掀翻整轮对话）；而这两个
    旗标是用户**显式**要发图，任何一张落空都硬报错退出——显式意图被静默吞掉
    是最难自查的失败形态。所以 -- 一张读不了就整单不发，不做"发得出几张算几张"。
    """
    parts: list[dict[str, Any]] = []
    for raw in paths or []:
        ref, problem = media.accept_file(Path(raw).expanduser())
        if problem:
            return [], f"--image {raw}：{problem}"
        parts.append(media.image_part(ref))
    if paste:
        clip = media.clipboard()
        if clip.problem:
            return [], f"--paste：{clip.problem}"
        #  位图（截图）与"复制的图片文件"都收；剪贴板里其它类型的文件不猜用途
        pasted = 0
        for data in clip.images:
            ref, problem = media.accept(data, "剪贴板图片")
            if problem:
                return [], f"--paste：{problem}"
            parts.append(media.image_part(ref))
            pasted += 1
        for path in clip.files:
            if not media.is_image_path(path):
                continue
            ref, problem = media.accept_file(path)
            if problem:
                return [], f"--paste：{problem}"
            parts.append(media.image_part(ref))
            pasted += 1
        if not pasted:
            return [], "--paste：剪贴板里没有图片（截图或复制图片文件后再试）"
    return parts, ""


def add_stats_flag(parser: argparse.ArgumentParser) -> None:
    """`--stats`：轮末在用量行后追加耗时 / 首 token / 吐字速率。主命令与 resume 共用。"""
    parser.add_argument(
        "--stats",
        action="store_true",
        help="每轮结束在用量行后追加耗时、首 token 延迟与输出速率（-p 与交互模式都认）",
    )


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
    parser.add_argument(
        "--output-schema",
        dest="output_schema",
        metavar="FILE|JSON",
        help="要求模型以符合该 JSON Schema 的对象收尾（文件路径或内联 JSON）；"
        "结果放在 json/stream-json 收尾对象的 output 字段，text 模式单独打印一行 JSON。"
        "只用于一次性模式",
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
        help="非交互写入一项配置，可重复（如 --set XIAOYU_MODEL=deepseek-flash）",
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
        if preset.region_env:
            #  区域型（Bedrock）：key 可选（Bedrock API key），没 key 就问区域、
            #  凭据走 AWS 自己的链；两样任一有就算配上了
            if existing:
                print(ui.secondary(f"已检测到 {preset.name} API key（永不回显）。"))
            elif key := ask_key(
                f"{preset.name} API key（模型 {'、'.join(preset.models)}；"
                "留空 = 不用 key，改走 AWS 凭证链）"
            ):
                values[preset.key_envs[0]] = key
                existing = True
            current = providers.bedrock_region()
            tip = (
                f"{preset.name}：AWS 区域"
                + ("" if existing else "（凭据走 AWS 默认凭证链；留空 = 不用 Bedrock）")
                + (f"（当前 {current}，回车 = 沿用）" if current else "")
            )
            if region := ask(preset.region_env, tip):
                values[preset.region_env] = region
            has_direct = has_direct or existing or bool(region or current)
            continue
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


def resume_hint(agent: Agent) -> str:
    """开场与退出时的一行：接回本会话的完整命令，复制即用。

    id 就是会话文件名（见 looks_like_session_id）——同一个 id 也是
    `xiaoyu sessions inspect/export` 的引用，拿去给别的工具分析这场会话。
    """
    log = getattr(agent, "session_log", None)  # run_repl 的测试替身没有它
    return f"接回本会话：xiaoyu resume {log.path.stem}" if log is not None else ""


def _session_label(info: SessionInfo) -> str:
    """会话在行内菜单里的一行标签（截到终端宽度，长了菜单高度就不准了）。"""
    place = Path(info.workspace).name or info.workspace
    return ui.fit(f"{info.started_at}  {info.model}  {place}  {_named(info)}{_session_text(info)}", 12)


def _session_text(info: SessionInfo) -> str:
    """列表里的正文：起过名用名字，否则首条消息开头；再带末条摘要——"想干什么"
    与"聊到哪了"一起看，挑会话续聊才不用逐个打开。末条与开头相同（单轮会话）不重复。"""
    head = info.label
    if info.last and not info.last.startswith(info.preview.rstrip("…")):
        return f"{head}  ⇢ {info.last}"
    return head


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
            f"{ui.secondary(place)}  {_named(info)}{_session_text(info)}"
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


def session_start_refused(agent: Agent) -> bool:
    """SessionStart 钩子（hooks.toml）在首轮之前跑一次；拦截 = 拒绝启动。

    放在 run_once / run_repl 的开头而不是各个 main 里：新会话与 resume 两条装配
    链最后都汇到这两个入口，钩子只该有一处触发点。接回的历史此时已经 restore
    完，钩子注入的那行环境说明排在本次会话的输入前面。
    """
    #  getattr：这两个入口的测试用极简替身充当 agent，钩子不是替身关心的事
    begin = getattr(agent, "begin_session", None)
    decision = begin() if begin is not None else None
    if decision is None or not decision.blocked:
        return False
    print(ui.error(f"SessionStart hook 拒绝启动：{decision.reason}"), file=sys.stderr)
    return True


def end_session(agent: Agent) -> None:
    """SessionEnd 钩子（hooks.toml）：会话收尾时一次，与 session_start_refused 成对。"""
    end = getattr(agent, "end_session", None)
    if end is not None:
        end()


def run_repl(repl_fn: Any, agent: Agent) -> int:
    """跑 REPL，退出时抹掉会话登记（抹不掉也无妨：心跳一停别人自会清理）。"""
    if session_start_refused(agent):
        return 2
    try:
        return repl_fn(agent)
    finally:
        end_session(agent)
        if agent.peer is not None:
            agent.peer.close()
        #  开场那行早被滚出屏幕了：退出时再给一次，复制最方便。没说过话的会话
        #  不给——resume 它只会得到「没有可恢复的消息」
        messages = getattr(agent, "messages", [])
        if any(m.get("role") == "user" for m in messages) and (hint := resume_hint(agent)):
            print(ui.secondary(hint))


def sessions_command(argv: list[str]) -> int:
    """`xiaoyu sessions`：列出本机在跑的小羽会话。

    一行一个、`·` 分隔（不排表格：inline TUI 的
    调性是紧凑）。列的是**可寻址性**——名字就是地址，`[ref]` 只在重名时才用得上。
    `xiaoyu sessions digest` 是历史维度：跨会话的 token 用量账本。
    """
    if argv and argv[0] == "digest":
        return sessions_digest_command(argv[1:])
    if argv and argv[0] == "export":
        return sessions_export_command(argv[1:])
    if argv and argv[0] == "rename":
        return sessions_rename_command(argv[1:])
    if argv and argv[0] == "inspect":
        return sessions_inspect_command(argv[1:])
    parser = argparse.ArgumentParser(
        prog="xiaoyu sessions",
        description=(
            "列出本机在跑的小羽会话（可作为 `xiaoyu send` 的收件人）。"
            "`xiaoyu sessions digest` 汇总历史会话的 token 用量；"
            "`xiaoyu sessions export <会话>` 导出一场历史会话；"
            "`xiaoyu sessions rename <会话> <名字>` 给它起个显示名。"
            "`xiaoyu sessions inspect <会话>` 查看执行时间线。"
        ),
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


def sessions_digest_command(argv: list[str]) -> int:
    """`xiaoyu sessions digest`：跨会话的 token 用量账本，按工作区聚合。

    回答"配额花在哪个项目、哪个模型上"。数据源是会话文件里轮末落盘的
    累计 usage 快照——没有快照的文件（旧版本记录 / 零调用）与跳过的损坏行
    都如实报数，不静默：沉默会暗示"全都算进来了"。
    """
    parser = argparse.ArgumentParser(
        prog="xiaoyu sessions digest",
        description="汇总历史会话的 token 用量（按工作区 × 模型）。",
    )
    parser.add_argument("--workspace", help="只看这个工作区（默认全部）")
    args = parser.parse_args(argv)
    digest = usage_digest(workspace=args.workspace)
    ranked = sorted(
        digest.by_workspace.items(),
        key=lambda item: item[1].prompt_tokens + item[1].completion_tokens,
        reverse=True,
    )
    if not ranked:
        print(ui.secondary("没有带用量记录的历史会话。"))
    else:
        total_sessions = sum(entry.sessions for _, entry in ranked)
        total_in = sum(entry.prompt_tokens for _, entry in ranked)
        total_out = sum(entry.completion_tokens for _, entry in ranked)
        print(ui.heading(
            f"用量账本（{total_sessions} 个会话 · "
            f"in {total_in:,} tok / out {total_out:,} tok）："
        ))
        for workspace, entry in ranked:
            print(
                f"  {ui.accent(shorten_home(workspace) or '（未知工作区）')}"
                + ui.secondary(
                    f"  ·  {entry.sessions} 会话"
                    f"  ·  in {entry.prompt_tokens:,} / out {entry.completion_tokens:,}"
                )
            )
            models = sorted(
                entry.by_model.items(), key=lambda item: item[1][1] + item[1][2], reverse=True
            )
            for model, (calls, prompt, completion) in models:
                print(ui.secondary(
                    f"    {model}: {calls} 次 · in {prompt:,} / out {completion:,}"
                ))
    #  诚实行：观察不到的部分要说出来
    notes = []
    if digest.no_usage:
        notes.append(f"{digest.no_usage} 个会话文件无用量记录（旧版本记录或未产生调用）")
    if digest.corrupt:
        notes.append(f"跳过 {digest.corrupt} 行疑似损坏的用量记录")
    if notes:
        print(ui.secondary("  （" + "；".join(notes) + "）"))
    return 0


def _session_ref_help() -> str:
    return "会话引用：`xiaoyu resume` 列表里的序号、--session-id 的名字，或会话文件名"


def _locate_session(ref: str, everywhere: bool) -> SessionInfo | None:
    info = find_session(ref, None if everywhere else str(Path.cwd().resolve()))
    if info is None:
        print(ui.error(f"找不到会话 {ref!r}（{_session_ref_help()}；--all 不按当前工作区过滤）"), file=sys.stderr)
    return info


def sessions_inspect_command(argv: list[str]) -> int:
    """按物理行号查看日志；支持存档路径与现有会话引用。"""
    from .session_inspect import inspect_session, render_report

    parser = argparse.ArgumentParser(prog="xiaoyu sessions inspect", description="只读查看会话执行时间线，默认脱敏。")
    parser.add_argument("ref", help=_session_ref_help() + "，也可直接给 JSONL 路径")
    parser.add_argument("--all", action="store_true", help="跨工作区查找会话")
    parser.add_argument("--kind", action="append", default=[], help="按类型筛选，可重复：request、tool、approval、compact…")
    parser.add_argument("--turn", type=int, help="用户输入段序号（0 为前言，插话也另起一段）")
    parser.add_argument("--request", type=int, help="全文件中的请求序号，从 1 开始")
    parser.add_argument("--tool-call", help="工具调用 ID")
    parser.add_argument("--errors", action="store_true", help="只看错误、拒绝与空补全")
    parser.add_argument("--raw", action="store_true", help="附带完整记录字段（仍脱敏，可能含对话正文）")
    parser.add_argument("--json", action="store_true", help="输出结构化 JSON")
    parser.add_argument("--limit", type=int, default=200, help="过滤后保留最后多少条，默认 200")
    args = parser.parse_args(argv)
    if args.limit < 1 or (args.turn is not None and args.turn < 0) or (args.request is not None and args.request < 1):
        parser.error("limit、request 必须大于 0，turn 必须不小于 0")
    path = Path(args.ref).expanduser()
    if not path.exists():
        info = _locate_session(args.ref, args.all)
        if info is None:
            return 2
        path = info.path
    try:
        report = inspect_session(path, kinds=tuple(args.kind), turn=args.turn, request=args.request,
                                 tool_call=args.tool_call, errors_only=args.errors, raw=args.raw, limit=args.limit)
    except (OSError, ValueError) as exc:
        from .mcp import _redact
        print(ui.error(f"诊断失败：{_redact(str(exc))}"), file=sys.stderr)
        return 2
    print(json.dumps(report, ensure_ascii=True, indent=2) if args.json else render_report(report, raw=args.raw), end="\n" if args.json else "")
    return 0


def sessions_export_command(argv: list[str]) -> int:
    """`xiaoyu sessions export <会话>`：导出一场历史会话的用户可见内容。

    只含 user / assistant 正文与工具调用摘要（tool 结果一行摘要），不含 system
    提示——那是内核的内部话，不是对话本身。默认打到 stdout，`-o` 落文件。
    """
    parser = argparse.ArgumentParser(
        prog="xiaoyu sessions export",
        description="导出一场历史会话（Markdown 或 JSON），不含 system 提示。",
    )
    parser.add_argument("ref", help=_session_ref_help())
    parser.add_argument("--format", choices=("md", "json"), default="md", help="md（默认）或 json")
    parser.add_argument("-o", "--out", help="写到这个文件（默认 stdout）")
    parser.add_argument("--all", action="store_true", help="不按当前工作区过滤")
    args = parser.parse_args(argv)
    info = _locate_session(args.ref, args.all)
    if info is None:
        return 2
    try:
        if args.format == "json":
            payload = {
                "session": {
                    "path": str(info.path), "started_at": info.started_at, "model": info.model,
                    "workspace": info.workspace, "title": info.title, "session_id": info.session_id,
                },
                "messages": export_messages(info.path),
            }
            text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        else:
            text = export_markdown(info)
    except (OSError, ValueError) as exc:
        print(ui.error(f"导出失败：{exc}"), file=sys.stderr)
        return 2
    if args.out:
        target = Path(args.out).expanduser()
        if target.is_symlink():
            print(ui.error(f"输出路径是符号链接，拒绝写入：{target}"), file=sys.stderr)
            return 2
        target.write_text(text, encoding="utf-8")
        print(ui.success(f"已导出到 {target}"))
    else:
        print(text, end="")
    return 0


def sessions_rename_command(argv: list[str]) -> int:
    """`xiaoyu sessions rename <会话> <名字>`：给历史会话起显示名（列表里代替首条消息开头）。"""
    parser = argparse.ArgumentParser(
        prog="xiaoyu sessions rename",
        description="给一场历史会话起显示名；正在被别的进程续写的会话不能改。",
    )
    parser.add_argument("ref", help=_session_ref_help())
    parser.add_argument("title", help="新名字（最长 80 字符）")
    parser.add_argument("--all", action="store_true", help="不按当前工作区过滤")
    args = parser.parse_args(argv)
    info = _locate_session(args.ref, args.all)
    if info is None:
        return 2
    try:
        title = rename_session(info.path, args.title)
    except SessionLockedError as exc:
        print(ui.error(f"会话正在被续写，改不了名：{exc}"), file=sys.stderr)
        return 2
    except (OSError, ValueError) as exc:
        print(ui.error(f"改名失败：{exc}"), file=sys.stderr)
        return 2
    print(ui.success(f"已改名：{title}") + ui.secondary(f"  ({info.path.name})"))
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
        "index",
        nargs="?",
        help="列表里的序号、开场横幅给的会话 id，或具名会话的名字（--session-id 起的、终端集成的 term-…）；都不是就当指令（此时默认恢复最近会话）",
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
        help="起始模式：auto=沙箱兜得住的免确认（出厂默认）；default=逐条确认；plan=只读规划态",
    )
    parser.add_argument("--yolo", action="store_true", help="不再逐个确认写文件和执行命令")
    parser.add_argument("--no-tui", dest="no_tui", action="store_true", help="用明文 REPL")
    add_guardrail_flags(parser)
    add_system_prompt_flags(parser)
    add_prompt_flag(parser)
    add_output_format(parser)
    add_stats_flag(parser)
    args = parser.parse_args(argv)
    try:
        resolve_system_prompt_flags(args)
        resolve_guardrail_flags(args)
    except ValueError as exc:
        print(ui.error(str(exc)), file=sys.stderr)
        return 2
    #  folder trust 门（与 main 同一道，先于 load_dotenv；resume 的工作区就是 cwd）
    trust = resolve_folder_trust(
        Path.cwd(),
        grant=getattr(args, "trust", False),
        interactive=sys.stdin.isatty() and sys.stderr.isatty(),
        unguarded=args.unguarded,
    )
    load_dotenv(untrusted_dir=None if trust.trusted else Path.cwd())

    #  位置参数消歧：`resume 3 "继续"` 里 3 是序号，`resume "继续跑测试"` 里
    #  首个词是指令的开头——argparse 分不出来，按"纯数字=序号"判
    index, words = split_resume_positionals(args.index, args.prompt)
    #  开场横幅给的「会话 id」（会话文件名）：首个词长这样就是在点名会话，不是指令
    by_id: SessionInfo | None = None
    if index is None and args.index is not None and looks_like_session_id(args.index):
        by_id = find_by_id(args.index)
        if by_id is None:
            print(ui.error(f"找不到会话 {args.index}"), file=sys.stderr)
            return 2
        words = list(args.prompt)
    elif index is None and args.index is not None:
        #  具名会话的名字（`--session-id`、终端集成的 `term-…`）也算点名：`@x` 收尾那行
        #  给的就是它。名字找不到时，`term-` 开头的一定是在点名（没人拿它当指令开头），
        #  报错；别的词仍当指令的第一个词——「resume fix the bug」不该因为没叫 fix 的会话
        #  就被拒
        if (by_id := find_by_name(args.index)) is not None:
            words = list(args.prompt)
        elif args.index.startswith(TERM_SESSION_PREFIX):
            print(ui.error(f"找不到会话 {args.index}"), file=sys.stderr)
            return 2
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
    if args.output_schema and not prompt:
        print(ui.error("--output-schema 只用于一次性模式（给出指令或从管道输入）"), file=sys.stderr)
        return 2
    try:
        output_schema = load_output_schema(args.output_schema)
    except ValueError as exc:
        print(ui.error(str(exc)), file=sys.stderr)
        return 2

    workspace = Path.cwd().resolve()
    sessions = list_sessions(workspace=None if args.all else str(workspace))
    if not sessions and not args.all:
        #  当前工作区没有就退回全量列表，别让用户空手而归
        sessions = list_sessions()
    if not sessions and by_id is None:
        print(ui.warning("没有可恢复的会话。"))
        return 1

    if by_id is not None:
        chosen = by_id
    elif args.last:
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
            #  切片会丢掉读回时的损坏记录，带上它，restore 才能照常提示
            loaded = LoadedMessages(loaded[: starts[args.fork]], loaded.corrupt_lines)

    resume_workspace = Path(chosen.workspace) if Path(chosen.workspace).is_dir() else workspace
    #  会话记录的工作区可能不是 cwd：换了目录就按新目录重新过一遍门
    if resume_workspace.resolve() != Path.cwd().resolve():
        trust = resolve_folder_trust(
            resume_workspace,
            grant=False,
            interactive=sys.stdin.isatty() and sys.stderr.isatty(),
            unguarded=args.unguarded,
        )
    _warn_env_problems()
    try:
        config = Config.from_env(
            workspace=resume_workspace,
            auto_approve=args.yolo or None,
            mode=args.mode,
            sandbox=args.sandbox,
            hardline=args.hardline,
            unattended=args.unattended,
            mcp_trust_changes=args.mcp_trust_changes,
            unguarded=args.unguarded or None,
            #  没重新给旗标就沿用原会话那份自定义 system prompt：续的是同一场
            #  对话，身份不该悄悄变回内置的
            system_prompt=args.system_prompt or load_system_prompt(chosen.path),
            append_system_prompt=args.append_system_prompt,
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
    agent.show_stats = args.stats

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
        return run_once(agent, prompt, args.output_format, output_schema)
    print(build_banner(model_label(agent), str(config.workspace)))
    print_update_notice()
    if budget_note := skills.budget_warning():
        print(ui.secondary(budget_note))
    print(ui.secondary(f"已恢复会话（{len(loaded)} 条消息，来自 {chosen.path.name}{forked}）"))
    if hint := resume_hint(agent):
        #  恢复写进的是新文件：下次要接的是这一份（它自包含，接得上之前的全部）
        print(ui.secondary(hint))
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
        description="检查 Python / 配置目录 / 磁盘 / 凭据有无 / 出网代理 / 沙箱 / 命令解析器 / MCP 配置 / 会话目录",
    )
    parser.add_argument("--json", action="store_true", help="机器可读输出（含进程快照）")
    parser.add_argument("-w", "--workspace", default="", help="按哪个工作区检查（默认当前目录）")
    parser.add_argument(
        "--probe",
        action="store_true",
        help="对默认模型真发一条最小请求（会出网、花一点点 token），记耗时并按分类报错；默认不出网",
    )
    parser.add_argument(
        "--bundle",
        nargs="?",
        const="",
        default=None,
        metavar="SESSION",
        help="打诊断包：体检结果 + 脱敏配置 + 指定会话（缺省最近一场）日志尾部 + 崩溃日志 + 版本平台信息，"
        "写成一个 JSON。密钥脱敏，但含路径与命令历史，分享前自查",
    )
    parser.add_argument("-o", "--out", help="诊断包的输出路径（默认当前目录 xiaoyu-doctor-<时间>.json）")
    args = parser.parse_args(argv)

    workspace = Path(args.workspace).expanduser() if args.workspace else None
    checks = diagnostics.run_doctor(workspace)
    if args.probe:
        load_dotenv()
        checks.append(diagnostics.probe_model())
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
    if args.bundle is not None:
        session: Path | None = None
        if args.bundle:
            info = find_session(args.bundle, str((workspace or Path.cwd()).resolve()))
            if info is None:
                print(ui.error(f"找不到会话 {args.bundle!r}（{_session_ref_help()}）"), file=sys.stderr)
                return 2
            session = info.path
        try:
            out = diagnostics.build_bundle(
                checks, workspace, session, Path(args.out) if args.out else None
            )
        except (OSError, ValueError) as exc:
            print(ui.error(f"诊断包写入失败：{exc}"), file=sys.stderr)
            return 2
        #  --json 时 stdout 只准出现那个 JSON：人读的两行走 stderr
        where = sys.stderr if args.json else sys.stdout
        print(ui.success(f"诊断包已写到 {out}"), file=where)
        print(ui.warning(f"  {diagnostics.BUNDLE_NOTICE}"), file=where)
    return 1 if diagnostics.overall(checks) == "fail" else 0


# ---------- shell 补全 ----------

COMPLETION_SHELLS = ("bash", "zsh", "fish")


def completion_words() -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """补全词表：(子命令, 说明) 与 (主命令旗标, 说明)，都从现有的表/parser 现取——
    不另维护一份清单，加了子命令或旗标补全自动跟上。"""
    subcommands = [(name, summary) for names, _, _, summary in SUBCOMMANDS for name in names]
    flags: list[tuple[str, str]] = []
    for action in build_parser()._actions:  # noqa: SLF001 - argparse 没有公开的动作清单
        for option in action.option_strings:
            if option.startswith("--"):
                flags.append((option, (action.help or "").split("（")[0].split("：")[0][:60]))
    return subcommands, flags


def _sh_quote(text: str) -> str:
    return "'" + text.replace("'", "'\\''") + "'"


def completion_script(shell: str) -> str:
    """最简补全脚本：第一个词补子命令与旗标，之后补旗标。手写而不引入 argcomplete：
    一个运行期依赖换"补全子命令名"不划算，子命令自己的旗标也不逐个展开。"""
    subcommands, flags = completion_words()
    sub_names = " ".join(name for name, _ in subcommands)
    flag_names = " ".join(flag for flag, _ in flags)
    if shell == "bash":
        return (
            "# xiaoyu bash 补全：eval \"$(xiaoyu completion bash)\" 或存进 ~/.bash_completion.d/\n"
            "_xiaoyu_complete() {\n"
            "    local cur=\"${COMP_WORDS[COMP_CWORD]}\"\n"
            "    if [ \"$COMP_CWORD\" -eq 1 ]; then\n"
            f"        COMPREPLY=( $(compgen -W \"{sub_names} {flag_names}\" -- \"$cur\") )\n"
            "    else\n"
            f"        COMPREPLY=( $(compgen -W \"{flag_names}\" -- \"$cur\") )\n"
            "    fi\n"
            "}\n"
            "complete -o default -F _xiaoyu_complete xiaoyu\n"
        )
    if shell == "zsh":
        sub_specs = " ".join(_sh_quote(f"{name}:{summary}") for name, summary in subcommands)
        flag_specs = " ".join(_sh_quote(f"{flag}:{summary}" if summary else flag) for flag, summary in flags)
        return (
            "# xiaoyu zsh 补全：eval \"$(xiaoyu completion zsh)\"（需先 autoload -U compinit && compinit）\n"
            "_xiaoyu() {\n"
            "    local -a subs flags\n"
            f"    subs=({sub_specs})\n"
            f"    flags=({flag_specs})\n"
            "    if (( CURRENT == 2 )); then\n"
            "        _describe -t subcommands '子命令' subs\n"
            "    fi\n"
            "    _describe -t options '旗标' flags\n"
            "    _files\n"
            "}\n"
            "compdef _xiaoyu xiaoyu\n"
        )
    if shell == "fish":
        lines = ["# xiaoyu fish 补全：xiaoyu completion fish > ~/.config/fish/completions/xiaoyu.fish"]
        for name, summary in subcommands:
            lines.append(f"complete -c xiaoyu -n __fish_use_subcommand -a {name} -d {_sh_quote(summary)}")
        for flag, summary in flags:
            desc = f" -d {_sh_quote(summary)}" if summary else ""
            lines.append(f"complete -c xiaoyu -l {flag[2:]}{desc}")
        return "\n".join(lines) + "\n"
    raise ValueError(f"不支持的 shell：{shell}")


def completion_command(argv: list[str]) -> int:
    """`xiaoyu completion bash|zsh|fish`：把补全脚本打到 stdout。"""
    parser = argparse.ArgumentParser(
        prog="xiaoyu completion",
        description='输出 shell 补全脚本。用法：eval "$(xiaoyu completion zsh)"，'
        "或 fish：xiaoyu completion fish > ~/.config/fish/completions/xiaoyu.fish",
    )
    parser.add_argument("shell", choices=COMPLETION_SHELLS)
    args = parser.parse_args(argv)
    print(completion_script(args.shell), end="")
    return 0


def confirm_config_change(question: str, rerun: str) -> bool:
    """改用户个人配置（启动文件、编辑器键绑定）前的确认。

    读不到输入（stdin 不是终端：在 agent 的 bash 工具里、管道里运行）时说清楚
    为什么没改、该去哪儿运行——这时读输出的往往是模型，一句"没有改动"它看不出
    原因，会接着去申请沙箱升权或自己动手改文件。刻意不提 --yes：那条路在沙箱里
    照样写不进去，只会把它引向升权。
    """
    try:
        answer = input(ui.prompt(question)).strip().lower()
    except EOFError:
        print()
        print(ui.warning("没有改动：读不到确认输入（不是在交互终端里运行的）。"))
        print(ui.secondary(f"这会改用户的个人配置，要用户本人确认：请在自己的终端里运行 {rerun}"))
        return False
    except KeyboardInterrupt:
        print()
        return False
    if answer not in ("y", "yes"):
        print(ui.secondary("没有改动。"))
        return False
    return True


def report_config_write_error(target: str, exc: OSError, rerun: str) -> None:
    """写个人配置失败。权限被拒多半是在 agent 的沙箱里跑的：同样指回用户自己的终端。"""
    print(ui.error(f"  {target}：写入失败 {exc}"), file=sys.stderr)
    if isinstance(exc, PermissionError):
        print(
            ui.secondary(f"  在 agent 会话里运行时，沙箱不让写这些文件：请在自己的终端里运行 {rerun}"),
            file=sys.stderr,
        )


def _rerun_command(prefix: str, argv: list[str]) -> str:
    import shlex

    return " ".join([prefix, *(shlex.quote(arg) for arg in argv)])


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
    rerun = _rerun_command("xiaoyu terminal-setup", argv)
    if not args.yes:
        print(ui.secondary("  会先留一份 .bak 备份；已有的 shift+enter 绑定不会被覆盖。"))
        if not confirm_config_change(f"写入这 {len(todo)} 个文件？[y/N] ", rerun):
            return 1
    for plan in todo:
        try:
            print(ui.success("  " + editor_setup.apply(plan)))
        except OSError as exc:
            report_config_write_error(plan.editor.name, exc, rerun)
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


_SDK_DISTRIBUTION = "xiaoyu-agent-sdk"


def _sdk_installed() -> bool:
    """同一环境里装没装嵌入 SDK（独立发行包 xiaoyu-agent-sdk）。

    SDK 精确钉住同版本的 xiaoyu-agent：update 只升本体会让它的 pin 落空，
    uninstall 只卸本体会留下一个导入即坏的 SDK——两条命令都要带上它。
    按发行包元数据判断而非可导入性：要交给 pip 的正是这个包名。
    """
    from importlib.metadata import PackageNotFoundError, distribution

    try:
        distribution(_SDK_DISTRIBUTION)
    except PackageNotFoundError:
        return False
    return True


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
    spec 可以是空格分隔的多个包，子进程侧按空白切开。
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
    specs = ["xiaoyu-agent" + (f"[{','.join(extras)}]" if extras else "")]
    if _sdk_installed():
        specs.append(_SDK_DISTRIBUTION)
        print(ui.secondary("检测到嵌入 SDK（xiaoyu-agent-sdk），一并升级以保持同版本"))
    print(ui.secondary(f"当前 xiaoyu {__version__}，执行 pip install --upgrade {' '.join(specs)}"))
    if _defer_pip_to_detached("update", " ".join(specs)):
        print(ui.secondary("Windows 不让程序覆盖正在运行的自己，升级改在本进程退出后继续。"))
        print(ui.secondary("pip 输出会接着打在这个窗口里，跑完按一次 Enter 回到命令提示符。"))
        return 0
    #  spec 作为独立 argv 传入、不经 shell，[] 不会被展开，无需引号
    result = subprocess.run([sys.executable, "-m", "pip", "install", "--upgrade", *specs])
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
    parser.add_argument("--effort", choices=list(EFFORT_LEVELS), help="默认推理深度，agent 对象可覆盖")
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
        "--agent-mcp",
        dest="agent_mcp",
        choices=("off", "http", "all"),
        default="off",
        help="agent 对象能否自带 MCP server（mcp_servers 字段）：off=不收（默认）；"
        "http=只收远端 Streamable HTTP；all=stdio 也收（会在本机、沙箱之外起子进程）",
    )
    parser.add_argument(
        "--browser-timeout",
        dest="browser_timeout",
        type=float,
        default=60.0,
        help="浏览器桥：等扩展回一次工具调用结果的上限（秒），默认 60（见 docs/browser-bridge.md）",
    )
    parser.add_argument(
        "--cors-origin",
        dest="cors_origins",
        action="append",
        default=[],
        metavar="ORIGIN",
        help="允许跨源访问的浏览器 origin（可重复），如 https://console.example.com。"
        "Chrome 扩展 origin 默认已放行（见 --no-cors-extensions），不必逐个列。名单外不发 CORS 头；"
        "非浏览器客户端不需要。token 仍照常校验；无 token 时名单外的浏览器 origin 一律 403",
    )
    parser.add_argument(
        "--no-cors-extensions",
        dest="cors_extensions",
        action="store_false",
        help="不默认给 Chrome 扩展 origin（chrome-extension://<id>）发 CORS 头，只认 --cors-origin 白名单"
        "（默认放行：扩展 id 每台机器不同，没法预先列）",
    )
    parser.add_argument(
        "--state-dir",
        dest="state_dir",
        help="agent 对象 / 会话清单 / 会话日志的落盘目录，默认用户配置目录下的 serve/<root slug>/"
        "（Linux/macOS 为 ~/.config/xiaoyu/serve/…）",
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
        "--tls-cert",
        dest="tls_cert",
        help="TLS 证书（PEM）。与 --tls-key 成对给，服务改以 https 监听（uvicorn 直接终止 TLS）",
    )
    parser.add_argument(
        "--tls-key",
        dest="tls_key",
        help="TLS 私钥（PEM），与 --tls-cert 成对给",
    )
    parser.add_argument(
        "--public-url",
        dest="public_url",
        default="",
        help="对外地址，写进 OpenAPI schema 的 servers（运行中的 /openapi.json 与 --print-openapi 都用）。"
        "编排器在容器里或经反代访问时填（Docker Desktop 常用 http://host.docker.internal:8420）",
    )
    args = parser.parse_args(argv)

    root = (Path(args.workspace).expanduser() if args.workspace else Path.cwd()).resolve()
    if not root.is_dir():
        print(ui.error(f"工作区不存在：{root}"), file=sys.stderr)
        return 2
    #  TLS 两件必须成对：只给一个起不了 https，静默退回 http 会让人以为已加密
    if bool(args.tls_cert) != bool(args.tls_key):
        print(ui.error("--tls-cert 与 --tls-key 必须一起给"), file=sys.stderr)
        return 2
    tls_cert = tls_key = None
    if args.tls_cert:
        tls_cert = Path(args.tls_cert).expanduser()
        tls_key = Path(args.tls_key).expanduser()
        for label, path in (("--tls-cert", tls_cert), ("--tls-key", tls_key)):
            if not path.is_file():
                print(ui.error(f"{label} 指向的文件不存在：{path}"), file=sys.stderr)
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
        effort=args.effort or "",
        approval=args.approval,
        approval_timeout=args.approval_timeout,
        max_sessions=max(1, args.max_sessions),
        mcp=args.mcp,
        agent_mcp=args.agent_mcp,
        cors_origins=tuple(args.cors_origins),
        cors_extensions=args.cors_extensions,
        browser_timeout=max(1.0, args.browser_timeout),
        state_dir=Path(args.state_dir).expanduser().resolve() if args.state_dir else None,
        persist=args.persist,
        public_url=args.public_url,
        tls_cert=tls_cert,
        tls_key=tls_key,
    )
    try:
        if args.print_openapi:
            return print_openapi(cfg)
        if args.host in ("127.0.0.1", "::1", "localhost") or args.token:
            print(ui.success(f"xiaoyu serve → {cfg.scheme}://{args.host}:{args.port}  (root: {root})"))
            extra = " · MCP /mcp" if args.mcp else ""
            print(ui.secondary(f"  文档 /docs · schema /openapi.json{extra} · Ctrl+C 停"))
        return serve(cfg)
    except ServeUnavailable:
        print(
            ui.error("serve 需要 fastapi 和 uvicorn，当前环境没装。"),
            file=sys.stderr,
        )
        print(f"  {envprobe.install_hint('xiaoyu-agent[serve]')}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


def uninstall_command(argv: list[str]) -> int:
    """卸载小羽：pip uninstall 本体 + 收拾装完之后留下的东西。

    裸 `pip uninstall` 只删包，不会碰用户配置目录、terminal-setup 写进
    编辑器的键绑定和 term install 写进 shell 启动文件的那一段；这条命令把"从装上到用过"的全过程反向走一遍。
    默认保留配置目录（用户可能只是换环境重装），--purge 才连它一起删。
    pip 的讲究与 update_command 相同：走 `sys.executable -m pip`，
    pipx/uv tool 环境没有 pip 模块，只能给出对应命令让用户自己跑。
    """
    from . import editor_setup, shell_setup
    from .config import user_config_dir

    parser = argparse.ArgumentParser(
        prog="xiaoyu uninstall",
        description="卸载小羽（pip uninstall xiaoyu-agent），并移除 terminal-setup "
        "写入的编辑器键绑定、term install 写入 shell 启动文件的终端集成；--purge 连配置目录（会话记录、用户级 .env、MCP 配置等）一起删",
    )
    parser.add_argument("--purge", action="store_true", help="连配置目录一起删（默认保留，重装可复用）")
    parser.add_argument("--yes", action="store_true", help="不询问，直接执行")
    parser.add_argument("--dry-run", action="store_true", help="只看计划，不动任何东西")
    args = parser.parse_args(argv)

    #  先把要动的东西全部打出来，确认后才动手——和 terminal-setup 同一姿势
    plans = editor_setup.removal_plans()
    shell_plans = shell_setup.removal_plans()
    config_dir = user_config_dir()
    purge_target = config_dir if args.purge and config_dir.is_dir() else None
    pip_ok = (
        subprocess.run([sys.executable, "-m", "pip", "--version"], capture_output=True).returncode == 0
    )
    packages = ["xiaoyu-agent"] + ([_SDK_DISTRIBUTION] if _sdk_installed() else [])

    for plan in plans:
        print(f"  {ui.success('将移除')}  {plan.editor.name}：shift+enter 绑定（留 .bak 备份）")
        print(ui.secondary(f"        {plan.path}"))
    for shell_plan in shell_plans:
        print(f"  {ui.success('将移除')}  终端集成（留 .bak 备份）")
        print(ui.secondary(f"        {shell_plan.path}"))
    if purge_target is not None:
        print(f"  {ui.success('将删除')}  配置目录（会话记录、用户级 .env、MCP 配置等）")
        print(ui.secondary(f"        {purge_target}"))
    elif args.purge:
        print(ui.secondary(f"  配置目录不存在，无需删除：{config_dir}"))
    else:
        print(ui.secondary(f"  保留配置目录（--purge 可连它一起删）：{config_dir}"))
    if pip_ok:
        print(f"  {ui.success('将执行')}  pip uninstall {' '.join(packages)}")
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
    for shell_plan in shell_plans:
        try:
            print(ui.success("  " + shell_setup.apply_removal(shell_plan)))
        except OSError as exc:
            print(ui.error(f"  {shell_plan.path}：写入失败 {exc}"), file=sys.stderr)
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
    if _defer_pip_to_detached("uninstall", " ".join(packages)):
        print(ui.secondary("Windows 不让程序删掉正在运行的自己，卸包改在本进程退出后继续。"))
        print(ui.secondary("pip 输出会接着打在这个窗口里，跑完按一次 Enter 回到命令提示符。"))
        return 0
    result = subprocess.run([sys.executable, "-m", "pip", "uninstall", "-y", *packages])
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
            f"提示：{envprobe.install_hint('xiaoyu-agent[tui]')} 可获得补全/历史/粘贴折叠",
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
    attention.waiting(attention.WAITING_INPUT)
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


def load_output_schema(spec: str | None) -> dict[str, Any] | None:
    """--output-schema 的值：存在的文件路径读文件，否则当内联 JSON 解析。"""
    if not spec:
        return None
    path = Path(spec)
    try:
        text = path.read_text(encoding="utf-8") if path.is_file() else spec
        schema = json.loads(text)
    except (OSError, ValueError) as exc:
        raise ValueError(f"--output-schema 不是合法的 JSON Schema 文件或内联 JSON：{exc}") from exc
    if not isinstance(schema, dict):
        raise ValueError("--output-schema 顶层必须是 JSON 对象")
    return schema


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
    log, restored = open_named(name, config.model, str(config.workspace))
    if config.system_prompt is None:
        #  续写同名会话而没再给 --system-prompt-file：沿用会话里记的那份
        config.system_prompt = load_system_prompt(log.path)
    return log, restored


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


_env_problems_shown = False


def _warn_env_problems() -> None:
    """写错了被忽略的配置，启动时说一声（一个进程只说一次）。"""
    global _env_problems_shown
    if _env_problems_shown:
        return
    _env_problems_shown = True
    from .config import env_problems

    for problem in env_problems():
        print(ui.warning(f"配置：{problem}"), file=sys.stderr)


def print_update_notice() -> None:
    """横幅之后提一句有新版（交互式启动专用，见 update_check 模块说明）。"""
    from . import update_check

    if notice := update_check.startup_notice():
        print(ui.secondary(notice))


def _trust_fingerprints(scope: str, workspace: Path) -> "dict[str, str] | None":
    """改工作区级 MCP 配置之前的指纹；用户级配置不归信任门管，返回 None。"""
    if scope != "project":
        return None
    from . import folder_trust

    return folder_trust.config_fingerprints(workspace)


def _trust_resync(scope: str, workspace: Path, before: "dict[str, str] | None") -> None:
    """用户亲手改完工作区级配置后同步信任指纹——自己加的 server 不该下次被追问。"""
    if scope != "project" or before is None:
        return
    from . import folder_trust

    try:
        folder_trust.resync_after_own_write(workspace, before)
    except OSError:
        pass


def resolve_folder_trust(
    workspace: Path, *, grant: bool, interactive: bool, unguarded: bool = False
) -> "folder_trust.TrustDecision":
    """启动期的 folder trust 门（见 folder_trust.py 模块 docstring）。

    必须在 load_dotenv 之前调用：工作区 .env 是被门管的对象，先读了再问
    等于门形同虚设。--trust 先记后判：记完 evaluate 自然走"信任表命中"。
    --unguarded 预设直接放行本次、**不记入信任表**：预设是这一跑的环境契约，
    不该变成下次普通启动时的持久信任。
    """
    from . import folder_trust

    if unguarded:
        return folder_trust.TrustDecision("trusted", folder_trust.workspace_key(workspace), ())
    if grant:
        key = folder_trust.workspace_key(workspace)
        #  信任绑在此刻的配置内容上：--trust 也是"重新看过、重新认"的入口
        bound = folder_trust.record_decision(
            key, True, workspace, folder_trust.config_fingerprints(workspace)
        )
        if bound is None:
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


def _acp_command(argv: list[str]) -> int:
    """子命令形态与 `--acp` 旗标完全等价：转写成旗标再走主解析器，两条路
    共用同一套参数、folder trust 门与 wire/acp 互斥检查，永不漂移。
    两种写法都留：编辑器/registry 的配置模板惯用子命令，`--acp` 是
    既有集成（含 ACP registry 提交物）的入口，属永久别名不做废弃。"""
    return main(["--acp", *argv])


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    #  崩溃面包屑：守护进程/后台线程/原生 segfault 的无声崩溃留一条痕迹。
    #  只在 CLI 入口装，不在 import 时装（嵌入宿主自管 excepthook）。
    from . import crash_guard

    crash_guard.install()
    #  启动期清扫陈旧临时目录（kill -9 / 断电留下的）：每进程一次、后台线程、
    #  失败吞掉；绝不动在跑会话的目录，边界见 tempdirs 模块说明。同样只在 CLI
    #  入口挂——嵌入宿主不该被库顺手扫它的临时目录。
    from . import tempdirs

    tempdirs.sweep_in_background()
    #  子命令拦截：nargs="*" 的 prompt 位置参数和 subparsers 不兼容，手动分流。
    #  清单与 --help 共用 SUBCOMMANDS 一张表
    if argv:
        for names, handler, _, _ in SUBCOMMANDS:
            if argv[0] in names:
                return subcommand_handler(handler)(argv[1:])
    args = build_parser().parse_args(argv)
    try:
        resolve_system_prompt_flags(args)
        resolve_guardrail_flags(args)
    except ValueError as exc:
        print(ui.error(str(exc)), file=sys.stderr)
        return 2
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
        unguarded=args.unguarded,
    )
    env_files = load_dotenv(
        Path(args.env_file).expanduser() if args.env_file else None,
        untrusted_dir=None if trust.trusted else gate_workspace,
    )

    #  wire/acp 模式：stdin 是协议通道，绝不能被当成管道指令读掉
    if (args.images or args.paste) and (args.wire or args.acp):
        print(
            ui.error("--image/--paste 只用于一次性模式（wire/acp 走各自协议里的图片通道）"),
            file=sys.stderr,
        )
        return 2
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
    if args.output_schema and not prompt:
        print(ui.error("--output-schema 只用于一次性模式（给出指令或从管道输入）"), file=sys.stderr)
        return 2
    try:
        output_schema = load_output_schema(args.output_schema)
    except ValueError as exc:
        print(ui.error(str(exc)), file=sys.stderr)
        return 2
    if (args.images or args.paste) and not prompt:
        print(
            ui.error("--image/--paste 只用于一次性模式（交互模式里用 Ctrl-V 直接贴图）"),
            file=sys.stderr,
        )
        return 2
    #  图片先落缓存再装配 agent：路径写错/剪贴板没图这类失败要最快报出来
    image_parts, image_problem = collect_image_parts(args.images, args.paste)
    if image_problem:
        print(ui.error(image_problem), file=sys.stderr)
        return 2

    workspace = Path(args.workspace).expanduser() if args.workspace else Path.cwd()
    if not workspace.is_dir():
        print(ui.error(f"工作区不存在：{workspace}"), file=sys.stderr)
        return 2
    warn_if_home_workspace(workspace)

    _warn_env_problems()
    try:
        config = Config.from_env(
            workspace=workspace,
            model=args.model,
            base_url=args.base_url,
            auto_approve=args.yolo or None,
            mode=args.mode,
            sandbox=args.sandbox,
            sandbox_network=args.sandbox_network,
            hardline=args.hardline,
            unattended=args.unattended,
            mcp_trust_changes=args.mcp_trust_changes,
            unguarded=args.unguarded or None,
            system_prompt=args.system_prompt,
            append_system_prompt=args.append_system_prompt,
            effort=args.effort,
            budget_tokens=args.budget_tokens,
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
        except (ValueError, SessionLockedError) as exc:
            #  会话名不合规 / 会话文件格式比本版新 / 会话正被另一个进程写入
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
    if args.goal:
        #  --goal 与会话里的 /goal 同义：一次性模式没有机会敲斜杠命令
        agent.set_goal(args.goal)
    agent.show_stats = args.stats
    if config.unguarded and prompt:
        #  一次性模式不进 repl/TUI，开场警告走 stderr（stdout 是这条命令的产物）
        from . import guardrails

        print(ui.error(guardrails.notice(config)), file=sys.stderr)
    #  copy=False：续写的就是历史所在那个文件，再抄一遍等于每次调用翻倍
    agent.restore(restored, copy=False)
    resumed = f"已接上会话 {args.session_id}（{len(restored)} 条消息）" if restored else ""

    if prompt:
        user_input: str | list[dict[str, Any]] = prompt
        if image_parts:
            #  显式发图撞上不看图的模型：硬报错，不做管线内那种降级说明。
            #  例外是点名了代读模型——一次性模式最常出现在脚本/CI 里，没人在环里
            #  改命令行重跑，用户既然显式配了代读就是要它在这种时候顶上
            if agent.registry.sees_images(config.model):
                user_input = [{"type": "text", "text": prompt}, *image_parts]
            else:
                block, notice = agent.caption_images(image_parts, guide=prompt)
                if not block:
                    print(
                        ui.error(
                            f"当前模型 {config.model} 未声明视觉能力，--image/--paste 发不出去。"
                            "换视觉模型（如 --model deepseek-flash）；"
                            "或用 XIAOYU_VISION_FALLBACK 点名一个代读模型；"
                            "或用 XIAOYU_VISION_MODELS 点名放行网关上的视觉模型"
                        ),
                        file=sys.stderr,
                    )
                    return 2
                #  提示走 stderr：stdout 是这条命令的产物，管道下游只该拿到正文
                print(ui.secondary(notice.strip()), file=sys.stderr)
                user_input = f"{prompt}\n\n{block}"
        if resumed and args.output_format == "text":
            print(ui.secondary(resumed))
        return run_once(agent, user_input, args.output_format, output_schema)
    print(build_banner(model_label(agent), str(config.workspace)))
    print_update_notice()
    if env_files:
        print(ui.secondary("已加载 " + ", ".join(str(p) for p in env_files)))
    print(ui.secondary(sandbox_status(config)))
    if hint := resume_hint(agent):
        print(ui.secondary(hint))
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
    _warn_env_problems()
    try:
        config = Config.from_env(
            workspace=workspace,
            model=args.model,
            base_url=args.base_url,
            auto_approve=args.yolo or None,
            mode=args.mode,
            sandbox=args.sandbox,
            sandbox_network=args.sandbox_network,
            hardline=args.hardline,
            unattended=args.unattended,
            mcp_trust_changes=args.mcp_trust_changes,
            unguarded=args.unguarded or None,
            system_prompt=args.system_prompt,
            append_system_prompt=args.append_system_prompt,
            effort=args.effort,
            budget_tokens=args.budget_tokens,
            workspace_trusted=workspace_trusted,
        )
        permissions = Permissions.load(config.workspace, include_workspace=workspace_trusted)
        try:
            session_log, restored = open_session(config, args.session_id)
        except (ValueError, SessionLockedError) as exc:
            #  会话名不合规 / 会话文件格式比本版新 / 会话正被另一个进程写入
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
    if session_start_refused(agent):
        return 2
    try:
        return server.serve()
    finally:
        end_session(agent)


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
            system_prompt=args.system_prompt,
            append_system_prompt=args.append_system_prompt,
            effort=args.effort,
            budget_tokens=args.budget_tokens,
            auto_approve=args.yolo or None,
            mode=args.mode,
            sandbox=args.sandbox,
            sandbox_network=args.sandbox_network,
            hardline=args.hardline,
            unattended=args.unattended,
            mcp_trust_changes=args.mcp_trust_changes,
            unguarded=args.unguarded or None,
        )
    ).serve()


#  退出报告里每条后台命令最多带这么多字符
_TERMINATED_COMMAND_CAP = 120


def terminate_background_commands(agent: Agent) -> list[dict[str, Any]]:
    """一次性模式收尾：把还在跑的后台任务停掉，返回其中**命令**任务的清单。

    一次性模式这一轮结束进程就退出，后台任务活不过它——模型刚说完"已在后台
    启动，完成后通知"，任务就没了。停掉是既定行为，这里只负责让它看得见：
    当场停（而不是留给 atexit）是为了报告属实——报出来的每一条都确实没跑完。
    monitor 不进清单：它是观察者，随会话结束本就是它的寿命，没有丢掉的工作。
    """
    tasks = getattr(getattr(agent, "toolbox", None), "tasks", None)
    if tasks is None:
        return []
    try:
        running = [task for task in tasks.running() if task.kind == "command"]
        report = []
        for task in running:
            command = " ".join(task.command.split())
            if len(command) > _TERMINATED_COMMAND_CAP:
                command = command[:_TERMINATED_COMMAND_CAP] + "…"
            report.append(
                {
                    "task_id": task.task_id,
                    "command": command,
                    "elapsed_seconds": round(task.elapsed(), 1),
                }
            )
        tasks.shutdown()
    except Exception:  # noqa: BLE001 - 收场的报告出错不该盖掉这次运行本身的结果
        return []
    #  清点与停掉之间自己跑完的不算被终止
    return [
        item
        for item, task in zip(report, running)
        if task.killed and task.proc.returncode != 0
    ]


def turn_stats_line(agent: Any) -> str:
    """`--stats` 开着时本轮的计时一行；没开或本轮没请求返回空串。
    getattr 兜底：测试里的替身 agent 没有这两个属性。"""
    if not getattr(agent, "show_stats", False):
        return ""
    stats = getattr(agent, "turn_stats", None)
    return stats.summary() if stats is not None else ""


def run_once(
    agent: Agent,
    user_input: str | list[dict[str, Any]],
    output_format: str = "text",
    output_schema: dict[str, Any] | None = None,
) -> int:
    """一次性模式的外壳：SessionStart / SessionEnd 钩子包在这一轮外面。"""
    if session_start_refused(agent):
        return 2
    try:
        return _run_once(agent, user_input, output_format, output_schema)
    finally:
        end_session(agent)


def _run_once(
    agent: Agent,
    user_input: str | list[dict[str, Any]],
    output_format: str = "text",
    output_schema: dict[str, Any] | None = None,
) -> int:
    error = ""
    if output_schema is not None:
        agent.set_output_schema(output_schema)
    try:
        agent.send(user_input)
    except KeyboardInterrupt:
        if agent.session_log:
            agent.session_log.event("interrupt")
        if output_format == "text":
            print(ui.warning("\n[已中断]"))
            return 130
        error = "interrupted"
    except errors.ContentFiltered as exc:
        #  服务端拒答不是故障，更不是小羽自己的 bug：text 模式也不该甩 traceback。
        #  说清被拒了、照常收场（后台任务、用量），退出码 1
        if agent.session_log:
            agent.session_log.event("error", error=f"ContentFiltered: {exc}")
        #  text 给人看，带上"重发也没用"那句；JSON 沿用「类型名: 文案」的形状
        error = (
            errors.classify(exc).hint
            if output_format == "text"
            else f"ContentFiltered: {exc}"
        )
    except Exception as exc:  # noqa: BLE001 - JSON 消费方要结构化错误，不是 traceback
        if agent.session_log:
            agent.session_log.event("error", error=f"{type(exc).__name__}: {exc}")
        if output_format == "text":
            raise
        error = f"{type(exc).__name__}: {exc}"

    #  要了 schema 却没收到结构化结果：消费方拿 null 没法用，按失败退出
    if output_schema is not None and agent.structured_output is None and not error:
        error = "模型没有按 --output-schema 给出结构化结果"

    terminated = terminate_background_commands(agent)

    if output_format == "text":
        if output_schema is not None and agent.structured_output is not None:
            print(json.dumps(agent.structured_output, ensure_ascii=False), flush=True)
        if error:
            #  走 stderr：stdout 可能正被管道接去当结果用
            print(ui.error(f"[{error}]"), file=sys.stderr)
        if terminated:
            #  走 stderr：stdout 可能正被管道接去当结果用
            listed = "\n".join(
                f"  {item['task_id']}  {item['command']}（已运行 {item['elapsed_seconds']:.0f}s）"
                for item in terminated
            )
            print(
                ui.warning(
                    f"[注意] {len(terminated)} 个后台任务在退出时被终止，没有跑完：\n{listed}\n"
                    "一次性模式在这一轮结束后就退出，不等后台任务；"
                    "要它跑完，就让命令在前台执行。"
                ),
                file=sys.stderr,
            )
        #  一次性模式也把用量打出来：做了模型路由就得看得见每个模型花了多少
        print(ui.secondary(f"\n{agent.usage}"))
        if stats := turn_stats_line(agent):
            print(ui.secondary(stats))
        return 1 if error else 0

    #  json / stream-json 的收尾对象结构相同；stream-json 带 kind 与事件流同一词汇
    payload: dict[str, Any] = {
        "result": agent.last_assistant_text(),
        "usage": agent.usage.to_dict(),
        "model": agent.config.model,
    }
    if output_schema is not None:
        payload["output"] = agent.structured_output
    if getattr(agent, "show_stats", False):
        #  --stats 的结构化形态：与 text 那一行同源的数字，不另算
        stats = agent.turn_stats
        payload["stats"] = {
            "duration_ms": stats.duration_ms,
            "ttft_ms": stats.ttft_ms,
            "completion_tokens": stats.completion_tokens,
            "requests": stats.requests,
        }
    if agent.session_log:
        payload["session_log"] = str(agent.session_log.path)
        if not getattr(agent.session_log, "complete", True):
            #  路径还在、内容却不全：CI 拿这个路径去 resume 会少掉后半段
            payload["session_log_complete"] = False
    if error:
        payload["error"] = error
    if terminated:
        #  只在确有任务被终止时出现：result 里那句"已在后台启动"此时不可信
        payload["background_tasks_terminated"] = terminated
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
    """开场打一行：这一档到底会不会问你。

    确认档不打——每条都问，没什么可预告的。auto 是出厂起始档，但"命令会自动跑"
    值得每次开场说一句，只是用次要色而不是警告色；沙箱不可用的降级（`modes.describe`
    自己改口）和 plan 档才用警告色——那两种是用户容易会错意的状态。
    """
    if agent.mode == modes.DEFAULT:
        return
    #  --yolo 下 auto 档那句"bash 仍逐条确认"不成立（全放行盖过了 auto 的放行矩阵），
    #  不打；plan 档仍要说——只读承诺不受 --yolo 影响
    if agent.config.auto_approve and agent.mode == modes.AUTO:
        return
    ready = agent.sandbox_ready()
    text = modes.describe(agent.mode, sandbox_ready=ready)
    style = ui.secondary if agent.mode == modes.AUTO and ready else ui.warning
    print(style(text))


def background_status(agent: Agent) -> str:
    """轮次结束后的 still-running 状态行（inline 架构下打一行即走）。"""
    tasks = getattr(agent.toolbox, "tasks", None)
    return tasks.still_running_line() if tasks is not None else ""


def repl(agent: Agent) -> int:
    #  窗口标题随会话走，任何退出路径都还原（与 TUI 同一纪律）
    attention.set_title(agent.config.workspace)
    try:
        return _repl_loop(agent)
    finally:
        attention.clear_title()


def _repl_loop(agent: Agent) -> int:
    config = agent.config
    #  模型/工作区/help 提示都在启动横幅里了，这里只留必须扎眼的警告
    if config.unguarded:
        from . import guardrails

        print(ui.error(guardrails.notice(config)))
    elif config.auto_approve:
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
            #  /<技能名> 参数… 展开成本轮提示；不是技能的才交给内建命令表
            expanded = skill_prompt(agent, action.args)
            if expanded is None:
                if handle_slash(agent, action.args):
                    return 0
                continue
            if not expanded:
                continue
            action = keys.InputAction("send", expanded)
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
        if stats := turn_stats_line(agent):
            print(ui.secondary(f"  {stats}"))
        #  一轮收尾 = 回到"等人"：铃（opt-in）+ 状态钩子
        attention.waiting(attention.WAITING_INPUT)
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


#  技能的显式入口前缀：/skill:<名字>。与内建命令撞名的技能只能从这里进
SKILL_SLASH_PREFIX = "/skill:"
#  内建命令名（含表外别名）：斜杠名字空间里内建永远优先，技能撞名不许遮住它
_BUILTIN_SLASH = frozenset(name for name in SLASH_COMMANDS if "<" not in name) | {"/undo"}


def skill_shadowed(name: str) -> bool:
    """这个技能名直接写成 /<名字> 会不会撞上内建命令。"""
    return f"/{name}" in _BUILTIN_SLASH


def skill_prompt(agent: Agent, line: str) -> str | None:
    """`/<技能名> 参数…` → 展开成本轮提示文本。

    返回 None = 这行不是技能调用（内建命令或没这个技能），交给 handle_slash；
    返回空串 = 是技能调用但展开失败（已打印原因），本轮不发。
    名字空间规则：内建命令 > 技能——`/help` 永远是帮助，同名技能要写
    `/skill:help`。`/skill:` 前缀下找不到也报错而不是回落到内建，用户既然
    写了前缀就是明确要技能。
    """
    head, _, rest = line.strip().partition(" ")
    explicit = head.startswith(SKILL_SLASH_PREFIX)
    name = head[len(SKILL_SLASH_PREFIX):] if explicit else head[1:]
    if not name or (not explicit and (head in _BUILTIN_SLASH or not agent.skills)):
        return None
    if not explicit and not any(skill.name == name for skill in agent.skills):
        #  不是已知技能：当作敲错的内建命令报"未知命令"，别为每个错字重扫磁盘
        return None
    arguments = rest.strip()
    result = agent._load_skill(name, arguments)  # noqa: SLF001 - 同包的前端入口
    if result.startswith("ERROR:"):
        print(ui.error(f"  {result}"))
        return ""
    print(ui.secondary(f"  已展开技能 {name}" + (f"（参数：{arguments}）" if arguments else "")))
    #  模型看到的就是 skill 工具加载的那份（目录头 + 支持文件 + 正文），外加
    #  一句"这是用户点名要执行的"：和模型自己按索引挑技能加载是两种语气
    return f"按下面这个技能的说明执行（用户以 /{name} 直接调用）：\n\n{result}"


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
            hint = envprobe.install_hint("xiaoyu-agent[tui]")
            print(ui.secondary(f"  装上可选依赖即可：{hint}"))
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
            else:
                print(ui.secondary("  " + manager.command(list(rest)).replace("\n", "\n  ")))
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
                print(ui.secondary("  没有发现技能。放到 ~/.agents/skills/<名字>/SKILL.md 即可被识别"))
                print(ui.secondary("  （随仓库共享的放工作区的 .xiaoyu/skills/ 或 .agents/skills/），"))
                print(ui.secondary("  或用 xiaoyu plugin add <owner/repo> 装一个插件包"))
            for skill in agent.skills:
                #  插件技能标出来源：名字里虽然带了包名前缀，但"这是装来的、能 update"
                #  和"这是我自己写的"是两回事
                origin = ui.secondary(f"  [插件 {skill.plugin}]") if skill.plugin else ""
                if skill.project:
                    origin = ui.secondary("  [工作区]")
                if skill_shadowed(skill.name):
                    #  与内建命令撞名的技能不是不能用，只是 /<名字> 归内建；说清入口
                    origin += ui.warning(f"  [与内建命令撞名，用 /skill:{skill.name} 调用]")
                print(f"  {skill.name}  {ui.secondary(skill.description or str(skill.path))}{origin}")
            if agent.skills:
                print(ui.secondary("  /<技能名> 参数… 可把技能直接展开成本轮提示"))
            from . import skills as skills_mod

            if hidden := skills_mod.disabled_skills(agent.config.workspace):
                shown = "、".join(hidden[:12]) + (f" 等 {len(hidden)} 个" if len(hidden) > 12 else "")
                print(ui.secondary(f"  已停用（{skills_mod.DISABLED_ENV}）：{shown}"))
    elif command == "/search":
        from .websearch import search_command

        print(ui.secondary(search_command(agent.config, agent.registry, " ".join(rest))))
    elif command == "/model":
        if rest:
            #  先解析再切：名字没人接就报错、原模型不动。否则"已切换到 grok"打了
            #  一句假成功，错误要到下一次请求才冒出来。解析成功时把路由一并打出来
            #  ——直连是靠环境变量静默启用的，不说用户不知道请求走哪家、钱花在谁那
            try:
                route = agent.registry.resolve(rest[0])
            except providers.UnknownModel as exc:
                print(ui.error(str(exc)))
            else:
                agent.switch_model(rest[0])
                owner = agent.registry.get(route.provider)
                where = owner.display if owner else route.provider
                print(ui.secondary(f"已切换到 {rest[0]}（{where}）"))
                if owner is not None and owner.wildcard:
                    #  通配 provider（网关）什么名字都接，本地解析答不了"存不存在"；
                    #  现场探一次它的清单，不在里面就提前说——否则要到第一次请求
                    #  才收到一句干巴巴的 400。只告警不拦：网关清单未必全，用户点名
                    #  要试的模型不该被本地拦死
                    for label, models, note in agent.registry.remote_models():
                        if label != owner.display:
                            continue
                        if models is None:
                            print(ui.warning(f"{label}清单获取失败，无法预检模型名：{note}"))
                        elif rest[0] not in models:
                            print(
                                ui.warning(
                                    f"{label}清单里没有 {rest[0]}，请求很可能失败"
                                    f"（清单共 {len(models)} 个，/model 不带参数可查看）"
                                )
                            )
                #  切模型顺手探一次它的 Models API 能力：自校正上下文上限、报能力/漂移
                for note in agent.refresh_capabilities():
                    print(ui.secondary(note))
        else:
            print(ui.secondary(f"当前模型 {agent.config.model}"))
            for note in agent.refresh_capabilities():
                print(ui.secondary(note))
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
            #  代读只在配了的时候印：默认没有，多印一行"未配置"是噪音。
            #  当前模型本来就能看图时也说清楚——代读此刻不会触发，别让用户以为
            #  自己看到的每张图都被转述过一道
            if note := agent.vision_note():
                print(ui.secondary(note))
    elif command == "/effort":
        if rest:
            level = rest[0].strip().lower()
            if level in ("off", "default"):
                level = ""
            if level and level not in EFFORT_LEVELS:
                print(ui.secondary(f"effort 只认 {' / '.join(EFFORT_LEVELS)}，或 off 恢复默认"))
            else:
                agent.config.effort = level
                print(ui.secondary(f"推理深度：{level or '上游默认'}"))
                if warning := effort_mismatch(agent, level):
                    print(ui.warning(warning))
        else:
            print(ui.secondary(f"推理深度：{agent.config.effort or '上游默认'}"))
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
                + (
                    f"  ⚠ 自动压缩：{paused}（/compact 可手动重试）\n"
                    if (paused := state.paused_reason()) else ""
                )
                +
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
    elif command == "/goal":
        if rest and rest == ["clear"]:
            print(ui.secondary(f"  {agent.set_goal('')}"))
        elif rest:
            print(ui.secondary(f"  {agent.set_goal(' '.join(rest))}"))
        elif agent.goal:
            print(ui.secondary(f"  当前验收目标：{agent.goal}（/goal clear 清除）"))
        else:
            print(ui.secondary("  未设验收目标；/goal <一句话> 设定后，模型收尾前会先核对是否达成"))
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
                if hint := rule_lint(rule):
                    print(ui.warning(f"  这条规则可能不会命中：{hint}"))
    elif command == "/resume":
        slash_resume(agent, rest, select)
    elif command == "/clear":
        agent.reset()
        print(ui.secondary("对话已清空"))
    elif command.startswith(SKILL_SLASH_PREFIX):
        #  正常路径上 skill_prompt 已先一步接走；走到这里说明前端没接技能展开
        print(ui.warning(f"技能调用（{command}）只在交互前端里可用，/skills 看技能列表"))
    else:
        print(ui.warning(f"未知命令 {command}，/help 看可用命令"))
    return False


def sandbox_status(config: Config) -> str:
    """一行沙箱状态：默默生效的保护要看得见，不然出问题时没人想得到它。"""
    from . import sandbox

    if not config.sandbox:
        return "沙箱：已关闭（--no-sandbox）——bash 命令可写任意路径"
    if not sandbox.available():
        #  为什么用不了、怎么办由 sandbox 给（Linux 上没装与装了跑不起来是两回事）
        why, remedy = sandbox.unavailable_reason()
        return f"沙箱：未生效（{why}），bash 命令可写任意路径。{remedy}"
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
        #  审批挂起 = 轮次卡在等人：铃（opt-in）与状态钩子把切走的人叫回来
        attention.waiting(attention.WAITING_APPROVAL)
        escalation = ui.escalation_notice(args) if name == "bash" else []
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
            #  升权申请先点名：批的是"这次不套沙箱"，不只是这条命令（与 TUI 同一份文案）
            for line in escalation:
                print(ui.warning(f"  ⚠ {line}"))
            #  破坏性操作 / 参数注入口在确认框里点名：用户看到的是"为什么要多想一下"
            if reason := command_check.command_risk(str(args.get("command", ""))):
                print(ui.warning(f"  ⚠ 注意：{reason}"))

        verb = "升权执行" if escalation else "执行"
        try:
            answer = input(
                ui.warning(f"  允许{verb} {name}? [y/N/a=本会话都允许，其它输入=拒绝理由] ")
            ).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return False

        verdict = interpret_confirm_answer(answer)
        if verdict is GRANT_SESSION:
            scope = permissions.grant_session_call(name, args)
            if scope is not None:
                print(ui.secondary(f"  （本次会话内 {scope} 不再逐次确认；/perm 可查看）"))
            else:
                print(ui.secondary("  （这条命令推不出会话授权范围，仅本次允许）"))
            return True
        return verdict

    return confirm


if __name__ == "__main__":
    raise SystemExit(main())
