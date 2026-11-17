"""`xiaoyu plugin`：插件包（skills + MCP 声明）的装卸更新。"""

from __future__ import annotations

import argparse
import shutil
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

from . import ui
from .cli import shorten_home

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
            for line in plugins.declaration_details(entry):
                print("      " + ui.secondary(ui.fit(line, reserve=6)))
    #  识别到但不装的东西逐条报出来：hooks 静默丢掉比不装更危险
    for note in bundle.notes:
        print(ui.warning(f"  注意：{note}"))


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
        #  来源可能是带令牌的 git 地址（https://user:token@host/…）：列表会被截图、
        #  被贴进工单，账号密码不回显
        from .mcp import _redact

        source = _redact(str(source))
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
        print(ui.warning(f"  注意：{note}"))
    return "，".join(parts)


def _print_mcp_diff(before: dict[str, Any], wanted: dict[str, Any]) -> None:
    from . import plugins

    for name in sorted(set(before) | set(wanted)):
        if name not in before:
            print(f"    + {ui.accent(name)}  {plugins.command_line(wanted[name])}")
            for line in plugins.declaration_details(wanted[name]):
                print("        " + ui.secondary(ui.fit(line, reserve=8)))
        elif name not in wanted:
            print(f"    - {ui.accent(name)}  {plugins.command_line(before[name])}")
        else:
            changes = plugins.declaration_changes(before[name], wanted[name])
            if changes:
                print(f"    ~ {ui.accent(name)}")
                for line in changes:
                    print("        " + ui.fit(line, reserve=8))


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

