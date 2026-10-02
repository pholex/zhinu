#!/usr/bin/env python3
"""发版说明生成：读上一个 tag 到 HEAD 的提交，按类型分组，再让小羽润色成面向用户的中文。

    python scripts/release_notes.py                 # 版本取 xiaoyu.__version__，起点取最近 tag
    python scripts/release_notes.py --since v0.59.0 --version 0.61.0
    python scripts/release_notes.py --no-model      # 只分组，不调模型

输出到 docs/releases/<版本>.md（已存在则覆盖）。release.yml 在 PyPI 发布成功后
拿这个文件建 GitHub Release；文件不存在就退回 --generate-notes。

模型不可用（没 key、超时、输出不合法、xiaoyu 起不来）时**退化为纯分组列表**，
不报错——发版流程不该被润色这一步卡住。分组列表本身就是合格的发版说明。

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
_SUBJECT = re.compile(r"^(?P<type>[a-z]+)(?:\((?P<scope>[^)]*)\))?!?:\s*(?P<subject>.+)$")
#  git log 分隔：字段 \x1f，记录 \x1e（subject / body 里不会出现这两个控制字符）
_LOG_FORMAT = "%H%x1f%s%x1f%b%x1e"

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


def render_plain(grouped: dict[str, list[dict[str, str]]]) -> str:
    """纯分组列表（模型退化路径，也是润色的输入）。body 只取第一段，缩进成子项。"""
    out: list[str] = []
    for key, title in GROUPS:
        items = grouped.get(key) or []
        if not items:
            continue
        out.append(f"## {title}")
        out.append("")
        for item in items:
            out.append(f"- {item['text']}（{item['sha'][:7]}）")
            first_para = item["body"].split("\n\n", 1)[0].strip()
            if first_para:
                for line in first_para.splitlines():
                    out.append(f"  {line.strip()}")
        out.append("")
    return "\n".join(out).rstrip() + "\n"


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


def render_document(version: str, since: str, body: str, today: dt.date | None = None) -> str:
    today = today or dt.date.today()
    return f"# {version}（{today.isoformat()}）\n\n自 {since} 以来的变化。\n\n{body}"


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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--version", help="版本号（默认读 xiaoyu.__version__）")
    parser.add_argument("--since", help="起点 tag / 提交（默认最近的 tag）")
    parser.add_argument("--model", help="润色用的模型（默认出厂默认模型）")
    parser.add_argument("--no-model", action="store_true", help="不润色，只输出分组列表")
    parser.add_argument("--timeout", type=float, default=180.0, help="润色时限（秒）")
    parser.add_argument("--out", help="输出路径（默认 docs/releases/<版本>.md）")
    args = parser.parse_args(argv)

    version = args.version or current_version()
    since = args.since or latest_tag()
    commits = collect_commits(since)
    if not commits:
        print(f"{since}..HEAD 没有任何非 merge 提交，不生成", file=sys.stderr)
        return 1
    plain = render_plain(group_commits(commits))
    body = None if args.no_model else polish(plain, args.model, args.timeout)
    out = Path(args.out) if args.out else REPO / "docs" / "releases" / f"{version}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_document(version, since, body or plain), encoding="utf-8")
    print(f"{'润色' if body else '分组列表'} → {out}（{len(commits)} 个提交，自 {since}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
