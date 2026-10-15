#!/usr/bin/env python3
"""发版一条命令：预检 → 改版本号 → 生成发版说明 → 同步 ACP 收录副本 → 提交 → 打 tag。绝不 push。

    python scripts/release.py 0.72.0              # 发 0.72.0
    python scripts/release.py --bump patch        # 从当前 __version__ 算下一个 patch（minor 同理）
    python scripts/release.py 0.72.0 --dry-run    # 只预检并打印要做的事，不改任何文件
    python scripts/release.py --check             # 只跑预检，不需要版本号

跑完后把 main 和 tag **一次**推走，之后全部无人值守：

    git push --atomic origin main vX.Y.Z

release.yml 收到 tag 后先等同一提交的 ci.yml / sdk.yml 结果（scripts/ci_gate.py），不重跑；
绿了就构建、发 PyPI、建 GitHub Release。红了这个版本号就烧掉，改个号再来——
不等 CI 绿再打 tag，是刻意的：一次推走比两段串行各等一次省一半时间。

预检项（FAIL 拦住；WARN 只打出来；SKIP 是工具缺失）：
- 在 main 上、工作树干净、HEAD == origin/main（先 fetch；没网降级为警告）；
- 目标版本号大于当前 `__version__`、大于所有本地 tag、大于 PyPI 索引里已发布的每个版本
  （烧毁的版本号不可复用；PyPI 查不到时警告）；tag 不存在；
- 自上个 tag 以来有非 merge 提交（空版本不发）；
- 公开文档里没有「尚未发布」字样（发版说明与历史验收记录除外）；
- HEAD（即将成为发版提交的父提交）最近一次 CI 结论为绿（要 `gh`；没有就跳过）；
- nightly 自测记录存在且不旧（只警告：它不是门，见 scripts/self_test.py）。

事实采集（collect_facts）与判定（evaluate）分开，判定可以喂假数据测。
"""

from __future__ import annotations

import argparse
import datetime as dt
import importlib.util
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

VERSION_FILE = Path("xiaoyu") / "__init__.py"
NOTES_DIR = Path("docs") / "releases"
ACP_PIN_FILE = Path("docs") / "acp-registry" / "agent.json"

OK, FAIL, WARN, SKIP = "OK", "FAIL", "WARN", "SKIP"


