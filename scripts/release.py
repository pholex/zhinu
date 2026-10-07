#!/usr/bin/env python3
"""发版预检与打 tag：把「发版」一节里的人工核对项做成一条命令，全部只读。

    python scripts/release.py --dry-run      # 只打印预检结果
    python scripts/release.py                # 预检全过后在本地打带注释的 tag vX.Y.Z（绝不 push）

预检项（对应 AGENTS.md「发版」与 docs/internal 的发版步骤）：
- 在 main 上、工作树干净；
- HEAD == origin/main（先 `git fetch`；没网时降级为警告，拿本地的 origin/main 比）；
- `xiaoyu/__init__.py` 的 `__version__` 大于所有本地 tag、也大于 PyPI 索引里已发布的
  每个版本（烧毁的版本号不可复用；PyPI 查不到时警告）；
- `docs/releases/<version>.md` 存在（release.yml 用它建 GitHub Release，缺了会退回
  自动生成的说明）；
- 公开文档里没有「尚未发布」字样（发版说明与历史验收记录除外，那里本来就是过去式）；
- main 最近一次 CI 结论为绿（要 `gh`；没有就跳过，并提醒自己去看）。

结果三档：FAIL 拦住打 tag；WARN 不拦但打出来（网络不通一类，自己判断）；SKIP 是工具缺失。
事实采集（collect_facts）与判定（evaluate）分开，判定可以喂假数据测。
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PYPI_INDEX = "https://pypi.org/simple/xiaoyu-agent/"
#  PyPI 在 CDN 后面，裸 urllib 的 UA 可能被挡；具名 UA 也方便对方日志区分
_USER_AGENT = "xiaoyu-release-check/1.0 (+https://github.com/pholex/zhinu)"
_NETWORK_TIMEOUT = 20

UNRELEASED_MARK = "尚未发布"
#  这些地方的「尚未发布」是历史陈述（某次验收时的状态），不是待清理的标注
UNRELEASED_EXEMPT = ("docs/releases/", "docs/sdk-validation.md")

OK, FAIL, WARN, SKIP = "OK", "FAIL", "WARN", "SKIP"


@dataclass
class Check:
    name: str
    status: str
    detail: str = ""


@dataclass
class Facts:
    """预检要用的全部事实；collect_facts 采，evaluate 判。字段为 None = 没采到。"""

    version: str
    branch: str | None
    dirty: list[str]
    head: str | None
    upstream_head: str | None
    fetch_error: str | None  # git fetch 失败的原因（None = 成功）
    tags: list[str]
    pypi_versions: list[str] | None  # None = 没查到
    pypi_error: str | None
    notes_exists: bool
    unreleased_hits: list[str]
    ci_run: dict | None  # gh run list 的最近一条；None = 没查
    ci_error: str | None = None
    extra: dict = field(default_factory=dict)


# ---- 版本号 -----------------------------------------------------------------


def version_key(text: str) -> tuple[int, ...]:
    """`0.66.0` → (0, 66, 0)。只认点分数字段；带后缀（rc1）的截到第一个非数字段，
    够比「大于已发布的每个版本」这一件事用，不引入 packaging。"""
    parts: list[int] = []
    for piece in text.strip().lstrip("v").split("."):
        if not piece.isdigit():
            break
        parts.append(int(piece))
    return tuple(parts) or (0,)


def current_version(repo: Path = REPO) -> str:
    text = (repo / "xiaoyu" / "__init__.py").read_text(encoding="utf-8")
    match = re.search(r'__version__\s*=\s*"([^"]+)"', text)
    if not match:
        raise SystemExit("读不到 xiaoyu/__init__.py 的 __version__")
    return match.group(1)


# ---- 事实采集 ---------------------------------------------------------------


def _git(repo: Path, *args: str, timeout: float = 30) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout)


def fetch_pypi_versions(url: str = PYPI_INDEX) -> list[str]:
    """PyPI simple 索引（JSON 形态）里已发布的版本。任何失败抛给调用方降级为警告。"""
    req = urllib.request.Request(
        url, headers={"User-Agent": _USER_AGENT, "Accept": "application/vnd.pypi.simple.v1+json"}
    )
    with urllib.request.urlopen(req, timeout=_NETWORK_TIMEOUT) as resp:
        body = resp.read().decode("utf-8", errors="replace")
    data = json.loads(body)
    versions = list(data.get("versions") or [])
    if not versions:
        #  老版索引没有 versions 字段：从文件名里抠
        for item in data.get("files") or []:
            match = re.match(r"xiaoyu[_-]agent-([0-9][^-]*)-", item.get("filename", ""))
            if match:
                versions.append(match.group(1))
    return sorted(set(versions), key=version_key)


def scan_unreleased(repo: Path = REPO) -> list[str]:
    """公开文档（README + docs/**/*.md）里带「尚未发布」的行，豁免历史记录。"""
    hits: list[str] = []
    candidates = [repo / "README.md", *sorted((repo / "docs").rglob("*.md"))]
    for path in candidates:
        if not path.is_file():
            continue
        rel = path.relative_to(repo).as_posix()
        if rel.startswith("docs/internal/") or rel.startswith(UNRELEASED_EXEMPT) or rel in UNRELEASED_EXEMPT:
            continue
        for lineno, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            if UNRELEASED_MARK in line:
                hits.append(f"{rel}:{lineno}: {line.strip()[:100]}")
    return hits


def latest_ci_run(repo: Path = REPO) -> dict | None:
    """main 分支 ci.yml 最近一次运行（gh 不在就返回 None）。"""
    proc = subprocess.run(
        ["gh", "run", "list", "--workflow", "ci.yml", "--branch", "main", "--limit", "1",
         "--json", "conclusion,status,headSha,url,createdAt"],
        cwd=str(repo), capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip()[-300:] or f"gh 退出码 {proc.returncode}")
    runs = json.loads(proc.stdout or "[]")
    return runs[0] if runs else None


def collect_facts(repo: Path = REPO, *, fetch: bool = True, network: bool = True) -> Facts:
    version = current_version(repo)
    branch_proc = _git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    branch = branch_proc.stdout.strip() if branch_proc.returncode == 0 else None
    status_proc = _git(repo, "status", "--porcelain")
    dirty = [line for line in status_proc.stdout.splitlines() if line.strip()]
    head_proc = _git(repo, "rev-parse", "HEAD")
    head = head_proc.stdout.strip() if head_proc.returncode == 0 else None

    fetch_error = None
    if fetch:
        try:
            proc = _git(repo, "fetch", "--quiet", "origin", "main", timeout=60)
            if proc.returncode != 0:
                fetch_error = proc.stderr.strip()[-200:] or f"退出码 {proc.returncode}"
        except (subprocess.TimeoutExpired, OSError) as exc:
            fetch_error = exc.__class__.__name__
    else:
        fetch_error = "按参数跳过 fetch"
    upstream_proc = _git(repo, "rev-parse", "origin/main")
    upstream_head = upstream_proc.stdout.strip() if upstream_proc.returncode == 0 else None

    tags_proc = _git(repo, "tag", "-l", "v*")
    tags = [t.strip() for t in tags_proc.stdout.splitlines() if t.strip()]

    pypi_versions = None
    pypi_error = None
    if network:
        try:
            pypi_versions = fetch_pypi_versions()
        except (urllib.error.URLError, OSError, ValueError, TimeoutError) as exc:
            pypi_error = f"{exc.__class__.__name__}: {exc}"[:200]
    else:
        pypi_error = "按参数跳过"

    notes_exists = (repo / "docs" / "releases" / f"{version}.md").is_file()
    unreleased_hits = scan_unreleased(repo)

    ci_run = None
    ci_error = None
    if shutil.which("gh"):
        try:
            ci_run = latest_ci_run(repo)
        except (RuntimeError, OSError, subprocess.TimeoutExpired, ValueError) as exc:
            ci_error = str(exc)[:200]
    else:
        ci_error = "没有 gh"

    return Facts(
        version=version, branch=branch, dirty=dirty, head=head, upstream_head=upstream_head,
        fetch_error=fetch_error, tags=tags, pypi_versions=pypi_versions, pypi_error=pypi_error,
        notes_exists=notes_exists, unreleased_hits=unreleased_hits, ci_run=ci_run, ci_error=ci_error,
    )


# ---- 判定 -------------------------------------------------------------------


def evaluate(facts: Facts) -> list[Check]:
    checks: list[Check] = []
    version = facts.version
    tag = f"v{version}"

    checks.append(Check("branch", OK if facts.branch == "main" else FAIL,
                        f"当前分支 {facts.branch or '未知'}（发版只从 main 打 tag）"))
    checks.append(Check("clean", OK if not facts.dirty else FAIL,
                        "工作树干净" if not facts.dirty else f"未提交改动 {len(facts.dirty)} 处：" + "; ".join(facts.dirty[:5])))

    if facts.upstream_head is None:
        checks.append(Check("upstream", WARN, "本地没有 origin/main，无法核对是否已推"))
    elif facts.head != facts.upstream_head:
        status = WARN if facts.fetch_error else FAIL
        checks.append(Check("upstream", status,
                            f"HEAD {facts.head[:7] if facts.head else '?'} ≠ origin/main "
                            f"{facts.upstream_head[:7]}"
                            + (f"（fetch 失败：{facts.fetch_error}，比的是本地缓存的 origin/main）" if facts.fetch_error
                               else "——先推 main、等 CI 绿")))
    else:
        checks.append(Check("upstream", WARN if facts.fetch_error else OK,
                            "HEAD == origin/main" + (f"（fetch 失败：{facts.fetch_error}，比的是本地缓存）" if facts.fetch_error else "")))

    key = version_key(version)
    newer_tags = [t for t in facts.tags if version_key(t) >= key]
    checks.append(Check("version_tags", FAIL if newer_tags else OK,
                        f"{version} 大于全部 {len(facts.tags)} 个本地 tag" if not newer_tags
                        else f"本地 tag 不低于 {version}：{', '.join(sorted(newer_tags, key=version_key))}"))
    if facts.pypi_versions is None:
        checks.append(Check("version_pypi", WARN, f"PyPI 索引查不到（{facts.pypi_error}），无法确认 {version} 没发过"))
    else:
        published = [v for v in facts.pypi_versions if version_key(v) >= key]
        checks.append(Check("version_pypi", FAIL if published else OK,
                            f"{version} 大于 PyPI 上全部 {len(facts.pypi_versions)} 个版本（最新 "
                            f"{facts.pypi_versions[-1] if facts.pypi_versions else '无'}）" if not published
                            else f"PyPI 已有不低于 {version} 的版本：{', '.join(published)}——烧毁的版本号不可复用，改 __version__"))

    checks.append(Check("notes", OK if facts.notes_exists else FAIL,
                        f"docs/releases/{version}.md 存在" if facts.notes_exists
                        else f"缺 docs/releases/{version}.md：先 `python scripts/release_notes.py`，看一眼再提交"))
    checks.append(Check("unreleased", FAIL if facts.unreleased_hits else OK,
                        f"公开文档没有「{UNRELEASED_MARK}」" if not facts.unreleased_hits
                        else f"公开文档仍有「{UNRELEASED_MARK}」标注：\n" + "\n".join(f"      {h}" for h in facts.unreleased_hits)))

    if facts.ci_run is None:
        checks.append(Check("ci", SKIP, f"没查 CI（{facts.ci_error or 'main 上没有 ci.yml 的运行记录'}）——自己去 Actions 页看一眼"))
    else:
        run = facts.ci_run
        conclusion = run.get("conclusion") or run.get("status") or "?"
        same_head = facts.head is not None and run.get("headSha") == facts.head
        if conclusion != "success":
            checks.append(Check("ci", FAIL, f"main 最近一次 CI 结论 {conclusion}：{run.get('url', '')}"))
        elif not same_head:
            checks.append(Check("ci", WARN,
                                f"最近一次绿的 CI 跑的是 {str(run.get('headSha', ''))[:7]}，不是本 HEAD"
                                "（只改文档的 push 不跑矩阵属正常；否则先推 main 等 CI）"))
        else:
            checks.append(Check("ci", OK, f"main 最近一次 CI 绿且就是本 HEAD：{run.get('url', '')}"))

    checks.append(Check("tag_free", FAIL if tag in facts.tags else OK,
                        f"tag {tag} 尚未存在" if tag not in facts.tags else f"tag {tag} 已存在"))
    return checks


def render(checks: list[Check]) -> str:
    width = max(len(c.name) for c in checks)
    lines = [f"  [{c.status:<4}] {c.name:<{width}}  {c.detail}" for c in checks]
    return "\n".join(lines)


def blocked(checks: list[Check]) -> list[Check]:
    return [c for c in checks if c.status == FAIL]


# ---- 打 tag -----------------------------------------------------------------


def create_tag(version: str, repo: Path = REPO, message: str | None = None) -> str:
    """本地打带注释的 tag；不 push——推 tag 触发发布，由人确认后手动做。"""
    tag = f"v{version}"
    proc = _git(repo, "tag", "-a", tag, "-m", message or f"xiaoyu {version}")
    if proc.returncode != 0:
        raise SystemExit(f"打 tag 失败：{proc.stderr.strip()}")
    return tag


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="只预检，不打 tag")
    parser.add_argument("--no-fetch", action="store_true", help="不 git fetch（离线）")
    parser.add_argument("--no-network", action="store_true", help="不查 PyPI（离线）")
    parser.add_argument("--message", help="tag 注释（默认「xiaoyu <版本>」）")
    args = parser.parse_args(argv)

    facts = collect_facts(fetch=not args.no_fetch, network=not args.no_network)
    checks = evaluate(facts)
    print(f"发版预检：{facts.version}（HEAD {facts.head[:7] if facts.head else '?'}）")
    print(render(checks))
    failed = blocked(checks)
    warned = [c for c in checks if c.status == WARN]
    if failed:
        print(f"\n预检未过：{', '.join(c.name for c in failed)}")
        return 1
    if warned:
        print(f"\n有 {len(warned)} 项警告（{', '.join(c.name for c in warned)}），自己判断要不要继续")
    if args.dry_run:
        print("\n--dry-run：预检通过，没有打 tag")
        return 0
    tag = create_tag(facts.version, message=args.message)
    print(f"\n已在本地打 tag {tag}（带注释）。推它才会发布：git push origin {tag}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
