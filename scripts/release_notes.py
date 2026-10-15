#!/usr/bin/env python3
"""发版说明生成：读上一个 tag 到 HEAD 的提交，按类型分组，全自动、不需要人改。

    python scripts/release_notes.py                 # 版本取 xiaoyu.__version__，起点取最近 tag
    python scripts/release_notes.py --since v0.59.0 --version 0.61.0
    python scripts/release_notes.py --polish        # 额外让小羽润色成面向用户的文案（可选）
    python scripts/release_notes.py --validation self_test.json   # 指定自测记录（默认取最近一次 nightly）

输出到 docs/releases/<版本>.md（已存在则覆盖）。release.yml 在 PyPI 发布成功后
拿这个文件建 GitHub Release；文件不存在就退回 --generate-notes。
scripts/release.py 发版时自动调这里，人不用单独跑。

**「行为变化（升级前看一眼）」一节从提交正文里抽**，不再手写：commit 正文里以
`行为变化:`（或 `BREAKING CHANGE:`）打头的 footer 行就是一条，到空行为止；subject
带 `!`（`feat!: …`）而没写 footer 的，拿 subject 当那一条。写提交时把"升级后用户会
感知到什么不同"放进 footer，发版说明就自动有了。

「本版验证」一节固定在文末，内容来自 tests_ai/self_test.md 跑出的记录文件
（scripts/self_test.py 每晚跑并存到用户配置目录 self-test/latest.json；也可
`--validation` 指定文件）。这一节不经模型润色——它是事实，不是文案。没有记录就写
「未附自测记录」。

润色是可选项（`--polish`）：模型不可用时退化为纯分组列表，不报错。分组列表本身
就是合格的发版说明。

分组规则：commit subject 的 `type(scope): 说明` 前缀，feat / fix / docs / chore，
其它类型（refactor / test / perf…）与无前缀的归"其它"。merge 提交不计。
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

GROUPS = (
    ("feat", "新功能"),
    ("fix", "修复"),
    ("docs", "文档"),
    ("chore", "工程"),
    ("other", "其它"),
)
_TYPES = {key for key, _ in GROUPS if key != "other"}
#  type(scope)!: subject —— scope 与 ! 可省
_SUBJECT = re.compile(r"^(?P<type>[a-z]+)(?:\((?P<scope>[^)]*)\))?(?P<bang>!)?:\s*(?P<subject>.+)$")
#  git log 分隔：字段 \x1f，记录 \x1e（subject / body 里不会出现这两个控制字符）
_LOG_FORMAT = "%H%x1f%s%x1f%b%x1e"

BEHAVIOR_TITLE = "## 行为变化（升级前看一眼）"
#  正文里这些 key 打头的行是「行为变化」footer（冒号中英文都认）
_BEHAVIOR_FOOTER = re.compile(r"^(?:行为变化|BREAKING[ -]CHANGE)[:：]\s*(?P<text>.*)$")

_POLISH_PROMPT = """下面是一个 Python 命令行工具（小羽，PyPI 包 xiaoyu-agent）新版本的提交清单，
已按类型分组。请把它改写成面向使用者的中文发版说明：

- 保留分组结构（标题沿用），每条一行，说"用户能感知到什么"而不是改了哪个文件；
- 纯内部重构、CI、测试类条目合并成一句或删掉；
- 不要编造清单里没有的功能；不要加"升级建议"之类的套话；
- 不要调用任何工具，只改写文本；
- 输出 Markdown 正文（不含一级标题），放进 notes 字段。

