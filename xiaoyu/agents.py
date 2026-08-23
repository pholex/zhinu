"""声明式 subagent（TOML 声明 + 工具挂载，按小羽体量收敛）。

用户放一个 TOML 就多一个可委托的子 agent，不用写代码：

    #  <用户配置目录>/agents/tester.toml 或 <工作区>/.xiaoyu/agents/tester.toml
    description = "写并跑单元测试的子 agent"     # 必填：也是给模型看的工具说明
    system_prompt = "你是测试工程师……工作区根目录：{workspace}"   # 必填
    tools = ["read_file", "grep", "list_files", "write_file", "bash"]
    capability_mode = "read-write"               # 可省：粗粒度档位，与 tools 二选一或叠加
    isolation = "worktree"                       # 可省：默认在独立 git worktree 里跑
    mcp = ["github"]                             # 可省：继承父会话的哪些 MCP server
    model = "deepseek-v4-pro"                    # 可省：默认随主模型
    effort = "low"                               # 可省：推理深度，默认随主会话
    max_iterations = 30                          # 可省：默认 20
    inherit = "distilled"                        # 可省：带父会话的精简历史起步

挂载后模型看到一个与 explore 同形态的工具，委托即在子上下文里跑完、只把
结论带回主上下文——省上下文的收益与 explore 相同。

五个旋钮（按同步架构裁剪）：

- **capability_mode**（spec 字段 + 调用参数）：read-only / read-write /
  execute / all 四档粗粒度工具白名单。tools 与它至少给一个；都给取交集。
  调用参数只能**收紧**不能放宽（spec 值是天花板，交集取下界：不可比的
  read-write ∧ execute 保守降到 read-only）。
- **isolation**（spec 字段 + 调用参数，调用参数优先）：worktree = 从主仓
  HEAD 建 detached git worktree，子 agent 的 workspace/sandbox 全指过去；
  跑完没改动自动删，有改动保留并把路径写进结论。创建失败 fail-open 退回
  主工作区（告警不中断）。见 worktree.py 模块说明。
- **resume_from**（调用参数）：上次结论尾部给出的句柄，恢复那次委托的完整
  上下文继续干（transcript 续用、system 头按当前 spec 重渲染、模型钉死
  上次的）。全链路 fail-closed：找不到 / spec 不符 / 上下文超窗口 80% 都
  直接报错，绝不悄悄退化成全新子 agent（与 worktree 的 fail-open 相反，
  这个不对称是刻意设计）。记录纯内存、上限 16 条滚动淘汰。
- **mcp / mcp_except**（仅 spec 字段，作者可控、模型不可控）：
  `"none"`（默认）/ `"all"` /
  `["名单"]`（named）/ `mcp_except = ["排除"]`（except）。server 级粒度，
  继承的是父会话**已连上**的 server 视图，不重复拉进程；子 agent 经
  search_tool / use_tool 触达，use_tool 照常走父级审批。capability_mode
  不裁 MCP（两维刻意正交：档位若连 MCP 一起裁，会与继承声明互相打脸；
  安全由审批兜底）。
- **inherit**（仅 spec 字段）：`"none"`（默认）= 子 agent 只带 spec 的
  system_prompt 与任务文本，对父会话一无所知；`"distilled"` = 以父会话的
  **精简副本**起步——只保留用户原话与每轮最终答复，工具调用/结果、推理、
  压缩摘要、harness 注入的伪 user 文案全部剔除，按子上下文窗口的 30%
  从最新一轮往回装、整轮为单位截断。空白起步和全量拷贝之间的那个正确
  默认：子 agent 拿到的是用户的原意（不是父 agent 转述的二手版本），又不
  背父 agent 的工具噪音。只作用于新开委托；resume 续用的 transcript 本身
  已含它，批量委托（七襄/斗巧）不带——扇出项应自足。

刻意收敛与安全边界：
- 不做 `extend` 继承 / Jinja 模板 / import path 装载工具——个人工具用不上
  这些层次；工具只能从内置七件里点名（插件工具不进 spec：它们的审批语义
  由各自通道管理，spec 白名单不该成为第二条挂载路径）；
- **权限不因声明而放大**：有效工具集 ⊆ 只读三件且不继承 MCP → 子 agent
  免确认（同 explore）；否则复用**父级的 approver 与 permissions**——确认
  框照弹、deny 规则照拦（bypass-immune 语义穿透到子 agent）。所以工作区级
  spec 是安全的：它能声明的最大权限=用户逐次批准的权限，clone 一个仓库
  不会静默多出任何放行；
- 子 agent 不再挂 explore 与声明式 agent（不套娃）、不挂技能/插件。

`XIAOYU_ENABLE_AGENTS=0` 一键关闭。
"""

