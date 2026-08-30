"""MCP（Model Context Protocol）客户端——只做 stdio transport。

给小羽接上 MCP 生态：声明在 mcp.json 里的 server（文件系统、数据库、内部平台
工具…）由小羽拉起子进程，其工具以 mcp__<server>__<tool> 的名字挂进 Toolbox。
设计取舍：

- **只做 stdio**：newline-delimited JSON-RPC 2.0 over stdin/stdout，纯 stdlib
  （Popen + 一条读线程）就够。不引官方 mcp SDK——那是 anyio 异步栈，塞进
  小羽的同步主循环要多养一个事件循环线程，为低频能力不值。
  远端 server 直连 Streamable HTTP（规范 2025-06-18）：url 非空即走 HTTP 传输，
  与 stdio 共用本模块的全部上层逻辑（熔断、惰性启动、代际事务、指纹基线）。
  老式 SSE 传输（2024-11-05）不实现——它已被规范标为过时，现役 server 都在
  Streamable HTTP 上；真遇到老 server 仍可用 mcp-remote 这类桥接当 stdio 跑。
- **懒加载不阻塞启动**：每个 server 的 spawn → initialize → tools/list 在
  后台线程跑，REPL 立即可用；工具就绪后在下一次组装 schemas 时追加注册
  （append-only 不重排已有前缀，prompt cache 只作废尾部）。
- **stderr 只进独立日志不进终端**：server 的 stderr 直连
  <用户配置目录>/logs/mcp-<name>.log（每次启动截断），不打花 REPL；
  /mcp 里给出日志路径，排障去那里看。
- **确定性命名**：工具名是 (server, 原名) 的**纯函数**——
  mcp__<server>__<tool> 消毒成 [A-Za-z0-9_-]、封顶 64 字符；消毒或截断改变了
  名字就追加该二元组的 12 位 sha256 哈希，不同工具永不塌缩同名。连接顺序、
  重连、re-sync 都不影响命名——权限规则（/allow）和会话历史里的名字跨代有效。
  同 server 列出重名工具 → 整个列表判非法（整代拒绝）；跨 server 撞名 →
  后到的一代整体回滚，绝不注册部分集合。
- **代际事务**：工具集变更（重连 / tools/list_changed）先在注册表外
  fetch+build 新一代，失败保留上一代原样继续服务；成功才整体 swap（原位替换 +
  删除 + 追加，顺序稳定）。代际间指纹变化的工具走 rug-pull 隔离（见 mcp_guard），
  /mcp approve 后换上新声明。
- **断线自动重连**（预算按 outage 计）：进程意外退出后指数退避重连
  （0.5s 起倍增至 30s），一次 outage 内最多 10 次；连接存活超过 30s 视为
  outage 结束、预算重置。效果：偶发崩溃可无限恢复，崩溃循环（哪怕偶尔连上）
  仍会耗尽上限整代下线。XIAOYU_MCP_RECONNECT=0 关闭。
- **${env:VAR} 展开**：command/args/env 值里的 ${env:VAR}（兼容 ${VAR}）
  启动时替换成环境变量值。未定义的保留字面量原样：server 拿到
  明显错误的 ${FOO} 会报出清楚的错，拿到空串只会报玄学 401。
- **子进程环境是纯白名单**：不是"继承全量再剔除"。
  os.environ 里躺着 XIAOYU_API_KEY / AWS_* / GITHUB_TOKEN……而 npx/uvx 拉起的
  第三方包正是供应链投毒最想要这些。基底只给定位/本地化必需项；密钥要给哪个
  server，在它的 env 块里用 ${env:VAR} 显式点名。
- **fail-closed 审批**：所有 MCP 工具 requires_approval=True。server 自报的
  readOnlyHint 等 annotations 是未经验证的第三方声明，不作为放行依据；
  想免确认用 /allow 权限规则，决定权在用户手里。
- **熔断**：一个 server 连续 3 次传输层失败后熔断 60 秒，期间调用
  立即返回并明确告诉模型"不要立刻重试"——同步主循环里每次干等满超时，
  几个回合就能烧光一轮预算。
- **schema 归一**：server 返回的 inputSchema 做最小消毒（可空 type 数组折叠、
  required 剪掉不存在的属性、裸字符串 schema 替换、缺 type 补 object）——
  严格校验的端点（Gemini/Kimi/OpenAI strict）会因一个畸形 schema 把整个
  tools 数组 400，殃及全部工具。
- **schema 落盘缓存 + 惰性 spawn**：连过一次的
  server 把工具声明按配置指纹缓存；下次启动零进程直接注册工具，首次真实调用
  才 spawn，连上后与 live 声明对账（新工具补注册、幽灵工具拦调用）。
  解掉"懒加载导致首轮模型看不见工具"的鸡生蛋。XIAOYU_MCP_CACHE=0 关闭。
- **配置准入 / OSV 恶意包预检 / 工具指纹基线（防 rug-pull）**：见 mcp_guard.py。
  基线变更的工具隔离不注册，/mcp approve <server> 重新批准；server 声明里
  `"trustToolChanges": true` 的自动接受并刷新基线（只记一行，见 ServerSpec）。
  XIAOYU_MCP_OSV=0 关闭预检。
- **父进程死亡看门狗**：见 mcp_watchdog.py。进程组隔离的另一半——小羽被
  kill -9 后 server 不再变永久孤儿。XIAOYU_MCP_WATCHDOG=0 关闭。
"""

from __future__ import annotations

import atexit
import contextlib
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator

from . import diagnostics, mcp_guard, media
from .config import Config, user_config_dir

#  活着的 MCP 连接数（stdio 子进程 + HTTP 会话），/diagnostics 与 doctor 可见
CONNECTIONS_LIVE = diagnostics.Gauge("mcp.connections.live")

#  MCP 规范修订号。server 端一般都向后兼容旧修订，握手时对方回什么版本就用什么。
PROTOCOL_VERSION = "2025-06-18"

#  initialize + tools/list 的超时：npx/uvx 首跑要现下包，给足余量。
INIT_TIMEOUT = 30.0
#  tools/call 默认超时（秒），可被 mcp.json 里的 timeout 字段逐 server 覆盖。
CALL_TIMEOUT = 120.0

#  工具描述超长会白吃每轮请求的 token：MCP server 的描述质量参差，硬顶一刀。
_DESCRIPTION_CAP = 1_000

#  配置文件名：工作区级用 .mcp.json（多家客户端通用的既成事实标准，
#  同一个仓库配一次全家通用）；用户级放配置目录下的 mcp.json。
WORKSPACE_FILE = ".mcp.json"


class McpError(RuntimeError):
    """MCP 协议层错误（超时 / server 退出 / JSON-RPC error 响应）。"""


def _enabled(name: str) -> bool:
    """环境变量开关：未设置 = 开。XIAOYU_MCP_OSV / _WATCHDOG / _CACHE 共用。"""
    return os.environ.get(name, "").strip().lower() not in ("0", "false", "no", "off")


# ---------- 配置 ----------


@dataclass
class ServerSpec:
    """mcp.json 里一个 server 的声明。"""

    name: str
    command: str
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    #  tools/call 超时（秒）
    timeout: float = CALL_TIMEOUT
    disabled: bool = False
    #  额外从父环境透传给 server 的变量名（或以 * 结尾的前缀，如 "MYAPP_*"）。
    #  _safe_env 的白名单是给"第三方 server"定的，对**宿主自己的** server 不够：
    #  它要靠 MYAPP_HOME 之类找到自己的数据目录，拿不到就默默解析成默认路径、
    #  读错实例的状态（起得来、连得上、答案是别人的——最难查的一类）。
    #  只透传点名的，不放开白名单本身。
    inherit_env: list[str] = field(default_factory=list)
    #  远端 server（Streamable HTTP，规范 2025-06-18）：url 非空即走 HTTP 传输，
    #  此时 command/args/env/inherit_env 都不适用。headers 是每次请求都带的
    #  自定义头（Authorization / API key 之类）。
    url: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    #  跳过 rug-pull 隔离：工具描述/schema 相对基线变化时自动接受并刷新基线，只在
    #  stderr 记一行，不再等 /mcp approve。给"来源可信、又跟着 @latest 走"的 server
    #  用（每次上游发版都要重批一遍，防线就成了噪音，用户会顺手全批）。不是默认：
    #  基线的意义正是让上游悄悄改描述这件事被看见；--yolo 也刻意不覆盖它（执行审批
    #  与供应链是两条轴）。
    trust_tool_changes: bool = False

    @property
    def is_http(self) -> bool:
        return bool(self.url)


#  ${env:VAR} 与 ${VAR} 两种写法都认——不同客户端生态各用其一，
#  用户从哪家抄来的配置都能直接用。未定义的变量展开为空串、不报错：
#  让 server 自己对缺失值报错，报错进它的 stderr 日志。
_ENV_PATTERN = re.compile(r"\$\{(?:env:)?([A-Za-z_][A-Za-z0-9_]*)\}")


def _expand(value: str, extra: dict[str, str] | None = None) -> str:
    #  未定义时保留 ${...} 字面量而非空串。
    #  extra = 宿主注入的环境（Config.extra_env）：daemon 嵌入场景没有 shell env，
    #  密钥走 Keychain→extra_env 进来，展开时优先于 os.environ——否则工作区
    #  .mcp.json 里的 ${VAR} 占位在无人值守进程里永远兑现不了。
    def lookup(m: re.Match[str]) -> str:
        name = m.group(1)
        if extra and name in extra:
            return extra[name]
        return os.environ.get(name, m.group(0))

    return _ENV_PATTERN.sub(lookup, value)