def _load_release_notes():
    spec = importlib.util.spec_from_file_location("release_notes", REPO / "scripts" / "release_notes.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@dataclass
class Check:
    name: str
    status: str
    detail: str = ""


@dataclass
class Facts:
    """预检要用的全部事实；collect_facts 采，evaluate 判。字段为 None = 没采到。"""

    current: str  # 当前 __version__
    target: str | None  # 要发的版本号（--check 时为 None）
    branch: str | None
    dirty: list[str]
    head: str | None
    upstream_head: str | None
    fetch_error: str | None  # git fetch 失败的原因（None = 成功）
    tags: list[str]
    last_tag: str | None
    change_count: int | None  # last_tag..HEAD 的非 merge 提交数；None = 没有 tag
    pypi_versions: list[str] | None  # None = 没查到
    pypi_error: str | None
    unreleased_hits: list[str]
    ci_run: dict | None  # gh run list 的最近一条；None = 没查
    ci_error: str | None = None
    self_test: dict | None = None  # nightly 记录（release_notes.load_validation 的形态）
    self_test_age: int | None = None  # 记录距今天数
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
    text = (repo / VERSION_FILE).read_text(encoding="utf-8")
    match = re.search(r'__version__\s*=\s*"([^"]+)"', text)
    if not match:
        raise SystemExit(f"读不到 {VERSION_FILE} 的 __version__")
    return match.group(1)


def bumped(current: str, part: str) -> str:
    """`--bump patch|minor|major` 的目标版本号。"""
    key = list(version_key(current))
    while len(key) < 3:
        key.append(0)
    major, minor, patch = key[:3]
    if part == "major":
        return f"{major + 1}.0.0"
    if part == "minor":
        return f"{major}.{minor + 1}.0"
    if part == "patch":
        return f"{major}.{minor}.{patch + 1}"
    raise SystemExit(f"--bump 只认 major / minor / patch，不认 {part}")


_VERSION_SHAPE = re.compile(r"^\d+\.\d+\.\d+$")


def write_version(version: str, repo: Path = REPO) -> None:
    path = repo / VERSION_FILE
    text = path.read_text(encoding="utf-8")
    new, count = re.subn(r'(__version__\s*=\s*")[^"]+(")', rf"\g<1>{version}\g<2>", text, count=1)
    if count != 1:
        raise SystemExit(f"改不了 {VERSION_FILE} 的 __version__")
    path.write_text(new, encoding="utf-8")


def bump_acp_pin(version: str, path: Path) -> bool:
    """ACP 收录文件（agent.json）的 version 与 `package` 里的 `==` 钉版本同步到 version。
    文件不存在返回 False；改了返回 True。registry 收录后会每小时扫 PyPI 自动 bump，
    在那之前副本跟着发版走，省一个单独提交。"""
    if not path.is_file():
        return False
    data = json.loads(path.read_text(encoding="utf-8"))
    changed = False
    if data.get("version") != version:
        data["version"] = version
        changed = True
    for dist in (data.get("distribution") or {}).values():
        if not isinstance(dist, dict):
            continue
        package = dist.get("package")
        if isinstance(package, str) and "==" in package:
            name = package.split("==", 1)[0]
            if package != f"{name}=={version}":
                dist["package"] = f"{name}=={version}"
                changed = True
    if changed:
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return changed


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


def collect_facts(target: str | None, repo: Path = REPO, *, fetch: bool = True, network: bool = True) -> Facts:
    current = current_version(repo)
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
    last_tag_proc = _git(repo, "describe", "--tags", "--abbrev=0", "--match", "v*")
    last_tag = last_tag_proc.stdout.strip() if last_tag_proc.returncode == 0 and last_tag_proc.stdout.strip() else None
    change_count = None
    if last_tag:
        count_proc = _git(repo, "rev-list", "--count", "--no-merges", f"{last_tag}..HEAD")
        change_count = int(count_proc.stdout.strip()) if count_proc.returncode == 0 and count_proc.stdout.strip().isdigit() else None

    pypi_versions = None
    pypi_error = None
    if network:
        try:
            pypi_versions = fetch_pypi_versions()
        except (urllib.error.URLError, OSError, ValueError, TimeoutError) as exc:
            pypi_error = f"{exc.__class__.__name__}: {exc}"[:200]
    else:
        pypi_error = "按参数跳过"

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

    notes = _load_release_notes()
    self_test = notes.load_validation(notes.latest_record_path())
    self_test_age = notes.record_age_days(self_test)

    return Facts(
        current=current, target=target, branch=branch, dirty=dirty, head=head, upstream_head=upstream_head,
        fetch_error=fetch_error, tags=tags, last_tag=last_tag, change_count=change_count,
        pypi_versions=pypi_versions, pypi_error=pypi_error,
        unreleased_hits=unreleased_hits, ci_run=ci_run, ci_error=ci_error,
        self_test=self_test, self_test_age=self_test_age,
    )


# ---- 判定 -------------------------------------------------------------------


def evaluate(facts: Facts) -> list[Check]:
    checks: list[Check] = []

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
                               else "——发版提交要长在已推的 main 上，先把 main 推上去或拉下来")))
    else:
        checks.append(Check("upstream", WARN if facts.fetch_error else OK,
                            "HEAD == origin/main" + (f"（fetch 失败：{facts.fetch_error}，比的是本地缓存）" if facts.fetch_error else "")))

    if facts.change_count is None:
        checks.append(Check("changes", WARN, "本地没有 v* tag，算不出自上版以来的提交数"))
    else:
        checks.append(Check("changes", OK if facts.change_count > 0 else FAIL,
                            f"自 {facts.last_tag} 以来 {facts.change_count} 个非 merge 提交"
                            if facts.change_count > 0 else f"自 {facts.last_tag} 以来没有任何提交，没东西可发"))

    if facts.target is not None:
        version = facts.target
        tag = f"v{version}"
        key = version_key(version)
        checks.append(Check("version_shape", OK if _VERSION_SHAPE.match(version) else FAIL,
                            f"目标版本 {version}" if _VERSION_SHAPE.match(version) else f"目标版本 {version} 不是 X.Y.Z 形状"))
        checks.append(Check("version_current", OK if key > version_key(facts.current) else FAIL,
                            f"{version} > 当前 __version__ {facts.current}" if key > version_key(facts.current)
                            else f"{version} 不大于当前 __version__ {facts.current}"))
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
                                else f"PyPI 已有不低于 {version} 的版本：{', '.join(published)}——烧毁的版本号不可复用，换一个"))
        checks.append(Check("tag_free", FAIL if tag in facts.tags else OK,
                            f"tag {tag} 尚未存在" if tag not in facts.tags else f"tag {tag} 已存在"))

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
                                "（只改文档的 push 不跑矩阵属正常；否则看一眼 Actions）"))
        else:
            checks.append(Check("ci", OK, f"main 最近一次 CI 绿且就是本 HEAD：{run.get('url', '')}"))

    if facts.self_test is None:
        checks.append(Check("self_test", WARN, "没有 nightly 自测记录（scripts/self_test.py 没跑过？发版说明会写「未附自测记录」）"))
    else:
        record = facts.self_test
        counted = max(record["total"] - record["skipped"], 1)
        rate = record["passed"] / counted
        age = f"{facts.self_test_age} 天前" if facts.self_test_age is not None else "时间未知"
        stale = facts.self_test_age is not None and facts.self_test_age > 7
        status = WARN if (stale or rate < 0.8) else OK
        checks.append(Check("self_test", status,
                            f"nightly 自测 {record['passed']}/{counted} 通过（{age}"
                            + ("，已超过 7 天" if stale else "") + ("，通过率低于 80%" if rate < 0.8 else "") + "）"))
    return checks


