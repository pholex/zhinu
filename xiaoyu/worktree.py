"""子 agent 的 git worktree 隔离（刻意的极简版）。

只做三件事：从主工作区的 HEAD 建 detached worktree、跑完检测有没有改动、
没改动就删掉。有改动的 worktree 保留、路径回报给父 agent——不做自动合并
与快照转移。

fail-open 纪律：隔离失败不该挡住委托本身——调用方捕获 WorktreeError 后
告警、退回主工作区继续跑。

已知裁剪：worktree 是 HEAD 的干净 checkout，父工作区未提交的改动不会带
过去（连未提交改动一起带需要 CoW 复制一类的基建，这里不值）。委托任务
依赖未提交改动时，模型该不用隔离或先让用户提交。
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import uuid
from pathlib import Path

from .config import user_config_dir

_GIT_TIMEOUT = 60.0


class WorktreeError(RuntimeError):
    """创建失败的统一出口：调用方据此降级回主工作区。"""


def git_root(path: Path) -> Path | None:
    """向上找 .git（与 folder_trust 同法：不起子进程，纯文件系统判断）。"""
    for candidate in (path, *path.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


def _run_git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    from .tools import _subprocess_hardening

    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=_GIT_TIMEOUT,
        **_subprocess_hardening(),
    )


def create(workspace: Path, prefix: str) -> Path:
    """在配置目录下建 detached worktree，返回其路径。

    路径：<配置目录>/worktrees/<仓名>-<路径哈希>/<prefix>-<hex6>。放配置目录
    而不是工作区里：不污染仓库、不撞 .gitignore、sandbox 随子 workspace 走。
    """
    root = git_root(workspace)
    if root is None:
        raise WorktreeError(f"{workspace} 不在 git 仓库里")
    if shutil.which("git") is None:
        raise WorktreeError("找不到 git 命令")
    #  仓名 + 路径哈希：同名不同处的仓不互相踩
    slug = f"{root.name}-{hashlib.sha256(str(root).encode('utf-8')).hexdigest()[:8]}"
    path = user_config_dir() / "worktrees" / slug / f"{prefix}-{uuid.uuid4().hex[:6]}"
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        result = _run_git(["worktree", "add", "--detach", str(path)], cwd=root)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise WorktreeError(f"git worktree add 失败：{exc}") from exc
    if result.returncode != 0:
        tail = (result.stderr or result.stdout).strip().splitlines()
        raise WorktreeError(f"git worktree add 失败：{tail[-1] if tail else 'exit ' + str(result.returncode)}")
    return path


def create_branch(workspace: Path, branch: str, prefix: str, base: str) -> Path:
    """在配置目录下建**挂在命名分支上**的 worktree（宸枢的 mission 隔离用）。

    与 create() 的差别只在分支形态：mission 的改动要经 `git merge --no-ff`
    收回主干，必须长在真实分支上，detached HEAD 合并不回来。分支已存在
    （崩溃后重建 worktree）就直接复用，否则从 base 新建。
    """
    root = git_root(workspace)
    if root is None:
        raise WorktreeError(f"{workspace} 不在 git 仓库里")
    if shutil.which("git") is None:
        raise WorktreeError("找不到 git 命令")
    slug = f"{root.name}-{hashlib.sha256(str(root).encode('utf-8')).hexdigest()[:8]}"
    path = user_config_dir() / "worktrees" / slug / f"{prefix}-{uuid.uuid4().hex[:6]}"
    path.parent.mkdir(parents=True, exist_ok=True)
    exists = (
        _run_git(["rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"], cwd=root).returncode
        == 0
    )
    args = (
        ["worktree", "add", str(path), branch]
        if exists
        else ["worktree", "add", "-b", branch, str(path), base]
    )
    try:
        result = _run_git(args, cwd=root)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise WorktreeError(f"git worktree add 失败：{exc}") from exc
    if result.returncode != 0:
        tail = (result.stderr or result.stdout).strip().splitlines()
        raise WorktreeError(
            f"git worktree add 失败：{tail[-1] if tail else 'exit ' + str(result.returncode)}"
        )
    return path


def dirty(path: Path) -> bool:
    """worktree 里有没有改动（含未跟踪文件与**已提交但无分支引用**的提交）。
    查不出来按有改动算——宁可多留一个目录，不能把没看过的改动删了。

    第二道检查针对"子 agent 在 detached worktree 里 git commit 收尾"的
    形态：porcelain 是干净的，但 HEAD 不属于任何分支——此时删 worktree
    等于把成果丢进只有 fsck 能捞的深渊。分支型 worktree（宸枢 mission）
    的提交都在分支上，不受影响，照常判干净可删。
    """
    try:
        result = _run_git(["status", "--porcelain"], cwd=path)
    except (OSError, subprocess.TimeoutExpired):
        return True
    if result.returncode != 0:
        return True
    if result.stdout.strip():
        return True
    try:
        contains = _run_git(
            ["branch", "--contains", "HEAD", "--format=%(refname)"], cwd=path
        )
    except (OSError, subprocess.TimeoutExpired):
        return True
    if contains.returncode != 0:
        return True
    #  detached HEAD 自身会被列成一行 "(no branch)" 伪条目——只认真分支
    branches = [
        line for line in contains.stdout.splitlines() if line.strip().startswith("refs/")
    ]
    return not branches


def remove(workspace: Path, path: Path) -> None:
    """删掉干净的 worktree。尽力而为：删不掉就留着，无害。"""
    root = git_root(workspace)
    if root is None:
        return
    try:
        _run_git(["worktree", "remove", str(path)], cwd=root)
    except (OSError, subprocess.TimeoutExpired):
        pass
