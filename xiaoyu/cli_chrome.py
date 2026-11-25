"""`xiaoyu chrome`：给 Chrome 扩展登记 native messaging host，免配置连本机 serve。

install 之后，扩展侧栏点一下「连接本机小羽」，Chrome 拉起 `xiaoyu chrome host`，
它找到或拉起 serve 并把地址和 token 交给扩展（机制见 chrome_host.py）。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import sys
from pathlib import Path

from . import chrome_host as ch
from . import ui

CHROME_USAGE = """用法：
  xiaoyu chrome install [--workspace 目录] [--approval ask|allow_all] [--extension-id ID]...
        登记 native messaging host（写进本机各 Chromium 系浏览器）；重跑可改选项
  xiaoyu chrome status       清单在不在位、serve 在不在跑
  xiaoyu chrome stop         停掉由扩展拉起的那个 serve（不碰你手动起的）
  xiaoyu chrome uninstall    删清单与启动脚本，并停掉 serve
详见 docs/browser-bridge.md「本机免配置连接」"""

_EXTENSION_ID = re.compile(r"^[a-p]{32}$")


def chrome_command(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(CHROME_USAGE)
        return 0 if argv else 2
    action, rest = argv[0], argv[1:]
    if action == "host":
        #  正常从 __main__ 的快路径进来；走到这里说明入口没经 __main__，照样能用
        return ch.host_main(rest)
    if os.name == "nt":
        print(ui.error("xiaoyu chrome 暂只支持 macOS 与 Linux"), file=sys.stderr)
        return 2
    handlers = {"install": _install, "status": _status, "stop": _stop, "uninstall": _uninstall}
    if action not in handlers:
        print(ui.error(f"未知操作：{action}"), file=sys.stderr)
        print(CHROME_USAGE, file=sys.stderr)
        return 2
    return handlers[action](rest)


def _install(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="xiaoyu chrome install")
    parser.add_argument("--workspace", help="serve 的 root（会话能去的范围上限），默认 ~")
    parser.add_argument("--approval", choices=("ask", "allow_all"),
                        help="审批档，默认 ask（打开浏览器就会自动起服务，默认不放开）")
    parser.add_argument("--extension-id", dest="extension_ids", action="append", default=[],
                        metavar="ID", help="允许连接的扩展 id（chrome://extensions 卡片上），可重复")
    args = parser.parse_args(argv)

    previous = ch.load_config()
    root = Path(args.workspace).expanduser() if args.workspace else (
        Path(previous.workspace) if previous else Path.home())
    root = root.resolve()
    if not root.is_dir():
        print(ui.error(f"工作区不存在：{root}"), file=sys.stderr)
        return 2
    bad = [i for i in args.extension_ids if not _EXTENSION_ID.match(i)]
    if bad:
        print(ui.error(f"扩展 id 应是 32 个 a–p 的小写字母：{', '.join(bad)}"), file=sys.stderr)
        return 2
    ids = _unique([*ch.KNOWN_EXTENSION_IDS, *(previous.extension_ids if previous else []),
                   *args.extension_ids])
    if not ids:
        print(ui.error("需要 --extension-id：在 chrome://extensions 打开开发者模式，扩展卡片上那串 32 位字母"),
              file=sys.stderr)
        return 2
    cfg = ch.HostConfig(
        workspace=str(root),
        approval=args.approval or (previous.approval if previous else "ask"),
        extension_ids=ids,
        path_env=os.environ.get("PATH", ""),
        python=sys.executable,
    )

    base = ch.base_dir()
    base.mkdir(parents=True, exist_ok=True)
    ch.scratch_dir().mkdir(parents=True, exist_ok=True)
    launcher = ch.launcher_path()
    launcher.write_text(ch.render_launcher(cfg.python, base), encoding="utf-8")
    launcher.chmod(launcher.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    ch.save_config(cfg)

    manifest = json.dumps(ch.render_manifest(launcher, ids), ensure_ascii=False, indent=2) + "\n"
    written = []
    for name, browser_dir in ch.browser_dirs():
        if not browser_dir.is_dir():
            continue
        target = ch.manifest_file(browser_dir)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(manifest, encoding="utf-8")
        written.append(name)

    if not written:
        print(ui.warning("没找到 Chromium 系浏览器的用户目录，清单没写进任何浏览器；装好浏览器后重跑"))
    else:
        print(ui.success(f"已登记到：{', '.join(written)}"))
    default = ch.default_workspace_for(cfg)
    print(ui.secondary(f"  root {root} · 审批 {cfg.approval} · 扩展 {', '.join(ids)}"))
    if default is not None:
        print(ui.secondary(f"  扩展里没选目录的会话落在空目录 {default}"))
    else:
        print(ui.warning(f"  root 不包含 {ch.scratch_dir()}，没选目录的会话会落在 root 本身"))
    print(ui.secondary("  API key 要放在 `xiaoyu config` 写的用户级 .env 里：浏览器拉起的进程读不到 ~/.zshrc 里 export 的变量"))
    if ch.serve_deps_missing():
        print(ui.warning("  还缺 serve 依赖：pip install 'xiaoyu-agent[serve]'"))
    state = ch.status()
    if state["running"] and state["version"] != state["hostVersion"]:
        print(ui.secondary("  在跑的 serve 是旧版本，扩展下次连接时会自动重启它"))
    return 0


def _status(argv: list[str]) -> int:
    cfg = ch.load_config()
    if cfg is None:
        print(ui.warning("没安装：xiaoyu chrome install --extension-id <id>"))
        return 1
    print(f"root {cfg.workspace} · 审批 {cfg.approval} · 扩展 {', '.join(cfg.extension_ids)}")
    launcher = ch.launcher_path()
    for name, browser_dir in ch.browser_dirs():
        if not browser_dir.is_dir():
            continue
        target = ch.manifest_file(browser_dir)
        data = ch._read_json(target)
        if not isinstance(data, dict):
            print(f"  {name}: {ui.warning('清单不在')}")
        elif data.get("path") != str(launcher):
            print(f"  {name}: {ui.warning('清单指向别处：' + str(data.get('path')))}")
        else:
            print(f"  {name}: 已登记")
    if not os.access(launcher, os.X_OK):
        print(ui.warning(f"  启动脚本不在或不可执行：{launcher}（重跑 install）"))
    state = ch.status()
    if state["running"]:
        note = "" if state["version"] == state["hostVersion"] else f"（旧版本，当前装的是 {state['hostVersion']}，下次连接自动重启）"
        print(f"serve 在跑：{state['baseUrl']} · {state['version']}{note}")
    else:
        print("serve 没在跑（扩展连接时会自动拉起）")
    return 0


def _stop(argv: list[str]) -> int:
    if ch.stop_running():
        print(ui.success("已停"))
    else:
        print("没有由扩展拉起、正在跑的 serve")
    return 0


def _uninstall(argv: list[str]) -> int:
    ch.stop_running()
    launcher = ch.launcher_path()
    removed = []
    for name, browser_dir in ch.browser_dirs():
        target = ch.manifest_file(browser_dir)
        data = ch._read_json(target)
        #  只删指向自家启动脚本的那份，别人同名写的不碰
        if isinstance(data, dict) and data.get("path") == str(launcher):
            target.unlink()
            removed.append(name)
    for path in (launcher, ch.config_path(), ch.state_path()):
        if path.exists():
            path.unlink()
    print(ui.success(f"已从 {', '.join(removed)} 移除" if removed else "没有要移除的清单"))
    print(ui.secondary(f"  空目录与日志留在 {ch.base_dir()}"))
    return 0


def _unique(items: list[str]) -> list[str]:
    seen: list[str] = []
    for item in items:
        if item not in seen:
            seen.append(item)
    return seen