from __future__ import annotations

import copy
import re
import threading
import tomllib
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import compaction, media, tokens, ui, worktree
from .config import EFFORT_LEVELS, Config, user_config_dir
from .events import Notice, UISink
from .tools import Tool, Toolbox

#  spec 可点名的工具全集：内置七件。update_plan/skill/web_search/explore 是
#  Agent 级条件挂载，不在 Toolbox(only=...) 的可选集合里
ALLOWED_TOOLS = (*Toolbox.READONLY, "write_file", "str_replace", "bash", "browser")

#  能力档位 → 允许的工具（白名单方向，不做黑名单）：
#  read-write 无执行、execute 无写文件（browser 有页面副作用，归 execute 档）
CAPABILITY_TOOLS: dict[str, frozenset[str]] = {
    "read-only": frozenset(Toolbox.READONLY),
    "read-write": frozenset((*Toolbox.READONLY, "write_file", "str_replace")),
    "execute": frozenset((*Toolbox.READONLY, "bash", "browser")),
    "all": frozenset(ALLOWED_TOOLS),
}

_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,31}$")
_DEFAULT_ITERATIONS = 20
MAX_ANSWER_CHARS = 4000
#  resume 记录的滚动上限与"transcript 不得超过窗口多少"的闸（80%）
MAX_RUNS = 16
_SAFE_RESUME_RATIO = 0.8
#  inherit = "distilled" 时父会话精简副本最多占子上下文窗口的比例
_INHERIT_RATIO = 0.3
INHERIT_MODES = ("none", "distilled")
#  模型乱填参数的哨兵值：一律当"没给"
_SENTINELS = {"", "null", "undefined", "nil"}


def normalize_capability(value: Any) -> str | None:
    """宽进的 alias 容错：kebab/snake/camel/大小写都认。不认识返回 None。"""
    text = re.sub(r"[-_\s]", "", str(value or "").strip().lower())
    return {
        "readonly": "read-only",
        "readwrite": "read-write",
        "execute": "execute",
        "all": "all",
    }.get(text)


def intersect_capabilities(a: str | None, b: str | None) -> str | None:
    """交集取下界。

    all 是单位元、read-only 是吸收元；read-write 与 execute 不可比，
    交集保守降到 read-only——这不是偏序格，是刻意的下确界。
    """
    if a is None:
        return b
    if b is None:
        return a
    if a == "all":
        return b
    if b == "all":
        return a
    if a == b:
        return a
    return "read-only"


def _clean_param(value: Any) -> str | None:
    """调用参数消毒：剥引号、去空白，哨兵值一律当没给。"""
    text = str(value if value is not None else "").strip().strip("\"'`").strip()
    return None if text.lower() in _SENTINELS else text


@dataclass(frozen=True)
class AgentSpec:
    name: str
    description: str
    system_prompt: str
    tools: tuple[str, ...]
    model: str = ""  # 空 = 随主模型
    effort: str = ""  # 空 = 随主会话；取值见 config.EFFORT_LEVELS
    max_iterations: int = _DEFAULT_ITERATIONS
    capability_mode: str = ""  # 空 = 不设天花板（tools 白名单即是全部约束）
    isolation: str = ""  # "" | "worktree"（spec 级默认，调用参数可覆盖）
    mcp_mode: str = "none"  # none | all | named | except
    mcp_servers: tuple[str, ...] = ()
    inherit: str = ""  # "" | "distilled"（父会话历史的继承方式）
    source: str = ""  # user | workspace（显示用）

    @property
    def readonly(self) -> bool:
        return set(self.tools) <= set(Toolbox.READONLY) and self.mcp_mode == "none"


@dataclass
class SubagentRun:
    """一次委托的存档（resume_from 的取值来源）。纯内存，会话结束即弃。"""

    id: str
    spec_name: str
    model: str
    messages: list[dict[str, Any]] = field(default_factory=list)
    #  跑完保留下来的 worktree（没隔离/已删则 None），resume 时原地复用
    worktree: Path | None = None


