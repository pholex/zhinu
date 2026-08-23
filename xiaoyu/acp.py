"""acp 模式：Agent Client Protocol server（agentclientprotocol.com）。

`xiaoyu acp`（等价旗标 `xiaoyu --acp`）后本进程即 ACP agent：Zed / Neovim / Emacs / marimo 等编辑器
客户端经 stdin/stdout 驱动（JSON-RPC 2.0，一行一条）。与自研 `--wire` 并存：
wire 是小羽自己的极简协议（面向自家外壳与测试驱动器），acp 是编辑器生态的
通用协议——接一层白得整个 ACP 客户端生态，符合「协议兼容、不自建生态」。

范围纪律：只接协议的标准面，不把 ACP 当自家 IPC 用——小羽的 TUI 是进程内
sink，不经协议层，所以不需要任何自定义扩展方法；可选能力按"编辑器语境
是否真受益 vs 架构税"逐项取舍（fs/terminal 的刻意留白理由见 initialize 条目）：

    initialize                  版本协商（只会说 1；client 更新就答自己的最新）
                                + 能力声明（loadSession=true；fs 只借"读"的
                                一半做未保存缓冲区哨兵（见下），fs/write 与
                                terminal 刻意不接——磁盘是小羽全部内核纪律的
                                唯一事实源（bash/测试回路、mtime 读前置检查、
                                rewind 快照、沙箱写域），写进缓冲区会把"改完
                                自己跑验证"的核心回路切成两个世界；
                                embeddedContext=true——上下文附件白吃白不吃；
                                image=true——贴图过 media.accept 统一闸后与 TUI
                                同款进部件历史，模型看不了图时不入历史（OpenAI
                                兼容面会 400）而是降级文字说明 + thought 警告；
                                audio 无内核通道，不声明）
    authenticate                「认证」=配置 provider（API key/网关）。
                                authMethods 声明一条 terminal 型（官方 registry
                                的收录硬门槛：空数组不收），指向现成的
                                `xiaoyu config` 交互向导；无 provider 配置时
                                session/new 回 -32000 auth_required。
                                authenticate 与 session/new 重试都会补读用户级
                                .env（setdefault，真实环境变量优先）——向导在
                                别的终端跑完，本进程立即可用，无需重启。
    session/new                 每 session 一个 Agent，cwd 即工作区（必须绝对路径）。
                                folder trust 按信任表非交互判定：没信任记录就
                                不吃工作区级 .mcp.json/permissions/.env（门的
                                纪律与 headless 一致，在 TUI 里 --trust 过的
                                目录自然放行）。client 随请求带的 mcpServers
                                接：与配置发现合并、同名 client 优先、session
                                作用域不落盘、${VAR} 不兑现、照过准入闸；只收
                                stdio 与 Streamable HTTP（mcpCapabilities 里如实
                                声明 http=true / sse=false），
                                跳过的条目一律 stderr 出声，不静默少几个工具。
    session/prompt              工作线程跑一轮；UIEvent → session/update 流。
                                会话内同一时刻只跑一轮（同步架构的约束是
                                会话级的），本会话忙时回 -32001；不同会话的
                                轮各自起线程并行推进。
                                以 /命令 开头且命中广告过的命令时不进模型：
                                当场执行、输出走 agent_message_chunk，也不入
                                对话历史（REPL 斜杠命令同款——前端层动作，
                                不是模型能看到的话）。没命中的照常当用户输入。
    available_commands_update   session/new、session/load 回包后广告斜杠命令
                                （REPL SLASH_COMMANDS 的 ACP 子集，描述同源）。
                                /model /mode 有原生选择器不重复广告；/resume
                                /rewind 是交互选择流程、/help /keys /exit 是
                                终端专属，都不进协议面。命令跑在工作线程：
                                /compact 会调模型做摘要，stdin 主循环必须
                                空着收 session/cancel。
    session/cancel              notification：打断当前轮 + 挂起审批按拒绝解决；
                                prompt 必须以 stopReason=cancelled 收尾（规范
                                要求，不能回 error——client 取消不是错误）。
    session/load                重开旧会话（Zed 重启后续聊的通道）。ACP sessionId
                                直接当命名会话名落盘（session/new 即建名为
                                sess-<hex> 的命名会话），load 时 find_named 定位
                                同一文件、load_messages 重建 agent.messages
                                （restore copy=False：续写原文件，不翻倍抄历史），
                                并按规范把整段对话回放成 session/update 流
                                （user_message_chunk / agent_message_chunk /
                                工具对）之后才回包。找不到的 sessionId 回
                                INVALID_PARAMS——绝不静默新建一个空会话冒充。
    session/set_config_option   模型与模式切换。session/new 随包声明
                                configOptions 两项：category="model"（候选=
                                switchable_models()）与 category="mode"（三档
                                交互模式，与下面 modes 那条面同一张表）——两个
                                category 都是 v1 stable 保留的语义标签。
                                模型切换=改 config.model（与 TUI 同一动作，粘性）；
                                该 session 正在跑的轮里不切（回 BUSY——避免与
                                降级链的粘性写打架）。粘性降级改了模型时，轮末发
                                config_option_update 把下拉框校正回真实状态。
                                模式**故意两条面都发**：把 modes 挪作他用的宿主
                                （拿它当 agent 选择器的就有）会让三档模式整个
                                不可达，这条是它够不到的备用出口。
    session/set_mode            交互模式切换（TUI Shift+Tab / /mode 的协议面）。
                                与 set_config_option(mode) 是同一个动作的两条面：
                                谁发起谁由响应确认，另一条面收到回声校正
                                （config_option_update / current_mode_update），
                                同一会话里两个选择器不会显示成不同模式。
                                session/new、session/load 回包带 modes（三档=
                                modes.MODES 那张表，auto 档描述按本机沙箱现状取
                                诚实版本——下拉框里就写明白，不骗人）。该 session
                                跑轮期间回 BUSY：set_mode 会往历史注入 plan 进出
                                说明，不能与工作线程抢 messages。模型侧退出 plan
                                （exit_plan_mode 工具）由 sink 在事件流里对账，
                                发 current_mode_update 把 client 的模式选择器扳回
                                真实状态；session/load 跟随旧会话最后生效的模式
                                （与模型选择同一纪律，last_mode 留痕）。
    session/request_permission  审批桥，选项与 TUI 确认框同一套语义：允许一次 /
                                本会话内该工具都允许 / 总是允许（能推导出安全
                                规则才出现，选中即写入持久规则）/ 拒绝。协议
                                没有"会话作用域"的选项 kind——会话档借最接近
                                的 allow_always 渲染，真实语义由 optionId 承载、
                                回程按 id 还原。持久规则的安全禁令（不许覆盖
                                任意代码执行入口）照常生效，被拒时降级为仅本
                                次允许并在 thought 轨道说明。回包缺失/畸形/
                                outcome=cancelled 一律 fail closed 按拒绝
                                （与 wire、嵌入面同一条纪律）。
    fs/read_text_file           只读哨兵（client 声明了 readTextFile 才用）：
                                编辑类调用走到审批点时读一次 client（编辑器
                                缓冲区）视角的文件内容，与磁盘不一致=用户有
                                未保存改动，直接按拒绝解决并把"先保存再重试"
                                回灌模型——把"静默覆盖未保存工作"降级成
                                "明确拦下"。哨兵 fail-open：无能力/超时/error
                                回包都退回正常审批流程，磁盘仍是唯一事实源。
                                已知限界：auto/--yolo 放行的编辑不过审批点，
                                也就不过哨兵（与该档"少问"的取舍一致）。

事件映射（按小羽事件词汇收敛）：
    text.delta      → agent_message_chunk
    notice          → agent_thought_chunk（系统性提示进"思考"轨道，不污染正文）
    tool.pending    → tool_call（status=pending；编辑类附 diff 内容，路径类附
                      locations——Zed 靠它们画 gutter 与文件跳转）
    tool.purpose    → tool_call_update（title=模型自述目的）
    tool.running    → tool_call_update（status=in_progress）
    tool.completed  → tool_call_update（status=completed|failed + 输出文本）
    tool.denied     → tool_call_update（status=failed；ACP 没有 denied 态）
    plan.updated    → plan（小羽计划状态词汇与 ACP 逐字相同，零转换）
    request.*/text.end/steer.accepted → 无对应，丢弃

工具调用没有跨事件的 id（小羽同步串行执行，事件按序配对即可）：pending 入队
发 id，purpose/running/completed/denied 按"最老的同名未终态调用"回配。
取消把历史里悬空的 tool_use 补成文本结果（agent.close_open_tool_calls），但
不发事件——ACP 侧的悬空 tool_call 由本层在轮末统一置 failed，不留转圈图标。

线程模型与 wire.py 同款（全同步、无 asyncio）：主线程读 stdin 分发；prompt
每会话一条工作线程跑 agent.send()（会话内单轮、会话间并行——serve/fanout
早已如此，这里只是拉齐）；审批在各自工作线程阻塞在 threading.Event 上，
挂起表按 request id 键控天然多路复用、按 sessionId 记归属（cancel 只解决
自己会话的挂起项）；stdout 写入全部过同一把锁。EOF：全部挂起审批按拒绝
解决、打断所有在跑的轮、共享预算等线程收尾。
"""

from __future__ import annotations

import base64
import inspect
import json
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, TextIO

