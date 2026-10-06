"""`xiaoyu term`：shell 集成的命令行入口（@x / @c / init）。

与会话主干共用一次性路径的零件（信任门、前端、Agent 装配）从 cli 取：
那些是进程入口的东西，这里只是其中一个子命令。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from . import modes, ui
from .agent import Agent
from .cli import (
    _warn_env_problems,
    build_toolbox,
    compose_prompt,
    oneshot_frontend,
    read_piped_stdin,
    resolve_folder_trust,
    run_once,
)
from .config import EFFORT_LEVELS, Config, MissingConfig, load_dotenv
from .permissions import Permissions
from .session_log import (
    SessionLockedError,
    SessionLog,
    install_exit_logging,
    load_system_prompt,
    open_named,
)


TERM_USAGE = """用法：
  xiaoyu term init <bash|zsh|fish|powershell> [--name 名字] [--command-not-found] [--natural]
        输出 shell 脚本：eval "$(xiaoyu term init zsh)"（fish 用 | source，PowerShell 用 Invoke-Expression）
  xiaoyu term install [zsh|bash|fish] [--natural] [--command-not-found] [--name 名字] [--yes] [--dry-run]
        把上面那一行写进 shell 启动文件（~/.zshrc 等）；不写 shell 就按 $SHELL 认，重跑可改选项
  xiaoyu term uninstall [--yes] [--dry-run]   从启动文件里移除 install 写入的那一段
  xiaoyu term run [问题…]      带着自上次提问以来跑过的命令向模型提问（脚本里定义成 @x / @xiaoyu）；
                               不带问题就读一行原样文本当问题（问号、引号、管道符都不必转义）
  xiaoyu term command [需求…]  一句话换一条命令，打到 stdout（zsh / bash 脚本里定义成 @c：zsh 放回提示符，
                               bash 推进历史按 ↑ 取）；不进会话、不执行任何东西
  xiaoyu term log <命令行>     备用入口：手动记一条命令（钩子不方便用内建追加的环境）
  xiaoyu term info             一行：会话 id · 模型 · 已用 token · 待交付命令数（放进提示符用）