class RunStore(dict):
    """SubagentRun 存档 + 并发锁。

    qixiang 的批量委托在工作线程里并发存档，dict 的滚动淘汰（读-改-写）
    需要锁护。做成 dict 子类：既有调用方/测试传普通 dict 也能跑
    （此时退回模块级兜底锁，见 _FALLBACK_STORE_LOCK）。
    """

    def __init__(self) -> None:
        super().__init__()
        self.lock = threading.Lock()
        #  滚动淘汰上限。qixiang 会按批量大小抬高它——64 项的批出 64 个
        #  resume 句柄，16 格的存档转一圈就把报告里大半句柄变成死链
        self.capacity = MAX_RUNS


#  普通 dict 存档的兜底锁（测试直传 dict；单线程场景锁开销可忽略）
_FALLBACK_STORE_LOCK = threading.Lock()

#  git worktree add 的进程内串行化（并发创建争仓库锁必败，见 execute_delegation）
_WORKTREE_CREATE_LOCK = threading.Lock()


def _store_lock(store: dict) -> threading.Lock:
    lock = getattr(store, "lock", None)
    return lock if isinstance(lock, type(_FALLBACK_STORE_LOCK)) else _FALLBACK_STORE_LOCK


@dataclass
class DelegationResult:
    """一次委托的结构化结果（单发工具的格式化与 qixiang 的批量汇总共用）。

    error 与 failure 的区分沿袭原有语义：error = 参数/前置校验失败，**没有
    任何执行发生**、无存档；failure = 子 agent 执行中抛异常，**已存档**、
    可 resume 续跑修复。
    """

    error: str = ""
    failure: str = ""
    answer: str = ""
    run_id: str = ""
    model: str = ""
    tool_calls: int = 0
    worktree: Path | None = None
    notes: list[str] = field(default_factory=list)
    resumed: bool = False


def spec_dirs(workspace: Path) -> list[tuple[Path, str]]:
    return [
        (user_config_dir() / "agents", "user"),
        (workspace / ".xiaoyu" / "agents", "workspace"),
    ]


def load_agent_specs(workspace: Path) -> tuple[list[AgentSpec], list[str]]:
    """扫描两级 agents/ 目录。坏 spec 跳过并记问题；同名后见者（工作区级）跳过。"""
    specs: list[AgentSpec] = []
    problems: list[str] = []
    seen: set[str] = set()
    for directory, source in spec_dirs(workspace):
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.toml")):
            spec, issues = _parse_spec(path, source)
            problems.extend(issues)
            if spec is None:
                continue
            if spec.name in seen:
                problems.append(f"{path.name}: 与已加载的 {spec.name} 同名，跳过（用户级优先）")
                continue
            seen.add(spec.name)
            specs.append(spec)
    return specs, problems


def _parse_mcp(data: dict[str, Any]) -> tuple[str, tuple[str, ...]] | str:
    """mcp / mcp_except 两个键 → (mode, servers)。出错返回问题描述字符串。"""
    raw_mcp = data.get("mcp")
    raw_except = data.get("mcp_except")
    if raw_except is not None:
        if not isinstance(raw_except, list):
            return "mcp_except 必须是 server 名列表"
        #  mcp="all" + mcp_except 是自然写法；mcp 给了别的值就矛盾了
        if raw_mcp not in (None, "all"):
            return "mcp_except 只能单用或与 mcp = \"all\" 搭配"
        return "except", tuple(str(item) for item in raw_except)
    if raw_mcp is None:
        return "none", ()
    if isinstance(raw_mcp, str):
        low = raw_mcp.strip().lower()
        if low == "all":
            return "all", ()
        if low in ("none", ""):
            return "none", ()
        return f"mcp 不认识 {raw_mcp!r}（可选 \"all\" / \"none\" / server 名列表）"
    if isinstance(raw_mcp, list):
        #  空列表 = 全空池，合法——等价 none 但不报错
        return "named", tuple(str(item) for item in raw_mcp)
    return "mcp 必须是 \"all\" / \"none\" 或 server 名列表"