=== 提交清单 ===
{notes}
"""


def parse_subject(subject: str) -> tuple[str, str]:
    """返回 (分组键, 去掉前缀的说明)。认不出前缀归 other、原样保留。"""
    match = _SUBJECT.match(subject.strip())
    if not match:
        return "other", subject.strip()
    kind = match.group("type")
    text = match.group("subject").strip()
    scope = match.group("scope")
    if scope:
        text = f"{scope}：{text}"
    return (kind if kind in _TYPES else "other"), text


def is_breaking_subject(subject: str) -> bool:
    """subject 带 `!`（`feat!: …` / `fix(x)!: …`）。"""
    match = _SUBJECT.match(subject.strip())
    return bool(match and match.group("bang"))


def behavior_notes(subject: str, body: str) -> list[str]:
    """一个提交的「行为变化」条目：正文里每个 footer 一条（到空行为止，续行并成一句）；
    没有 footer 但 subject 带 `!` 的，拿去前缀的 subject 当一条。"""
    notes: list[str] = []
    current: list[str] | None = None
    for raw in body.splitlines():
        line = raw.strip()
        match = _BEHAVIOR_FOOTER.match(line)
        if match:
            if current:
                notes.append(" ".join(current))
            current = [match.group("text").strip()] if match.group("text").strip() else []
            continue
        if current is None:
            continue
        if not line:
            if current:
                notes.append(" ".join(current))
            current = None
            continue
        current.append(line)
    if current:
        notes.append(" ".join(current))
    notes = [n for n in notes if n]
    if not notes and is_breaking_subject(subject):
        notes.append(parse_subject(subject)[1])
    return notes


def strip_footers(body: str) -> str:
    """正文去掉「行为变化」footer 段落（它们单独成节，不该再出现在条目子项里）。"""
    kept: list[str] = []
    for para in body.split("\n\n"):
        first = para.strip().splitlines()[0] if para.strip() else ""
        if _BEHAVIOR_FOOTER.match(first):
            continue
        kept.append(para)
    return "\n\n".join(kept).strip()


def collect_commits(since: str, repo: Path = REPO) -> list[dict[str, str]]:
    """since..HEAD 的非 merge 提交，按时间正序（老的在前）。"""
    proc = subprocess.run(
        ["git", "-C", str(repo), "log", "--no-merges", "--reverse",
         f"--format={_LOG_FORMAT}", f"{since}..HEAD"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", check=True,
    )
    commits = []
    for record in proc.stdout.split("\x1e"):
        record = record.strip("\n")
        if not record.strip():
            continue
        sha, subject, body = (record.split("\x1f", 2) + ["", ""])[:3]
        commits.append({"sha": sha.strip(), "subject": subject.strip(), "body": body.strip()})
    return commits


def group_commits(commits: list[dict[str, str]]) -> dict[str, list[dict[str, str]]]:
    grouped: dict[str, list[dict[str, str]]] = {key: [] for key, _ in GROUPS}
    for commit in commits:
        kind, text = parse_subject(commit["subject"])
        grouped[kind].append({**commit, "text": text})
    return grouped


def render_behavior(commits: list[dict[str, str]]) -> str:
    """「行为变化」一节；没有任何条目返回空串（节整个不出现）。"""
    lines: list[str] = []
    for commit in commits:
        for note in behavior_notes(commit["subject"], commit.get("body", "")):
            lines.append(f"- {note}（{commit['sha'][:7]}）")
    if not lines:
        return ""
    return BEHAVIOR_TITLE + "\n\n" + "\n".join(lines) + "\n"


def render_plain(grouped: dict[str, list[dict[str, str]]]) -> str:
    """纯分组列表（也是润色的输入）。body 只取第一段（footer 段落除外），缩进成子项。"""
    out: list[str] = []
    for key, title in GROUPS:
        items = grouped.get(key) or []
        if not items:
            continue
        out.append(f"## {title}")
        out.append("")
        for item in items:
            out.append(f"- {item['text']}（{item['sha'][:7]}）")
            first_para = strip_footers(item["body"]).split("\n\n", 1)[0].strip()
            if first_para:
                for line in first_para.splitlines():
                    out.append(f"  {line.strip()}")
        out.append("")
    return "\n".join(out).rstrip() + "\n"


def render_body(commits: list[dict[str, str]], polished: str | None = None) -> str:
    """整个正文：「行为变化」节在前（有才出现），后面是分组列表或润色稿。"""
    behavior = render_behavior(commits)
    main = polished or render_plain(group_commits(commits))
    return (behavior + "\n" + main) if behavior else main


def polish(plain: str, model: str | None, timeout: float) -> str | None:
    """让小羽润色；任何失败返回 None（调用方退化为 plain）。"""
    prompt = _POLISH_PROMPT.replace("{notes}", plain)
    schema = json.dumps({
        "type": "object",
        "properties": {"notes": {"type": "string"}},
        "required": ["notes"],
    })
    env = dict(os.environ)
    for key in ("XIAOYU_ENABLE_HOOKS", "XIAOYU_ENABLE_SKILLS", "XIAOYU_ENABLE_PLUGINS",
                "XIAOYU_ENABLE_MCP", "XIAOYU_ENABLE_AGENTS", "XIAOYU_ENABLE_WEB_SEARCH",
                "XIAOYU_ENABLE_BROWSER", "XIAOYU_UPDATE_CHECK"):
        env[key] = "0"
    #  空临时工作区、不开 --yolo：它只需要改写文本。别加 --mode plan——plan 档会把
    #  structured_output 当写操作拦下，无人值守里又退不出 plan，结构化结果永远交不出来
    argv = [sys.executable, "-m", "xiaoyu", "-p", prompt,
            "--output-format", "json", "--output-schema", schema]
    if model:
        argv += ["--model", model]
    with tempfile.TemporaryDirectory(prefix="xiaoyu-release-notes-") as scratch:
        try:
            proc = subprocess.run(
                argv + ["--workspace", scratch], capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=timeout, env=env,
                stdin=subprocess.DEVNULL, cwd=scratch,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            print(f"[release_notes] 润色不可用（{exc.__class__.__name__}），退化为分组列表", file=sys.stderr)
            return None
    if proc.returncode != 0:
        print(f"[release_notes] 润色失败（退出码 {proc.returncode}）：{proc.stderr.strip()[-300:]}",
              file=sys.stderr)
        return None
    lines = [line for line in proc.stdout.splitlines() if line.strip()]
    try:
        output = json.loads(lines[-1]).get("output") if lines else None
    except json.JSONDecodeError:
        output = None
    notes = output.get("notes") if isinstance(output, dict) else None
    if not isinstance(notes, str) or not notes.strip():
        print("[release_notes] 润色输出不合法，退化为分组列表", file=sys.stderr)
        return None
    return notes.strip() + "\n"


VALIDATION_TITLE = "## 本版验证"
NO_VALIDATION = "未附自测记录"
#  nightly 记录超过这个天数就在节里标出来（只提示，不拦发版）
STALE_DAYS = 7


def self_test_dir() -> Path:
    """nightly 自测记录目录：用户配置目录下的 self-test/（与 xiaoyu.config.user_config_dir
    同一套约定，这里不 import xiaoyu——脚本在没装包的环境里也要能跑）。"""
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "xiaoyu" / "self-test"


def latest_record_path() -> Path | None:
    path = self_test_dir() / "latest.json"
    return path if path.is_file() else None


def load_validation(path: Path | None) -> dict | None:
    """读自测记录；没给、读不了、形态不对都返回 None（调用方写「未附自测记录」）。

    认两种形态：xiaoyu 收尾对象原样（`output` 里是清单）、或清单本身在顶层。
    scripts/self_test.py 另外补 `recorded_at`（ISO 时间）与 `source`（版本 / 分支 / 提交）。"""
    if path is None:
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    output = data.get("output") if isinstance(data.get("output"), dict) else data
    if not isinstance(output.get("total"), int) or not isinstance(output.get("passed"), int):
        return None
    items = output.get("items") if isinstance(output.get("items"), list) else []
    source = data.get("source") if isinstance(data.get("source"), dict) else {}
    return {
        "model": data.get("model") or output.get("model") or "",
        "platform": data.get("platform") or output.get("platform") or "",
        "recorded_at": data.get("recorded_at") if isinstance(data.get("recorded_at"), str) else "",
        "source": {k: str(v) for k, v in source.items() if isinstance(v, (str, int))},
        "total": output["total"],
        "passed": output["passed"],
        "skipped": output.get("skipped") if isinstance(output.get("skipped"), int) else 0,
        "items": [item for item in items if isinstance(item, dict)],
    }


def record_age_days(record: dict | None, today: dt.date | None = None) -> int | None:
    """记录距今几天；没有时间就 None。"""
    if not record or not record.get("recorded_at"):
        return None
    try:
        stamp = dt.datetime.fromisoformat(record["recorded_at"])
    except ValueError:
        return None
    return ((today or dt.date.today()) - stamp.date()).days


def render_validation(record: dict | None, today: dt.date | None = None) -> str:
    """「本版验证」一节：记录时间与源码 / 模型 / 平台 / N/M 项通过 / 未通过与未跑的项（带证据）。"""
    lines = [VALIDATION_TITLE, ""]
    if record is None:
        lines.append(f"- {NO_VALIDATION}")
        return "\n".join(lines) + "\n"
    if record.get("recorded_at"):
        source = record.get("source") or {}
        where = ""
        if source.get("version"):
            where = f"，源码 {source['version']}"
            if source.get("branch") or source.get("commit"):
                where += f"（{source.get('branch', '?')}@{str(source.get('commit', ''))[:7]}）"
        age = record_age_days(record, today)
        stale = f"，已 {age} 天" if age is not None and age > STALE_DAYS else ""
        lines.append(f"- 记录：{record['recorded_at'][:16].replace('T', ' ')}{where}{stale}")
    lines.append(f"- 模型：{record['model'] or '未记录'}")
    lines.append(f"- 平台：{record['platform'] or '未记录'}")
    counted = record["total"] - record["skipped"]
    lines.append(f"- 第一人称自测（tests_ai/self_test.md）：{record['passed']}/{counted} 项通过"
                 + (f"，{record['skipped']} 项未跑" if record["skipped"] else ""))
    for status, label in (("fail", "未通过"), ("skip", "未跑")):
        picked = [item for item in record["items"] if item.get("status") == status]
        if picked:
            lines.append(f"- {label}：" + "；".join(
                f"{item.get('id', '?')}（{item.get('evidence', '')}）" if item.get("evidence") else str(item.get("id", "?"))
                for item in picked
            ))
    return "\n".join(lines) + "\n"


def render_document(version: str, since: str, body: str, today: dt.date | None = None,
                    validation: dict | None = None) -> str:
    today = today or dt.date.today()
    head = f"# {version}（{today.isoformat()}）\n\n自 {since} 以来的变化。\n\n{body}"
    return head.rstrip("\n") + "\n\n" + render_validation(validation, today)


def current_version(repo: Path = REPO) -> str:
    text = (repo / "xiaoyu" / "__init__.py").read_text(encoding="utf-8")
    match = re.search(r'__version__\s*=\s*"([^"]+)"', text)
    if not match:
        raise SystemExit("读不到 xiaoyu/__init__.py 的 __version__")
    return match.group(1)


def latest_tag(repo: Path = REPO) -> str:
    proc = subprocess.run(["git", "-C", str(repo), "describe", "--tags", "--abbrev=0"],
                          capture_output=True, text=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0 or not proc.stdout.strip():
        raise SystemExit("找不到任何 tag，请用 --since 指定起点")
    return proc.stdout.strip()


def resolve_validation(spec: str | None) -> tuple[Path | None, dict | None]:
    """`--validation` 的三种取值：None/"auto" = 最近一次 nightly；"none" = 不附；其它 = 文件路径。"""
    if spec in (None, "", "auto"):
        path = latest_record_path()
    elif spec == "none":
        path = None
    else:
        path = Path(spec)
    return path, load_validation(path)


def generate(version: str, since: str, *, repo: Path = REPO, out: Path | None = None,
             do_polish: bool = False, model: str | None = None, timeout: float = 180.0,
             validation: str | None = "auto", today: dt.date | None = None) -> Path:
    """生成 docs/releases/<version>.md 并返回路径；since..HEAD 没有提交时抛 SystemExit。"""
    commits = collect_commits(since, repo=repo)
    if not commits:
        raise SystemExit(f"{since}..HEAD 没有任何非 merge 提交，不生成")
    polished = polish(render_plain(group_commits(commits)), model, timeout) if do_polish else None
    body = render_body(commits, polished)
    out = out or repo / "docs" / "releases" / f"{version}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    path, record = resolve_validation(validation)
    if path is not None and record is None:
        print(f"[release_notes] 自测记录 {path} 读不了或形态不对，节里写「{NO_VALIDATION}」", file=sys.stderr)
    out.write_text(render_document(version, since, body, today=today, validation=record), encoding="utf-8")
    print(f"{'润色' if polished else '分组列表'} → {out}（{len(commits)} 个提交，自 {since}"
          f"{'，附自测记录 ' + str(path) if record else '，无自测记录'}）")
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--version", help="版本号（默认读 xiaoyu.__version__）")
    parser.add_argument("--since", help="起点 tag / 提交（默认最近的 tag）")
    parser.add_argument("--polish", action="store_true", help="让小羽润色成面向用户的文案（默认不润色）")
    parser.add_argument("--no-model", action="store_true", help=argparse.SUPPRESS)  # 旧名，现在就是默认
    parser.add_argument("--model", help="润色用的模型（默认出厂默认模型）")
    parser.add_argument("--timeout", type=float, default=180.0, help="润色时限（秒）")
    parser.add_argument("--out", help="输出路径（默认 docs/releases/<版本>.md）")
    parser.add_argument("--validation", default="auto",
                        help="自测记录文件；auto（默认）= 最近一次 nightly 的 latest.json，none = 不附")
    args = parser.parse_args(argv)

    version = args.version or current_version()
    since = args.since or latest_tag()
    generate(version, since, out=Path(args.out) if args.out else None,
             do_polish=args.polish and not args.no_model, model=args.model, timeout=args.timeout,
             validation=args.validation)
    return 0


if __name__ == "__main__":
    sys.exit(main())