详见 docs/terminal-integration.md"""


def term_command(argv: list[str]) -> int:
    """`xiaoyu term`：shell 集成。

    人不进 REPL，在自己的 shell 里干活，`@x 问题` 时把刚跑过的命令作为上下文
    交给模型并续写同一个会话。`log` / `info` 是 shell 钩子与提示符要调的，
    走 term.fast_command（__main__ 入口在导入本模块之前就把它们拦下了；从
    这里进来说明入口没走 __main__，照样能用，只是慢）。
    """
    from . import term

    action = argv[0] if argv else ""
    if action == "init":
        return term_init_command(argv[1:])
    if action == "install":
        return term_install_command(argv[1:])
    if action == "uninstall":
        return term_uninstall_command(argv[1:])
    if action == "run":
        return term_run_command(argv[1:])
    if action == "command":
        return term_suggest_command(argv[1:])
    if action in ("log", "info"):
        return term.fast_command(argv)
    if action in ("", "-h", "--help", "help"):
        print(TERM_USAGE)
        return 0
    print(ui.error(f"未知的 term 子命令：{action}"), file=sys.stderr)
    print(TERM_USAGE, file=sys.stderr)
    return 2


def term_init_command(argv: list[str]) -> int:
    """`xiaoyu term init <shell>`：把集成脚本打到 stdout，由用户 eval / source。"""
    from . import term

    parser = argparse.ArgumentParser(
        prog="xiaoyu term init",
        description="输出 shell 集成脚本（会话变量、@x / @c、记命令的钩子）。",
    )
    parser.add_argument("shell", choices=term.SHELLS)
    parser.add_argument(
        "--name",
        help="具名会话：id 固定为 term-<名字>，多个终端共用、关掉重开也续着聊（默认每个终端一个随机 id）",
    )
    parser.add_argument(
        "--command-not-found",
        action="store_true",
        help="敲错的命令整行交给模型（默认不开：每个 typo 都打一次模型太费钱）",
    )
    parser.add_argument(
        "--natural",
        action="store_true",
        help="不敲 @c：整行是一句自然语言（含非 ASCII 字符、且第一个词不是命令）就转给 @c。"
        "只支持 zsh，会包一层回车键（默认不开）",
    )
    parser.add_argument(
        "--launcher",
        help="脚本里怎么调 xiaoyu（默认：PATH 上有就用 xiaoyu，没有就用当前解释器 -m xiaoyu）",
    )
    args = parser.parse_args(argv)
    directory = term.pending_dir()
    try:
        session_id = term.session_id_for(args.name)
        #  先渲染再建目录：参数不对（--natural 配了不支持的 shell）就什么都别留下
        script = term.render_script(
            args.shell,
            session_id,
            named=bool(args.name),
            command_not_found=args.command_not_found,
            natural=args.natural,
            launcher=args.launcher,
            directory=directory,
        )
    except ValueError as exc:
        print(ui.error(str(exc)), file=sys.stderr)
        return 2
    #  pending 文件里是脱敏前的原始命令行：目录自己可读就够了
    try:
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    except OSError as exc:
        print(ui.error(f"建不了 {directory}：{exc}"), file=sys.stderr)
        return 1
    sys.stdout.write(script)
    return 0


def _confirm(question: str) -> bool:
    try:
        answer = input(ui.prompt(question)).strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    return answer in ("y", "yes")


def term_install_command(argv: list[str]) -> int:
    """`xiaoyu term install`：把 `term init` 那一行写进 shell 启动文件。

    改的是工作区之外的用户配置，所以和 terminal-setup 同一姿势：先把计划打出来，
    确认后才写，写前留 .bak。
    """
    from . import shell_setup, term

    parser = argparse.ArgumentParser(
        prog="xiaoyu term install",
        description="把终端集成（@x / @c）写进 shell 启动文件，新开的终端自动生效。",
    )
    parser.add_argument("shell", nargs="?", choices=shell_setup.SHELLS, help="默认按 $SHELL 认")
    parser.add_argument("--name", help="具名会话（同 term init --name）")
    parser.add_argument("--command-not-found", action="store_true", help="同 term init --command-not-found")
    parser.add_argument("--natural", action="store_true", help="同 term init --natural（只支持 zsh）")
    parser.add_argument("--yes", action="store_true", help="不询问，直接写入")
    parser.add_argument("--dry-run", action="store_true", help="只看计划，不写任何文件")
    args = parser.parse_args(argv)

    shell = args.shell or shell_setup.detect_shell()
    if shell is None:
        print(ui.error("认不出你用的 shell，请写明：xiaoyu term install zsh|bash|fish"), file=sys.stderr)
        print(ui.secondary("PowerShell 请照 docs/terminal-integration.md 把那一行贴进 $PROFILE。"), file=sys.stderr)
        return 2
    if args.natural and shell not in term._NATURAL:
        print(ui.error(f"--natural 目前只支持 {' / '.join(term._NATURAL)}（{shell} 里请照常写 @c）"), file=sys.stderr)
        return 2
    line = shell_setup.init_line(
        shell,
        term.default_launcher(shell),
        name=args.name,
        command_not_found=args.command_not_found,
        natural=args.natural,
    )
    plan = shell_setup.plan_install(shell, line)
    marks = {"install": ui.success("将写入"), "update": ui.success("将更新"), "already": ui.secondary("已配好"),
             "manual": ui.warning("跳过"), "broken": ui.error("跳过")}
    print(f"  {marks[plan.action]}  {_display_path(plan.path)}：{plan.detail}")
    if plan.action in ("install", "update"):
        print(ui.secondary(f"        {line}"))
    if plan.action not in ("install", "update"):
        return 0 if plan.action == "already" else 1
    if args.dry_run:
        return 0
    if not args.yes:
        print(ui.secondary("  会先留一份 .bak 备份；只动小羽自己标记的那一段。"))
        if not _confirm("写入？[y/N] "):
            print(ui.secondary("没有改动。"))
            return 1
    try:
        print(ui.success("  " + shell_setup.apply(plan)))
    except OSError as exc:
        print(ui.error(f"  写入失败：{exc}"), file=sys.stderr)
        return 1
    print(ui.secondary(f"新开一个终端即可使用 @x / @c；当前终端执行 source {_display_path(plan.path)} 立刻生效。"))
    return 0


def term_uninstall_command(argv: list[str]) -> int:
    """`xiaoyu term uninstall`：移除 install 写入的段落；用户手写的行不动。"""
    from . import shell_setup

    parser = argparse.ArgumentParser(
        prog="xiaoyu term uninstall",
        description="从 shell 启动文件里移除 xiaoyu term install 写入的终端集成。",
    )
    parser.add_argument("--yes", action="store_true", help="不询问，直接移除")
    parser.add_argument("--dry-run", action="store_true", help="只看计划，不写任何文件")
    args = parser.parse_args(argv)

    plans = shell_setup.removal_plans()
    manual = [path for path, kind in shell_setup.installed_in() if kind == "manual"]
    for plan in plans:
        print(f"  {ui.success('将移除')}  {_display_path(plan.path)}（留 .bak 备份）")
    for path in manual:
        print(f"  {ui.warning('跳过')}  {path}：term init 是你自己写的，要停用请手动删那一行")
    if not plans:
        print(ui.secondary("没有需要移除的。"))
        return 0
    if args.dry_run:
        return 0
    if not args.yes and not _confirm(f"移除这 {len(plans)} 处？[y/N] "):
        print(ui.secondary("没有改动。"))
        return 1
    for plan in plans:
        try:
            print(ui.success("  " + shell_setup.apply_removal(plan)))
        except OSError as exc:
            print(ui.error(f"  {plan.path}：写入失败 {exc}"), file=sys.stderr)
            return 1
    print(ui.secondary("已打开的终端里 @x / @c 仍在，关掉重开后消失。"))
    return 0


def _display_path(path: Path) -> str:
    home = Path.home()
    try:
        return "~/" + str(path.relative_to(home))
    except ValueError:
        return str(path)


def open_term_session(config: Config, session_id: str) -> tuple[SessionLog, list[dict[str, Any]]]:
    """终端会话：有则续、无则建，与 `--session-id` 同一套，只是目录固定在
    sessions/term/ 下——shell 里 cd 到哪都是同一段对话。"""
    from . import term

    log, restored = open_named(session_id, config.model, str(config.workspace), term.term_sessions_dir())
    if config.system_prompt is None:
        config.system_prompt = load_system_prompt(log.path)
    return log, restored


def read_question_line(label: str = "问 › ") -> str | None:
    """`@x` / `@c` 不带问题时读一行当问题。写在命令行上的问题要先过 shell 的解析，
    问号、引号、括号、管道符都会被它吃掉或报错；这里读到的是原样文本。

    提示符写到 stderr：stdout 留给回答（`@x > 答案.txt` 时它不是终端）。
    返回 None = 用户取消（Ctrl-C / Ctrl-D）。
    """
    sys.stderr.write(ui.prompt(label))
    sys.stderr.flush()
    try:
        return input().strip()
    except (EOFError, KeyboardInterrupt):
        print(file=sys.stderr)
        return None


def term_run_command(argv: list[str]) -> int:
    """`xiaoyu term run [问题…]`（脚本里的 @x）：取走 pending 里的命令，拼成
    终端上下文放在问题前面，走一次性路径续写本终端的会话。

    与 `-p` 共用一切：审批（stdin 是终端就照常问）、信任门、输出格式 text。
    刻意不默认 --yolo：这是在用户自己的机器、自己的目录里跑。
    """
    from . import term

    parser = argparse.ArgumentParser(
        prog="xiaoyu term run",
        description="带着自上次提问以来跑过的命令向模型提问（续写本终端的会话）。",
    )
    parser.add_argument(
        "question",
        nargs="*",
        help="问题（也可从管道给：cat err.log | @x 这是什么错；不带问题就读一行原样文本）",
    )
    parser.add_argument("--model", help="这一问换个模型")
    parser.add_argument("--mode", choices=list(modes.CYCLE), default=None, help="起始模式（同主命令）")
    parser.add_argument("--yolo", action="store_true", help="不再逐个确认写文件和执行命令")
    parser.add_argument("--effort", choices=list(EFFORT_LEVELS), default=None, help="推理深度")
    parser.add_argument(
        "--handoff", action="store_true",
        help="问题取自 @c 转过来的那句需求（集成脚本用；@c 判定不是一条命令能办的事时）",
    )
    args = parser.parse_args(argv)

    session_id = term.current_session()
    if not session_id:
        print(
            ui.error(
                f"没有 {term.SESSION_ENV}：先在 shell 里 eval \"$(xiaoyu term init zsh)\""
                "（bash/fish/powershell 同理，见 xiaoyu term --help）"
            ),
            file=sys.stderr,
        )
        return 2
    if args.handoff:
        question = term.take_handoff(session_id)
        if not question:
            print(ui.error("没有 @c 转过来的需求"), file=sys.stderr)
            return 2
        print(ui.secondary("这不像一条命令能办的事，转给 @x"), file=sys.stderr)
    else:
        question = compose_prompt(args.question, read_piped_stdin())
    if not question and sys.stdin.isatty():
        typed = read_question_line()
        if typed is None:
            return 130
        question = typed
    if not question:
        print(
            ui.error("要问什么？用法：@x <问题>；或只敲 @x 回车，再输入问题（原样读入，不必转义）"),
            file=sys.stderr,
        )
        return 2

    workspace = Path.cwd()
    #  与主命令同一道门，先于 load_dotenv：工作区 .env 是被门管的对象
    trust = resolve_folder_trust(
        workspace, grant=False, interactive=sys.stdin.isatty() and sys.stderr.isatty()
    )
    load_dotenv(untrusted_dir=None if trust.trusted else workspace)
    _warn_env_problems()

    pending = term.pending_path(session_id)
    entries = term.drain_pending(pending)
    prompt = term.compose(question, entries)
    try:
        config = Config.from_env(
            workspace=workspace,
            model=args.model,
            auto_approve=args.yolo or None,
            mode=args.mode,
            effort=args.effort,
            workspace_trusted=trust.trusted,
        )
        permissions = Permissions.load(config.workspace, include_workspace=trust.trusted)
        approver, sink = oneshot_frontend(permissions, "text")
        session_log, restored = open_term_session(config, session_id)
        agent = Agent(
            config,
            build_toolbox(config, None),
            approver=approver,
            session_log=session_log,
            permissions=permissions,
            sink=sink,
        )
    except (MissingConfig, ValueError, SessionLockedError) as exc:
        #  没交到模型手上的命令放回去，修好配置再问时还带着
        term.requeue(pending, entries)
        print(ui.error(str(exc)), file=sys.stderr)
        return 2
    install_exit_logging(agent.session_log)
    #  copy=False：续写的就是历史所在那个文件（同 --session-id）
    agent.restore(restored, copy=False)
    note = [f"会话 {session_id}"]
    if entries:
        note.append(f"带上 {len(entries)} 条命令")
    if restored:
        note.append(f"接上 {len(restored)} 条消息")
    #  走 stderr：stdout 可能正被管道接去当结果用
    print(ui.secondary(" · ".join(note)), file=sys.stderr)
    return run_once(agent, prompt, "text")


#  `@c` 等多久：一条命令的事，模型十几秒不回就是出了问题，不该陪着生成级的长超时
TERM_COMMAND_TIMEOUT = 30.0
#  默认推理深度：把一句话翻成一条命令是有界任务，想得深只是让人多等
TERM_COMMAND_EFFORT = "low"


def _leading_flags(
    argv: list[str], valued: tuple[str, ...], switches: tuple[str, ...]
) -> tuple[list[str], list[str]]:
    """把 argv 切成（开头的旗标段, 其余）。旗标只认开头那一段：需求本身常带着
    横杠（「把 -rf 开头的文件删掉」「去掉 --verbose」），整段交给 argparse 会被
    当成不认识的旗标报错。`--` 可以显式结束旗标段。"""
    index = 0
    while index < len(argv):
        token = argv[index]
        if token == "--":
            return argv[:index], argv[index + 1 :]
        if token in switches:
            index += 1
        elif token in valued:
            index += 2
        elif token.split("=", 1)[0] in valued:
            index += 1
        else:
            break
    return argv[:index], argv[index:]


def term_command_reply(config: Config, messages: list[dict[str, str]], model: str | None, effort: str | None) -> str:
    """`@c` 的那一次请求：非流式、不带工具，与压缩摘要走同一条 create 路径。

    用哪个模型：点名了就用点名的；没点名先用辅助模型（XIAOYU_SUMMARY_MODEL，
    与摘要同一个"有界任务用便宜模型"的位置），它没有 provider 能接再用主模型。
    推理深度默认 low；这一档是替用户选的，端点不认时去掉它再试一次，而不是
    让一个用户没提过的参数把请求打死。点名的 --effort 被拒就照实报错。
    """
    from . import providers

    registry = providers.build(config)
    try:
        route = registry.resolve(model or config.summary_model or config.model)
    except providers.UnknownModel:
        if model:
            raise
        route = registry.resolve(config.model)
    request: dict[str, Any] = {"model": route.model, "messages": messages}
    level = providers.effort_for(
        route.provider, route.model, effort or TERM_COMMAND_EFFORT, EFFORT_LEVELS
    )
    try:
        response = route.client.chat.completions.create(**request, reasoning_effort=level)
    except Exception:  # noqa: BLE001 - 只有替用户选的那一档才重试，见 docstring
        if effort:
            raise
        response = route.client.chat.completions.create(**request)
    return str(response.choices[0].message.content or "")


def term_suggest_command(argv: list[str]) -> int:
    """`xiaoyu term command [需求…]`（脚本里的 @c）：一句话换一条命令。

    命令打到 stdout（shell 函数接走，放回人的提示符或历史），说明与提示走
    stderr。不进会话、不带工具、不取走 pending：见 term 模块 docstring。
    退出码：0 给了命令；1 模型认为这不是一条命令能办的事（说明照打）；
    3 同上但带了 --handoff：需求已留给 `term run --handoff`；2 用法或配置错；130 被打断。
    """
    from . import command_check, errors, folder_trust, term
    from .mcp import _redact

    flags, words = _leading_flags(
        argv, ("--model", "--effort", "--shell", "--shell-version"), ("-h", "--help", "--handoff")
    )
    parser = argparse.ArgumentParser(
        prog="xiaoyu term command",
        description="一句话换一条命令（脚本里的 @c）：命令放回你的提示符，由你回车执行。"
        "旗标只认写在需求前面的。",
        add_help=False,
    )
    parser.add_argument("-h", "--help", action="store_true", help="显示这段说明")
    parser.add_argument("--model", help="这一次换个模型（默认用辅助模型 XIAOYU_SUMMARY_MODEL）")
    parser.add_argument(
        "--effort", choices=list(EFFORT_LEVELS), default=None,
        help=f"推理深度（默认 {TERM_COMMAND_EFFORT}）",
    )
    parser.add_argument("--shell", default="", help="命令要在哪种 shell 里跑（集成脚本会带上）")
    parser.add_argument("--shell-version", default="", help="shell 版本（集成脚本会带上）")
    parser.add_argument(
        "--handoff", action="store_true",
        help=f"不是一条命令能办的事就把需求转给 @x：留下交接、以退出码 {term.HANDOFF_EXIT} 告诉脚本（集成脚本会带上）",
    )
    args = parser.parse_args(flags)
    if args.help:
        #  stdout 是给 shell 函数接命令用的：帮助打到那里会被放上提示符
        parser.print_help(sys.stderr)
        return 0

    ask = " ".join(words).strip()
    material = read_piped_stdin()
    if not ask and material:
        ask, material = material, ""
    if not ask and sys.stdin.isatty():
        typed = read_question_line("要什么命令 › ")
        if typed is None:
            return 130
        ask = typed
    if not ask:
        print(
            ui.error("要什么命令？用法：@c <一句话需求>；或只敲 @c 回车，再输入需求（原样读入，不必转义）"),
            file=sys.stderr,
        )
        return 2

    workspace = Path.cwd()
    #  与主命令同一道门，但不发问也不提示：没过门只是不读工作区的 .env，
    #  这里既不加载工作区的工具也不执行任何东西
    trust = folder_trust.evaluate(workspace, interactive=False)
    load_dotenv(untrusted_dir=None if trust.trusted else workspace)
    _warn_env_problems()

    session_id = term.current_session()
    environment, fresh = term.load_environment(args.shell, args.shell_version)
    if fresh:
        print(ui.secondary(f"已记下本机环境：{term.environment_summary(environment)}"), file=sys.stderr)
    entries = (
        term.peek_pending(term.pending_path(session_id), term.RECENT_COMMANDS) if session_id else []
    )
    messages = term.command_messages(
        ask,
        environment=environment,
        cwd=str(workspace),
        situation=term.session_situation(),
        entries=entries,
        recall=term.load_recall(session_id),
        material=material,
    )
    #  一两秒里屏幕上什么都没有，人会以为没按上回车：占一行，拿到回答就擦掉
    waiting = sys.stderr.isatty()
    if waiting:
        sys.stderr.write(ui.secondary("想一下…"))
        sys.stderr.flush()
    try:
        config = Config.from_env(
            workspace=workspace, model=args.model, workspace_trusted=trust.trusted
        )
        config.request_timeout = min(config.request_timeout, TERM_COMMAND_TIMEOUT)
        try:
            reply = term_command_reply(config, messages, args.model, args.effort)
        finally:
            if waiting:
                sys.stderr.write("\r\x1b[2K")
                sys.stderr.flush()
    except KeyboardInterrupt:
        print(file=sys.stderr)
        return 130
    except (MissingConfig, ValueError) as exc:
        print(ui.error(str(exc)), file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - 请求失败按分类说一句，不甩 traceback 到提示符上
        verdict = errors.classify(exc)
        print(
            ui.error(f"没拿到命令（{verdict.kind}）：{type(exc).__name__}: {_redact(str(exc))[:300]}"),
            file=sys.stderr,
        )
        if verdict.hint:
            print(ui.secondary(verdict.hint), file=sys.stderr)
        return 1
    try:
        command, note = term.parse_suggestion(reply)
    except ValueError as exc:
        print(ui.error(f"{exc}，没有可用的命令"), file=sys.stderr)
        return 1
    if not command:
        #  讲解、多步排查、要总结的事：脚本接得住就原样转给 @x，人不必再敲一遍。
        #  模型的说明不打——它多半是「这得用 @x」或一句反问，@x 会自己接着问
        if args.handoff and session_id and term.save_handoff(session_id, compose_prompt([ask], material)):
            return term.HANDOFF_EXIT
        print(ui.secondary(note or "这不像是一条命令能办的事；要它动手可以用 @x"), file=sys.stderr)
        return 1
    if note:
        print(ui.secondary(note), file=sys.stderr)
    #  这条命令由人自己回车，不过审批：能认出来的破坏性与提权形态在这里点一句
    for reason in (command_check.dangerous_command(command), command_check.privileged_command(command)):
        if reason:
            print(ui.warning(f"留意：{reason}"), file=sys.stderr)
    term.save_recall(session_id, ask, command)
    print(command)
    return 0

