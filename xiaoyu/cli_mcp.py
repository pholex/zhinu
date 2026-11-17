"""`xiaoyu mcp`：MCP server 声明的增删查，以及不经模型的 probe。"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

from . import ui
from .cli import _trust_fingerprints, _trust_resync, shorten_home

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
    "--url": True,
    "--header": True,
    "-H": True,
    "--cwd": True,
    "--tools": True,
    "--force": False,
    "-f": False,
    "--help": False,
    "-h": False,
}


def split_mcp_command(
    argv: list[str], flags: dict[str, bool] | None = None
) -> tuple[list[str], list[str]]:
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
            takes_value = (flags if flags is not None else _MCP_ADD_FLAGS).get(token.split("=", 1)[0], False)
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
    "  xiaoyu mcp add <名字> --url https://…         添加一个远端 MCP server\n"
    "  xiaoyu mcp list                              列出已声明的 server\n"
    "  xiaoyu mcp remove <名字> [--scope …]          删除一个声明\n"
    "  xiaoyu mcp probe <名字 | 命令行> [--script f]   不经模型探测一个 server：握手、列工具，或按脚本调用\n"
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
    if action == "probe":
        return mcp_probe_command(argv[1:])
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
    parser.add_argument(
        "--cwd",
        metavar="目录",
        help="stdio server 的工作目录（文件系统类 server 把它当根用）；同样支持 ${env:VAR}",
    )
    parser.add_argument(
        "--tools",
        metavar="名,名",
        help="只暴露这几个工具（逗号分隔；server 其余工具模型看不见也调不到）",
    )
    parser.add_argument("-f", "--force", action="store_true", help="同名声明已存在时覆盖")
    head, command_argv = split_mcp_command(argv)
    args = parser.parse_args(head)
    scope = args.scope or "project"
    tools = [item.strip() for item in (args.tools or "").split(",") if item.strip()]
    if args.tools is not None and not tools:
        print(ui.error("--tools 至少要给一个工具名"), file=sys.stderr)
        return 2
    if args.url and args.cwd:
        print(ui.error("--cwd 只对 stdio server 有意义（远端 server 没有本地进程）"), file=sys.stderr)
        return 2

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
        if args.cwd:
            entry["cwd"] = args.cwd
    if tools:
        entry["tools"] = tools
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
    trust_before = _trust_fingerprints(scope, Path.cwd())
    try:
        mcp.write_config_file(path, data)
    except OSError as exc:
        print(ui.error(f"写不进 {path}：{exc}"), file=sys.stderr)
        return 1
    _trust_resync(scope, Path.cwd(), trust_before)

    print(ui.success(f"已写入 {path}"))
    summary = args.url if args.url else " ".join(command_argv)
    print(f"  {ui.accent(args.name)}  {ui.secondary('·')}  {summary}")
    #  Windows 上 npx 是 .cmd、pipx/uvx 常不在 PATH 里——现在提示比启动后翻日志便宜。
    #  只对 stdio 有意义：远端 server 根本没有本地命令
    if command_argv and shutil.which(command_argv[0]) is None:
        print(
            ui.warning(f"  提示：当前 PATH 里找不到 {command_argv[0]}，装好之前这个 server 起不来")
        )
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
            if raw.get("cwd"):
                notes.append(f"cwd: {raw['cwd']}")
            if isinstance(raw.get("tools"), list):
                notes.append(f"只暴露 {len(raw['tools'])} 个工具")
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
    trust_before = _trust_fingerprints(scope, workspace)
    try:
        mcp.write_config_file(path, data)
    except OSError as exc:
        print(ui.error(f"写不进 {path}：{exc}"), file=sys.stderr)
        return 1
    _trust_resync(scope, workspace, trust_before)
    print(ui.success(f"已从 {path} 删除 {args.name}"))
    return 0


_PROBE_FLAGS = {"--script": True, "--url": True, "--timeout": True, "--help": False, "-h": False}
#  probe 脚本里认的步骤名（驼峰与下划线都收：手写 JSON 的人两种都会写）
_PROBE_OPS = {"listTools": "listTools", "list_tools": "listTools", "callTool": "callTool", "call_tool": "callTool"}
#  脚本模式下每步结果文本的封顶：排障要的是形状与错误，不是整页正文
_PROBE_TEXT_CAP = 4000


def _probe_spec(target: list[str], url: str | None, timeout: float | None):
    """`xiaoyu mcp probe` 的目标 → ServerSpec。

    一个词且是已声明的 server 名 → 用配置里那条（与运行期同一条加载路径：
    ${env:VAR} 展开、准入规则都在）；否则整段当启动命令行（临时 server，不写盘）；
    --url 则是远端。
    """
    from . import mcp

    if url:
        spec = mcp.ServerSpec(name="probe", command="", url=url)
    elif len(target) == 1 and MCP_NAME_PATTERN.match(target[0]):
        known = {spec.name: spec for spec in mcp.load_server_specs(Path.cwd())}
        spec = known.get(target[0])
        if spec is None:
            if shutil.which(target[0]) is None:
                names = ", ".join(known) or "（无）"
                raise ValueError(
                    f"没有名为 {target[0]!r} 的 MCP server，PATH 里也没有这个命令。已声明：{names}"
                )
            spec = mcp.ServerSpec(name=target[0], command=target[0])
    elif target:
        spec = mcp.ServerSpec(name=Path(target[0]).name or "probe", command=target[0], args=target[1:])
    else:
        raise ValueError("要探测谁？给一个已声明的 server 名、一条启动命令行，或 --url")
    if timeout is not None:
        spec = replace(spec, timeout=timeout)
    return spec


def _probe_script(path: str) -> list[dict[str, Any]]:
    """读 --script：顶层是步骤列表，或 {"steps": [...]}；每步 {"op": ..., "name", "args"}。"""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"脚本 {path} 读不了：{exc}") from exc
    steps = data.get("steps") if isinstance(data, dict) else data
    if not isinstance(steps, list):
        raise ValueError("脚本顶层应是步骤列表，或 {\"steps\": [...]}")
    parsed: list[dict[str, Any]] = []
    for index, step in enumerate(steps, 1):
        if not isinstance(step, dict):
            raise ValueError(f"第 {index} 步不是对象")
        op = _PROBE_OPS.get(str(step.get("op", "")))
        if op is None:
            raise ValueError(f"第 {index} 步的 op 只能是 listTools / callTool：{step.get('op')!r}")
        name = step.get("name")
        if op == "callTool" and (not isinstance(name, str) or not name):
            raise ValueError(f"第 {index} 步缺 name（要调哪个工具）")
        args = step.get("args", step.get("arguments", {}))
        if not isinstance(args, dict):
            raise ValueError(f"第 {index} 步的 args 应是对象")
        parsed.append({"op": op, "name": name, "args": args})
    return parsed


def _probe_emit(record: dict[str, Any]) -> None:
    print(json.dumps(record, ensure_ascii=False), flush=True)


def _probe_run_script(server, steps: list[dict[str, Any]]) -> bool:
    """按脚本逐步执行，每步一行 JSON（耗时、错误分类都在）。返回是否全部成功。"""
    from . import mcp

    all_ok = True
    for index, step in enumerate(steps, 1):
        record: dict[str, Any] = {"step": index, "op": step["op"]}
        if step["name"]:
            record["name"] = step["name"]
        started = time.monotonic()
        try:
            if step["op"] == "listTools":
                declared = server._list_tools()  # noqa: SLF001 - 排障工具，直接走协议层
                record["ok"] = True
                record["tools"] = [str(item.get("name", "")) for item in declared]
            else:
                #  走 _request 而不是 call_tool：call_tool 把协议错误折成文本，这里要的
                #  正是错误分类（timeout / rpc / dropped…）
                result = server._request(  # noqa: SLF001
                    "tools/call",
                    {"name": step["name"], "arguments": step["args"]},
                    timeout=server.spec.timeout,
                )
                text, media = mcp._render_result(result)  # noqa: SLF001
                record["ok"] = not bool(result.get("isError"))
                record["is_error"] = bool(result.get("isError"))
                record["text"] = text[:_PROBE_TEXT_CAP] + ("…" if len(text) > _PROBE_TEXT_CAP else "")
                if media:
                    record["media"] = len(media)
                if isinstance(result.get("structuredContent"), dict):
                    record["structured"] = result["structuredContent"]
        except mcp.McpError as exc:
            record["ok"] = False
            record["error"] = mcp._redact(str(exc))  # noqa: SLF001
            record["error_kind"] = exc.kind or "unknown"
            if exc.status is not None:
                record["status"] = exc.status
        record["seconds"] = round(time.monotonic() - started, 3)
        all_ok = all_ok and bool(record["ok"])
        _probe_emit(record)
    return all_ok


def mcp_probe_command(argv: list[str]) -> int:
    """`xiaoyu mcp probe`：不经模型、不进会话，直接把一个 server 拉起来看。

    无脚本：握手 + 列工具，打印 serverInfo / 协议版本 / 能力 / instructions / 工具表。
    有脚本：按顺序执行 listTools / callTool，每步一行 JSON——给人和脚本都能读的
    排障输出（耗时、错误分类、结果形状）。走的是运行期同一个 McpServer：准入规则、
    OSV 预检、cwd 校验、超时与错误分类都和会话里一模一样，"probe 通了会话里却不通"
    不会发生在这一层。
    """
    from . import mcp

    parser = argparse.ArgumentParser(
        prog="xiaoyu mcp probe",
        description="不经模型探测一个 MCP server：握手、列工具；给 --script 则按脚本调用并逐步输出 JSON 行。",
        usage="xiaoyu mcp probe [--script FILE] [--timeout 秒] <server名 | 命令 [参数…] | --url 地址>",
        epilog="脚本形状：[{\"op\":\"listTools\"}, {\"op\":\"callTool\",\"name\":\"echo\",\"args\":{\"text\":\"hi\"}}]",
    )
    parser.add_argument("target", nargs="*", help="已声明的 server 名，或一条启动命令行（选项要写在它前面）")
    parser.add_argument("--url", help="直接探测一个远端 Streamable HTTP 地址（不必先 add）")
    parser.add_argument("--script", metavar="FILE", help="按脚本顺序执行 listTools / callTool，每步一行 JSON")
    parser.add_argument("--timeout", type=float, metavar="秒", help="每次调用的超时（默认取声明里的或内置默认）")
    #  选项段 | 目标段的切分与 add 同一套：probe 没有"名字"这个位置参数，垫一个占位
    head, rest = split_mcp_command(["_", *argv], _PROBE_FLAGS)
    args = parser.parse_args(head[1:])
    target = [*args.target, *rest]
    if args.timeout is not None and args.timeout <= 0:
        print(ui.error("--timeout 得是正数"), file=sys.stderr)
        return 2
    try:
        spec = _probe_spec(target, args.url, args.timeout)
        steps = _probe_script(args.script) if args.script else None
    except ValueError as exc:
        print(ui.error(str(exc)), file=sys.stderr)
        return 2

    #  日志单独起名：同名 server 可能正在某个会话里跑，不能截断它的日志
    log_path = mcp.McpManager._log_path(f"probe-{spec.name}")  # noqa: SLF001
    server = mcp.McpServer(spec, log_path=log_path)
    #  server 自己的日志通知（notifications/message）直接上 stderr——这就是排障要看的
    server.on_log = lambda level, text: print(ui.warning(f"  [{spec.name} {level}] {text}"), file=sys.stderr)
    scripted = steps is not None
    started = time.monotonic()
    try:
        try:
            server.ensure_started()
        except mcp.McpError as exc:
            reason = mcp._redact(str(exc))  # noqa: SLF001
            if scripted:
                record = {
                    "step": 0, "op": "initialize", "ok": False, "error": reason,
                    "error_kind": exc.kind or "unknown",
                    "seconds": round(time.monotonic() - started, 3),
                }
                if not spec.is_http:
                    record["log"] = str(log_path)
                    record["log_tail"] = mcp.log_tail(log_path)
                _probe_emit(record)
            else:
                print(ui.error(f"连不上 {spec.name}：{reason}"), file=sys.stderr)
                if not spec.is_http and "排障看日志" not in reason:
                    print(ui.secondary("  " + mcp.log_hint(log_path).replace("\n", "\n  ")), file=sys.stderr)
            return 1
        elapsed = time.monotonic() - started
        capabilities = sorted(server.capabilities)
        if scripted:
            _probe_emit({
                "step": 0, "op": "initialize", "ok": True,
                "seconds": round(elapsed, 3),
                "server": server.server_info, "protocol": server.protocol_version,
                "capabilities": capabilities, "tools": len(server.live_declared),
                "instructions": server.instructions,
            })
            return 0 if _probe_run_script(server, steps or []) else 1
        print(ui.success(
            f"已连接 {spec.name}"
            + (f" · {server.server_info}" if server.server_info else "")
            + f" · 协议 {server.protocol_version} · 握手 {elapsed:.2f}s"
        ))
        if not spec.is_http:
            print(ui.secondary(f"  命令：{' '.join([spec.command, *spec.args])}" + (f"  (cwd {spec.cwd})" if spec.cwd else "")))
        else:
            print(ui.secondary(f"  地址：{mcp.display_url(spec.url)}"))
        print(ui.secondary("  能力：" + (", ".join(capabilities) or "（无）")))
        if server.instructions:
            text = server.instructions.strip()
            if len(text) > _PROBE_TEXT_CAP:
                text = text[:_PROBE_TEXT_CAP] + "…"
            print(ui.secondary("  instructions："))
            for line in text.splitlines():
                print(ui.secondary(f"    {line}"))
        declared = server.live_declared
        hidden = [item for item in declared if not spec.allows_tool(str(item.get("name", "")))]
        print(ui.heading(f"  工具（{len(declared)}）" + (f"，其中 {len(hidden)} 个不在声明的 tools 名单里" if hidden else "")))
        for item in declared:
            name = str(item.get("name", ""))
            description = str(item.get("description") or "").strip().splitlines()
            brief = description[0] if description else "(无描述)"
            if len(brief) > 100:
                brief = brief[:100] + "…"
            mark = "  " if spec.allows_tool(name) else "✗ "
            print(f"    {mark}{ui.accent(name)}  {ui.secondary(brief)}")
        if not spec.is_http:
            print(ui.secondary(f"  日志：{shorten_home(str(log_path))}"))
        return 0
    finally:
        server.close()