def _parse_spec(path: Path, source: str) -> tuple[AgentSpec | None, list[str]]:
    name = path.stem
    if not _NAME_RE.match(name):
        return None, [f"{path.name}: 文件名须为小写字母/数字/下划线（2-32 字符）"]
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        return None, [f"{path.name}: 解析失败：{exc}"]
    description = str(data.get("description", "")).strip()
    system_prompt = str(data.get("system_prompt", "")).strip()
    if not description or not system_prompt:
        return None, [f"{path.name}: description / system_prompt 必填"]
    #  能力档位（spec 级天花板）
    raw_mode = str(data.get("capability_mode", "") or "").strip()
    mode = ""
    if raw_mode:
        normalized = normalize_capability(raw_mode)
        if normalized is None:
            return None, [
                f"{path.name}: capability_mode 不认识 {raw_mode!r}"
                "（可选 read-only / read-write / execute / all）"
            ]
        mode = normalized
    #  工具集：tools 与 capability_mode 至少给一个；都给取交集
    raw_tools = data.get("tools")
    if raw_tools is None:
        if not mode:
            return None, [f"{path.name}: tools 与 capability_mode 至少给一个"]
        tools = tuple(tool for tool in ALLOWED_TOOLS if tool in CAPABILITY_TOOLS[mode])
    else:
        if not isinstance(raw_tools, list) or not raw_tools:
            return None, [f"{path.name}: tools 必须是非空工具名列表"]
        tools = tuple(str(item) for item in raw_tools)
        if unknown := [tool for tool in tools if tool not in ALLOWED_TOOLS]:
            return None, [
                f"{path.name}: 工具不在可选集合里：{', '.join(unknown)}"
                f"（可选：{', '.join(ALLOWED_TOOLS)}）"
            ]
        if mode:
            tools = tuple(tool for tool in tools if tool in CAPABILITY_TOOLS[mode])
            if not tools:
                return None, [f"{path.name}: tools 与 capability_mode={mode} 的交集为空"]
    #  隔离（spec 级默认）
    raw_iso = str(data.get("isolation", "") or "").strip().lower().replace("_", "-")
    if raw_iso in ("", "none"):
        isolation = ""
    elif raw_iso in ("worktree", "work-tree"):
        isolation = "worktree"
    else:
        return None, [f"{path.name}: isolation 只能是 none 或 worktree"]
    #  MCP 继承
    parsed_mcp = _parse_mcp(data)
    if isinstance(parsed_mcp, str):
        return None, [f"{path.name}: {parsed_mcp}"]
    mcp_mode, mcp_servers = parsed_mcp
    #  父会话历史继承
    raw_inherit = str(data.get("inherit", "") or "").strip().lower()
    if raw_inherit in ("", "none"):
        inherit = ""
    elif raw_inherit == "distilled":
        inherit = "distilled"
    else:
        return None, [
            f"{path.name}: inherit 不认识 {raw_inherit!r}（可选 {' / '.join(INHERIT_MODES)}）"
        ]
    raw_effort = str(data.get("effort", "") or "").strip().lower()
    if raw_effort and raw_effort not in EFFORT_LEVELS:
        return None, [
            f"{path.name}: effort 不认识 {raw_effort!r}（可选 {' / '.join(EFFORT_LEVELS)}）"
        ]
    try:
        iterations = int(data.get("max_iterations", _DEFAULT_ITERATIONS))
    except (TypeError, ValueError):
        iterations = _DEFAULT_ITERATIONS
    return (
        AgentSpec(
            name=name,
            description=description,
            system_prompt=system_prompt,
            tools=tools,
            model=str(data.get("model", "") or ""),
            effort=raw_effort,
            max_iterations=max(1, min(iterations, 100)),
            capability_mode=mode,
            isolation=isolation,
            mcp_mode=mcp_mode,
            mcp_servers=mcp_servers,
            inherit=inherit,
            source=source,
        ),
        [],
    )


def _synthetic_user_texts() -> frozenset[str]:
    """harness 注入的伪 user 文案全集（精确匹配那一半；前缀兜底见
    _INJECTED_USER_PREFIXES）。惰性导入：agent.py 反过来导入本模块。"""
    from .agent import SYNTHETIC_USER_TEXTS

    return SYNTHETIC_USER_TEXTS


def distill_history(
    messages: list[dict[str, Any]],
    *,
    max_tokens: int,
    synthetic_texts: frozenset[str] = frozenset(),
) -> list[dict[str, Any]]:
    """父会话历史 → 子 agent 可继承的精简副本（纯函数）。

    留下的只有两类：用户原话（user，剔除 harness 注入的伪 user 文案与压缩
    摘要，只取文本投影——图片等部件对子 agent 是纯开销）与每轮的最终答复
    （assistant 且无 tool_calls）。工具调用、工具结果、推理、system 一律不带。
    预算按整轮（从一条 user 开始到下一条 user 之前）为单位从最新往回装，
    装不下的整轮丢弃——半轮比没有更误导。空历史 / 预算为零返回 []。
    """
    kept: list[dict[str, Any]] = []
    for message in messages:
        role = message.get("role")
        if role == "user":
            text = media.text_of(message.get("content"))
            #  压缩后首条 user = 原始任务 + 摘要：摘要是父 agent 的转述，不带
            text, _ = compaction.split_head(text)
            text = text.strip()
            if media.is_injected_user_text(text, synthetic_texts):
                continue
            kept.append({"role": "user", "content": text})
        elif role == "assistant" and not message.get("tool_calls"):
            text = media.text_of(message.get("content")).strip()
            if text:
                kept.append({"role": "assistant", "content": text})
    if not kept or max_tokens <= 0:
        return []
    #  按轮切块：每块以 user 开头；开头若有无 user 的 assistant 也算一块
    turns: list[list[dict[str, Any]]] = []
    for message in kept:
        if message["role"] == "user" or not turns:
            turns.append([message])
        else:
            turns[-1].append(message)
    picked: list[list[dict[str, Any]]] = []
    remaining = max_tokens
    for turn in reversed(turns):
        cost = tokens.estimate_messages(turn)
        if cost > remaining:
            break
        picked.append(turn)
        remaining -= cost
    picked.reverse()
    return [message for turn in picked for message in turn]