from . import __version__, folder_trust, mcp, mcp_guard, media, modes
from .config import Config, MissingConfig, load_dotenv, user_env_path
from .permissions import Permissions, suggest_allow_rule
from .session_log import (
    SessionLog,
    check_session_id,
    find_named,
    install_exit_logging,
    last_mode,
    last_model,
    open_named,
)
from .tools import Toolbox
from .providers import UnknownModel
from .agent import SYNTHETIC_USER_TEXTS, Agent, Allow, Deny, Interrupted
from .events import (
    Notice,
    PlanUpdated,
    TextDelta,
    ToolCompleted,
    ToolDenied,
    ToolPending,
    ToolPurpose,
    ToolRunning,
    UIEvent,
)

PROTOCOL_VERSION = 1

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
#  ACP 规范保留：需要先完成认证（对小羽=配置 provider）才能建会话
AUTH_REQUIRED = -32000
#  自定义段：-32000 已被占用（见上），忙碌另起一个
BUSY = -32001

#  authMethods 的唯一一条。官方 registry 的收录硬门槛是"至少声明一种认证
#  方式"（空数组不收，只认 agent/terminal 两型）。小羽的"认证"=配置
#  provider（API key/网关），terminal 型正好指向现成的 `xiaoyu config`
#  交互向导：client 拉起终端跑它，写用户级 .env，完事重试即可。
AUTH_METHOD: dict[str, Any] = {
    "id": "config-wizard",
    "name": "配置模型与 API Key",
    "description": "运行 xiaoyu config 交互向导，把 API key / 网关地址写入用户级配置",
    "type": "terminal",
    "args": ["config"],
    "env": {},
}

#  小羽工具名 → ACP ToolKind（没列到的一律 other；MCP 工具天然走 other）
_TOOL_KINDS = {
    "bash": "execute",
    "write_file": "edit",
    "str_replace": "edit",
    "read_file": "read",
    "list_files": "search",
    "grep": "search",
    "explore": "search",
    "search_tool": "search",
    "web_search": "fetch",
    "browser": "fetch",
    "update_plan": "think",
    "exit_plan_mode": "think",
    "task_output": "execute",
    "monitor": "execute",
    "kill_task": "execute",
}

_TITLE_LIMIT = 80


def _text_content(text: str) -> dict[str, Any]:
    return {"type": "content", "content": {"type": "text", "text": text}}


def _tool_title(name: str, args: dict[str, Any]) -> str:
    """人读的一行标题：bash 给命令、文件类给路径、检索给 pattern。"""
    value = ""
    if name == "bash":
        value = str(args.get("command", "") or "")
    elif name in ("read_file", "write_file", "str_replace", "list_files"):
        value = str(args.get("path", "") or "")
    elif name in ("grep", "explore", "web_search"):
        value = str(args.get("pattern") or args.get("query") or "")
    value = " ".join(value.split())
    if not value:
        return name
    if len(value) > _TITLE_LIMIT:
        value = value[: _TITLE_LIMIT - 1] + "…"
    return f"{name}: {value}"


def config_options(agent: Agent) -> list[dict[str, Any]]:
    """session/new 与 set_config_option 回包里的 configOptions。

    两项：模型选择（category="model"）与交互模式（category="mode"）。两个
    category 都是 v1 stable 明文保留的语义标签（"Model selector"/"Session mode
    selector"），不是自造——client 据此决定图标/快捷键/摆放位置。

    模型：候选与 TUI `/model` 的 Tab 补全同源（switchable_models：合并清单 +
    全限定名 + 降级链 + 网关探测缓存），不做现场网络探测——session/new 在启动
    热路径上。当前模型不在候选里（用户 --model 点了个清单外的名字）也要列进去：
    currentValue 必须是 options 的合法成员，否则 client 的下拉框没法定位。

    模式：与 modes 字段（SessionModeState）同一张表，**故意两处都发**。规范
    两条面都有，而 client 只认自己那条：把 modes 挪作他用的宿主（拿它当 agent
    选择器的就有）会让三档交互模式整个不可达，configOptions 这条是它够不到的
    备用出口。两条面的一致性由切换后互相回声保证（见 _handle_set_mode /
    _handle_set_config_option）。
    """
    current = agent.config.model
    names: list[str] = []
    for name in [current, *agent.switchable_models()]:
        if name not in names:
            names.append(name)
    return [
        {
            "id": "model",
            "name": "模型",
            "category": "model",
            "type": "select",
            "currentValue": current,
            "options": [{"value": name, "name": name} for name in names],
        },
        {
            "id": "mode",
            "name": "模式",
            "category": "mode",
            "type": "select",
            "currentValue": agent.mode,
            "options": [
                {"value": name, "name": label, "description": desc}
                for name, label, desc in _mode_entries(agent)
            ],
        },
    ]


def _mode_entries(agent: Agent) -> list[tuple[str, str, str]]:
    """(id, 显示名, 描述) 三元组，modes 与 configOptions 两处共用一份。

    auto 档的描述按本机沙箱现状取 modes.desc 的诚实版本：沙箱不可用时
    "命令自动放行"是假的，不能让下拉框替我们撒谎。
    """
    ready = agent.sandbox_ready()
    return [
        (item.name, item.label, modes.desc(item.name, sandbox_ready=ready))
        for item in modes.MODES
    ]


def mode_state(agent: Agent) -> dict[str, Any]:
    """session/new 与 session/load 回包里的 modes（SessionModeState）。

    与 TUI Shift+Tab 循环同一张表（modes.MODES），id 用稳定标识 name、
    显示名用 label——client 端下拉框和 TUI 提示符念的是同一套词。auto 档
    的 description 按本机沙箱现状取 modes.desc 的诚实版本：沙箱不可用时
    "命令自动放行"是假的，不能让下拉框替我们撒谎。
    """
    return {
        "currentModeId": agent.mode,
        "availableModes": [
            {"id": name, "name": label, "description": desc}
            for name, label, desc in _mode_entries(agent)
        ],
    }


# ---------- 斜杠命令（available_commands_update 的协议面） ----------


@dataclass(frozen=True)
class AcpCommand:
    """一条 ACP 可见的斜杠命令。name 不带斜杠（规范如此，client 自己画 /）；
    hint 是 input.hint（空串=无参数命令）；run 拿 (agent, 参数串) 回输出文本。
    描述不在这里写——广告时从 cli.SLASH_COMMANDS 取，与 /help、TUI 补全
    菜单同一份文案，不再有第二处会漂移的说法。"""

    name: str
    hint: str
    run: Callable[[Agent, str], str]


def _cmd_usage(agent: Agent, args: str) -> str:
    return str(agent.usage)


def _cmd_context(agent: Agent, args: str) -> str:
    used = agent.context_tokens()
    limit = agent.config.context_limit
    #  显示前同步：/model 切换后 compactor 里还是旧模型的上限（与 REPL 同款）
    agent.compactor.context_limit = limit
    budget = agent.compactor.budget()
    state = agent.compactor.state
    return (
        f"{used} / {limit} tok（{used / limit:.0%}）\n"
        f"压缩阈值 {budget} tok · 已压缩 {state.count} 次（上次省 {state.saved_tokens} tok）\n"
        f"消息 {len(agent.messages)} 条 · 估算依据：{agent.context_source()}\n"
        f"摘要模型链：{' → '.join(r.qualified for r in agent.summary_models())}"
    )


def _cmd_compact(agent: Agent, args: str) -> str:
    return agent.maybe_compact(force=True) or "无需压缩"


def _cmd_tools(agent: Agent, args: str) -> str:
    lines = []
    for name in agent.toolbox.names():
        tool = agent.toolbox.get(name)
        flag = " [需确认]" if tool and tool.requires_approval else ""
        if tool and not tool.available():
            flag += " [不可用]"
        lines.append(f"{name}{flag}")
    return "\n".join(lines)


def _cmd_skills(agent: Agent, args: str) -> str:
    if not agent.skills:
        return (
            "没有发现技能。放到 ~/.agents/skills/<名字>/SKILL.md 即可被识别，"
            "或用 xiaoyu plugin add <owner/repo> 装一个插件包"
        )
    lines = []
    for skill in agent.skills:
        origin = f"  [插件 {skill.plugin}]" if skill.plugin else ""
        lines.append(f"{skill.name}  {skill.description or skill.path}{origin}")
    return "\n".join(lines)


def _cmd_tasks(agent: Agent, args: str) -> str:
    tasks = getattr(agent.toolbox, "tasks", None)
    entries = tasks.all() if tasks is not None else []
    if not entries:
        return "没有后台任务（bash 的 run_in_background、monitor 工具会出现在这里）"
    lines = []
    for task in entries:
        mark = "monitor " if task.kind == "monitor" else ""
        if task.done.is_set():
            state = f"{task.status}（exit {task.exit_code}）"
        else:
            state = f"running（{task.elapsed():.0f}s）"
        lines.append(f"{task.task_id}  {mark}{state}  {task.description}\n  日志：{task.log_path}")
    return "\n".join(lines)


def _cmd_mcp(agent: Agent, args: str) -> str:
    from . import mcp

    if not agent.config.enable_mcp:
        return "MCP 已被 XIAOYU_ENABLE_MCP=0 关闭"
    manager = mcp.launch(agent.config)
    if manager is None:
        return mcp.McpManager.usage_hint()
    parts = args.split()
    if parts and parts[0] == "approve":
        if len(parts) < 2:
            return "用法：/mcp approve <server名>（批准变更工具，解除隔离）"
        return manager.approve(parts[1])
    return manager.describe()