#  stdio 子进程环境白名单。密钥类一律不进：要传给某个 server 就在配置的
#  env 块里显式写 ${env:VAR}。XDG_* 前缀放行（Linux 桌面定位类）。
_SAFE_ENV_KEYS = frozenset(
    {"PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "LC_CTYPE", "TERM", "SHELL", "TMPDIR"}
)
_SAFE_ENV_PREFIXES = ("XDG_",)
#  Windows 定位类（大小写不敏感比较）：都是路径/系统信息，不携带秘密
_SAFE_ENV_KEYS_WINDOWS = frozenset(
    {
        "APPDATA", "LOCALAPPDATA", "PROGRAMDATA", "PROGRAMFILES", "PROGRAMFILES(X86)",
        "SYSTEMROOT", "SYSTEMDRIVE", "COMSPEC", "PATHEXT", "USERPROFILE",
        "HOMEDRIVE", "HOMEPATH", "TEMP", "TMP", "WINDIR", "OS",
        "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE",
    }
)


def _inherited(names: list[str] | None) -> dict[str, str]:
    """按点名/前缀从父环境取变量（见 ServerSpec.inherit_env）。取不到的跳过。"""
    picked: dict[str, str] = {}
    for name in names or []:
        if name.endswith("*"):
            prefix = name[:-1]
            picked.update(
                {k: v for k, v in os.environ.items() if prefix and k.startswith(prefix)}
            )
        elif (value := os.environ.get(name)) is not None:
            picked[name] = value
    return picked


def _safe_env(
    extra: dict[str, str] | None = None, inherit: list[str] | None = None
) -> dict[str, str]:
    """从零构造 stdio 子进程环境：白名单基底 + 点名透传 + 配置声明的 env 覆盖。

    三层的覆盖方向固定：白名单 < 点名透传（inherit_env）< 配置声明的 env。
    越靠近这个 server 自己的声明，优先级越高。
    """
    env = {
        key: value
        for key, value in os.environ.items()
        if key in _SAFE_ENV_KEYS
        or key.startswith(_SAFE_ENV_PREFIXES)
        or (os.name == "nt" and key.upper() in _SAFE_ENV_KEYS_WINDOWS)
    }
    env.update(_inherited(inherit))
    #  MCP stdio 协议规定 UTF-8，但 Windows 上 Python 子进程的管道默认用
    #  locale 编码（cp1252/gbk）——Python 实现的 server（含自家看门狗）一打印
    #  非 ASCII 就 UnicodeEncodeError 崩掉。只影响 Python 子进程，Node 恒 UTF-8。
    env.setdefault("PYTHONUTF8", "1")
    env.setdefault("PYTHONIOENCODING", "utf-8")
    if extra:
        env.update(extra)
    return env


#  server 报错文本里的凭据脱敏：server 把请求
#  原样回显进错误信息是常见毛病，别让 token 经 tool result 进对话历史。
#  只作用于错误路径——正常输出里的 key=value 可能是用户要的真实数据。
_CREDENTIAL_PATTERN = re.compile(
    r"ghp_[A-Za-z0-9]{20,}"
    r"|sk-[A-Za-z0-9_-]{16,}"
    r"|Bearer\s+\S+"
    r"|\b(?:token|api_key|apikey|password|secret)=\S+",
    re.IGNORECASE,
)


def _redact(text: str) -> str:
    return _CREDENTIAL_PATTERN.sub("[REDACTED]", text)


def config_paths(workspace: Path) -> list[Path]:
    """配置文件位置，前面的优先（同名 server 覆盖后面的）。"""
    return [workspace / WORKSPACE_FILE, user_config_dir() / "mcp.json"]


#  `xiaoyu mcp` 的 --scope 取值 → config_paths 的下标。作用域名字对外，
#  文件名只有这里知道。
SCOPES = ("project", "user")


def scope_path(scope: str, workspace: Path) -> Path:
    """作用域名 → 配置文件路径。project=工作区 .mcp.json、user=用户级 mcp.json。"""
    return config_paths(workspace)[SCOPES.index(scope)]


def read_config_file(path: Path) -> dict[str, Any]:
    """读一个配置文件的原始 JSON（顶层其它键原样保留）。不存在返回 {}。

    与 `_parse_config_file` 的容错策略**相反**：那边是运行期读取，坏文件只警告不拦
    启动；这边是写入路径的第一步，坏文件必须抛——把用户手写的配置当成空 dict，
    下一步写回就是整文件覆盖。
    """
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise McpError(f"{path} 读不出来：{exc}") from exc
    if not isinstance(data, dict):
        raise McpError(f"{path} 的顶层不是 JSON 对象")
    return data


def write_config_file(path: Path, data: dict[str, Any]) -> None:
    """写回配置文件。同目录临时文件 + os.replace：写到一半崩了也不留半个配置。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def load_server_specs(
    workspace: Path,
    extra_env: dict[str, str] | None = None,
    include_project: bool = True,
) -> list[ServerSpec]:
    """读全部配置文件，合并出 server 列表。坏文件/坏条目只上 stderr 警告，不拦启动。

    extra_env：宿主注入的环境变量（Config.extra_env），参与 ${VAR} 展开且优先于
    os.environ——常驻 daemon 这类嵌入宿主把 Keychain 密钥递进来的唯一通道。

    include_project=False = 工作区未通过 folder trust 门（见 folder_trust.py）：
    只认用户级配置，工作区 .mcp.json 整个跳过——"clone 即拉起进程"正是那道门
    要堵的洞。跳过时打一行 stderr，静默降级是最坏的失败方式。
    """
    merged: dict[str, ServerSpec] = {}
    #  倒序读（用户级先、工作区级后）：工作区的同名声明覆盖用户级的
    for path in reversed(config_paths(workspace)):
        if not include_project and path == workspace / WORKSPACE_FILE:
            if _present(path):
                print(
                    f"[工作区未受信任：{path} 里的 MCP server 不启动]", file=sys.stderr
                )
            continue
        for spec in _parse_config_file(path, extra_env):
            merged[spec.name] = spec
    specs = []
    for spec in merged.values():
        if spec.disabled:
            continue
        #  准入三点执行的中间一点（加载期）：前有 `xiaoyu mcp add` 的写入期、
        #  后有 ensure_started 的启动期——手写进配置文件的、绕过加载路径直接
        #  构造 spec 的，都还得再过一遍（保存/启动双点缺一不可）
        reason = (
            mcp_guard.endpoint_violation(spec.url)
            if spec.is_http
            else mcp_guard.admission_violation(spec.command, spec.args, spec.env)
        )
        if reason:
            print(f"[MCP server {spec.name!r} 被安全规则拦截：{reason}，已忽略]", file=sys.stderr)
            continue
        specs.append(spec)
    return specs


def _present(path: Path) -> bool:
    """文件存在与否；探测出错按存在处理（只影响要不要打告警，宁多勿漏）。"""
    try:
        return path.is_file()
    except OSError:
        return True


def spec_fingerprint(spec: ServerSpec) -> str:
    """schema 缓存的配置指纹：command/args/env 任何变化都让缓存失效。
    env 的值也参与（sha256 单向，不泄漏）：换了 token 就该重新连一次拿新 schema。"""
    material = json.dumps([spec.command, spec.args, spec.env], ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


def _parse_config_file(
    path: Path, extra_env: dict[str, str] | None = None
) -> list[ServerSpec]:
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[MCP 配置 {path} 解析失败：{exc}]", file=sys.stderr)
        return []
    servers = data.get("mcpServers")
    if not isinstance(servers, dict):
        return []
    specs, problems = parse_server_mapping(servers, extra_env=extra_env)
    for problem in problems:
        print(f"[MCP 配置 {path}：{problem}]", file=sys.stderr)
    return specs


def parse_server_mapping(
    servers: dict[str, Any],
    *,
    extra_env: dict[str, str] | None = None,
    expand: bool = True,
) -> tuple[list[ServerSpec], list[str]]:
    """`mcpServers` 形状（name → 声明对象）→ (ServerSpec 列表, 跳过/忽略说明)。

    配置文件与 serve 的 agent 对象（`mcp_servers` 字段）共用这一个解析器：
    宿主应用往 agent 里写的就是它在 .mcp.json 里写惯的那一段，不另发明形状。
    `expand=False` 时 `${VAR}` 不兑现（协议通道来的值是对端算好的终值；拿本
    进程环境去改写它既不合预期、也给了对端一条读服务端环境变量的路——与 acp
    的 client_server_specs 同一条边界）。
    """
    specs: list[ServerSpec] = []
    problems: list[str] = []

    def ex(value: Any) -> str:
        return _expand(str(value), extra_env) if expand else str(value)

    for name, raw in servers.items():
        if not isinstance(raw, dict):
            problems.append(f"{name!r} 不是对象，已忽略")
            continue
        kind = raw.get("type")
        #  形状即类型：有 url 就是远端（Streamable HTTP），有 command 就是 stdio。
        #  type 字段只用来纠错，不作为唯一判据——各家生态里它时有时无。
        remote = isinstance(raw.get("url"), str) and bool(raw["url"].strip())
        if kind == "sse" or (kind == "http" and not remote):
            #  sse 是 2024-11-05 的老传输，小羽只实现了 Streamable HTTP
            problems.append(
                f"server {name!r}：{kind} 传输不支持"
                f"{'（缺 url）' if kind == 'http' else '（老式 SSE，请改用 Streamable HTTP 的 url）'}，"
                "已忽略"
            )
            continue
        if not remote and not isinstance(raw.get("command"), str):
            problems.append(f"{name!r} 既没有 command 也没有 url，已忽略")
            continue
        timeout = raw.get("timeout", CALL_TIMEOUT)
        if not isinstance(timeout, (int, float)) or timeout <= 0:
            timeout = CALL_TIMEOUT
        elif timeout >= 1000:
            #  有的客户端生态里 mcp.json 的 timeout 按毫秒计（默认 120000）。用户
            #  跨家抄配置很常见，≥1000 一律按毫秒解释——没人需要 17 分钟以上的工具超时。
            timeout /= 1000
        if remote:
            specs.append(
                ServerSpec(
                    name=str(name),
                    command="",
                    url=ex(raw["url"]).strip(),
                    headers={
                        str(key): ex(value) for key, value in (raw.get("headers") or {}).items()
                    },
                    timeout=float(timeout),
                    disabled=bool(raw.get("disabled", False)),
                    trust_tool_changes=bool(raw.get("trustToolChanges", False)),
                )
            )
            continue
        specs.append(
            ServerSpec(
                name=str(name),
                command=ex(raw["command"]),
                args=[ex(item) for item in raw.get("args") or []],
                env={str(key): ex(value) for key, value in (raw.get("env") or {}).items()},
                timeout=float(timeout),
                disabled=bool(raw.get("disabled", False)),
                inherit_env=[
                    str(name) for name in raw.get("inheritEnv") or [] if str(name).strip()
                ],
                trust_tool_changes=bool(raw.get("trustToolChanges", False)),
            )
        )
    return specs, problems


# ---------- 确定性命名 ----------

_NAME_CLEAN = re.compile(r"[^A-Za-z0-9_-]")
#  OpenAI function name 的长度上限
_NAME_CAP = 64
#  lossy 归一化时追加的身份哈希长度（12 位 hex）
_NAME_HASH_LEN = 12


def public_tool_name(server: str, tool: str) -> str:
    """(server, tool) → 模型可见名，纯函数。

    干净情形逐字返回 mcp__<server>__<tool>；字符替换或截断**任何一种**改变了
    名字，就追加二元组身份的 12 位 sha256 哈希——两个不同身份即使归一化后相同，
    哈希也必不同。没有任何跨调用状态：连接顺序 / 重连 / re-sync 都不影响结果，
    /allow 权限规则里存的名字因此跨会话、跨代稳定。

    残余碰撞面只剩 server 名自带 __ 的干净拼接（a__b/c 与 a/b__c）——
    由 swap 阶段的跨 server 冲突预检兜底（整代回滚，绝不静默影蔽）。
    """
    joined = f"mcp__{server}__{tool}"
    normalized = _NAME_CLEAN.sub("_", joined)
    if normalized == joined and len(normalized) <= _NAME_CAP:
        return normalized
    digest = hashlib.sha256(f"{server}\0{tool}".encode()).hexdigest()[:_NAME_HASH_LEN]
    return normalized[: _NAME_CAP - _NAME_HASH_LEN - 1] + "_" + digest


def declared_violation(declared: list[dict[str, Any]]) -> str | None:
    """一代工具声明的合法性：同 server 重名 → 整个列表非法。

    重名不是"挑一个注册"能修的：两个声明争一个确定性名字，任选其一都是
    静默影蔽。返回原因文本；合法返回 None。
    """
    names = [str(item.get("name", "")) for item in declared]
    duplicates = {name for name in names if names.count(name) > 1}
    if duplicates:
        shown = ", ".join(sorted(duplicates)[:3])
        return f"server 在 tools/list 里重复列出同名工具（{shown}），整个工具列表判非法"
    return None


# ---------- 单个 server ----------


class _HttpChannel:
    """MCP Streamable HTTP 传输（规范 2025-06-18）。

    与 stdio 的结构差异（看代码前先知道这三条，否则会觉得少了半个 server）：

    1. **没有常驻读线程**：请求的响应就在这次 POST 的响应体里——`application/json`
       一条、`text/event-stream` 若干条，发完当场派发。所以一个 server 的请求是
       **串行**的（写锁包住整个往返）；stdio 那边靠 id 关联可以多条在飞，这里
       不做——MCP 调用本来一问一答，为并发引入连接池不值。
    2. **server→client 方向要单开一条 GET SSE 长流**：
       notifications/tools/list_changed（rug-pull 监督的触发源）只走那条。
       server 回 405 就是"我不提供"，按没有处理，不当失败。
    3. **会话靠 Mcp-Session-Id 头**：initialize 的响应给一个，此后每次带上；
       server 回 404 表示会话被回收，等价于 stdio 那边的进程没了。
    """

    #  SSE 长流不设读超时（本来就长时间没数据），请求往返用 spec.timeout
    _STREAM_TIMEOUT = None

    def __init__(self, spec: ServerSpec) -> None:
        self.spec = spec
        self.session_id = ""
        #  握手后才带协议版本头（规范：initialize 那次还不知道协商结果）
        self.negotiated = False
        self._stream: Any = None
        self._stream_thread: threading.Thread | None = None
        self._closed = False

    # ---- 出站 ----

    def _headers(self, accept: str) -> dict[str, str]:
        headers = {
            "Accept": accept,
            "Content-Type": "application/json",
            "User-Agent": f"xiaoyu/{_version()}",
        }
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        if self.negotiated:
            headers["MCP-Protocol-Version"] = PROTOCOL_VERSION
        #  自定义头最后合并：用户点名的优先（他可能就是要覆盖 UA/Accept）
        headers.update(self.spec.headers)
        return headers

    def post(self, payload: dict[str, Any], timeout: float) -> list[dict[str, Any]]:
        """发一条 JSON-RPC 消息，返回这次往返里收到的全部消息（可能为空）。"""
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            self.spec.url,
            data=body,
            headers=self._headers("application/json, text/event-stream"),
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                #  会话 id 只在 initialize 的响应里出现，但每次都读一遍无害
                if new_id := response.headers.get("Mcp-Session-Id"):
                    self.session_id = new_id
                kind = (response.headers.get("Content-Type") or "").split(";")[0].strip()
                if response.status == 202:
                    return []  # 通知被接收，无响应体
                if kind == "text/event-stream":
                    return list(_read_sse(response))
                raw = response.read().decode("utf-8", "replace").strip()
                if not raw:
                    return []
                message = json.loads(raw)
                return [message] if isinstance(message, dict) else list(message)
        except urllib.error.HTTPError as exc:
            if exc.code == 404 and self.session_id:
                raise McpError("远端会话已失效（HTTP 404），需要重连") from exc
            detail = _redact(exc.read().decode("utf-8", "replace")[:200]) if exc.fp else ""
            raise McpError(f"HTTP {exc.code} {exc.reason}{'：' + detail if detail else ''}") from exc
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
            raise McpError(f"请求 {self.spec.name} 失败：{exc}") from exc

    # ---- server→client 长流 ----

    def open_stream(self, dispatch: Callable[[dict[str, Any]], None]) -> None:
        """尝试开 GET SSE 长流；server 不支持（405）就安静放弃。"""
        request = urllib.request.Request(
            self.spec.url, headers=self._headers("text/event-stream"), method="GET"
        )
        try:
            stream = urllib.request.urlopen(request, timeout=self._STREAM_TIMEOUT)
        except urllib.error.HTTPError as exc:
            if exc.code in (404, 405, 501):
                return  # 规范允许不提供这条流
            print(
                f"[MCP server {self.spec.name!r} 的事件流打不开：HTTP {exc.code}，"
                "工具变更通知本次不可用]",
                file=sys.stderr,
            )
            return
        except (urllib.error.URLError, OSError) as exc:
            print(
                f"[MCP server {self.spec.name!r} 的事件流打不开：{exc}，"
                "工具变更通知本次不可用]",
                file=sys.stderr,
            )
            return
        self._stream = stream
        self._stream_thread = threading.Thread(
            target=self._pump_stream,
            args=(stream, dispatch),
            name=f"xiaoyu-mcp-sse-{self.spec.name}",
            daemon=True,
        )
        self._stream_thread.start()

    def _pump_stream(self, stream: Any, dispatch: Callable[[dict[str, Any]], None]) -> None:
        try:
            for message in _read_sse(stream):
                if self._closed:
                    return
                dispatch(message)
        except Exception as exc:  # noqa: BLE001 - 见下：这里只负责安静收场
            #  长流断了不等于 server 没了（代理超时最常见）：下次 POST 会说话，
            #  别把一次断流误报成 server 挂了。
            #  异常类型刻意放到最宽：close() 与本线程的 readline 天然竞态，
            #  http.client 在竞态瞬间抛什么全看版本（3.14 上是内部 fp 已置空的
            #  AttributeError，不是 OSError）——按类型枚举必漏，漏了就是每次
            #  关闭都往 stderr 吐一段 traceback。
            if not self._closed:
                print(
                    f"[MCP server {self.spec.name!r} 的事件流中断：{exc}]", file=sys.stderr
                )
            return

    def close(self) -> None:
        self._closed = True
        if self._stream is not None:
            with contextlib.suppress(Exception):
                self._stream.close()
            self._stream = None
        if not self.session_id:
            return
        #  规范：客户端应当显式 DELETE 掉会话，让 server 释放资源。尽力而为。
        request = urllib.request.Request(
            self.spec.url, headers=self._headers("application/json"), method="DELETE"
        )
        with contextlib.suppress(Exception):
            urllib.request.urlopen(request, timeout=5.0).close()


def _read_sse(response: Any) -> "Iterator[dict[str, Any]]":
    """SSE 流 → JSON-RPC 消息，**边读边吐**（生成器）。

    生成器不是风格选择：GET 长流要的就是"事件到一条派发一条"，先收集再返回
    等于要等流结束——而那条流本来就不会结束。POST 那边 list() 一下即可。

    多行 data 按规范用 \n 拼接。解析不出 JSON 的块跳过——server 拿注释行做
    心跳是常见做法，不该把心跳当协议错误。
    """
    buffer: list[str] = []
    for raw in response:
        line = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
        line = line.rstrip("\r\n")
        if line.startswith(":"):
            continue  # 注释/心跳
        if line == "":
            if buffer:
                try:
                    message = json.loads("\n".join(buffer))
                except json.JSONDecodeError:
                    message = None
                if isinstance(message, dict):
                    yield message
                buffer = []
            continue
        if line.startswith("data:"):
            buffer.append(line[5:].lstrip())
    if buffer:
        try:
            message = json.loads("\n".join(buffer))
        except json.JSONDecodeError:
            message = None
        if isinstance(message, dict):
            yield message


class McpServer:
    """一个 stdio MCP server 子进程：拉起、握手、列工具、调工具、关闭。

    线程模型：一条读线程独占 stdout，解析出的响应放进 _responses 由条件变量
    唤醒等待方；写入方共用 _write_lock。所有公开方法线程安全。
    """

    def __init__(self, spec: ServerSpec, log_path: Path) -> None:
        self.spec = spec
        self.log_path = log_path
        self._proc: subprocess.Popen[str] | None = None
        #  远端 server 的传输通道（stdio 型恒为 None）。两种传输共用本类的
        #  全部上层逻辑：熔断、惰性启动、代际事务、幽灵工具拦截、指纹基线。
        self._http: _HttpChannel | None = _HttpChannel(spec) if spec.is_http else None
        self._cond = threading.Condition()
        self._responses: dict[int, dict[str, Any]] = {}
        self._next_id = 0
        self._write_lock = threading.Lock()
        self._dead = False
        #  熔断状态：连续传输层失败计数 + 熔断截止时刻（monotonic）
        self._failures = 0
        self._breaker_until = 0.0
        #  server 声明的名字/版本（initialize 响应里的 serverInfo），/mcp 展示用
        self.server_info = ""
        #  最近一次 call_tool 的图片部件（见 call_tool）：调用方紧接着取走
        self.last_media: list[dict[str, Any]] = []
        #  惰性启动状态：schema 缓存命中的 server 直到第一次真实调用才 spawn
        self._start_lock = threading.Lock()
        self.started = False
        self.start_error: str | None = None
        #  连上后 server 实际声明的工具（原始 dict + 名字集合，幽灵工具靠它拦）
        self.live_declared: list[dict[str, Any]] = []
        self.live_names: set[str] = set()
        #  代际事务钩子（由 manager 在注册成功后接线；None = 不监督）：
        #  on_tools_changed ← notifications/tools/list_changed（读线程上发火，
        #  接线方绝不能在回调里同步发请求——响应正是读线程自己派发的，会自锁死）
        self.on_tools_changed: Callable[[], None] | None = None
        #  on_disconnect ← stdout EOF（进程意外退出）。主动 close 不触发。
        self.on_disconnect: Callable[[], None] | None = None
        self._closing = False
        #  是否已计入 CONNECTIONS_LIVE（启动成功 +1、收进程 -1，重启不重复计）
        self._counted = False
        self._drain_thread: threading.Thread | None = None
        #  重连预算（按 outage 计，见模块 docstring）；归 manager 的重连线程读写
        self.reconnect_attempts = 0
        self.connected_at: float | None = None

    #  熔断参数：3 连败开路、60s 后自动半开。
    #  只数传输层失败（超时/进程退出/JSON-RPC error）；isError 是业务失败，
    #  server 本身是健康的，不算。
    _BREAKER_THRESHOLD = 3
    _BREAKER_COOLDOWN = 60.0

    # ---- 生命周期 ----

    def bootstrap(self) -> list[dict[str, Any]]:
        """启动并返回原始工具声明列表，失败抛 McpError。"""
        self.ensure_started()
        return self.live_declared

    def ensure_started(self) -> None:
        """幂等启动：准入 → OSV 预检 → spawn → 握手 → tools/list。

        schema 缓存命中的 server 平时不启动，第一次真实工具调用走到这里才 spawn
        （调用方线程同步等，上限 INIT_TIMEOUT）。失败会记住并在后续调用立即重抛，
        不反复重试把每次工具调用都拖满超时。
        """
        with self._start_lock:
            if self.started:
                if self.start_error:
                    raise McpError(self.start_error)
                return
            try:
                self._start_locked()
                self._count_live(True)
            except McpError as exc:
                self.start_error = str(exc)
                self.close()
                raise
            except Exception as exc:  # noqa: BLE001 - 统一包成 McpError 往上抛
                self.start_error = f"{type(exc).__name__}: {exc}"
                self.close()
                raise McpError(self.start_error) from exc
            finally:
                self.started = True

    def _start_locked(self) -> None:
        spec = self.spec
        #  准入双点执行的第二点：启动即是最后一道闸。两种传输各有各的判据：
        #  stdio 判"这条命令像不像攻击"，HTTP 判"这个地址会不会让凭据裸奔"。
        if spec.is_http:
            if reason := mcp_guard.endpoint_violation(spec.url):
                raise McpError(f"地址被安全规则拦截：{reason}")
        else:
            if reason := mcp_guard.admission_violation(spec.command, spec.args, spec.env):
                raise McpError(f"配置被安全规则拦截：{reason}")
            #  OSV 恶意包预检（fail-open，命中才拦）。必须在 watchdog 包装 argv
            #  之前基于原始 spec 判断——包装后 argv[0] 是 python，预检会静默失效。
            #  远端 server 没有包可查，这条不适用。
            if _enabled("XIAOYU_MCP_OSV"):
                if reason := mcp_guard.osv_malware_check(spec.command, spec.args):
                    raise McpError(f"启动被拦截：{reason}")
        self._spawn()
        result = self._request(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "xiaoyu", "version": _version()},
            },
            timeout=INIT_TIMEOUT,
        )
        info = result.get("serverInfo") or {}
        self.server_info = " ".join(
            str(part) for part in (info.get("name"), info.get("version")) if part
        )
        if self._http is not None:
            #  协商完成的那一刻就要立旗：规范要求 initialize 响应**之后**的每一次
            #  请求都带 MCP-Protocol-Version，包括紧接着这条 initialized 通知。
            #  （立旗晚一行，通知就成了唯一漏带的那条——server 严格校验时只有
            #  这一条被拒，症状是"握手过了但 server 认为没握手"。）
            self._http.negotiated = True
        self._notify("notifications/initialized")
        if self._http is not None:
            #  server→client 长流放在 initialized 之后开：server 有权在握手
            #  完成前拒绝这条 GET
            self._http.open_stream(self._dispatch)
        #  没声明 tools capability 的 server（纯 prompts/resources 型）不发
        #  tools/list：省一次注定报 method-not-found 的往返
        capabilities = result.get("capabilities") or {}
        declared = self._list_tools() if "tools" in capabilities else []
        self.live_declared = declared
        self.live_names = {str(item.get("name", "")) for item in declared}

    def _spawn(self) -> None:
        if self._http is not None:
            #  HTTP 没有"拉起进程"这一步：连接在第一次 POST 时建立，这里只把
            #  死标志清掉（restart 会重走这条路）。
            with self._cond:
                self._dead = False
            return
        #  ~ 展开 + which：Windows 上 npx/uvx 这类 .cmd 入口不经 shell 找不到，
        #  which 一次全平台通吃。
        expanded = os.path.expanduser(self.spec.command)
        command = shutil.which(expanded) or expanded
        #  环境走 _safe_env 白名单（见模块 docstring）；配置里声明的 env 覆盖
        #  继承值——这个覆盖方向不能反，反了配置值会被父环境静默盖掉。
        #  加固参数里的 start_new_session 同时解决另一件事：REPL 里 Ctrl-C 打断
        #  一轮对话时，SIGINT 发给前台进程组——server 不隔离出去会被连带杀掉。
        #  函数级导入避免与 tools.py 循环引用（tools 在模块层 import mcp）。
        from .tools import _subprocess_hardening

        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        #  stderr 直连日志文件（每次启动截断），句柄交给子进程后本端立即关闭。
        #  不用管道：管道写满会把不 drain 的子进程卡死，文件永远不会。
        argv = [command, *self.spec.args]
        #  父进程死亡看门狗（mcp_watchdog.py）：start_new_session 让 server 躲开
        #  Ctrl-C，副作用是小羽被 kill -9 后它们变永久孤儿——看门狗轮询 ppid
        #  补上这一半。stdio 全程透传，协议不经手。
        if _enabled("XIAOYU_MCP_WATCHDOG"):
            argv = [
                sys.executable, "-m", "xiaoyu.mcp_watchdog",
                "--ppid", str(os.getpid()), "--", *argv,
            ]
        log = open(self.log_path, "w", encoding="utf-8")  # noqa: SIM115
        try:
            self._proc = subprocess.Popen(
                argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=log,
                text=True,
                encoding="utf-8",
                errors="replace",
                #  行缓冲：协议就是一行一条消息
                bufsize=1,
                env=_safe_env(self.spec.env, self.spec.inherit_env),
                **_subprocess_hardening(),
            )
        except OSError as exc:
            raise McpError(f"无法启动 {self.spec.command}：{exc}") from exc
        finally:
            log.close()
        self._drain_thread = threading.Thread(
            target=self._drain_stdout,
            name=f"xiaoyu-mcp-{self.spec.name}",
            daemon=True,
        )
        self._drain_thread.start()

    def alive(self) -> bool:
        if self._http is not None:
            #  远端没有进程可查：只要没被判死（会话失效/传输错）就算在线
            return self.started and not self._dead
        return self._proc is not None and self._proc.poll() is None

    def close(self) -> None:
        #  主动关闭：先立旗再收进程，EOF 到来时读线程据此不触发重连
        self._closing = True
        self._shutdown_proc()

    def restart(self) -> list[dict[str, Any]]:
        """断线后重启一代：收掉旧进程，重置协议状态，重新 spawn → 握手 → 列工具。

        serverName 不变 + 确定性命名 = 新一代的工具名逐字复现。
        成功返回新一代声明并清掉 start_error；失败抛 McpError（进程已收干净，
        调用方可按预算再试）。只应由 manager 的重连线程调用。
        """
        with self._start_lock:
            if self._closing:
                raise McpError("server 已关闭，不再重启")
            self._shutdown_proc()
            #  等旧读线程退场（进程已收，秒级）：它迟到的 EOF/响应有 `_proc is
            #  proc` 代际守卫兜底，join 只是把窗口关到零
            thread = self._drain_thread
            if thread is not None:
                thread.join(timeout=5.0)
            with self._cond:
                self._dead = False
                self._responses.clear()
                self._next_id = 0
            #  熔断是"进程活着但请求连败"的防线，新一代从零开始
            self._failures = 0
            self._breaker_until = 0.0
            try:
                self._start_locked()
            except McpError:
                self._shutdown_proc()
                raise
            except Exception as exc:  # noqa: BLE001 - 统一包成 McpError
                self._shutdown_proc()
                raise McpError(f"{type(exc).__name__}: {exc}") from exc
            if self._closing:
                #  与 close() 赛跑输了：把刚拉起的新一代收掉，绝不留孤儿
                self._shutdown_proc()
                raise McpError("server 已关闭，不再重启")
            self.started = True
            self.start_error = None
            return self.live_declared

    def _count_live(self, alive: bool) -> None:
        if alive and not self._counted:
            self._counted = True
            CONNECTIONS_LIVE.inc()
        elif not alive and self._counted:
            self._counted = False
            CONNECTIONS_LIVE.dec()

    def _shutdown_proc(self) -> None:
        self._count_live(False)
        if self._http is not None:
            self._http.close()
            with self._cond:
                self._dead = True
                self._cond.notify_all()
            return
        proc = self._proc
        if proc is None:
            return
        try:
            if proc.poll() is None:
                #  尽量体面：先关 stdin（stdio server 的约定退出信号），
                #  不走再 terminate，最后 kill 兜底
                for hangup in (
                    lambda: proc.stdin and proc.stdin.close(),
                    proc.terminate,
                    proc.kill,
                ):
                    try:
                        hangup()
                        proc.wait(timeout=2)
                        return
                    except (OSError, subprocess.TimeoutExpired, ValueError):
                        continue
        finally:
            #  管道句柄显式关掉：留给 GC 会攒出 ResourceWarning
            for stream in (proc.stdin, proc.stdout):
                if stream:
                    with contextlib.suppress(OSError, ValueError):
                        stream.close()

    # ---- 协议 ----

    def _list_tools(self) -> list[dict[str, Any]]:
        tools: list[dict[str, Any]] = []
        cursor: str | None = None
        #  分页有界循环：行为不端的 server 无限翻页也拖不死我们
        for _ in range(50):
            params: dict[str, Any] = {"cursor": cursor} if cursor else {}
            result = self._request("tools/list", params, timeout=INIT_TIMEOUT)
            page = result.get("tools")
            if isinstance(page, list):
                tools.extend(item for item in page if isinstance(item, dict))
            cursor = result.get("nextCursor")
            #  必须是非空字符串才翻下一页：挡住乱返回的 server
            if not isinstance(cursor, str) or not cursor:
                break
        return tools

    def call_tool(self, tool: str, args: dict[str, Any]) -> str:
        """tools/call，把 content 各部件拼成回给模型的文本。协议错误也返回文本。

        图片部件不在返回值里（返回值是"给模型的文本"，形状不能变——它同时是
        工具结果、trace 记录、审批预览的输入），改挂在 `last_media` 上由调用方
        紧接着取走。每次调用先清空：上一次的图绝不能粘到这一次的结果上。
        """
        self.last_media = []
        if not self.alive():
            hint = (
                "后台正在自动重连，稍后重试或先做别的事。"
                if self.on_disconnect is not None and not self._closing
                else ""
            )
            return (
                f"ERROR: MCP server {self.spec.name} 进程已退出，无法调用。{hint}"
                f"排障看日志：{self.log_path}"
            )
        #  熔断快速失败：同步主循环里每次干等满超时（默认 120s），模型对着
        #  一个坏 server 重试几轮就能烧掉十几分钟。开路期间立即返回。
        now = time.monotonic()
        if now < self._breaker_until:
            remaining = int(self._breaker_until - now) + 1
            return (
                f"ERROR: MCP server {self.spec.name} 连续失败已熔断，"
                f"约 {remaining}s 后自动恢复。不要立刻重试这个工具——"
                "先做别的事，或改用其它工具/告知用户。"
            )
        try:
            result = self._request(
                "tools/call",
                {"name": tool, "arguments": args},
                timeout=self.spec.timeout,
            )
        except McpError as exc:
            self._failures += 1
            if self._failures >= self._BREAKER_THRESHOLD:
                self._breaker_until = time.monotonic() + self._BREAKER_COOLDOWN
                self._failures = 0
            return f"ERROR: MCP 调用失败（{self.spec.name}/{tool}）：{_redact(str(exc))}"
        self._failures = 0
        text, self.last_media = _render_result(result)
        return text

    def _request(self, method: str, params: dict[str, Any], timeout: float) -> dict[str, Any]:
        with self._cond:
            if self._dead:
                raise McpError(self._exit_reason())
            self._next_id += 1
            request_id = self._next_id
        self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        deadline = time.monotonic() + timeout
        with self._cond:
            while request_id not in self._responses:
                if self._dead:
                    raise McpError(self._exit_reason())
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise McpError(f"{method} 超时（{timeout:g}s）")
                self._cond.wait(remaining)
            reply = self._responses.pop(request_id)
        if "error" in reply:
            error = reply["error"] or {}
            raise McpError(f"{error.get('message', '未知错误')}（code {error.get('code')}）")
        result = reply.get("result")
        return result if isinstance(result, dict) else {}

    def _notify(self, method: str) -> None:
        self._send({"jsonrpc": "2.0", "method": method})

    def _send(self, payload: dict[str, Any]) -> None:
        if self._http is not None:
            self._post(payload)
            return
        proc = self._proc
        if proc is None or proc.stdin is None:
            raise McpError("server 未启动")
        line = json.dumps(payload, ensure_ascii=False)
        try:
            with self._write_lock:
                proc.stdin.write(line + "\n")
                proc.stdin.flush()
        except (OSError, ValueError) as exc:
            raise McpError(f"写入 server 失败（多半已退出）：{exc}") from exc

    def _drain_stdout(self) -> None:
        """读线程：逐行解析 stdout，直到 EOF（server 退出）。

        一切写共享状态/发回调的动作都带 `self._proc is proc` 代际守卫：restart
        换代后，旧读线程迟到的 EOF 不能把新一代标死，迟到的响应也不能污染
        新一代的 id 空间（restart 会把 id 归零）。
        """
        proc = self._proc
        assert proc is not None and proc.stdout is not None
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                #  server 把日志错打到 stdout 是常见毛病，跳过非 JSON 行
                continue
            if not isinstance(message, dict):
                continue
            self._dispatch(message, generation=proc)
        with self._cond:
            if self._proc is not proc:
                return
            self._dead = True
            self._cond.notify_all()
        #  意外退出才报断线（主动 close/restart 不算）；manager 据此起重连线程
        callback = self.on_disconnect
        if callback is not None and not self._closing:
            callback()

    def _dispatch(self, message: dict[str, Any], generation: Any = None) -> None:
        """一条收到的 JSON-RPC 消息 → 响应槽 / 反向请求应答 / 变更通知。

        stdio 与 HTTP 共用。generation 是 stdio 读线程的代际守卫（restart 换代
        后，旧线程迟到的响应不能污染新一代的 id 空间）；HTTP 那边响应就在本次
        POST 的响应体里、不存在迟到，传 None 即可。
        """
        if generation is not None and self._proc is not generation:
            return
        if "method" in message:
            if message.get("id") is not None:
                self._answer_server_request(message)
            elif message.get("method") == "notifications/tools/list_changed":
                #  热更新钩子：只报信，fetch/swap 由 manager 的独立线程做
                #  （在本线程发请求会自锁死：响应正等着本线程派发）
                callback = self.on_tools_changed
                if callback is not None and not self._closing:
                    callback()
            #  其余通知（progress、logging…）一律忽略
            return
        if "id" in message:
            with self._cond:
                self._responses[message["id"]] = message
                self._cond.notify_all()

    def _post(self, payload: dict[str, Any]) -> None:
        """HTTP 传输的发送：一次往返，收到的消息当场派发。

        写锁包住整个往返（不只是"写"）：远端没有 id 关联的读线程兜底，两个
        请求交叉在飞时后到的响应会落进先到那次的 read——串行是这里的正确性
        前提，不是性能取舍。
        """
        channel = self._http
        if channel is None:
            raise McpError("server 未启动")
        timeout = self.spec.timeout if payload.get("method") != "initialize" else INIT_TIMEOUT
        try:
            with self._write_lock:
                messages = channel.post(payload, timeout)
        except McpError:
            with self._cond:
                self._dead = True
                self._cond.notify_all()
            raise
        for message in messages:
            self._dispatch(message)

    def _answer_server_request(self, message: dict[str, Any]) -> None:
        """server 反向发来的请求：ping 回 pong，其余一律 method-not-found。

        不接 sampling/roots/elicitation——那是让 server 反过来使唤客户端的通道，
        小羽不开放。不回错误响应会让规矩的 server 干等，所以要显式拒绝。
        """
        method = message.get("method")
        if method == "ping":
            reply: dict[str, Any] = {"jsonrpc": "2.0", "id": message["id"], "result": {}}
        else:
            reply = {
                "jsonrpc": "2.0",
                "id": message["id"],
                "error": {"code": -32601, "message": f"xiaoyu 不支持 {method}"},
            }
        try:
            self._send(reply)
        except McpError:
            pass

    def _exit_reason(self) -> str:
        if self._http is not None:
            return f"与远端 server 的连接已断开（{self.spec.url}）"
        code = self._proc.returncode if self._proc else None
        return (
            f"server 进程已退出（exit {code}）。"
            f"排障看日志：{self.log_path}"
        )


def _version() -> str:
    from . import __version__

    return __version__


def _render_result(result: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    """tools/call 的 result → (回给模型的文本, 图片部件列表)。

    text 部件直接拼；resource 取其内嵌文本；都没有时兜底 structuredContent 的 JSON。

    **图片单独返回、不在这里做取舍**：能不能真的发给模型，取决于当前路由到的
    型号（见 Registry.sees_images），而协议层不知道也不该知道模型是谁。这里只
    负责把 base64 落盘换成引用（见 media.store_base64），由 agent 决定是随后
    附给模型、还是降级成一行说明。audio 仍是纯占位：内核没有音频通路。
    """
    parts: list[str] = []
    images: list[dict[str, Any]] = []
    for item in result.get("content") or []:
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        if kind == "text":
            parts.append(str(item.get("text", "")))
        elif kind == "image":
            ref, problem = media.store_base64(
                str(item.get("data") or ""), str(item.get("mimeType") or "")
            )
            if ref:
                images.append(media.image_part(ref))
            else:
                parts.append(f"[image 内容已省略：{problem}]")
        elif kind == "audio":
            parts.append("[audio 内容已省略：无法回灌给文本模型]")
        elif kind == "resource":
            resource = item.get("resource") or {}
            text = resource.get("text")
            parts.append(
                str(text)
                if text is not None
                else f"[二进制资源已省略：{resource.get('uri', '?')}]"
            )
        elif kind == "resource_link":
            parts.append(f"[资源链接] {item.get('uri', '?')} {item.get('description', '')}".rstrip())
    if not parts and isinstance(result.get("structuredContent"), dict):
        parts.append(json.dumps(result["structuredContent"], ensure_ascii=False))
    text = "\n".join(part for part in parts if part) or "(空结果)"
    #  isError 是"工具执行失败"（参数错、业务失败），前缀 ERROR: 对齐内置工具的
    #  约定，让主循环的 ok 统计和模型的自愈路径都能识别。错误文本过凭据脱敏：
    #  server 把带 token 的请求原样回显进报错是常见毛病。
    if result.get("isError"):
        #  失败结果里的图片不往上带：模型这一轮该看的是错误原因
        return f"ERROR: {_redact(text)}", []
    return text, images


#  schema 归一时要递归进去的位置。properties 等的 **key 是用户属性名不是关键字**，
#  只递归 value 绝不改 key（改了 key 的典型翻车：把名为 definitions 的参数改名，
#  生成非法属性名，整个 tools 数组 400）。
_SCHEMA_MAP_KEYS = ("properties", "patternProperties", "$defs", "definitions")
_SCHEMA_LIST_KEYS = ("anyOf", "oneOf", "allOf", "prefixItems")
_SCHEMA_ONE_KEYS = ("items", "additionalProperties", "not", "if", "then", "else")


def _normalize_schema(node: Any) -> Any:
    """server 返回的 inputSchema 最小消毒。

    严格校验的端点（Gemini / Kimi / OpenAI strict）会因一个畸形 schema 把整个
    tools 数组 400——一个坏 server 殃及全部工具。只修四类高频畸形：
    - 裸字符串 schema（additionalProperties: "object" 这类 server 输出 bug）
    - type 是数组：["string","null"] 折叠成 "string"（取第一个非 null）
    - required 里指向不存在属性的项剪掉（Gemini 会 400）
    - 有 properties 却缺 type 的补 "object"
    只递归 schema 位置；required/enum/examples 的值不是 schema，不进去
    （进去会把字面量误当裸字符串 schema 替换掉）。
    """
    if isinstance(node, str):
        return {"type": "object", "properties": {}}
    if not isinstance(node, dict):
        return node
    node = dict(node)  # 浅拷贝：不改 server 给的原对象
    kind = node.get("type")
    if isinstance(kind, list):
        non_null = [item for item in kind if item != "null"]
        node["type"] = non_null[0] if non_null else "null"
    for key in _SCHEMA_MAP_KEYS:
        if isinstance(node.get(key), dict):
            node[key] = {name: _normalize_schema(sub) for name, sub in node[key].items()}
    for key in _SCHEMA_LIST_KEYS:
        if isinstance(node.get(key), list):
            node[key] = [_normalize_schema(sub) for sub in node[key]]
    for key in _SCHEMA_ONE_KEYS:
        #  additionalProperties 的合法布尔值不动
        if key in node and not isinstance(node[key], bool):
            node[key] = _normalize_schema(node[key])
    properties = node.get("properties")
    if "type" not in node and isinstance(properties, dict):
        node["type"] = "object"
    if isinstance(node.get("required"), list) and isinstance(properties, dict):
        node["required"] = [name for name in node["required"] if name in properties]
    node = _collapse_const_union(node)
    return node


#  const 折叠时 Python 类型 → JSON Schema 类型名。bool 必须排在 int 之前判断
#  （Python 里 bool 是 int 子类，顺序反了 true/false 会被并进整数枚举）
_CONST_TYPES: tuple[tuple[type, str], ...] = (
    (bool, "boolean"),
    (int, "integer"),
    (float, "number"),
    (str, "string"),
)


def _collapse_const_union(node: dict[str, Any]) -> dict[str, Any]:
    """anyOf 全是同型纯 const 时折叠成 enum。

    某些语言生态的 server 把闭集枚举生成为
    `{"anyOf": [{"const": "red"}, {"const": "green"}]}`，而严格校验的端点
    对这种形态要么拒绝要么误处理，property 级 `enum` 才是通行形态。
    只在**每个非 null 分支都是同一 primitive 类型的纯 const**（分支里除
    const 外只有注解类键）时才折叠；单个 {"type":"null"} 分支容忍并丢弃
    ——与上面 type 数组折叠丢 null 的既有取舍一致（运行期报错好过注册期
    整个 tools 数组 400）。混合 union 原样穿透。分支序保序。
    """
    branches = node.get("anyOf")
    if not isinstance(branches, list) or not branches:
        return node
    values: list[Any] = []
    kind: str | None = None
    for branch in branches:
        if not isinstance(branch, dict):
            return node
        if branch.get("type") == "null" and "const" not in branch:
            continue  # 可空标记：容忍并丢弃
        if "const" not in branch:
            return node
        value = branch["const"]
        for py_type, name in _CONST_TYPES:
            if isinstance(value, py_type):
                if kind is None:
                    kind = name
                elif kind != name:
                    return node  # 跨类型混合：不折叠
                break
        else:
            return node  # 非 primitive const（对象/数组/None）：不折叠
        #  同型才走到这里，True==1 跨型撞值的坑已被 kind 检查挡在门外
        if value not in values:
            values.append(value)
    if kind is None or not values:
        return node
    node = dict(node)
    del node["anyOf"]
    node["type"] = kind
    node["enum"] = values
    return node


# ---------- 远程工具 + 管理器 ----------


@dataclass
class RemoteTool:
    """一个已就绪的 MCP 工具，字段与 tools.Tool 对齐（由 Toolbox 包装注册）。

    不直接依赖 tools.Tool 类型：mcp.py 不 import tools，避免循环引用。
    """

    name: str
    description: str
    parameters: dict[str, Any]
    handler: Callable[..., str]
    check_fn: Callable[[], bool]
    #  来源 server 名（检索模式按 server 分组/公告用；从消毒后的全限定名
    #  反解不可靠，这里存原名）
    server: str = ""
    #  server 侧原始工具名与声明指纹：代际 swap 靠它们判断原位替换还是保留
    raw_name: str = ""
    fingerprint: str = ""


def _make_remote_tool(
    manager: "McpManager", server: McpServer, declared: dict[str, Any]
) -> RemoteTool:
    tool_name = str(declared.get("name", ""))
    exposed = public_tool_name(server.spec.name, tool_name)
    description = str(declared.get("description") or "").strip() or "(server 未提供描述)"
    if len(description) > _DESCRIPTION_CAP:
        description = description[:_DESCRIPTION_CAP] + "…"
    #  描述里带上来源：审批框里用户一眼看出这是哪个 server 的能力
    description = f"[MCP·{server.spec.name}] {description}"
    schema = declared.get("inputSchema")
    if isinstance(schema, dict):
        schema = _normalize_schema(schema)
    if not isinstance(schema, dict) or schema.get("type") != "object":
        #  缺失/畸形 schema 补一个空对象 schema：OpenAI 端点会拒绝非 object 的顶层
        schema = {"type": "object", "properties": {}}

    def handler(**kwargs: Any) -> str:
        #  惰性启动：schema 缓存注册的工具在第一次真实调用时才 spawn server
        try:
            server.ensure_started()
        except McpError as exc:
            return (
                f"ERROR: MCP server {server.spec.name} 启动失败：{exc}。"
                f"排障看日志：{server.log_path}"
            )
        #  缓存与现实对账（每 server 一次）：server 新增的工具补注册
        manager.reconcile(server)
        if tool_name not in server.live_names:
            return (
                f"ERROR: MCP server {server.spec.name} 的当前版本不再提供 {tool_name}"
                "（schema 缓存已过期并刷新，该工具随后会从列表消失）。换其它工具。"
            )
        text = server.call_tool(tool_name, kwargs)
        #  图片紧接着取走交给 manager 暂存：工具 handler 的返回值形状是"文本"，
        #  这条约定不为多模态破例（trace / 审批预览 / 权限规则全靠它）
        manager.stash_media(server.last_media)
        return text

    def check() -> bool:
        if server.start_error:
            return False
        if not server.started:
            #  缓存待启动：必须可见，否则模型永远不会发起那第一次调用
            return True
        return server.alive() and tool_name in server.live_names

    return RemoteTool(
        name=exposed,
        description=description,
        parameters=schema,
        handler=handler,
        check_fn=check,
        server=server.spec.name,
        raw_name=tool_name,
        fingerprint=mcp_guard.tool_fingerprint(declared),
    )


class McpManager:
    """一组 server 的启动与状态。启动全程后台线程，公开方法只读快照、线程安全。"""

    #  重连预算（类属性便于测试改小）：
    #  首个延迟 0.5s 逐次翻倍封顶 30s；一次 outage 内最多 10 次；
    #  连接存活 ≥ RECONNECT_MAX_DELAY（稳定窗 = 退避上限）即视为 outage 结束
    RECONNECT_INITIAL_DELAY = 0.5
    RECONNECT_MAX_DELAY = 30.0
    RECONNECT_MAX_ATTEMPTS = 10

    def __init__(self, specs: list[ServerSpec]) -> None:
        self._specs = specs
        self._lock = threading.Lock()
        self._servers: dict[str, McpServer] = {}
        #  name → "loading" | "ready" | "cached" | "failed: …" | "blocked: …" | "closed"
        self._states: dict[str, str] = {spec.name: "loading" for spec in specs}
        #  就绪工具的 append-only 列表：Toolbox 每次组装 schemas 前来同步一次
        self._tools: list[RemoteTool] = []
        #  最近一次工具调用产出的图片部件，等 Toolbox 取走（见 take_media）
        self._media: list[dict[str, Any]] = []
        self._closed = False
        #  每个 server 最近一次拿到的完整声明（缓存或 live）与已注册工具原名，
        #  /mcp approve 与对账去重靠它们
        self._declared: dict[str, list[dict[str, Any]]] = {}
        self._registered: dict[str, set[str]] = {}
        self._quarantined: dict[str, list[str]] = {}
        self._reconciled: set[str] = set()
        #  在飞的后台启动线程：close 时逐个 join，收尾必须同步完成
        self._boot_threads: list[threading.Thread] = []
        #  代际服务线程（re-sync / 重连）及其调度状态。dirty/running 两集合
        #  实现通知合并：re-sync 进行中又来通知只置 dirty，循环消化不堆线程
        self._service_threads: list[threading.Thread] = []
        self._resync_dirty: set[str] = set()
        self._resync_running: set[str] = set()
        self._reconnecting: set[str] = set()
        #  close 时置位：重连线程的退避等待立即醒来退场
        self._close_event = threading.Event()
        #  工具指纹基线（防 rug-pull）与 schema 缓存都放用户配置目录
        self._baseline_path = user_config_dir() / "mcp-approved.json"
        self._cache_path = user_config_dir() / "cache" / "mcp-schemas.json"
        self._baseline = mcp_guard.load_baseline(self._baseline_path)
        self._cache = self._load_cache() if _enabled("XIAOYU_MCP_CACHE") else {}

    def _load_cache(self) -> dict[str, Any]:
        try:
            data = json.loads(self._cache_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(data, dict) or data.get("version") != 1:
            return {}
        servers = data.get("servers")
        return servers if isinstance(servers, dict) else {}

    def start(self) -> None:
        for spec in self._specs:
            #  schema 缓存命中：零进程直接注册工具，
            #  首次真实调用才 spawn——解掉"懒加载导致首轮模型看不见工具"的鸡生蛋。
            #  配置指纹不匹配 = 缓存作废，走正常后台启动。
            entry = self._cache.get(spec.name)
            if (
                isinstance(entry, dict)
                and entry.get("fingerprint") == spec_fingerprint(spec)
                and isinstance(entry.get("tools"), list)
            ):
                server = McpServer(spec, log_path=self._log_path(spec.name))
                server.server_info = str(entry.get("server_info", ""))
                with self._lock:
                    self._servers[spec.name] = server
                    error = declared_violation(entry["tools"]) or self._swap_generation_locked(
                        spec.name, server, entry["tools"]
                    )
                    self._states[spec.name] = f"failed: {error}" if error else "cached"
                continue
            thread = threading.Thread(
                target=self._bootstrap_one,
                args=(spec,),
                name=f"xiaoyu-mcp-boot-{spec.name}",
                daemon=True,
            )
            self._boot_threads.append(thread)
            thread.start()

    def _bootstrap_one(self, spec: ServerSpec) -> None:
        server = McpServer(spec, log_path=self._log_path(spec.name))
        try:
            declared = server.bootstrap()
        except McpError as exc:
            server.close()
            with self._lock:
                self._states[spec.name] = f"failed: {exc}"
            return
        except Exception as exc:  # noqa: BLE001 - 单个 server 崩不拦其它的
            server.close()
            with self._lock:
                self._states[spec.name] = f"failed: {type(exc).__name__}: {exc}"
            return
        error = declared_violation(declared)
        with self._lock:
            #  manager 已被关闭（退出/测试清理）：不注册，把刚拉起的子进程收掉。
            #  没有这一步，关闭时还在启动中的 server 会变成孤儿进程。
            if self._closed:
                self._states[spec.name] = "closed"
            else:
                error = error or self._swap_generation_locked(spec.name, server, declared)
                if error:
                    self._states[spec.name] = f"failed: {error}"
                else:
                    self._servers[spec.name] = server
                    #  live 启动的不需要再对账
                    self._reconciled.add(spec.name)
                    self._states[spec.name] = "ready"
                    self._write_cache_locked(spec, server)
                    self._supervise_locked(server)
                    return
        server.close()

    def _swap_generation_locked(
        self, name: str, server: McpServer, declared: list[dict[str, Any]]
    ) -> str | None:
        """整代 swap 的落笔阶段：基线裁决 → 冲突预检 →
        与现役代对齐（原位替换 / 删除 / 追加）。持锁调用；幂等。

        fetch 与 declared_violation 校验由调用方在锁外完成——这里
        只做落笔。注册表就在本进程锁内，所以冲突可以**先检查后落笔**，
        冲突时上一代原样保留继续服务（先卸旧代再注册的顺序做不到这一点，
        冲突只能回滚到零）。返回错误原因文本；成功返回 None。

        顺序纪律：保留/替换的工具停在原位（原位替换 = dict 覆盖语义，full-schema
        模式的 prompt cache 前缀不动），删除只挪后缀，新工具追加在最尾。
        """
        admitted, quarantined, updates = mcp_guard.admit_tools(
            self._baseline.get(name, {}), declared
        )
        if quarantined and server.spec.trust_tool_changes:
            #  trustToolChanges：变更工具照单全收、基线跟着刷新，只留一行痕迹。
            #  仍走同一条 admit 路径而不是绕过基线——基线要持续跟上，日后把开关
            #  关掉时才有正确的"上次"可比，而不是从开关打开那天起全是陈年指纹。
            for item in declared:
                raw = str(item.get("name", ""))
                if raw in quarantined:
                    updates[raw] = mcp_guard.tool_fingerprint(item)
            admitted, accepted, quarantined = list(declared), quarantined, []
            print(
                f"[MCP {name}：{len(accepted)} 个工具的描述/schema 相对上次已变化，"
                f"按 trustToolChanges 自动接受并刷新基线：{', '.join(accepted[:5])}]",
                file=sys.stderr,
            )
        new_decls = {str(item.get("name", "")): item for item in admitted}
        #  跨 server 冲突预检：确定性命名下冲突只可能是别的 server 占了
        #  本 server 的命名空间（如 a__b/c 与 a/b__c）——整代拒绝，大声报错
        owned = {tool.name for tool in self._tools if tool.server != name}
        conflicts = sorted(
            public_name
            for raw in new_decls
            if (public_name := public_tool_name(name, raw)) in owned
        )
        if conflicts:
            return (
                f"命名空间冲突：{', '.join(conflicts[:3])} 已被其它 server 注册，"
                "本代整体回滚（保留上一代），不注册部分集合"
            )
        self._declared[name] = declared
        if updates:
            #  TOFU：首见工具并入基线立即落盘
            self._baseline.setdefault(name, {}).update(updates)
            with contextlib.suppress(OSError):
                mcp_guard.save_json_atomic(self._baseline_path, self._baseline)
        previously_quarantined = self._quarantined.get(name) or []
        self._quarantined[name] = quarantined
        if quarantined and quarantined != previously_quarantined:
            print(
                f"[MCP {name}：{len(quarantined)} 个工具的描述/schema 相对上次已变化，"
                f"已隔离不注册（防 rug-pull）。核对无误后用 /mcp approve {name} 重新批准："
                f"{', '.join(quarantined[:5])}]",
                file=sys.stderr,
            )
        #  对齐现役代：其它 server 的工具原样保留；本 server 的按新一代裁决——
        #  声明消失/被隔离的删除，指纹没变的保留原对象，变了的原位换新
        next_tools: list[RemoteTool] = []
        seen: set[str] = set()
        for tool in self._tools:
            if tool.server != name:
                next_tools.append(tool)
                continue
            item = new_decls.get(tool.raw_name)
            if item is None:
                continue
            seen.add(tool.raw_name)
            if tool.fingerprint == mcp_guard.tool_fingerprint(item):
                next_tools.append(tool)
            else:
                next_tools.append(_make_remote_tool(self, server, item))
        for raw, item in new_decls.items():
            if raw not in seen:
                next_tools.append(_make_remote_tool(self, server, item))
        self._tools = next_tools
        self._registered[name] = set(new_decls)
        return None

    def _write_cache_locked(self, spec: ServerSpec, server: McpServer) -> None:
        if not _enabled("XIAOYU_MCP_CACHE"):
            return
        entry = {
            "fingerprint": spec_fingerprint(spec),
            "tools": server.live_declared,
            "server_info": server.server_info,
        }
        #  内容没变就不写盘（write-through with skip）
        if self._cache.get(spec.name) == entry:
            return
        self._cache[spec.name] = entry
        with contextlib.suppress(OSError):
            mcp_guard.save_json_atomic(
                self._cache_path, {"version": 1, "servers": self._cache}
            )

    def reconcile(self, server: McpServer) -> None:
        """缓存路径懒启动后与 live 声明对账（每 server 只做一次）。

        新增的工具经基线裁决后补注册；消失的"幽灵工具"由 live_names 在
        check_fn / handler 两处拦住，随 schemas 重组自然消失。最后刷新缓存。
        """
        name = server.spec.name
        with self._lock:
            if name in self._reconciled:
                return
            self._reconciled.add(name)
            error = declared_violation(server.live_declared) or self._swap_generation_locked(
                name, server, server.live_declared
            )
            if error:
                #  live 声明整代非法：不 swap，缓存代（最后一个合法代）继续服务，
                #  状态里亮出原因
                print(f"[MCP {name}：对账失败，沿用缓存声明：{error}]", file=sys.stderr)
                self._states[name] = f"degraded: {error}"
                self._supervise_locked(server)
                return
            self._states[name] = "ready"
            self._write_cache_locked(server.spec, server)
            self._supervise_locked(server)

    # ---- 代际监督：list_changed 热更新 + 断线重连 ----

    def _supervise_locked(self, server: McpServer) -> None:
        """server 就绪后接线代际钩子（幂等）。持锁调用。"""
        server.connected_at = time.monotonic()
        server.on_tools_changed = lambda: self._schedule_resync(server)
        if _enabled("XIAOYU_MCP_RECONNECT"):
            server.on_disconnect = lambda: self._schedule_reconnect(server)
            if not server.alive():
                #  接线前就崩了（EOF 已经过去，回调当时还是 None）：补一枪。
                #  必须在独立线程发——_schedule_reconnect 要拿本锁
                threading.Thread(target=server.on_disconnect, daemon=True).start()

    def _spawn_service_locked(self, target: Callable[[], None], name: str) -> None:
        thread = threading.Thread(target=target, name=name, daemon=True)
        #  顺手清掉已退场的：service 线程数 = 事件数，长会话别攒僵尸引用
        self._service_threads = [t for t in self._service_threads if t.is_alive()]
        self._service_threads.append(thread)
        thread.start()

    def _schedule_resync(self, server: McpServer) -> None:
        """tools/list_changed → 独立线程 re-sync。通知风暴合并成 dirty 循环。"""
        name = server.spec.name
        with self._lock:
            if self._closed:
                return
            self._resync_dirty.add(name)
            if name in self._resync_running:
                return
            self._resync_running.add(name)
            self._spawn_service_locked(
                lambda: self._resync_worker(server), f"xiaoyu-mcp-resync-{name}"
            )

    def _resync_worker(self, server: McpServer) -> None:
        """代际事务的动态变更侧：fetch 新一代（锁外）→ swap（锁内）。

        fetch 失败/列表非法 → 上一代原样保留继续服务（re-sync 永远
        不能把一个好代换成坏代）。循环直到 dirty 消化干净。
        """
        name = server.spec.name
        while True:
            with self._lock:
                if self._closed or name not in self._resync_dirty:
                    self._resync_running.discard(name)
                    return
                self._resync_dirty.discard(name)
            try:
                declared = server._list_tools()
            except McpError as exc:
                print(f"[MCP {name}：工具列表刷新失败，保留上一代：{exc}]", file=sys.stderr)
                continue
            if reason := declared_violation(declared):
                print(f"[MCP {name}：{reason}，保留上一代]", file=sys.stderr)
                continue
            server.live_declared = declared
            server.live_names = {str(item.get("name", "")) for item in declared}
            with self._lock:
                if self._closed:
                    self._resync_running.discard(name)
                    return
                if error := self._swap_generation_locked(name, server, declared):
                    print(f"[MCP {name}：{error}]", file=sys.stderr)
                else:
                    self._write_cache_locked(server.spec, server)

    def _schedule_reconnect(self, server: McpServer) -> None:
        """进程意外退出 → 重连线程（每 server 同时至多一个 = 一次 outage）。"""
        name = server.spec.name
        with self._lock:
            if self._closed or name in self._reconnecting:
                return
            self._reconnecting.add(name)
            self._states[name] = "reconnecting"
            self._spawn_service_locked(
                lambda: self._reconnect_worker(server), f"xiaoyu-mcp-reconnect-{name}"
            )

    def _reconnect_worker(self, server: McpServer) -> None:
        """一次 outage 的重连循环：指数退避，预算按 outage 计。

        进门先判上一段连接是否"稳定"（存活 ≥ 退避上限）：稳定 → 上一次 outage
        已了结，预算清零；不稳定 → 沿用累计——崩溃循环哪怕每次都短暂连上，
        也会耗尽预算整代下线，不会永远重启。
        """
        name = server.spec.name
        now = time.monotonic()
        if (
            server.connected_at is not None
            and now - server.connected_at >= self.RECONNECT_MAX_DELAY
        ):
            server.reconnect_attempts = 0
        server.connected_at = None
        succeeded = False
        try:
            while True:
                server.reconnect_attempts += 1
                attempt = server.reconnect_attempts
                if attempt > self.RECONNECT_MAX_ATTEMPTS:
                    with self._lock:
                        self._drop_generation_locked(name)
                        self._states[name] = (
                            f"failed: 连续 {self.RECONNECT_MAX_ATTEMPTS} 次重连失败，"
                            "已放弃（工具已整代下线；修好 server 后重启会话恢复）"
                        )
                    #  解除监督：耗尽后不再对这个进程尸体反复立案
                    server.on_disconnect = None
                    server.on_tools_changed = None
                    print(
                        f"[MCP {name}：连续 {self.RECONNECT_MAX_ATTEMPTS} 次重连失败，"
                        f"工具已整代下线。排障看日志：{server.log_path}]",
                        file=sys.stderr,
                    )
                    return
                delay = min(
                    self.RECONNECT_MAX_DELAY,
                    self.RECONNECT_INITIAL_DELAY * 2 ** (attempt - 1),
                )
                with self._lock:
                    if self._closed:
                        return
                    self._states[name] = (
                        f"reconnecting: 断线重连中（第 {attempt}/"
                        f"{self.RECONNECT_MAX_ATTEMPTS} 次，{delay:g}s 后）"
                    )
                if self._close_event.wait(delay):
                    return
                try:
                    declared = server.restart()
                except McpError as exc:
                    print(
                        f"[MCP {name}：重连失败"
                        f"（第 {attempt}/{self.RECONNECT_MAX_ATTEMPTS} 次）：{exc}]",
                        file=sys.stderr,
                    )
                    continue
                if reason := declared_violation(declared):
                    #  连上了但列表非法：按失败尝试计，绝不拿非法代换掉上一代
                    print(f"[MCP {name}：重连后{reason}，按失败计]", file=sys.stderr)
                    continue
                with self._lock:
                    if self._closed:
                        return
                    server.connected_at = time.monotonic()
                    if error := self._swap_generation_locked(name, server, declared):
                        self._states[name] = f"degraded: {error}"
                    else:
                        self._states[name] = "ready"
                        self._write_cache_locked(server.spec, server)
                succeeded = True
                print(f"[MCP {name}：已重连（第 {attempt} 次尝试）]", file=sys.stderr)
                return
        finally:
            with self._lock:
                self._reconnecting.discard(name)
            #  成功与 discard 之间的窄窗：新进程立刻又崩，EOF 回调被
            #  _reconnecting 挡掉——这里补判一次，两种时序都兜住
            if succeeded and not server.alive() and not self._closed:
                self._schedule_reconnect(server)

    def _drop_generation_locked(self, name: str) -> None:
        """一个 server 的现役代整体下线（重连预算耗尽）。持锁调用。"""
        self._tools = [tool for tool in self._tools if tool.server != name]
        self._registered[name] = set()
        self._quarantined.pop(name, None)

    def approve(self, name: str) -> str:
        """/mcp approve：把 server 当前声明的指纹整体写进基线，解除隔离。"""
        with self._lock:
            declared = self._declared.get(name)
            server = self._servers.get(name)
            if declared is None or server is None:
                known = ", ".join(self._declared) or "（无）"
                return f"没有名为 {name!r} 的已连接 server。已知：{known}"
            count = len(self._quarantined.get(name) or [])
            if not count:
                return f"{name} 没有被隔离的工具，无需批准"
            self._baseline[name] = {
                str(item.get("name", "")): mcp_guard.tool_fingerprint(item)
                for item in declared
            }
            with contextlib.suppress(OSError):
                mcp_guard.save_json_atomic(self._baseline_path, self._baseline)
            #  整代重 swap：解除隔离的注册进来，之前已注册但声明变过的原位换上
            #  新 schema/描述（否则模型照着旧 schema 调新工具）
            if error := self._swap_generation_locked(name, server, declared):
                return f"批准失败：{error}"
            return f"已批准 {name} 的当前工具集（解除隔离 {count} 个）"

    @staticmethod
    def _log_path(name: str) -> Path:
        safe = _NAME_CLEAN.sub("_", name)
        return user_config_dir() / "logs" / f"mcp-{safe}.log"

    def ready_tools(self) -> list[RemoteTool]:
        """当前已就绪的全部工具（append-only 快照，顺序稳定）。"""
        with self._lock:
            return list(self._tools)

    def loading(self) -> bool:
        """还有 server 在握手中（检索模式回给模型 status: partial 用）。"""
        with self._lock:
            return any(state == "loading" for state in self._states.values())

    def stash_media(self, parts: list[dict[str, Any]]) -> None:
        if parts:
            with self._lock:
                self._media.extend(parts)

    def take_media(self) -> list[dict[str, Any]]:
        """取走暂存的图片部件（取完即清）。

        一次工具调用可能返回多张图，一批 tool_calls 也可能有好几个都返回图，
        所以是累加 + 一次取走，由 agent 在这批工具跑完后统一附给模型。
        """
        with self._lock:
            parts, self._media = self._media, []
        return parts

    def wait_ready(self, timeout: float) -> None:
        """等所有 server 出结果（就绪或失败），最多等 timeout 秒。

        通用等待面（不只是测试/一次性模式用）：嵌入宿主自建 manager 后要等
        工具清单齐了再开工，用它，别照 loading() 自己写轮询——真发生过。
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                if all(state != "loading" for state in self._states.values()):
                    return
            time.sleep(0.05)

    def close(self) -> None:
        with self._lock:
            self._closed = True
        #  重连线程的退避等待立即醒来，看到 _closed 退场
        self._close_event.set()
        #  join 而不是限时等状态：启动中的 server 由 _bootstrap_one 发现 _closed
        #  后自行收尾，必须等它做完——异步收尾意味着 close 返回时子进程可能还
        #  活着，Windows 上删不掉被占用的日志文件（临时目录清理 WinError 32）。
        #  正常路径线程秒级结束；真卡死的（INIT_TIMEOUT=30 内）最多等 15 秒放弃，
        #  孤儿由看门狗按父进程死亡兜底回收。
        for thread in self._boot_threads:
            thread.join(timeout=15.0)
        self._boot_threads.clear()
        with self._lock:
            servers = list(self._servers.values())
            self._servers.clear()
        for server in servers:
            server.close()
        #  最后收代际服务线程：server 进程已死，卡在 fetch 里的请求立即报错返回
        with self._lock:
            service = list(self._service_threads)
            self._service_threads.clear()
        for thread in service:
            thread.join(timeout=15.0)

    def describe(self) -> str:
        """/mcp 的状态输出。"""
        if not self._specs:
            return self.usage_hint()
        with self._lock:
            states = dict(self._states)
            counts = {name: len(registered) for name, registered in self._registered.items()}
            quarantined = {name: list(names) for name, names in self._quarantined.items() if names}
            servers = dict(self._servers)
        lines = []
        for spec in self._specs:
            name = spec.name
            state = states.get(name, "loading")
            server = servers.get(name)
            detail = f"{counts.get(name, 0)} 个工具"
            if server and server.server_info:
                detail += f" · {server.server_info}"
            if server and server.start_error:
                lines.append(f"  {name}: 启动失败: {server.start_error}")
            elif state == "ready":
                if server and not server.alive():
                    detail += "（进程已退出）"
                lines.append(f"  {name}: 就绪 · {detail}")
            elif state == "cached":
                lines.append(f"  {name}: 就绪（schema 缓存，进程按首次调用启动）· {detail}")
            elif state == "loading":
                lines.append(f"  {name}: 启动中…（就绪后工具自动挂载）")
            else:
                lines.append(f"  {name}: {state}")
            if name in quarantined:
                shown = ", ".join(quarantined[name][:5])
                lines.append(
                    f"    ⚠ {len(quarantined[name])} 个工具因描述/schema 变更被隔离"
                    f"（{shown}）—— 核对后 /mcp approve {name}"
                )
            lines.append(f"    日志：{self._log_path(name)}")
        return "\n".join(lines)

    @staticmethod
    def usage_hint() -> str:
        return (
            "未配置 MCP server。最省事的加法（写的就是下面那两个文件）：\n"
            "  xiaoyu mcp add <名字> [--scope user] <命令> [参数…]\n"
            "  例：xiaoyu mcp add chrome-devtools --scope user npx -y chrome-devtools-mcp@latest\n"
            "也可以直接手写（工作区级优先）：\n"
            f"  <工作区>/{WORKSPACE_FILE}\n"
            f"  {user_config_dir() / 'mcp.json'}\n"
            '格式（与多家客户端的 mcp.json 通用）：{"mcpServers": {"名字": '
            '{"command": "npx", "args": ["-y", "某个包"], "env": {"KEY": "${env:VAR}"}}}}\n'
            '远端 server 用 --url 直连（Streamable HTTP）：xiaoyu mcp add 名字 --url https://…'
        )


class McpView:
    """父会话 MCP manager 的 server 级筛选视图（子 agent 继承用）。

    不持有任何进程——生命周期归父 manager；接口与 Toolbox 消费 manager 的
    子集对齐（ready_tools / loading / take_media）。粒度是 server 而不是
    tool：精确名匹配、引用不存在的名字不报错（过滤后少一个而已）。

    已知边界：media 暂存是 manager 级共享的，子 agent 取图时理论上可能把
    父级同批工具刚暂存的图一并取走。同步架构下父级在子 agent 返回后才收
    自己那批，实际窗口极窄，不为此加标记链路。
    """

    def __init__(self, manager: McpManager, mode: str = "all", names: tuple[str, ...] = ()) -> None:
        self._manager = manager
        self._mode = mode  # all | named | except
        self._names = set(names)

    def _allows(self, server: str) -> bool:
        if self._mode == "named":
            return server in self._names
        if self._mode == "except":
            return server not in self._names
        return True

    def ready_tools(self) -> list[RemoteTool]:
        return [remote for remote in self._manager.ready_tools() if self._allows(remote.server)]

    def loading(self) -> bool:
        return self._manager.loading()

    def take_media(self) -> list[dict[str, Any]]:
        return self._manager.take_media()


# ---------- 进程级单例 ----------

#  按工作区缓存 manager：同一进程里反复构造 Toolbox（REPL + explore 已关掉 MCP，
#  正常只有一次，但测试会多次）不重复拉起子进程。
_managers: dict[Path, McpManager] = {}
#  不按工作区缓存、但仍要被 at-exit 兜底关掉的 manager（显式 specs 起的那种，
#  见 launch_specs）。缓存键是工作区，而这些 manager 的清单是按会话/宿主来的，
#  同一工作区的两个会话可以有不同清单——共用一份会串台，所以只登记不复用。
_extra_managers: list[McpManager] = []
_atexit_registered = False


def _ensure_atexit() -> None:
    global _atexit_registered
    if not _atexit_registered:
        atexit.register(shutdown_all)
        _atexit_registered = True


def launch(config: Config, extra_specs: list[ServerSpec] | None = None) -> McpManager | None:
    """按配置拉起（或复用）本工作区的 MCP manager。没配置任何 server 返回 None。

    extra_specs：配置发现之外再挂几条 server（ACP client 随会话下发的那种）。
    **同名以 extra_specs 为准**——它是用户在编辑器里亲手配的，比配置文件里
    的同名条目更贴近此刻的意图。带 extra_specs 时走不缓存的路径（理由见
    _extra_managers），返回的 manager 由调用方 close，at-exit 也会兜底。
    """
    if extra_specs:
        merged = {spec.name: spec for spec in load_server_specs(
            config.workspace,
            config.extra_env,
            include_project=getattr(config, "workspace_trusted", True),
        )}
        merged.update({spec.name: spec for spec in extra_specs})
        return launch_specs(list(merged.values()))
    manager = _managers.get(config.workspace)
    if manager is not None:
        return manager
    specs = load_server_specs(
        config.workspace,
        config.extra_env,
        #  getattr 兜底：嵌入宿主可能拿旧版 Config 对象构造（没有这个字段）
        include_project=getattr(config, "workspace_trusted", True),
    )
    if not specs:
        return None
    manager = McpManager(specs)
    manager.start()
    _managers[config.workspace] = manager
    _ensure_atexit()
    return manager


def launch_specs(specs: list[ServerSpec]) -> McpManager:
    """按给定 specs 现起一个 manager 并登记到 at-exit 清扫。

    嵌入宿主自带 server 清单时的入口：自己 `McpManager(specs)` 也能跑，但那份
    不在任何登记表里——进程退出时无人 close，子进程要靠看门狗兜底回收。走这里
    就有兜底。**不复用、不缓存**：调用方拿到的永远是新的一份，用完自己 close
    （提前 close 过的，at-exit 再关一次是幂等的）。

    **空清单也返回真 manager，不返回 None**：第一版对空清单返回 None，第一个
    真实消费者就踩了——嵌入方拿它造 McpView 时顺手往下传，而 `Toolbox(mcp_view=None)`
    的语义是"回到配置发现"，于是操作者自己 mcp.json 里的 server 泄进了一个
    本不该看到它们的会话。空 manager 不起任何子进程，代价为零；让"零个 server"
    与"没有指定视图"这两件事在类型上就分得开，比在 docstring 里叮嘱可靠。
    """
    manager = McpManager(specs)
    manager.start()
    _extra_managers.append(manager)
    _ensure_atexit()
    return manager


def shutdown_all() -> None:
    """关掉全部 server 子进程（atexit 兜底；测试也直接调）。"""
    for manager in list(_managers.values()) + list(_extra_managers):
        manager.close()
    _managers.clear()
    _extra_managers.clear()