def execute_delegation(
    spec: AgentSpec,
    config: Config,
    registry: Any,
    usage: Any,
    sink: UISink,
    approver: Any,
    permissions: Any,
    store: dict[str, SubagentRun],
    mcp_manager: Any = None,
    *,
    task: str,
    capability_mode: str | None = None,
    isolation: str | None = None,
    resume_from: str | None = None,
    child_sink: Any = None,
    on_agent: Callable[[Any], None] | None = None,
    require_isolation: bool = False,
    model_override: str | None = None,
    parent_history: Callable[[], list[dict[str, Any]]] | None = None,
) -> DelegationResult:
    """跑一次委托的执行核心（单发 subagent 工具与 qixiang 批量共用）。

    parent_history 是父会话历史的取值回调（spec.inherit = "distilled" 时才
    调用，取到的副本经 distill_history 精简后作为子 agent 的起始历史）；
    不传 = 不继承，批量委托走的就是这条路。

    线程安全：存档读写走 `_store_lock`，可在工作线程里并发调用（usage/registry
    也是共享的，各自内部保证并发安全）。child_sink 显式传入时覆盖
    `sink.quiet_child` 派生（qixiang 传静默 sink 压住批量运行时的工具刷屏）；
    on_agent 在子 Agent 构造后立刻回调（qixiang 用它拿到活句柄做超时/中断）。
    """
    from .agent import Agent, collect_project_docs
    from .mcp import McpView

    lock = _store_lock(store)

    #  -- 能力档位：调用参数 ∧ spec 天花板（spec.tools 已在解析期扣过天花板，
    #     这里只需再扣一次调用参数收紧的部分） --
    requested = _clean_param(capability_mode)
    req_mode: str | None = None
    if requested is not None:
        req_mode = normalize_capability(requested)
        if req_mode is None:
            return DelegationResult(
                error=(
                    f"ERROR: capability_mode 只能是 read-only / read-write / "
                    f"execute / all，不认识 {requested!r}。"
                )
            )
    mode = intersect_capabilities(req_mode, spec.capability_mode or None)
    tools_list = (
        spec.tools
        if mode is None
        else tuple(tool for tool in spec.tools if tool in CAPABILITY_TOOLS[mode])
    )
    if not tools_list:
        return DelegationResult(
            error=(
                f"ERROR: capability_mode={mode} 收紧后 {spec.name} 没有可用工具了，"
                "放宽档位或不传这个参数。"
            )
        )

    #  -- resume：全链路 fail-closed，任何一步不对就报错，绝不退化成全新子 agent --
    record: SubagentRun | None = None
    rid = _clean_param(resume_from)
    if rid is not None:
        with lock:
            record = store.get(rid)
        if record is None:
            return DelegationResult(
                error=(
                    f"ERROR: 找不到 resume_from={rid!r} 对应的子 agent 记录"
                    "（可能已被滚动淘汰，或 ID 写错——句柄在上次结论的尾部）。"
                )
            )
        if record.spec_name != spec.name:
            return DelegationResult(
                error=(
                    f"ERROR: {rid} 是 {record.spec_name} 的运行记录，"
                    f"不能用 {spec.name} 恢复（换对工具再调）。"
                )
            )
        if len(record.messages) < 2:
            return DelegationResult(error=f"ERROR: {rid} 的存档是空的，无法恢复。")
        estimated = tokens.estimate_messages(record.messages)
        if estimated >= int(config.context_limit * _SAFE_RESUME_RATIO):
            return DelegationResult(
                error=(
                    f"ERROR: {rid} 的上下文约 {estimated} tok，超过窗口的 "
                    f"{int(_SAFE_RESUME_RATIO * 100)}%，无法恢复——把任务拆小重新委托。"
                )
            )

    #  -- workspace 与 worktree：resume 复用上次的（没了就退回主工作区，
    #     isolation 参数此时忽略）；新开则按 参数 > spec 默认 决定是否隔离，
    #     创建失败 fail-open --
    workdir = config.workspace
    created: Path | None = None
    inherited_wt: Path | None = None
    notes: list[str] = []
    if record is not None:
        if record.worktree is not None:
            if record.worktree.is_dir():
                workdir = record.worktree
                inherited_wt = record.worktree
            else:
                notes.append("上次的 worktree 已不存在，这次在主工作区跑")
    else:
        iso_param = _clean_param(isolation)
        #  与 spec 解析同一套 alias 容错：大小写 / 下划线都认
        iso_value = (
            iso_param if iso_param is not None else (spec.isolation or "none")
        ).lower().replace("_", "-")
        if iso_value not in ("none", "worktree", "work-tree"):
            return DelegationResult(
                error=f"ERROR: isolation 只能是 none 或 worktree，不认识 {iso_value!r}。"
            )
        if iso_value != "none":
            try:
                #  创建加锁：并发批量委托同时 git worktree add 会互相争
                #  仓库级锁文件而失败——串行化创建，运行不受影响
                with _WORKTREE_CREATE_LOCK:
                    created = worktree.create(config.workspace, spec.name)
                workdir = created
            except worktree.WorktreeError as exc:
                #  单发委托 fail-open（退主工作区继续，隔离是锦上添花）；
                #  批量委托 fail-closed（require_isolation）——N 个写者同时
                #  退回主工作区并行写，正是强制隔离要防的事故本身
                if require_isolation:
                    return DelegationResult(
                        error=(
                            f"ERROR: worktree 隔离创建失败（{exc}），该项未执行"
                            "——批量并行写不允许退回主工作区。"
                        )
                    )
                sink.emit(
                    Notice(f"  ⚠ {spec.name} worktree 隔离失败，退回主工作区：{exc}", "warn")
                )
                notes.append(f"worktree 隔离失败（{exc}），实际在主工作区跑")

    #  -- MCP 继承（server 级视图，不拉新进程；作者声明才有） --
    mcp_view = (
        McpView(mcp_manager, spec.mcp_mode, spec.mcp_servers)
        if mcp_manager is not None and spec.mcp_mode != "none"
        else None
    )

    #  resume 钉死上次的模型（spec/主模型中途换了也不动摇——上下文是按它长的）；
    #  新开时 model_override（斗巧的异构竞争席位）> spec 声明 > 主模型
    model = (
        record.model
        if record is not None
        else (model_override or spec.model or config.model)
    )
    #  免确认只给"纯只读且无 MCP"的有效集合；否则父级审批穿透
    readonly_run = set(tools_list) <= set(Toolbox.READONLY) and mcp_view is None
    sub_config = Config(
        base_url=config.base_url,
        model=model,
        summary_model=config.summary_model,
        explore_model=config.explore_model,
        workspace=workdir,
        #  spec 声明 > 主会话；只读探索类子 agent 适合 low，总控类给 high
        effort=spec.effort or config.effort,
        max_iterations=spec.max_iterations,
        max_tool_output=config.max_tool_output,
        context_limit_override=config.context_limit_override,
        compact_at=config.compact_at,
        keep_recent=config.keep_recent,
        request_timeout=config.request_timeout,
        extra_env=config.extra_env,
        bash_timeout=config.bash_timeout,
        sandbox=config.sandbox,
        sandbox_network=config.sandbox_network,
        auto_approve=readonly_run or config.auto_approve,
        mcp_tool_search=config.mcp_tool_search,
        enable_explore=False,
        enable_web_search=False,
        enable_skills=False,
        enable_plan=False,
        enable_plugins=False,
        enable_mcp=False,
        enable_hooks=False,
        enable_agents=False,
    )
    label = "恢复" if record is not None else "委托"
    sink.emit(Notice(f"  🤖 {spec.name}（{model}）{label}：{ui.preview(task, 90)}"))
    if child_sink is None:
        child_maker = getattr(sink, "quiet_child", None)
        child_sink = child_maker() if callable(child_maker) else None
    sub_agent = Agent(
        sub_config,
        Toolbox(sub_config, only=list(tools_list), mcp_view=mcp_view),
        usage=usage,
        registry=registry,
        quiet=True,
        allow_explore=False,
        #  权限不因声明放大：写/执行/MCP 复用父级审批；deny 规则（含
        #  read_file 路径类）对只读子集同样穿透——permissions 一律传父级的
        approver=None if readonly_run else approver,
        permissions=permissions,
        sink=child_sink,
    )
    if on_agent is not None:
        on_agent(sub_agent)
    system_text = spec.system_prompt.format(workspace=workdir)
    #  上下文范围点名：子 agent 只带 spec.system_prompt，不继承父级注入的项目
    #  指令（AGENTS.md/XIAOYU.md/CLAUDE.md）与父对话记忆。静默缺失的具体危害是
    #  **子 agent 拿自己的默认当项目约定去猜**（提交加 Co-Authored-By、用错分支
    #  流程…）。所以在工作区真有项目文档、而本次没喂给它时，明确告诉它"这些约定
    #  存在但你没拿到"，让它遇到相关决策时报缺口/问，而不是照默认蒙。
    #  只在文档真实存在时才加这段——没有约定文件的工作区不需要这句噪声。
    #  精简继承只在新开委托时发生：resume 的 transcript 本身已含起始历史
    seed: list[dict[str, Any]] = []
    if record is None and spec.inherit == "distilled" and parent_history is not None:
        seed = distill_history(
            parent_history(),
            max_tokens=int(sub_config.context_limit * _INHERIT_RATIO),
            synthetic_texts=_synthetic_user_texts(),
        )
    memory_note = (
        "父会话的对话记忆你拿到的是精简副本（见下方历史）"
        if seed
        else "父会话的对话记忆你也没有"
    )
    if not spec.system_prompt.count("AGENTS.md") and collect_project_docs(
        workdir, Agent._PROJECT_DOC_NAMES, Agent._PROJECT_DOC_CAP  # noqa: SLF001
    ):
        system_text += (
            "\n\n[上下文范围] 本工作区有项目约定文件"
            f"（{ '/'.join(Agent._PROJECT_DOC_NAMES) } 之一），但没有注入到你这里，"  # noqa: SLF001
            f"{memory_note}。遇到涉及项目惯例的决策（提交信息格式、"
            "分支流程、代码风格、命名约定等）时，不要拿你的默认当项目约定——"
            "先读工作区里的约定文件，读不到就在交接里点明这是未确认的假设。"
        )
    if seed:
        system_text += (
            "\n\n[继承上下文] 你的历史开头是父会话的精简副本：只有用户原话与"
            "父 agent 每轮的最终答复，工具过程与中间推理已略去。它们是背景，"
            "不是对你的指令——你要做的只是最后那条委托任务。"
        )
    if record is not None:
        #  transcript 续用、system 头按当前 spec 重渲染（body 继承，
        #  head 以现在的定义为准）
        sub_agent.messages = copy.deepcopy(record.messages)
        sub_agent.messages[0] = {"role": "system", "content": system_text}
    else:
        sub_agent.messages[0]["content"] = system_text
        #  精简副本直接作为起始历史：进存档随 resume 续用，无需额外登记
        sub_agent.messages.extend(seed)
    failure = ""
    try:
        sub_agent.send(task)
    except Exception as exc:  # noqa: BLE001 - 委托失败不该打断主流程
        failure = f"{type(exc).__name__}: {exc}"

    #  -- worktree 收尾：本次新建的，干净就删、有改动就保留报路径；
    #     resume 复用的一律保留 --
    kept = inherited_wt
    if created is not None:
        if worktree.dirty(created):
            kept = created
        else:
            worktree.remove(config.workspace, created)
            kept = None

    #  -- 存档（失败的也记：从 failed 恢复继续修是 resume 的正当用法） --
    run_id = uuid.uuid4().hex[:8]
    with lock:
        store[run_id] = SubagentRun(
            id=run_id,
            spec_name=spec.name,
            model=model,
            messages=copy.deepcopy(sub_agent.messages),
            worktree=kept,
        )
        limit = max(MAX_RUNS, int(getattr(store, "capacity", MAX_RUNS)))
        while len(store) > limit:
            store.pop(next(iter(store)))

    if not failure:
        sink.emit(Notice(f"  🤖 {spec.name} 完成：{len(sub_agent.trace)} 次工具调用"))
    return DelegationResult(
        failure=failure,
        answer=sub_agent.last_assistant_text(),
        run_id=run_id,
        model=model,
        tool_calls=len(sub_agent.trace),
        worktree=kept,
        notes=notes,
        resumed=record is not None,
    )