def _cmd_perm(agent: Agent, args: str) -> str:
    return (
        f"当前模式：{modes.describe(agent.mode, sandbox_ready=agent.sandbox_ready())}\n"
        f"{agent.permissions.describe()}"
    )


def _cmd_rule(kind: str, agent: Agent, args: str) -> str:
    from .permissions import parse_rule

    rule = parse_rule(f"{kind} {args}") if args.strip() else None
    if rule is None:
        return f"规则格式：/{kind} bash(git *) 或 /{kind} write_file"
    try:
        path = agent.permissions.add_persistent(rule)
    except ValueError as exc:
        #  持久 allow 不许覆盖任意代码执行入口（与 REPL 同一道闸）
        return f"已拒绝：{exc}"
    return f"已写入 {path}：{rule}"


_ACP_COMMANDS: tuple[AcpCommand, ...] = (
    AcpCommand("usage", "", _cmd_usage),
    AcpCommand("context", "", _cmd_context),
    AcpCommand("compact", "", _cmd_compact),
    AcpCommand("tools", "", _cmd_tools),
    AcpCommand("skills", "", _cmd_skills),
    AcpCommand("tasks", "", _cmd_tasks),
    AcpCommand("mcp", "approve <server名>（可选）", _cmd_mcp),
    AcpCommand("perm", "", _cmd_perm),
    AcpCommand("allow", "bash(git *) 或 write_file", lambda agent, args: _cmd_rule("allow", agent, args)),
    AcpCommand("deny", "bash(git *) 或 write_file", lambda agent, args: _cmd_rule("deny", agent, args)),
)


def available_commands() -> list[dict[str, Any]]:
    """available_commands_update 的载荷。描述从 cli.SLASH_COMMANDS 取
    （懒 import：cli 只在 acp_main 里懒 import 本模块，运行期必已加载；
    键缺失直接 KeyError——两张表脱节该在测试里炸出来，不静默降级）。"""
    from .cli import SLASH_COMMANDS

    entries: list[dict[str, Any]] = []
    for command in _ACP_COMMANDS:
        entry: dict[str, Any] = {
            "name": command.name,
            "description": SLASH_COMMANDS[f"/{command.name}"],
        }
        if command.hint:
            entry["input"] = {"hint": command.hint}
        entries.append(entry)
    return entries


def match_command(text: str) -> tuple[AcpCommand, str] | None:
    """prompt 文本 → 命中的命令与参数串；没命中回 None（照常当用户输入，
    "/etc/hosts 是什么"这类只是开头像命令的话不该被吃掉）。"""
    parts = text.strip().split(None, 1)
    if not parts or not parts[0].startswith("/"):
        return None
    name = parts[0][1:]
    for command in _ACP_COMMANDS:
        if command.name == name:
            return command, parts[1].strip() if len(parts) > 1 else ""
    return None


def prompt_content(blocks: Any) -> str | list[dict[str, Any]]:
    """session/prompt 的 ContentBlock 数组 → agent.send 的输入。

    纯文本给字符串（与无图输入的历史形态一致）；带图给部件列表（文本在前、
    图片在后，与 TUI 贴图同款）。基线类型 text / resource_link 必收；
    embeddedContext 已声明所以 resource（内嵌上下文）也收，包上 uri 标注；
    image 已声明——base64 解开后走 media.accept 统一闸（格式/体积的判断
    全仓只写这一遍），不合格的图降级成一行文字说明进正文：为一张坏图拒掉
    整条 prompt 不划算，但也绝不静默丢弃。audio 未声明能力，client 不该发；
    发了也只是安静跳过（liberal in what we accept）。
    """
    texts: list[str] = []
    images: list[dict[str, Any]] = []
    for block in blocks if isinstance(blocks, list) else []:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "text":
            texts.append(str(block.get("text", "") or ""))
        elif kind == "resource_link":
            uri = str(block.get("uri", "") or "")
            name = str(block.get("name", "") or "") or uri
            texts.append(f"[附件] {name}（{uri}）")
        elif kind == "resource":
            resource = block.get("resource") or {}
            if isinstance(resource, dict) and isinstance(resource.get("text"), str):
                uri = str(resource.get("uri", "") or "")
                texts.append(f'<context uri="{uri}">\n{resource["text"]}\n</context>')
        elif kind == "image":
            try:
                data = base64.b64decode(str(block.get("data", "") or ""), validate=True)
            except (ValueError, TypeError):
                texts.append("[图片未能接收：数据不是有效的 base64]")
                continue
            ref, problem = media.accept(data, "随消息附上的图片")
            if ref:
                images.append(media.image_part(ref))
            else:
                texts.append(f"[图片未能接收：{problem}]")
    text = "\n\n".join(part for part in texts if part.strip())
    if not images:
        return text
    return [media.text_part(text), *images] if text else images


def _degrade_images(
    session: "_Session", content: str | list[dict[str, Any]]
) -> str | list[dict[str, Any]]:
    """模型看不了图时的收口（sees_images fail-closed 的另一半）。

    图片部件不入历史——走的是 OpenAI 兼容面，塞进去每一轮请求都会被上游
    400，整个会话卡死。与 TUI 的"这条只发文字，换模型后重发"同一诚实纪律，
    但协议面没有"扣住图等重发"的交互：改成明确告诉两边——模型收到
    "有图被省略"的说明（它据此可以请用户换模型），用户在 thought 轨道
    看到原因。切到视觉模型（下拉框就能切）后重发即可。
    """
    if not isinstance(content, list):
        return content
    agent = session.agent
    count = len(media.images_of(content))
    if not count or agent.registry.sees_images(agent.config.model):
        return content
    session.sink.emit(
        Notice(
            f"[消息附带 {count} 张图片，当前模型 {agent.config.model} 看不了，"
            "已降级为文字说明（模型下拉框换支持视觉的模型后重发，"
            "或用 XIAOYU_VISION_MODELS 点名放行）]",
            "warn",
        )
    )
    text = "".join(
        str(part.get("text") or "")
        for part in content
        if isinstance(part, dict) and part.get("type") == media.TEXT_PART
    )
    note = (
        f"[用户随消息附了 {count} 张图片，但当前模型 {agent.config.model} "
        "不接受图片输入，已省略。若图片是完成任务的关键，请告知用户换用"
        "支持视觉的模型后重发。]"
    )
    return f"{text}\n\n{note}" if text.strip() else note