def render(checks: list[Check]) -> str:
    width = max(len(c.name) for c in checks)
    lines = [f"  [{c.status:<4}] {c.name:<{width}}  {c.detail}" for c in checks]
    return "\n".join(lines)


def blocked(checks: list[Check]) -> list[Check]:
    return [c for c in checks if c.status == FAIL]


# ---- 动手 -------------------------------------------------------------------


def create_tag(version: str, repo: Path = REPO, message: str | None = None) -> str:
    """本地打带注释的 tag；不 push——推 tag 触发发布，由人确认后手动做。"""
    tag = f"v{version}"
    proc = _git(repo, "tag", "-a", tag, "-m", message or f"xiaoyu {version}")
    if proc.returncode != 0:
        raise SystemExit(f"打 tag 失败：{proc.stderr.strip()}")
    return tag


def prepare_release(version: str, since: str, repo: Path = REPO, *, polish: bool = False,
                    validation: str | None = "auto", acp_fork: Path | None = None,
                    today: dt.date | None = None) -> dict:
    """改版本号、生成发版说明、同步 ACP 副本、提交、打 tag。返回做了什么。"""
    notes = _load_release_notes()
    write_version(version, repo)
    notes_path = notes.generate(version, since, repo=repo, do_polish=polish, validation=validation, today=today)
    staged = [VERSION_FILE.as_posix(), notes_path.relative_to(repo).as_posix()]
    if bump_acp_pin(version, repo / ACP_PIN_FILE):
        staged.append(ACP_PIN_FILE.as_posix())
    fork_changed = bump_acp_pin(version, acp_fork) if acp_fork else False
    add = _git(repo, "add", "--", *staged)
    if add.returncode != 0:
        raise SystemExit(f"git add 失败：{add.stderr.strip()}")
    commit = _git(repo, "commit", "-q", "-m", f"chore(release): {version}")
    if commit.returncode != 0:
        raise SystemExit(f"git commit 失败：{commit.stderr.strip()}")
    tag = create_tag(version, repo)
    return {"version": version, "tag": tag, "staged": staged, "notes": notes_path, "acp_fork_changed": fork_changed}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("version", nargs="?", help="要发的版本号 X.Y.Z")
    parser.add_argument("--bump", choices=("major", "minor", "patch"), help="从当前 __version__ 算下一个版本号")
    parser.add_argument("--check", action="store_true", help="只跑预检，不需要版本号")
    parser.add_argument("--dry-run", action="store_true", help="预检 + 打印要做的事，不改任何文件")
    parser.add_argument("--no-fetch", action="store_true", help="不 git fetch（离线）")
    parser.add_argument("--no-network", action="store_true", help="不查 PyPI（离线）")
    parser.add_argument("--polish", action="store_true", help="发版说明让小羽润色（默认纯分组列表）")
    parser.add_argument("--validation", default="auto",
                        help="自测记录：auto（默认）= 最近一次 nightly，none = 不附，或文件路径")
    parser.add_argument("--acp-fork", help="ACP registry fork 里的 xiaoyu/agent.json 路径，一并改版本（不提交）")
    args = parser.parse_args(argv)

    if args.version and args.bump:
        parser.error("版本号和 --bump 只能给一个")
    current = current_version()
    target = args.version or (bumped(current, args.bump) if args.bump else None)
    if target is None and not args.check:
        parser.error("要么给版本号，要么 --bump，要么 --check")

    facts = collect_facts(target, fetch=not args.no_fetch, network=not args.no_network)
    checks = evaluate(facts)
    print(f"发版预检：{current} → {target or '（只预检）'}（HEAD {facts.head[:7] if facts.head else '?'}）")
    print(render(checks))
    failed = blocked(checks)
    warned = [c for c in checks if c.status == WARN]
    if failed:
        print(f"\n预检未过：{', '.join(c.name for c in failed)}")
        return 1
    if warned:
        print(f"\n有 {len(warned)} 项警告（{', '.join(c.name for c in warned)}），自己判断要不要继续")
    if args.check or target is None:
        return 0
    since = facts.last_tag or "HEAD~1"
    if args.dry_run:
        print(f"\n--dry-run：预检通过。真跑会：改 {VERSION_FILE} 到 {target} → 生成 {NOTES_DIR / (target + '.md')}"
              f"（自 {since}）→ 同步 {ACP_PIN_FILE} → 提交 chore(release): {target} → 打 tag v{target}")
        return 0
    done = prepare_release(target, since, polish=args.polish, validation=args.validation,
                           acp_fork=Path(args.acp_fork) if args.acp_fork else None)
    print(f"\n已提交 chore(release): {target}（{', '.join(done['staged'])}）并打 tag {done['tag']}。")
    if done["acp_fork_changed"]:
        print(f"ACP fork 副本已改到 {target}（没提交，自己 commit / amend 后 push）。")
    print(f"推走即发布（一次推 main 与 tag，之后无人值守）：\n\n    git push --atomic origin main {done['tag']}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