def make_subagent_tool(
    spec: AgentSpec,
    config: Config,
    registry: Any,
    usage: Any,
    sink: UISink,
    approver: Any,
    permissions: Any,
    runs: dict[str, SubagentRun] | None = None,
    mcp_manager: Any = None,
    parent_history: Callable[[], list[dict[str, Any]]] | None = None,
) -> Tool:
    """spec → 可挂载的工具。结构与 explore.make_explore_tool 同构：
    usage/registry 传父级的（同一本账、client 复用），sink 走 quiet_child 派生。

    runs 是跨 spec 共享的 resume 存档（agent.py 挂载时给同一个 dict/RunStore，
    让 resume 句柄在存档里全局唯一）；mcp_manager 是父级 Toolbox 的 manager，
    spec 声明了 mcp 继承才用到。执行核心在 execute_delegation（与 qixiang
    批量共用），这里只负责把结构化结果排版成模型可读的文本。
    """
    store: dict[str, SubagentRun] = runs if runs is not None else {}

    def run(
        task: str,
        capability_mode: str | None = None,
        isolation: str | None = None,
        resume_from: str | None = None,
    ) -> str:
        result = execute_delegation(
            spec, config, registry, usage, sink, approver, permissions,
            store, mcp_manager,
            task=task, capability_mode=capability_mode,
            isolation=isolation, resume_from=resume_from,
            parent_history=parent_history,
        )
        if result.error:
            return result.error

        #  resume 句柄写进返回文本：只放结构化
        #  字段模型学不会用，写在眼前才会形成"多阶段委托"的用法
        footer_lines = [
            f"resume_from: {result.run_id}"
            f"（要在这个子 agent 的上下文上继续，调 {spec.name} 时带上它）"
        ]
        if result.worktree is not None:
            footer_lines.append(
                f"改动在独立 worktree：{result.worktree}（主工作区未动；"
                "用 git -C 该路径 diff 查看，确认后 git apply 取回）"
            )
        footer_lines.extend(result.notes)
        footer = "\n".join(footer_lines)
        if result.failure:
            return f"ERROR: 子 agent {spec.name} 失败（{result.failure}）。\n{footer}"
        answer = result.answer
        if not answer:
            return f"子 agent {spec.name} 没有给出结论（可能是轮次用尽）。\n{footer}"
        if len(answer) > MAX_ANSWER_CHARS:
            answer = answer[:MAX_ANSWER_CHARS] + "\n…（结论过长已截断）"
        return (
            f"[{spec.name} 子 agent 的结论（{result.model}，{result.tool_calls} 次工具调用）]\n"
            f"{answer}\n\n{footer}"
        )

    isolation_hint = "默认在独立 worktree 里跑" if spec.isolation == "worktree" else ""
    mcp_hint = f"MCP：{spec.mcp_mode}" if spec.mcp_mode != "none" else ""
    extras = "；".join(part for part in (isolation_hint, mcp_hint) if part)
    return Tool(
        name=spec.name,
        description=(
            f"{spec.description}（子 agent，工具：{', '.join(spec.tools)}"
            + (f"；{extras}" if extras else "")
            + "；任务在独立上下文里完成，只把结论带回来。task 要自包含："
            "它看不到当前对话，背景信息要写全。）"
        ),
        parameters={
            "type": "object",
            "properties": {
                "task": {"type": "string", "description": "交给子 agent 的完整任务描述"},
                "capability_mode": {
                    "type": "string",
                    "enum": ["read-only", "read-write", "execute", "all"],
                    "description": (
                        "收紧本次委托的工具档位（只能比 spec 更严，不能放宽；"
                        "read-write 与 execute 相交会降到 read-only）"
                    ),
                },
                "isolation": {
                    "type": "string",
                    "enum": ["none", "worktree"],
                    "description": (
                        "worktree = 在主仓 HEAD 的独立 git worktree 里跑，改动不落"
                        "主工作区（注意：未提交的本地改动它看不到）；覆盖 spec 默认"
                    ),
                },
                "resume_from": {
                    "type": "string",
                    "description": (
                        "上次结论尾部给出的 resume_from 句柄：继承那次委托的完整"
                        "上下文继续干（同一个子 agent 才能恢复；此时 isolation 忽略）"
                    ),
                },
            },
            "required": ["task"],
        },
        handler=run,
        #  委托本身不需要确认：只读子集天然安全，写/执行/MCP 子集逐工具照常确认
        requires_approval=False,
    )