class AcpSink:
    """UIEvent → session/update 翻译器（兼一个 session 的工具调用配对状态）。

    只被两类调用触碰：agent 工作线程（emit / 轮末 fail_open_calls）和同一
    线程里跑的 approver（current_call_id）——单线程访问，无需加锁。
    """

    def __init__(self, server: "AcpServer", session_id: str, workspace: Path) -> None:
        self._server = server
        self._session_id = session_id
        self._workspace = workspace
        self._seq = 0
        #  未终态的调用，按发生序：[{"id", "name", "running"}]
        self._open: list[dict[str, Any]] = []
        #  模式对账基准（track_mode 登记）：agent 侧改了模式（exit_plan_mode
        #  工具）要发 current_mode_update，client 发起的切换则只刷新基准
        self._agent: Agent | None = None
        self._mode = ""

    # ---------- 模式对账 ----------

    def track_mode(self, agent: Agent) -> None:
        """登记/刷新对账基准，不发通知。session 装配后调用一次；
        client 发起的 set_mode 成功后再调（切换结果由响应确认，不必回声）。"""
        self._agent = agent
        self._mode = agent.mode

    def sync_mode(self) -> None:
        """agent 侧的模式变化 → current_mode_update。挂在 emit 入口：
        exit_plan_mode 的 handler 改完模式，紧随其后的 ToolCompleted 就把
        通知带出去，client 的模式选择器在轮内就跟上，不用等轮末。"""
        if self._agent is not None and self._agent.mode != self._mode:
            self._mode = self._agent.mode
            self._update(
                {"sessionUpdate": "current_mode_update", "currentModeId": self._mode}
            )

    # ---------- 工具调用配对 ----------

    def _oldest(self, name: str, running: bool | None = None) -> dict[str, Any] | None:
        for call in self._open:
            if call["name"] == name and (running is None or call["running"] == running):
                return call
        return None

    def current_call_id(self, name: str) -> str | None:
        """审批桥用：正在等审批的调用（最老的同名未运行项）。"""
        call = self._oldest(name, running=False)
        return call["id"] if call else None

    # ---------- 出站 ----------

    def _update(self, update: dict[str, Any]) -> None:
        self._server._notify(
            "session/update", {"sessionId": self._session_id, "update": update}
        )

    def _absolute(self, path: str) -> str:
        raw = Path(path)
        return str(raw if raw.is_absolute() else self._workspace / raw)

    def _pending_extras(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        """编辑类调用的 diff 内容与位置——Zed 的 gutter/预览就吃这两样。"""
        extras: dict[str, Any] = {}
        path = args.get("path")
        if isinstance(path, str) and path.strip():
            extras["locations"] = [{"path": self._absolute(path)}]
            if name == "str_replace":
                extras["content"] = [
                    {
                        "type": "diff",
                        "path": self._absolute(path),
                        "oldText": str(args.get("old_str", "") or ""),
                        "newText": str(args.get("new_str", "") or ""),
                    }
                ]
            elif name == "write_file":
                old: str | None = None
                try:
                    old = Path(self._absolute(path)).read_text(encoding="utf-8")
                except OSError:
                    pass  # 新文件：oldText=null 即 ACP 的"新建"语义
                extras["content"] = [
                    {
                        "type": "diff",
                        "path": self._absolute(path),
                        "oldText": old,
                        "newText": str(args.get("content", "") or ""),
                    }
                ]
        return extras

    # ---------- 事件入口 ----------

    def emit(self, event: UIEvent) -> None:
        self.sync_mode()
        if isinstance(event, TextDelta):
            self._update(
                {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": event.text},
                }
            )
        elif isinstance(event, Notice):
            self._update(
                {
                    "sessionUpdate": "agent_thought_chunk",
                    "content": {"type": "text", "text": event.text},
                }
            )
        elif isinstance(event, ToolPending):
            self._seq += 1
            call_id = f"call-{self._seq}"
            self._open.append({"id": call_id, "name": event.name, "running": False})
            self._update(
                {
                    "sessionUpdate": "tool_call",
                    "toolCallId": call_id,
                    "title": _tool_title(event.name, event.args),
                    "kind": _TOOL_KINDS.get(event.name, "other"),
                    "status": "pending",
                    "rawInput": event.args,
                    **self._pending_extras(event.name, event.args),
                }
            )
        elif isinstance(event, ToolPurpose):
            call = self._oldest(event.name, running=False)
            if call is not None:
                self._update(
                    {
                        "sessionUpdate": "tool_call_update",
                        "toolCallId": call["id"],
                        "title": event.purpose,
                    }
                )
        elif isinstance(event, ToolRunning):
            call = self._oldest(event.name, running=False)
            if call is not None:
                call["running"] = True
                self._update(
                    {
                        "sessionUpdate": "tool_call_update",
                        "toolCallId": call["id"],
                        "status": "in_progress",
                    }
                )
        elif isinstance(event, ToolCompleted):
            call = self._oldest(event.name)
            if call is not None:
                self._open.remove(call)
                self._update(
                    {
                        "sessionUpdate": "tool_call_update",
                        "toolCallId": call["id"],
                        "status": "completed" if event.ok else "failed",
                        "content": [_text_content(event.output)],
                        "rawOutput": {"output": event.output},
                    }
                )
        elif isinstance(event, ToolDenied):
            call = self._oldest(event.name)
            if call is not None:
                self._open.remove(call)
                reason = "被 deny 规则拦下" if event.by == "rule" else "用户拒绝了本次调用"
                self._update(
                    {
                        "sessionUpdate": "tool_call_update",
                        "toolCallId": call["id"],
                        "status": "failed",
                        "content": [_text_content(reason)],
                    }
                )
        elif isinstance(event, PlanUpdated):
            self._update(
                {
                    "sessionUpdate": "plan",
                    "entries": [
                        {
                            "content": item.get("step", ""),
                            "priority": "medium",
                            "status": item.get("status", "pending"),
                        }
                        for item in event.plan
                    ],
                }
            )
        #  request.started/ended、text.end、steer.accepted：无 ACP 对应，丢弃

    def replay(self, messages: list[dict[str, Any]]) -> None:
        """session/load 的历史回放：整段对话补发成 session/update 流。

        与 render.replay_transcript 的结构同源（user/assistant/tool 三分支 +
        按 tool_call_id 配对），但不复用它：ACP 有一等的 user_message_chunk，
        用户消息当 Notice 回放会在 Zed 里变成"agent 的思考"；且协议回放要
        全文保真，不做 scrollback 式的行数截断。工具对走 emit(ToolPending/
        ToolCompleted) 现成通路——编辑类的 diff 内容、locations 白得，
        配对状态也在轮末清零（每个 pending 都立刻等到终态）。
        harness 注入文案的跳过纪律与 turn_starts/replay_transcript 同一套。
        """
        #  tool_call id → (工具名, 参数)；参数在历史里是 JSON 字符串
        calls: dict[str, tuple[str, dict[str, Any]]] = {}
        for record in messages:
            role = record.get("role")
            if role == "user":
                text = media.text_of(record.get("content"))
                if (
                    not text.strip()
                    or text in SYNTHETIC_USER_TEXTS
                    or text.startswith("[系统提示]")
                ):
                    continue
                self._update(
                    {
                        "sessionUpdate": "user_message_chunk",
                        "content": {"type": "text", "text": text},
                    }
                )
            elif role == "assistant":
                for call in record.get("tool_calls") or []:
                    function = call.get("function") or {}
                    try:
                        args = json.loads(function.get("arguments") or "{}")
                    except (json.JSONDecodeError, TypeError):
                        args = {}
                    if not isinstance(args, dict):
                        args = {}
                    calls[str(call.get("id") or "")] = (
                        str(function.get("name") or "tool"),
                        args,
                    )
                text = media.text_of(record.get("content"))
                if text.strip():
                    self._update(
                        {
                            "sessionUpdate": "agent_message_chunk",
                            "content": {"type": "text", "text": text},
                        }
                    )
            elif role == "tool":
                name, args = calls.pop(str(record.get("tool_call_id") or ""), ("", {}))
                if not name:
                    name = str(record.get("name") or "tool")
                output = media.text_of(record.get("content"))
                self.emit(ToolPending(name, args))
                self.emit(
                    ToolCompleted(
                        name, output=output, ok=not output.startswith("ERROR"), seconds=0.0
                    )
                )
        #  没等到结果的调用（会话死在工具执行中）不补发——restore 已在 agent
        #  历史里用"结果未知"文案修复，回放侧照 replay_transcript 的取舍：不求完备

    def fail_open_calls(self, reason: str) -> None:
        """轮末收尾：把没等到终态的 ACP tool_call 全部置 failed（取消/异常路径）。"""
        open_calls, self._open = self._open, []
        for call in open_calls:
            self._update(
                {
                    "sessionUpdate": "tool_call_update",
                    "toolCallId": call["id"],
                    "status": "failed",
                    "content": [_text_content(reason)],
                }
            )


class _Session:
    def __init__(
        self, session_id: str, agent: Agent, sink: AcpSink, workspace: Path
    ) -> None:
        self.session_id = session_id
        self.agent = agent
        self.sink = sink
        self.workspace = workspace  # 相对路径归一用（哨兵、diff 同一基准）
        self.cancelled = False  # 本轮是否收到过 session/cancel（决定 stopReason）
        #  本会话的当前轮工作线程；turn 状态只在主线程（stdin 分发循环）读写，
        #  会话内同一时刻只有一轮
        self.turn: threading.Thread | None = None
        #  client 下拉框当前显示的模型：轮末与 config.model 对账，粘性降级
        #  （_stream_with_recovery 改 config.model）后发 config_option_update 校正
        self.advertised_model = agent.config.model

    def turn_active(self) -> bool:
        return self.turn is not None and self.turn.is_alive()


#  工厂签名：cli 侧负责 Config/Permissions/SessionLog 装配，本层只管协议。
#  approver/sink 由本层先造好传入（与 wire 的 attach 回填同源的解耦，换个方向）。
#  (workspace, approver, sink, session_name, create) → (agent, 待回放的历史)。
#  create=True 新建同名命名会话（历史必空）；False 找回既有会话并 restore 好
#  agent（copy=False 续写原文件），找不到抛 LookupError——本层映射成
#  INVALID_PARAMS，其余异常一律 INTERNAL_ERROR。
def client_server_specs(raw: Any) -> tuple[list["mcp.ServerSpec"], list[str]]:
    """session/new 里 client 下发的 mcpServers → ServerSpec 列表 + 跳过说明。

    这些 server 是**用户在编辑器里亲手配的**（不是仓库文件里躺着的），所以
    folder trust 那道门不管它——那道门防的是"clone 即拉起进程"。但准入闸照过：
    mcp_guard.admission_violation 判的是"这条命令像不像攻击"，与来源无关。

    四条刻意的边界（评审定死，别放宽）：
    ① **session 作用域、绝不落盘**：不写进任何 .mcp.json，关掉编辑器就没了；
    ② **同名以 client 为准**（合并在 mcp.launch 里做）；
    ③ **`${VAR}` 不兑现**：配置文件里的占位符是小羽自己的约定，client 下发的
       值是编辑器已经算好的最终值——再展开一次等于拿本进程环境去改写用户
       在编辑器里看到的东西；
    ④ **stdio 与 http（Streamable HTTP）都收**，老式 sse 不收——initialize 的
       mcpCapabilities 里怎么声明，这里就怎么收，两处必须一致：声明了收不了，
       用户看到的是 server 凭空消失。远端地址过 endpoint_violation（明文 http
       只许回环——headers 里放的是凭据）。
    """
    specs: list[mcp.ServerSpec] = []
    skipped: list[str] = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", "") or "").strip()
        #  形状即类型（与配置文件同一判据）：有 url 就是远端，type 只用来纠错
        kind = str(item.get("type", "") or ("http" if item.get("url") else "stdio"))
        if kind == "http":
            url = str(item.get("url", "") or "").strip()
            if not name or not url:
                skipped.append(f"{name or '(未命名)'}：缺 name 或 url")
                continue
            #  规范里 headers 与 env 一样是 [{name,value}] 数组
            headers = {
                str(entry["name"]): str(entry.get("value", ""))
                for entry in item.get("headers") or []
                if isinstance(entry, dict) and entry.get("name")
            }
            if reason := mcp_guard.endpoint_violation(url):
                skipped.append(f"{name}：被安全规则拦截（{reason}）")
                continue
            specs.append(mcp.ServerSpec(name=name, command="", url=url, headers=headers))
            continue
        if kind != "stdio":
            #  只剩老式 sse：initialize 里我们就声明了 sse=false，规矩的 client
            #  不会发；真发了也说清楚，不静默少一个 server
            skipped.append(f"{name or '(未命名)'}：{kind} 传输未实现（能力里已声明不支持）")
            continue
        command = str(item.get("command", "") or "").strip()
        if not name or not command:
            skipped.append(f"{name or '(未命名)'}：缺 name 或 command")
            continue
        args = [str(a) for a in item.get("args") or [] if isinstance(a, (str, int, float))]
        #  规范里 env 是 [{name, value}] 数组，不是对象——照抄成字典会静默丢掉全部
        #  环境变量（server 起来了但连不上后端，最难查的那种失败）
        env = {
            str(entry["name"]): str(entry.get("value", ""))
            for entry in item.get("env") or []
            if isinstance(entry, dict) and entry.get("name")
        }
        if reason := mcp_guard.admission_violation(command, args, env):
            skipped.append(f"{name}：被安全规则拦截（{reason}）")
            continue
        specs.append(mcp.ServerSpec(name=name, command=command, args=args, env=env))
    return specs, skipped


AgentFactory = Callable[[Path, Any, AcpSink, str, bool], tuple[Agent, list[dict[str, Any]]]]


def _factory_takes_servers(factory: Any) -> bool:
    """工厂是否接受 mcp_servers 关键字（收不到就当它是 0.34.0 时代的旧签名）。"""
    try:
        params = inspect.signature(factory).parameters
    except (TypeError, ValueError):  # C 实现/不可内省的可调用对象
        return False
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return True
    return "mcp_servers" in params


def resolve_mcp_view(
    config: Config,
    mcp_view: "mcp.McpView | None",
    mcp_servers: "list[mcp.ServerSpec] | None",
) -> "tuple[mcp.McpView | None, str]":
    """定 session 用哪个 MCP 视图，返回 (视图, 该出声的说明)。

    抽成纯决策（唯一副作用是真要合并时 launch 一个 manager）是为了让"忽略"
    这件事没有藏身处：**每一条把 client 下发清单丢掉的路径都必须给出说明**。
    第一版漏了宿主注入那条——宿主打开网关注入 server、以为生效了，实际一条
    都没进去，且没有任何输出。这正是这条协议面最该防的失败形态，而它比丢在
    协议层更深、更难查。

    三条路径：
    - 宿主显式传了 view：以宿主为准（它离用户意图更近，且两个"替代配置发现"
      的注入面叠在一起没有合理语义）——但要出声；
    - 本机 enable_mcp=false：忽略并出声。宿主显式传 view 时该开关不拦（那是
      宿主程序自己的决定），但协议对端不该有权重新打开一个被本机关掉的子系统；
    - 其余：与配置发现合并（同名 client 优先，合并在 mcp.launch 里做），
      manager 登记进 at-exit 清扫。
    """
    if not mcp_servers:
        return mcp_view, ""
    count = len(mcp_servers)
    if mcp_view is not None:
        return mcp_view, (
            f"acp：宿主已注入 MCP 工具视图，忽略 client 随会话下发的 {count} 条 server"
            "（两者都是替代配置发现的注入面，以宿主为准）"
        )
    if not config.enable_mcp:
        return None, (
            f"acp：本机 MCP 已关闭（enable_mcp=false），忽略 client 下发的 {count} 条 server"
        )
    manager = mcp.launch(config, extra_specs=mcp_servers)
    if manager is None:  # pragma: no cover - 带 extra_specs 时 launch 恒有返回
        #  空清单也会给出空 manager（见 launch_specs 的 docstring）。真走到这里
        #  只可能是将来把那条约定改坏了——别编一句"server 起不来"糊过去，那会
        #  把内部错误伪装成用户的配置问题。
        return None, "acp：MCP 视图构造失败（内部错误，请报 issue）"
    return mcp.McpView(manager, "all"), ""


def build_agent_factory(
    *,
    model: str | None = None,
    base_url: str | None = None,
    append_system_prompt: str | None = None,
    auto_approve: bool | None = None,
    mode: str | None = None,
    sandbox: bool | None = None,
    sandbox_network: bool | None = None,
    mcp_view: "mcp.McpView | None" = None,
) -> AgentFactory:
    """造一个 AcpServer 要的 agent 工厂：每 session 现配 Config/Permissions/SessionLog。

    ACP 的工作区随 session/new 的 cwd 来（编辑器一个项目一个 session），所以
    Agent 不在启动期装配，而是给 server 一个工厂。这条装配链原本只长在
    `cli.acp_main` 里，嵌入宿主（自己起 AcpServer、注入自己的 stdin/stdout 或
    工具箱）想复用只能整段抄——抄一次就漂一次，实测漏抄 install_exit_logging
    是第一个真实案例。所以它在这里公开，CLI 自己也走这一份。

    参数即 CLI 那几个旗标的库面等价物；**空值一律等于"不覆盖"**：
    `Config.from_env` 的 overrides 是 `if value is not None` 才 setattr，所以
    空串会真的把模型名覆盖成空。这里把 "" 归一成 None，宿主传哪种都安全。

    mcp_view：宿主自带 MCP 清单时传进来，替代（而非合并）配置发现——
    见 tools.Toolbox 的同名参数。不传就走配置发现，与 CLI 行为一致。

    工厂返回的函数额外收一个 mcp_servers（AcpServer 把 client 随 session/new
    下发的清单传进来，见 client_server_specs）：与配置发现合并、同名 client
    优先。宿主显式传了 mcp_view 时以宿主为准，client 那份忽略——两个"替代
    配置发现"的注入面叠在一起没有合理语义，宿主离用户意图更近。

    folder trust 按信任表非交互判定（headless 纪律）：TUI 里 --trust 过的目录
    自然放行，没记录的不吃工作区级 .mcp.json/permissions/.env，也绝不在协议
    通道上发问。
    """

    def build_agent(
        workspace: Path,
        approver: Any,
        sink: AcpSink,
        session_name: str,
        create: bool,
        mcp_servers: "list[mcp.ServerSpec] | None" = None,
    ) -> tuple[Agent, list[dict[str, Any]]]:
        trusted = folder_trust.evaluate(workspace, interactive=False).verdict == "trusted"
        config = Config.from_env(
            workspace=workspace,
            model=model or None,
            base_url=base_url or None,
            auto_approve=auto_approve or None,
            mode=mode or None,
            sandbox=sandbox,
            sandbox_network=sandbox_network,
            append_system_prompt=append_system_prompt or None,
            workspace_trusted=trusted,
        )
        permissions = Permissions.load(config.workspace, include_workspace=trusted)
        #  ACP sessionId 即命名会话名（sess-<hex> 天然过 check_session_id）：
        #  session/load 靠它跨进程找回同一个文件。create 时名字是新造的 uuid，
        #  不可能撞上已有会话；load 则必须找得到——open_named 的"无则建"语义
        #  在这里是错的（静默用空会话冒充旧会话），所以先 find_named 把关。
        history: list[dict[str, Any]] = []
        follow_mode = ""
        if create:
            session_log = SessionLog.create(
                config.model, str(config.workspace), session_id=session_name
            )
        else:
            try:
                #  client 给的名字要进文件名 glob，先过与 --session-id 同一道闸；
                #  不合规的名字必然不是本 agent 发的 id，按"未知会话"处理
                check_session_id(session_name)
            except ValueError as exc:
                raise LookupError(f"非法 sessionId {session_name!r}：{exc}") from exc
            path = find_named(session_name, str(config.workspace))
            if path is None:
                raise LookupError(
                    f"未知 sessionId {session_name!r}（本工作区没有对应的会话记录）"
                )
            #  恢复时优先跟随旧会话最后生效的模型（/model、下拉框、粘性降级的
            #  留痕都算），而不是本次启动的默认/--model——在 open_named 之前
            #  覆盖，reopened 事件记下的就是跟随后的模型。读不出来保持现配置。
            if recorded := last_model(path):
                config.model = recorded
            #  模式同一纪律：历史里可能带着 plan 进出说明，模式不跟上，
            #  模型以为在规划态而关卡全开（或反过来）。restore 之后 adopt
            follow_mode = last_mode(path)
            session_log, history = open_named(
                session_name, config.model, str(config.workspace)
            )
        view, note = resolve_mcp_view(config, mcp_view, mcp_servers)
        if note:
            print(note, file=sys.stderr)
        agent = Agent(
            config,
            Toolbox(config, mcp_view=view),
            approver=approver,
            session_log=session_log,
            permissions=permissions,
            sink=sink,
        )
        if history:
            #  与 --session-id 续写同款：copy=False，接上下文但不把历史再抄一遍
            agent.restore(history, copy=False)
        if follow_mode:
            agent.adopt_mode(follow_mode)
        install_exit_logging(session_log)
        return agent, history

    return build_agent


class AcpServer:
    """stdio JSON-RPC server。一个进程多 session（每 session 一个 Agent），
    轮按 session 并行：会话内同一时刻只跑一轮（同步架构的约束是会话级的），
    本会话忙时回 BUSY；不同会话的轮在各自工作线程同时推进——Zed 开两个
    project、或"一进程 demux 多会话"的 client 都靠这个。"""

    def __init__(
        self,
        agent_factory: AgentFactory,
        stdin: TextIO | None = None,
        stdout: TextIO | None = None,
    ) -> None:
        self._factory = agent_factory
        #  工厂能不能接 client 下发的 server 清单：构造期探一次，别每次 try/except
        #  （工厂内部真抛 TypeError 会被误读成签名不符）。旧签名的自建工厂
        #  （嵌入宿主 0.34.0 时代写的那种）照常工作，只是收不到 client 的清单。
        self._factory_takes_servers = _factory_takes_servers(agent_factory)
        self._stdin = stdin or sys.stdin
        self._stdout = stdout or sys.stdout
        self._write_lock = threading.Lock()
        self._sessions: dict[str, _Session] = {}
        #  挂起的 server→client 审批：id → (Event, 结果槽, 所属 sessionId)。
        #  归属必须记：cancel 只解决自己会话的挂起项，不能替别的会话拒审批
        self._pending: dict[str, tuple[threading.Event, dict[str, Any], str]] = {}
        self._pending_lock = threading.Lock()
        #  server→client 请求的 id 序号：多个会话的工作线程并发取号，锁内自增
        self._request_seq = 0
        self._closed = False
        self._mcp_note_shown = False
        #  initialize 时 client 声明的 fs.readTextFile：未保存缓冲区哨兵的开关
        self._client_fs_read = False

    # ---------- 嵌入宿主的访问面 ----------

    def agent_for(self, session_id: str) -> Agent | None:
        """按 sessionId 取活着的 Agent，没有就 None。

        给嵌入宿主用：内核有些能力没有 ACP 协议面——`Agent.steer`（线程安全的
        轮内插话）就是，规范里没有对应方法。宿主在协议之外的通道上收到这类
        指令时，要能落到对应会话的 Agent 上。

        为什么是访问面而不是在协议里自造 `_session/steer`：方言要两边都实现、
        且会被误当成标准面（models/set_model 那次的教训），而访问面只是把内核
        已有能力交出去，谁需要谁在自己那层翻译——与"宿主方言全留在宿主那层"
        同一条分界线。

        返回的是活对象：会话被 session/load 顶替后旧 Agent 就不在册了，
        宿主每次现取，别缓存。
        """
        session = self._sessions.get(session_id)
        return session.agent if session is not None else None

    # ---------- 出站 ----------

    def _send(self, message: dict[str, Any]) -> None:
        with self._write_lock:
            self._stdout.write(json.dumps(message, ensure_ascii=False) + "\n")
            self._stdout.flush()

    def _notify(self, method: str, params: dict[str, Any]) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def _respond(self, req_id: Any, result: dict[str, Any]) -> None:
        self._send({"jsonrpc": "2.0", "id": req_id, "result": result})

    def _fail(self, req_id: Any, code: int, message: str) -> None:
        self._send({"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}})

    # ---------- 主循环 ----------

    def serve(self) -> int:
        #  stdout 接管：协议流的唯一写口是 _send（走构造时捕获的 self._stdout）。
        #  服务期间把 sys.stdout 指到 stderr——进程内任何漏网的 print（插件、
        #  hook、第三方库的调试输出）都不再能污染 JSON-RPC 流。这类污染的
        #  症状是 client 侧"解析失败/会话卡死"，离病因（某个 print）极远，
        #  堵在出口比逐处排查便宜。诊断输出照旧可读：只是换到了 stderr。
        original_stdout = sys.stdout
        sys.stdout = sys.stderr
        try:
            for raw in self._stdin:
                line = raw.strip()
                if not line:
                    continue
                try:
                    message = json.loads(line)
                except json.JSONDecodeError as exc:
                    self._fail(None, PARSE_ERROR, f"JSON 解析失败：{exc}")
                    continue
                if not isinstance(message, dict):
                    self._fail(None, INVALID_REQUEST, "必须是 JSON 对象")
                    continue
                if "method" in message:
                    self._handle_request(message)
                elif "id" in message:
                    self._resolve_pending(message)
                else:
                    self._fail(None, INVALID_REQUEST, "既不是请求也不是响应")
        except KeyboardInterrupt:
            pass
        finally:
            sys.stdout = original_stdout
            self._shutdown()
        return 0

    # ---------- client → server ----------

    def _handle_request(self, message: dict[str, Any]) -> None:
        req_id = message.get("id")
        method = message.get("method")
        params = message.get("params") or {}
        if not isinstance(params, dict):
            self._fail(req_id, INVALID_PARAMS, "params 必须是对象")
            return

        if method == "initialize":
            capabilities = params.get("clientCapabilities")
            fs = capabilities.get("fs") if isinstance(capabilities, dict) else None
            self._client_fs_read = bool(isinstance(fs, dict) and fs.get("readTextFile"))
            client_version = params.get("protocolVersion")
            version = (
                client_version
                if isinstance(client_version, int) and 0 < client_version <= PROTOCOL_VERSION
                else PROTOCOL_VERSION  # 不认识的版本：答自己支持的最新，client 决定去留
            )
            self._respond(
                req_id,
                {
                    "protocolVersion": version,
                    "agentCapabilities": {
                        "loadSession": True,
                        "promptCapabilities": {
                            "image": True,
                            "audio": False,
                            "embeddedContext": True,
                        },
                        #  client 据此决定敢不敢下发远端 server。如实声明：
                        #  http=Streamable HTTP（规范 2025-06-18，已实现）；
                        #  sse=2024-11-05 的老传输，没实现就不能说有——声明了
                        #  收不了，用户看到的是 server 凭空消失。
                        "mcpCapabilities": {"http": True, "sse": False},
                    },
                    "agentInfo": {"name": "xiaoyu", "title": "小羽", "version": __version__},
                    "authMethods": [AUTH_METHOD],
                },
            )
        elif method == "authenticate":
            #  terminal 型的"认证"发生在向导进程里（写用户级 .env）。这里要做的
            #  是把新写的键补进本进程环境（setdefault：真实环境变量仍优先），
            #  下一个 session/new 就能组出 provider 表，无需重启 agent
            load_dotenv(explicit=user_env_path())
            self._respond(req_id, {})
        elif method == "session/new":
            self._handle_new_session(req_id, params)
        elif method == "session/load":
            self._handle_load_session(req_id, params)
        elif method == "session/prompt":
            self._handle_prompt(req_id, params)
        elif method == "session/set_config_option":
            self._handle_set_config_option(req_id, params)
        elif method == "session/set_mode":
            self._handle_set_mode(req_id, params)
        elif method == "session/cancel":
            #  notification：无论成败都不回包
            self._handle_cancel(params)
        else:
            self._fail(req_id, METHOD_NOT_FOUND, f"未知方法 {method!r}")

    def _workspace_from(self, req_id: Any, params: dict[str, Any]) -> Path | None:
        """session/new 与 session/load 共用的 cwd 校验；失败已回错误、返回 None。"""
        cwd = str(params.get("cwd", "") or "")
        if not cwd or not Path(cwd).is_absolute():
            self._fail(req_id, INVALID_PARAMS, "cwd 必须是绝对路径")
            return None
        workspace = Path(cwd)
        if not workspace.is_dir():
            self._fail(req_id, INVALID_PARAMS, f"工作区不存在：{workspace}")
            return None
        return workspace

    def _build_session(
        self,
        req_id: Any,
        workspace: Path,
        session_id: str,
        create: bool,
        client_servers: "list[mcp.ServerSpec] | None" = None,
    ) -> "_Session | None":
        """装配一个 session（新建或找回）并登记；失败已回错误、返回 None。"""
        sink = AcpSink(self, session_id, workspace)

        def approver(name: str, args: dict[str, Any]) -> Allow | Deny:
            return self._request_permission(session_id, name, args)

        extra: dict[str, Any] = {}
        if client_servers:
            if self._factory_takes_servers:
                extra["mcp_servers"] = client_servers
            elif not self._mcp_note_shown:
                self._mcp_note_shown = True
                print(
                    "acp：当前 agent 工厂不接 client 下发的 mcpServers，"
                    f"本次忽略 {len(client_servers)} 条",
                    file=sys.stderr,
                )
        try:
            try:
                agent, history = self._factory(
                    workspace, approver, sink, session_id, create, **extra
                )
            except MissingConfig:
                #  向导可能刚在别的终端跑完而本进程环境还是旧的：补读用户级
                #  .env（setdefault）再试一次；仍缺配置才回 auth_required——
                #  client 见到它就引导用户走 authMethods 里的 terminal 向导
                load_dotenv(explicit=user_env_path())
                agent, history = self._factory(
                    workspace, approver, sink, session_id, create, **extra
                )
        except UnknownModel as exc:
            #  provider 配好了、模型名解析不了：不是认证问题，别把用户往向导上引
            self._fail(req_id, INTERNAL_ERROR, str(exc))
            return None
        except MissingConfig as exc:
            self._fail(req_id, AUTH_REQUIRED, str(exc))
            return None
        except LookupError as exc:
            self._fail(req_id, INVALID_PARAMS, str(exc) or f"未知 sessionId {session_id!r}")
            return None
        except Exception as exc:  # noqa: BLE001 - 配置类失败要发给 client，不是炸进程
            self._fail(req_id, INTERNAL_ERROR, f"{type(exc).__name__}: {exc}")
            return None
        session = _Session(session_id, agent, sink, workspace)
        self._sessions[session_id] = session
        #  基准先于 replay 登记：回放期间模式没变，不该冒出 current_mode_update
        sink.track_mode(agent)
        if not create:
            #  规范：load 必须先把整段对话回放成 update 流，之后才允许回包
            sink.replay(history)
        return session

    def _handle_new_session(self, req_id: Any, params: dict[str, Any]) -> None:
        workspace = self._workspace_from(req_id, params)
        if workspace is None:
            return
        session_id = f"sess-{uuid.uuid4().hex[:16]}"
        session = self._build_session(
            req_id, workspace, session_id, create=True,
            client_servers=self._client_servers(params),
        )
        if session is None:
            return
        self._respond(
            req_id,
            {
                "sessionId": session_id,
                "configOptions": config_options(session.agent),
                "modes": mode_state(session.agent),
            },
        )
        self._advertise_commands(session_id)

    def _handle_load_session(self, req_id: Any, params: dict[str, Any]) -> None:
        workspace = self._workspace_from(req_id, params)
        if workspace is None:
            return
        session_id = str(params.get("sessionId", "") or "")
        if not session_id:
            self._fail(req_id, INVALID_PARAMS, "sessionId 不能为空")
            return
        existing = self._sessions.get(session_id)
        if existing is not None and existing.turn_active():
            #  同一 id 正在跑轮还要重载：拒掉，不能从正在写日志的会话脚下抽文件
            self._fail(req_id, BUSY, "该会话本轮进行中，不能重载")
            return
        session = self._build_session(
            req_id, workspace, session_id, create=False,
            client_servers=self._client_servers(params),
        )
        if session is None:
            return
        #  Zed 从 load 回包里读 selectors 与 modes（与 session/new 同款），照给
        self._respond(
            req_id,
            {
                "configOptions": config_options(session.agent),
                "modes": mode_state(session.agent),
            },
        )
        self._advertise_commands(session_id)

    def _client_servers(self, params: dict[str, Any]) -> "list[mcp.ServerSpec]":
        """client 随 session/new、session/load 下发的 mcpServers。

        跳过的条目一律打 stderr：静默少几个 server 的表现是"某个工具就是不
        存在"，离病因极远——正是这条协议面最该防的失败形态。
        """
        specs, skipped = client_server_specs(params.get("mcpServers"))
        for note in skipped:
            print(f"acp：client 下发的 MCP server 跳过 —— {note}", file=sys.stderr)
        return specs

    def _advertise_commands(self, session_id: str) -> None:
        """session 建好后广告斜杠命令（notification，跟在回包之后）。
        命令集是静态的，每 session 广告一次即可。"""
        self._notify(
            "session/update",
            {
                "sessionId": session_id,
                "update": {
                    "sessionUpdate": "available_commands_update",
                    "availableCommands": available_commands(),
                },
            },
        )

    def _handle_set_config_option(self, req_id: Any, params: dict[str, Any]) -> None:
        session = self._sessions.get(str(params.get("sessionId", "")))
        if session is None:
            self._fail(req_id, INVALID_PARAMS, "未知 sessionId（先 session/new）")
            return
        config_id = str(params.get("configId", ""))
        if config_id not in ("model", "mode"):
            self._fail(req_id, INVALID_PARAMS, "未知 configId（可用：model、mode）")
            return
        value = params.get("value")
        if not isinstance(value, str) or not value.strip():
            self._fail(req_id, INVALID_PARAMS, "value 需要非空字符串")
            return
        if config_id == "mode":
            self._set_mode_via(req_id, session, value.strip(), config_face=True)
            return
        if session.turn_active():
            #  跑轮期间 config.model 归降级链（粘性写在工作线程），主线程不抢
            self._fail(req_id, BUSY, "本轮进行中不能切模型（等本轮结束或 session/cancel）")
            return
        #  与 TUI /model 同一动作：切换粘性生效于下一次请求，并在日志留痕
        #  （session/load 跟随旧模型靠它）。候选之外的名字也收（TUI 同款
        #  自由度）：显式寻址/新模型不该被下拉框挡住
        session.agent.switch_model(value.strip())
        session.advertised_model = session.agent.config.model
        self._respond(req_id, {"configOptions": config_options(session.agent)})

    def _handle_set_mode(self, req_id: Any, params: dict[str, Any]) -> None:
        session = self._sessions.get(str(params.get("sessionId", "")))
        if session is None:
            self._fail(req_id, INVALID_PARAMS, "未知 sessionId（先 session/new）")
            return
        self._set_mode_via(
            req_id, session, str(params.get("modeId", "") or ""), config_face=False
        )

    def _set_mode_via(
        self, req_id: Any, session: _Session, mode_id: str, config_face: bool
    ) -> None:
        """切模式的唯一动作，session/set_mode 与 set_config_option(mode) 共用。

        两条协议面对同一状态，所以**改完要给另一条面发回声**：client 发起的
        那条由响应确认（不回声，否则等于替它复述），另一条面上的选择器若不
        校正就会停在旧值——同一个会话里两个选择器显示不同模式，比没有第二条
        面更糟。config_face 就是用来分辨"哪条面是发起方"的。
        """
        if mode_id not in modes.BY_NAME:
            #  刻意不走 modes.get 的"不认识退回默认档"：那是配置读取的容错，
            #  协议面收到未知 id 该明说，静默切去默认档等于换了个模式冒充
            self._fail(
                req_id,
                INVALID_PARAMS,
                f"未知模式 {mode_id!r}（可用：{'、'.join(modes.CYCLE)}）",
            )
            return
        if session.turn_active():
            #  切模式会往历史注入 plan 进出说明，不能与工作线程抢 messages
            self._fail(req_id, BUSY, "本轮进行中不能切模式（等本轮结束或 session/cancel）")
            return
        session.agent.set_mode(mode_id)
        if config_face:
            #  发起方是 configOptions：回包带新的 configOptions，另一条面
            #  （modes）靠 current_mode_update 校正——sync_mode 比对基准后自己发
            session.sink.sync_mode()
            self._respond(req_id, {"configOptions": config_options(session.agent)})
        else:
            #  发起方是 set_mode：响应即确认，只刷新对账基准不回声；
            #  另一条面（configOptions）发 config_option_update 校正
            session.sink.track_mode(session.agent)
            self._respond(req_id, {})
            self._notify_config_options(session)

    def _notify_config_options(self, session: _Session) -> None:
        self._notify(
            "session/update",
            {
                "sessionId": session.session_id,
                "update": {
                    "sessionUpdate": "config_option_update",
                    "configOptions": config_options(session.agent),
                },
            },
        )

    def _handle_prompt(self, req_id: Any, params: dict[str, Any]) -> None:
        session = self._sessions.get(str(params.get("sessionId", "")))
        if session is None:
            self._fail(req_id, INVALID_PARAMS, "未知 sessionId（先 session/new）")
            return
        content = prompt_content(params.get("prompt"))
        #  部件列表按构造必含图片，只有纯文本形态才可能整条为空
        if isinstance(content, str) and not content.strip():
            self._fail(req_id, INVALID_PARAMS, "prompt 需要非空内容")
            return
        if session.turn_active():
            self._fail(req_id, BUSY, "该会话已有一轮在进行中（可 session/cancel 打断）")
            return
        session.cancelled = False
        session.turn = threading.Thread(
            target=self._run_turn,
            args=(req_id, session, content),
            daemon=True,
            name=f"acp-turn-{session.session_id}",
        )
        session.turn.start()

    def _run_turn(
        self, req_id: Any, session: _Session, content: str | list[dict[str, Any]]
    ) -> None:
        agent = session.agent
        stop_reason = "end_turn"
        error = ""
        try:
            if isinstance(content, str) and (matched := match_command(content)) is not None:
                #  命中命令：不进模型、不入历史，输出走 agent_message_chunk。
                #  照样跑在工作线程——/compact 会调模型做摘要，主循环得空着收 cancel
                command, args = matched
                output = command.run(agent, args)
                session.sink.emit(TextDelta(output if output.strip() else "（无输出）"))
            else:
                agent.send(_degrade_images(session, content))
        except (Interrupted, KeyboardInterrupt):
            agent.close_open_tool_calls("本轮被 ACP client 取消。")
            stop_reason = "cancelled"
        except Exception as exc:  # noqa: BLE001 - client 要结构化错误，不是 traceback
            if session.cancelled:
                #  规范：取消后必须回 cancelled，不能回 error——打断打在异常路径上
                #  （网络层把中断包装成了别的异常）也算取消
                stop_reason = "cancelled"
            else:
                error = f"{type(exc).__name__}: {exc}"
                if agent.session_log:
                    agent.session_log.event("error", error=error)
        finally:
            session.sink.fail_open_calls(
                "调用未完成（本轮已取消或出错）" if not error else f"调用未完成（{error}）"
            )
        #  轮末兜底对账：模式在 handler 之后再无事件跟出（异常路径）也不能漏
        session.sink.sync_mode()
        if session.advertised_model != agent.config.model:
            #  粘性降级在本轮换了模型：先校正 client 的下拉框，再回 prompt 结果
            session.advertised_model = agent.config.model
            self._notify(
                "session/update",
                {
                    "sessionId": session.session_id,
                    "update": {
                        "sessionUpdate": "config_option_update",
                        "configOptions": config_options(agent),
                    },
                },
            )
        if error:
            self._fail(req_id, INTERNAL_ERROR, error)
        else:
            self._respond(req_id, {"stopReason": stop_reason})

    def _handle_cancel(self, params: dict[str, Any]) -> None:
        session = self._sessions.get(str(params.get("sessionId", "")))
        #  取消未知/空闲 session 是静默 no-op。挂起审批必须解决（否则该会话的
        #  工作线程永远阻塞在 Event 上），但只解决本会话的——别的会话的审批
        #  各归各轮，不能被替死
        if session is None:
            return
        if session.turn_active():
            session.cancelled = True
        self._deny_pending("用户取消了本轮", session_id=session.session_id)
        session.agent.interrupt()

    def _deny_pending(self, reason: str, session_id: str | None = None) -> None:
        """挂起审批按 cancelled 解决。session_id=None 解决全部（EOF/收尾），
        否则只解决该会话名下的（session/cancel 的作用域）。"""
        with self._pending_lock:
            if session_id is None:
                pending = list(self._pending.values())
                self._pending.clear()
            else:
                matched = [
                    key for key, entry in self._pending.items() if entry[2] == session_id
                ]
                pending = [self._pending.pop(key) for key in matched]
        for event, slot, _owner in pending:
            slot["result"] = {"outcome": {"outcome": "cancelled"}, "_reason": reason}
            event.set()

    def _register_pending(
        self, session_id: str
    ) -> tuple[str, threading.Event, dict[str, Any]]:
        """登记一个 server→client 请求：锁内取号+挂表，多会话工作线程并发安全。"""
        event = threading.Event()
        slot: dict[str, Any] = {}
        with self._pending_lock:
            self._request_seq += 1
            req_id = f"srv-{self._request_seq}"
            self._pending[req_id] = (event, slot, session_id)
        return req_id, event, slot

    # ---------- server → client（fs 只读哨兵） ----------

    def _read_client_file(self, session_id: str, path: str, timeout: float = 10.0) -> str | None:
        """fs/read_text_file 桥：拿 client（编辑器缓冲区）视角的文件内容。

        拿不到（无能力/超时/error 回包/连接关闭）一律 None——哨兵 fail-open，
        绝不因为一次探测挡住正常审批流程。超时后把挂起项摘掉，迟到的响应由
        _resolve_pending 的 pop-None 分支静默丢弃。
        """
        if not self._client_fs_read or self._closed:
            return None
        req_id, event, slot = self._register_pending(session_id)
        self._send(
            {
                "jsonrpc": "2.0",
                "id": req_id,
                "method": "fs/read_text_file",
                "params": {"sessionId": session_id, "path": path},
            }
        )
        waited = 0.0
        while not event.wait(0.2):
            waited += 0.2
            if self._closed or waited >= timeout:
                with self._pending_lock:
                    self._pending.pop(req_id, None)
                return None
        result = slot.get("result")
        if isinstance(result, dict) and isinstance(result.get("content"), str):
            return result["content"]
        return None

    def _buffer_conflict(self, session: _Session, name: str, args: dict[str, Any]) -> str:
        """未保存缓冲区哨兵：编辑类调用在审批前比对 client 视角与磁盘。

        返回冲突说明；空串=无冲突或无从判断（fail-open）。只在审批点生效
        ——auto/--yolo 放行的编辑不过审批点也就不过哨兵，是该档"少问"
        取舍的一部分。磁盘始终是唯一事实源：哨兵只拦，不把缓冲区内容
        当成文件真相去用。
        """
        if name not in ("write_file", "str_replace"):
            return ""
        path = args.get("path")
        if not isinstance(path, str) or not path.strip():
            return ""
        raw = Path(path).expanduser()
        absolute = raw if raw.is_absolute() else session.workspace / raw
        buffer = self._read_client_file(session.session_id, str(absolute))
        if buffer is None:
            return ""
        try:
            disk = absolute.read_text(encoding="utf-8", errors="replace")
        except OSError:
            #  磁盘上还没有这个文件、编辑器里却读得到内容 = 未保存的新文件
            disk = None
        if disk is not None and buffer == disk:
            return ""
        return (
            f"文件 {path} 在编辑器里有未保存的改动（缓冲区与磁盘不一致），"
            "为避免覆盖用户未保存的工作，本次编辑已拦下。"
            "请提示用户先保存该文件，然后重试。"
        )

    # ---------- server → client（审批） ----------

    def _request_permission(self, session_id: str, name: str, args: dict[str, Any]) -> Allow | Deny:
        """Agent 的 approver（在工作线程被调用）：发 session/request_permission，
        阻塞等 client 响应。连接关闭 fail closed 按拒绝。编辑类调用先过
        未保存缓冲区哨兵（_buffer_conflict），冲突直接按拒绝解决——理由
        原文回灌模型（"先保存再重试"），不再打扰用户按一次注定错的允许。"""
        if self._closed:
            return Deny("acp 连接已关闭，无人审批")
        session = self._sessions.get(session_id)
        if session is not None:
            conflict = self._buffer_conflict(session, name, args)
            if conflict:
                return Deny(conflict)
        call_id = session.sink.current_call_id(name) if session else None
        #  能推导出"范围恰好覆盖这类调用"的安全规则才给"总是允许"选项
        #  （与 TUI 确认框同一判据）；推不出只是少一个选项
        rule = (
            suggest_allow_rule(name, args, session.agent.permissions.workspace)
            if session is not None
            else None
        )
        options: list[dict[str, Any]] = [
            {"optionId": "allow-once", "name": "允许", "kind": "allow_once"},
        ]
        scope = (
            session.agent.permissions.session_grant_label(name, args)
            if session is not None
            else name
        )
        if scope is not None:
            #  协议没有"会话作用域"的 kind：借最接近的 allow_always 渲染，
            #  真实语义由 optionId 承载、回程按 id 还原
            options.append(
                {
                    "optionId": "allow-session",
                    "name": f"本会话内 {scope} 都允许",
                    "kind": "allow_always",
                }
            )
        if rule is not None:
            options.append(
                {
                    "optionId": "allow-always",
                    "name": f"总是允许（写入规则 {rule}）",
                    "kind": "allow_always",
                }
            )
        options.append({"optionId": "reject-once", "name": "拒绝", "kind": "reject_once"})
        req_id, event, slot = self._register_pending(session_id)
        tool_call: dict[str, Any] = {
            "title": _tool_title(name, args),
            "kind": _TOOL_KINDS.get(name, "other"),
            "status": "pending",
            "rawInput": args,
        }
        if call_id:
            tool_call["toolCallId"] = call_id
        self._send(
            {
                "jsonrpc": "2.0",
                "id": req_id,
                "method": "session/request_permission",
                "params": {
                    "sessionId": session_id,
                    "toolCall": tool_call,
                    "options": options,
                },
            }
        )
        while not event.wait(0.2):
            if self._closed:
                return Deny("acp 连接已关闭，无人审批")
        return self._resolve_verdict(session, name, args, rule, slot.get("result"))

    def _resolve_verdict(
        self,
        session: "_Session | None",
        name: str,
        args: dict[str, Any],
        rule: Any,
        payload: Any,
    ) -> Allow | Deny:
        """审批回包 → Allow/Deny，含会话授权与持久规则的落地。
        看不懂的一律 fail closed 按拒绝。"""
        if not isinstance(payload, dict):
            return Deny("acp client 返回了无法解析的审批结果")
        outcome = payload.get("outcome")
        if not isinstance(outcome, dict):
            return Deny("acp client 的 outcome 不是对象")
        kind = outcome.get("outcome")
        if kind == "cancelled":
            return Deny(str(payload.get("_reason", "") or "客户端取消了审批"))
        if kind != "selected":
            return Deny("acp client 返回了未知的 outcome")
        option = outcome.get("optionId")
        if option == "allow-once":
            return Allow()
        if option == "allow-session" and session is not None:
            scope = session.agent.permissions.grant_session_call(name, args)
            if scope is not None:
                session.sink.emit(
                    Notice(f"[本会话内 {scope} 不再逐次确认（/perm 可查看）]", "info")
                )
            return Allow()
        if option == "allow-always" and session is not None and rule is not None:
            try:
                path = session.agent.permissions.add_persistent(rule)
            except ValueError as exc:
                #  持久 allow 的安全禁令照常生效。用户的意图明确是放行，
                #  本次照批，只是规则不落盘——差别要说出来，不能静默
                session.sink.emit(Notice(f"[规则未写入：{exc}；已仅允许本次]", "warn"))
                return Allow()
            session.sink.emit(Notice(f"[已写入 {path}：{rule}]", "info"))
            return Allow()
        return Deny("用户拒绝了本次调用")

    def _resolve_pending(self, message: dict[str, Any]) -> None:
        req_id = message.get("id")
        with self._pending_lock:
            entry = self._pending.pop(str(req_id), None)
        if entry is None:
            return  # 迟到/重复的响应：cancel 或 shutdown 已替它做了决定
        event, slot, _owner = entry
        if "result" in message:
            slot["result"] = message["result"]
        #  error 响应或缺 result：slot 留空，_resolve_verdict(None) fail closed
        event.set()

    # ---------- 收尾 ----------

    def _shutdown(self) -> None:
        self._closed = True
        self._deny_pending("acp 连接已关闭")
        turns: list[threading.Thread] = []
        for session in self._sessions.values():
            if session.turn_active():
                session.cancelled = True
                session.agent.interrupt()
                turns.append(session.turn)  # type: ignore[arg-type]  # turn_active 保证非 None
        #  共享一个 10s 预算：N 个会话不叠加成 N×10s 的退出等待
        deadline = time.monotonic() + 10.0
        for turn in turns:
            turn.join(timeout=max(0.0, deadline - time.monotonic()))
        #  client 断开（EOF）不经任何别的收尾，日志此前只能等 atexit 记成
        #  normal——disconnect 更接近真相。放在 join 之后：轮还在跑就落 exit
        #  的话，后面的消息会写到 exit 事件的下面。close 幂等，atexit 再关一次
        #  是 no-op（进程级钩子见 session_log.install_exit_logging）。
        for session in self._sessions.values():
            if session.agent.session_log is not None:
                session.agent.session_log.close("disconnect")


