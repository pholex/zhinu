"""宸枢（chenshu）：统筹织造模式（Sovereign-Weave）——总枢坐镇其上，规划、
分派、监督、汇总，统御众羽协同推进巨型工程。

宸=帝居/北极所在，枢=天枢/中枢。主 agent 化身唯一的"宸枢"（总枢），把一个
仓库的大工程拆成 scope 互不重叠的 mission，为每个 mission 起一个跑在
**独立 git 分支 worktree** 里的 worker，评审通过、依赖齐备后经唯一的
merge 闸收回主干。与七襄（qixiang，一次性批量扇出）的分工：七襄是
"无状态扇出+聚合"，宸枢是"有状态编排+隔离+评审+合并"。

协作协议**由代码而非提示词强制**：
- 所有协作产物（inbox 消息 / finding / review / mission 状态 / 活动日志）
  只能经 chenshu_* 工具落到 `.xiaoyu/chenshu/`（明文 markdown + JSON，
  永久保留作审计轨迹），worker 手写协议文件不生效——merge 闸只认工具写的。
- build worker 的 write_file / str_replace 出了自己的 worktree 直接被
  审批层拒绝（Deny 回灌改指令：scope 外的问题用 chenshu_finding 归档）。
- merge 五道闸：deps 先合 → 必须有 review 且最新一轮 clean → review 盖的
  commit 必须等于分支当前 tip（分支一动 clean 自动作废）→ diff 文件必须
  全部命中 mission scope → 主 checkout 必须停在 base 分支上。
  被挡下的合并也写日志：拒绝是一个带理由的决策。

同步架构的适配（与"编排者挂起等唤醒"的事件驱动形态的刻意分叉）：
- worker 在工作线程里跑，完成时进事件队列 + 搭通知轨道；总枢用
  **chenshu_wait**（有界阻塞，默认 300s）显式等下一个事件，而不是结束
  回合等外部唤醒——xiaoyu 的回合由用户驱动，没有自续回合的机制，
  显式等待是同步回路里的正确形态。
- worker 上限默认 4（XIAOYU_CHENSHU_MAX_WORKERS）：单机场景的保守值。

已知边界（诚实记录，不装作有）：
- worker 的 bash 不做命令级审查（macOS 上有 Seatbelt 沙箱兜写越界，
  其它平台靠 briefing 纪律）；reviewer / survey 的只读性是工具集级的
  （没有写工具），reviewer 的 bash 同样只有纪律约束。
- 429/限流治理、跨进程恢复线程（重启后 worker 线程不复活，重入 init 会
  "收养"：mission/worktree/审计全保留，花名册退役重 spawn）。
"""

from __future__ import annotations

import contextlib
import fnmatch
import json
import queue
import re
import subprocess
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from . import ui, worktree
from .config import Config
from .events import Notice, UISink
from .tools import Tool, Toolbox

CHENSHU = "chenshu"
BROADCAST = "all"
MISSION_KINDS = ("build", "survey")
#  worker 工具集（比声明式 subagent 的 20 轮宽裕：mission 是大活）
_WORKER_ITERATIONS = 60
BUILD_TOOLS = ("read_file", "grep", "list_files", "write_file", "str_replace", "bash")
SURVEY_TOOLS = ("read_file", "grep", "list_files")
REVIEW_TOOLS = ("read_file", "grep", "list_files", "bash")
_WAIT_DEFAULT = 300
_WAIT_MAX = 600
_INBOX_LIMIT = 20
_SLUG_RE = re.compile(r"[^a-z0-9]+")
_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{1,31}$")


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _slugify(text: str, limit: int = 40) -> str:
    slug = _SLUG_RE.sub("-", text.lower()).strip("-")[:limit].strip("-")
    return slug or "mission"


# ---------- git 帮手（全部经 worktree._run_git：同一套 hardening/超时） ----------


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return worktree._run_git(args, cwd)


def _git_out(args: list[str], cwd: Path) -> str | None:
    try:
        result = _git(args, cwd)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip()


# ---------- scope 语义 ----------


def _scope_stem(pattern: str) -> str:
    """scope 的"根干"：**第一个通配符之前**的目录/文件路径。

    src/**、src/*.py、src/**/*.py、src/ 都归一成 src。scope 的所有权语义
    统一按根干子树算——plan 的不相交检查与 merge 的命中检查用同一个根干，
    两道闸才不会各说各话（通配尾巴只是写法，不参与所有权判定）。
    空干=整仓，拒绝。
    """
    text = pattern.strip()
    cut = len(text)
    for index, ch in enumerate(text):
        if ch in "*?[":
            cut = index
            break
    had_glob = cut < len(text)
    text = text[:cut]
    #  通配符紧贴的最后一段是半截文件名（src/a* → "src/a"），所有权退到父目录；
    #  截口恰好在 "/" 上（src/**/*.py → "src/"）则最后一段是完整目录，不退
    if had_glob and text and not text.endswith("/"):
        text = text.rpartition("/")[0]
    return text.strip("/")


def scopes_conflict(a: str, b: str) -> bool:
    """保守判定：根干相等或互为路径前缀即冲突（宁可误报逼总枢拆细）。"""
    sa, sb = _scope_stem(a), _scope_stem(b)
    if not sa or not sb:
        return True
    return sa == sb or sa.startswith(sb + "/") or sb.startswith(sa + "/")


def scope_match(path: str, patterns: tuple[str, ...]) -> bool:
    """diff 文件是否命中 mission scope。

    所有权按根干子树（与 scopes_conflict 同一个 _scope_stem——plan 保留了
    什么，merge 就放行什么）；fnmatch 只作附加放行，不作收紧。
    """
    for pattern in patterns:
        pat = pattern.strip()
        stem = _scope_stem(pat)
        if stem and (path == stem or path.startswith(stem + "/")):
            return True
        if fnmatch.fnmatch(path, pat):
            return True
    return False


# ---------- 数据模型 ----------


@dataclass
class Mission:
    id: str
    title: str
    kind: str  # build | survey
    scope: tuple[str, ...] = ()
    branch: str = ""  # survey 无分支
    worktree: str = ""
    deps: tuple[str, ...] = ()
    status: str = "pending"  # pending | active | completed | merged
    owner: str = ""
    notes: list[str] = field(default_factory=list)


@dataclass
class Member:
    """花名册条目。线程/活 agent 不入档（进程内另存），重启即退役。"""

    name: str
    kind: str  # worker | reviewer
    mission_id: str = ""
    review_target: str = ""
    status: str = "running"  # running | done | failed | retired
    #  完成后的交接摘要（总枢在 wait/status 里看）
    handoff: str = ""


class ChenshuError(RuntimeError):
    """工具层直接把它的文案回给模型。"""


class ChenshuRuntime:
    """宸枢的进程内运行时 + 磁盘协议 store。

    磁盘上只有两类东西：state.json（机器真相：missions/花名册/base 分支）
    与 comms 产物（inbox/findings/reviews markdown、activity.log、
    MISSIONS.md 人类视图）。线程、活 agent、事件队列是进程内的，
    重启不复活——init 的"收养"路径负责把花名册对齐现实。
    """

    def __init__(
        self,
        config: Config,
        registry: Any,
        usage: Any,
        sink: UISink,
        permissions: Any,
        notify: Callable[[str, str], None] | None = None,
    ) -> None:
        self.config = config
        self.registry = registry
        self.usage = usage
        self.sink = sink
        self.permissions = permissions
        self.notify = notify or (lambda text, key="": None)
        self.root = config.workspace / ".xiaoyu" / "chenshu"
        self.lock = threading.RLock()
        self.active = False
        self.base_branch = ""
        self.missions: list[Mission] = []
        self.members: list[Member] = []
        self.events: queue.Queue[str] = queue.Queue()
        self._threads: dict[str, threading.Thread] = {}
        self._agents: dict[str, Any] = {}
        #  worker 完成后的 transcript 存档（resume 重唤醒用；纯内存）
        self._archives: dict[str, list[dict[str, Any]]] = {}
        self._seq = 0

    # ---------- 磁盘 ----------

    def _state_path(self) -> Path:
        return self.root / "state.json"

    def _save(self) -> None:
        payload = {
            "base_branch": self.base_branch,
            "missions": [asdict(m) for m in self.missions],
            "members": [asdict(m) for m in self.members],
        }
        self.root.mkdir(parents=True, exist_ok=True)
        self._state_path().write_text(
            json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        self._render_missions_md()

    def _load(self) -> bool:
        path = self._state_path()
        if not path.is_file():
            return False
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        self.base_branch = str(payload.get("base_branch", ""))
        self.missions = []
        for raw in payload.get("missions", []):
            with contextlib.suppress(TypeError):
                raw = dict(raw)
                raw["scope"] = tuple(raw.get("scope", ()))
                raw["deps"] = tuple(raw.get("deps", ()))
                self.missions.append(Mission(**raw))
        self.members = []
        for raw in payload.get("members", []):
            with contextlib.suppress(TypeError):
                self.members.append(Member(**raw))
        return True

    def log(self, actor: str, action: str, **kv: Any) -> None:
        parts = [_now_iso(), actor, action]
        parts += [f"{key}={value}" for key, value in kv.items()]
        line = " ".join(str(part) for part in parts) + "\n"
        with contextlib.suppress(OSError):
            self.root.mkdir(parents=True, exist_ok=True)
            with (self.root / "activity.log").open("a", encoding="utf-8") as fh:
                fh.write(line)

    def _render_missions_md(self) -> None:
        lines = ["# 宸枢 missions（工具生成，勿手改）", ""]
        for m in self.missions:
            mark = {"pending": "○", "active": "◐", "completed": "●", "merged": "✔"}.get(
                m.status, "?"
            )
            lines.append(
                f"- {mark} {m.id} [{m.kind}] {m.title}"
                + (f" · 分支 {m.branch}" if m.branch else "")
                + (f" · owner {m.owner}" if m.owner else "")
                + (f" · deps {','.join(m.deps)}" if m.deps else "")
            )
            if m.scope:
                lines.append(f"  - scope: {', '.join(m.scope)}")
        with contextlib.suppress(OSError):
            (self.root / "MISSIONS.md").write_text(
                "\n".join(lines) + "\n", encoding="utf-8"
            )

    # ---------- 查找 ----------

    def mission(self, mission_id: str) -> Mission | None:
        for m in self.missions:
            if m.id == mission_id:
                return m
        return None

    def _mission_by_ref(self, ref: str) -> Mission | None:
        """merge/review 的目标既认 mission id 也认分支名。"""
        for m in self.missions:
            if m.id == ref or (m.branch and m.branch == ref):
                return m
        return None

    def member(self, name: str) -> Member | None:
        for m in self.members:
            if m.name == name:
                return m
        return None

    def _live_count(self) -> int:
        return sum(1 for t in self._threads.values() if t.is_alive())

    def _require_active(self) -> None:
        if not self.active:
            raise ChenshuError("ERROR: 宸枢未启动——先调 chenshu_init。")

    # ---------- init / adopt ----------

    def init(self) -> str:
        root = worktree.git_root(self.config.workspace)
        if root is None:
            raise ChenshuError("ERROR: 工作区不在 git 仓库里——宸枢依赖分支与 worktree。")
        if _git_out(["rev-parse", "HEAD"], root) is None:
            raise ChenshuError("ERROR: 仓库还没有任何 commit，先建一个初始提交。")
        branch = _git_out(["branch", "--show-current"], root)
        if not branch:
            raise ChenshuError("ERROR: 主 checkout 处于 detached HEAD，切回分支再启动。")
        with self.lock:
            adopted = self._load()
            retired: list[str] = []
            if adopted:
                #  收养：mission/worktree/审计全保留；线程不跨进程，
                #  花名册里还挂着 running 的一律退役，由总枢重新 spawn
                for member in self.members:
                    if member.status == "running":
                        member.status = "retired"
                        retired.append(member.name)
            self.base_branch = self.base_branch or branch
            self.root.mkdir(parents=True, exist_ok=True)
            self._exclude_from_git(root)
            self.active = True
            self._save()
            self.log(CHENSHU, "adopt" if adopted else "init", base=self.base_branch)
        lines = [
            f"宸枢已启动。base 分支：{self.base_branch}，协议目录：{self.root}",
            "工作流：chenshu_plan 拆 mission（scope 两两不相交）→ chenshu_spawn "
            "逐个起 worker（把依赖已解锁的一口气发满）→ chenshu_wait 等事件 → "
            "评审通过后 chenshu_merge 收回主干 → 全部 ✔ 后 chenshu_teardown。",
            "纪律：你是唯一的编排者，不亲手写产品代码（合并期集成修复除外）；"
            "mission 状态归协议管，不要用 update_plan 记它。",
        ]
        if retired:
            lines.append(
                f"上个会话的成员已退役：{', '.join(retired)}——mission 与 worktree "
                "还在，用 chenshu_spawn 重新指派。"
            )
        if self.missions:
            lines.append(f"已有 {len(self.missions)} 个 mission（chenshu_status 查看）。")
        return "\n".join(lines)

    def _exclude_from_git(self, root: Path) -> None:
        """协议目录写进 .git/info/exclude（不动用户的 .gitignore）。"""
        entry = ".xiaoyu/chenshu/"
        path = root / ".git" / "info" / "exclude"
        with contextlib.suppress(OSError):
            existing = path.read_text(encoding="utf-8") if path.is_file() else ""
            if entry not in existing:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(
                    existing + ("" if existing.endswith("\n") or not existing else "\n")
                    + entry + "\n",
                    encoding="utf-8",
                )

    # ---------- plan ----------

    def plan(self, items: list[dict[str, Any]]) -> str:
        self._require_active()
        with self.lock:
            #  已 merged 的 mission 不再占 scope
            taken: list[tuple[str, str]] = [
                (m.id, pattern)
                for m in self.missions
                if m.kind == "build" and m.status != "merged"
                for pattern in m.scope
            ]
            known = {m.id for m in self.missions}
            new: list[Mission] = []
            for raw in items:
                if not isinstance(raw, dict):
                    raise ChenshuError("ERROR: missions 每项必须是对象。")
                title = str(raw.get("title", "")).strip()
                if not title:
                    raise ChenshuError("ERROR: mission 缺 title。")
                kind = str(raw.get("kind", "build")).strip() or "build"
                if kind not in MISSION_KINDS:
                    raise ChenshuError(
                        f"ERROR: kind 只能是 {'/'.join(MISSION_KINDS)}，不认识 {kind!r}。"
                    )
                scope = tuple(
                    str(s).strip() for s in raw.get("scope", []) if str(s).strip()
                )
                if kind == "build":
                    if not scope:
                        raise ChenshuError(f"ERROR: build mission「{title}」必须给 scope。")
                    for pattern in scope:
                        if not _scope_stem(pattern):
                            raise ChenshuError(
                                f"ERROR: 「{title}」的 scope {pattern!r} 等于整个仓库"
                                "——拆细到目录/文件。"
                            )
                        for owner_id, other in taken:
                            if scopes_conflict(pattern, other):
                                raise ChenshuError(
                                    f"ERROR: 「{title}」的 scope {pattern!r} 与 "
                                    f"{owner_id} 的 {other!r} 重叠——scope 必须两两"
                                    "不相交（共享文件归属唯一一个 mission）。"
                                )
                deps = tuple(str(d).strip() for d in raw.get("deps", []) if str(d).strip())
                mid = f"M{len(self.missions) + len(new) + 1}"
                for dep in deps:
                    if dep not in known and dep not in {m.id for m in new}:
                        raise ChenshuError(f"ERROR: 「{title}」的依赖 {dep} 不是已知 mission。")
                #  分支名带 mission id 保证唯一：纯中文标题 slug 化后全都退化成
                #  同一个兜底词，撞名会让第二个 mission 永远起不来
                mission = Mission(
                    id=mid,
                    title=title,
                    kind=kind,
                    scope=scope,
                    branch=f"feat/{mid.lower()}-{_slugify(title)}" if kind == "build" else "",
                    deps=deps,
                )
                if kind == "build":
                    taken += [(mid, pattern) for pattern in scope]
                new.append(mission)
            self.missions.extend(new)
            self._save()
            for mission in new:
                self.log(CHENSHU, "plan", mission=mission.id, kind=mission.kind, title=mission.title)
        lines = [f"已登记 {len(new)} 个 mission："]
        for mission in new:
            lines.append(
                f"  {mission.id} [{mission.kind}] {mission.title}"
                + (f" · 分支 {mission.branch}" if mission.branch else "")
                + (f" · deps {','.join(mission.deps)}" if mission.deps else "")
            )
        lines.append("下一步：把依赖已解锁的 mission 用 chenshu_spawn 一口气全部指派出去。")
        return "\n".join(lines)

    # ---------- comms（frontmatter-lite 的 markdown 文件） ----------

    def _next_seq(self) -> int:
        with self.lock:
            self._seq += 1
            return self._seq

    def send(self, sender: str, to: str, subject: str, body: str) -> str:
        self._require_active()
        names = {m.name for m in self.members} | {CHENSHU, BROADCAST}
        if to not in names:
            raise ChenshuError(
                f"ERROR: 收件人 {to!r} 不在花名册里。可选：{', '.join(sorted(names))}"
            )
        if to == sender:
            raise ChenshuError("ERROR: 不能发给自己。")
        directory = self.root / "inbox"
        directory.mkdir(parents=True, exist_ok=True)
        stamp = _now_iso()
        name = f"{int(time.time())}-{self._next_seq():03d}-{sender}-{to}.md"
        (directory / name).write_text(
            f"---\nfrom: {sender}\nto: {to}\nsubject: {subject}\nsent_at: {stamp}\n---\n\n{body}\n",
            encoding="utf-8",
        )
        self.log(sender, "inbox.send", to=to, subject=subject)
        return f"已发给 {to}：{subject}"

    def read_inbox(self, caller: str, limit: int = _INBOX_LIMIT) -> str:
        self._require_active()
        directory = self.root / "inbox"
        if not directory.is_dir():
            return "收件箱是空的。"
        entries: list[tuple[str, str, str, str, str]] = []
        for path in sorted(directory.glob("*.md"), reverse=True):
            meta, body = _parse_front(path)
            to = meta.get("to", "")
            #  读时过滤而非消费：没有已读标记，"处理过没有"靠调用方自己的上下文
            if caller != CHENSHU and to not in (caller, BROADCAST):
                continue
            entries.append(
                (meta.get("sent_at", ""), meta.get("from", "?"), to,
                 meta.get("subject", ""), body.strip())
            )
            if len(entries) >= limit:
                break
        if not entries:
            return "没有给你的消息。"
        blocks = [
            f"[{sent}] {sender} → {to} · {subject}\n{body}"
            for sent, sender, to, subject, body in entries
        ]
        return "\n\n".join(blocks)

    def file_finding(self, sender: str, kind: str, title: str, body: str) -> str:
        self._require_active()
        if kind not in ("bug", "improve", "vuln", "idea"):
            raise ChenshuError("ERROR: finding 类型只能是 bug / improve / vuln / idea。")
        directory = self.root / "findings"
        directory.mkdir(parents=True, exist_ok=True)
        member = self.member(sender)
        mission_note = f"mission: {member.mission_id}\n" if member and member.mission_id else ""
        name = f"{int(time.time())}-{self._next_seq():03d}-{kind}.md"
        (directory / name).write_text(
            f"---\nfrom: {sender}\ntype: {kind}\n{mission_note}filed_at: {_now_iso()}\n---\n\n"
            f"# {title}\n\n{body}\n\n## 为什么没有直接修\n"
            "发现者的 scope 不含此处；越界改动会破坏 scope 隔离，故归档待总枢分派。\n",
            encoding="utf-8",
        )
        self.log(sender, "finding.file", type=kind, title=ui.preview(title, 60))
        return "已归档。总枢会决定：分派给现有 mission / 新开 mission / 进 backlog。"

    # ---------- review ----------

    def submit_review(self, caller: str, target: str, verdict: str, summary: str) -> str:
        self._require_active()
        mission = self._mission_by_ref(target)
        if mission is None or not mission.branch:
            raise ChenshuError(f"ERROR: {target!r} 不是任何 build mission 的 id/分支。")
        member = self.member(caller)
        allowed = caller == CHENSHU or (
            member is not None
            and member.kind == "reviewer"
            and member.review_target in (mission.id, mission.branch)
        )
        if not allowed:
            raise ChenshuError("ERROR: 只有被指派到该目标的 reviewer（或总枢）能提交评审。")
        verdict = verdict.strip().lower()
        if verdict != "clean" and not re.match(r"^p[12]-\d+", verdict):
            raise ChenshuError(
                "ERROR: verdict 只能是 clean 或 p1-N / p2-N（N=问题数，如 p1-2）。"
            )
        root = worktree.git_root(self.config.workspace)
        assert root is not None
        tip = _git_out(["rev-parse", mission.branch], root)
        if tip is None:
            raise ChenshuError(f"ERROR: 分支 {mission.branch} 不存在或读不到 tip。")
        directory = self.root / "reviews"
        directory.mkdir(parents=True, exist_ok=True)
        #  轮次自动编号（模型无法伪造）；提交时盖当前 tip——分支一动 clean 作废。
        #  数数+落盘要在锁内：并发评审各自数出同一轮次会互相覆盖，
        #  clean 盖掉 p1 的话已知有问题的分支就能过闸
        with self.lock:
            existing = len(list(directory.glob(f"{mission.id}-r*.md")))
            round_no = existing + 1
            (directory / f"{mission.id}-r{round_no}.md").write_text(
                f"---\ntarget: {mission.id}\nbranch: {mission.branch}\nround: {round_no}\n"
                f"verdict: {verdict}\nreviewed_commit: {tip}\nreviewer: {caller}\n"
                f"reviewed_at: {_now_iso()}\n---\n\n{summary}\n",
                encoding="utf-8",
            )
        self.log(caller, "review.write", target=mission.id, round=round_no, verdict=verdict)
        return (
            f"第 {round_no} 轮评审已记录：{verdict}（盖 commit {tip[:10]}）。"
            + ("" if verdict == "clean" else "请用 chenshu_send 通知作者修复后重评。")
        )

    def latest_review(self, mission: Mission) -> dict[str, str] | None:
        directory = self.root / "reviews"
        if not directory.is_dir():
            return None
        best: tuple[int, dict[str, str]] | None = None
        for path in directory.glob(f"{mission.id}-r*.md"):
            meta, _ = _parse_front(path)
            with contextlib.suppress(ValueError):
                round_no = int(meta.get("round", "0"))
                if best is None or round_no > best[0]:
                    best = (round_no, meta)
        return best[1] if best else None

    # ---------- merge（唯一合并入口） ----------

    def merge(self, target: str) -> str:
        self._require_active()
        with self.lock:
            mission = self._mission_by_ref(target)
            if mission is None:
                raise ChenshuError(f"ERROR: {target!r} 不是已知 mission 的 id/分支。")
            if mission.status == "merged":
                return f"{mission.id} 已经合并过了。"
            unmet = [
                dep for dep in mission.deps
                if (d := self.mission(dep)) is None or d.status != "merged"
            ]
            if unmet:
                self.log(CHENSHU, "merge.blocked", mission=mission.id, reason="deps-unmerged")
                raise ChenshuError(
                    f"ERROR: 依赖未合并：{', '.join(unmet)}——按 deps 顺序先合它们。"
                )
            #  survey 交付的是知识不是代码：completed 即视为收账
            if mission.kind == "survey":
                mission.status = "merged"
                self._save()
                self.log(CHENSHU, "merge.noop", mission=mission.id)
                return f"{mission.id} 是 survey，无代码可合，已标记完结。"
            root = worktree.git_root(self.config.workspace)
            assert root is not None
            current = _git_out(["branch", "--show-current"], root)
            if current != self.base_branch:
                self.log(CHENSHU, "merge.blocked", mission=mission.id, reason="base-mismatch")
                raise ChenshuError(
                    f"ERROR: 主 checkout 在 {current or 'detached HEAD'}，不在 base"
                    f"（{self.base_branch}）上——切回去再合，否则合并落不到正确的分支。"
                )
            owner_thread = self._threads.get(mission.owner)
            if owner_thread is not None and owner_thread.is_alive():
                self.log(CHENSHU, "merge.blocked", mission=mission.id, reason="worker-running")
                raise ChenshuError(
                    f"ERROR: {mission.owner} 还在跑——chenshu_wait 等它收工再合。"
                )
            tip = _git_out(["rev-parse", mission.branch], root)
            if tip is None:
                raise ChenshuError(f"ERROR: 分支 {mission.branch} 不存在。")
            review = self.latest_review(mission)
            if review is None:
                self.log(CHENSHU, "merge.blocked", mission=mission.id, reason="no-review")
                raise ChenshuError(
                    f"ERROR: {mission.id} 还没有任何评审——spawn 一个 reviewer"
                    f"（review_target={mission.id}）先过闸。"
                )
            if review.get("verdict") != "clean":
                self.log(CHENSHU, "merge.blocked", mission=mission.id, reason="not-clean")
                raise ChenshuError(
                    f"ERROR: 最新一轮评审是 {review.get('verdict')}（第 "
                    f"{review.get('round')} 轮）——先修复并重评到 clean。"
                )
            if review.get("reviewed_commit") != tip:
                self.log(CHENSHU, "merge.blocked", mission=mission.id, reason="tip-moved")
                raise ChenshuError(
                    "ERROR: 评审盖的 commit 与分支当前 tip 不一致（评审后又有新提交）"
                    "——重新评审后再合。"
                )
            #  scope 检查的输入必须真拿到：git 超时/报错时绝不 fail-open——
            #  一个被文档承诺"代码强制"的闸，静默退化成 no-op 比没有更糟
            try:
                diff_result = _git(
                    ["diff", "--name-only", f"{self.base_branch}...{mission.branch}"],
                    root,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                self.log(CHENSHU, "merge.blocked", mission=mission.id, reason="diff-failed")
                raise ChenshuError(
                    f"ERROR: scope 检查失败（git diff 出错：{exc}）——拒绝合并，稍后重试。"
                ) from exc
            if diff_result.returncode != 0:
                self.log(CHENSHU, "merge.blocked", mission=mission.id, reason="diff-failed")
                raise ChenshuError(
                    "ERROR: scope 检查失败（git diff 非零退出）——拒绝合并，稍后重试。"
                )
            files = [line for line in diff_result.stdout.splitlines() if line.strip()]
            #  build 分支空 diff = worker 多半没 commit（CI 上 git 身份缺失时
            #  commit 静默失败就是这个形态）。静默合并会把失败掩盖成成功
            if not files:
                self.log(CHENSHU, "merge.blocked", mission=mission.id, reason="empty-diff")
                raise ChenshuError(
                    f"ERROR: {mission.branch} 相对 {self.base_branch} 没有任何已提交"
                    "改动——worker 是否忘了 git commit？确认确实无代码可交付，"
                    "就把 mission 标 completed 后直接 teardown，merge 只收有改动的分支。"
                )
            offenders = [f for f in files if not scope_match(f, mission.scope)]
            if offenders:
                self.log(CHENSHU, "merge.blocked", mission=mission.id, reason="out-of-scope")
                raise ChenshuError(
                    "ERROR: 改动越出 mission scope：" + ", ".join(offenders[:10])
                    + ("…" if len(offenders) > 10 else "")
                    + "。要么回退这些改动，要么（确属必要时）先用 chenshu_plan 的"
                    " scope 语义重新划界。"
                )
            result = _git(
                ["merge", "--no-ff", mission.branch, "-m",
                 f"merge: {mission.branch}——{mission.title}"],
                root,
            )
            if result.returncode != 0:
                #  冲突现场必须清理干净，否则主 checkout 卡在合并中态
                _git(["merge", "--abort"], root)
                combined = f"{result.stdout}\n{result.stderr}"
                #  只有真冲突才给"rebase 解冲突"的指引；其它失败（git 身份缺失、
                #  钩子拒绝……）要把 git 的原话摆出来，别拿冲突剧本误导
                if "CONFLICT" in combined:
                    self.log(CHENSHU, "merge.blocked", mission=mission.id, reason="conflict")
                    raise ChenshuError(
                        "ERROR: 合并冲突，已回滚（scope 不相交时不该发生——多半是别的"
                        " mission 先合入了公共文件）。让 owner 在 worktree 里 rebase "
                        f"{self.base_branch} 解冲突、提交、重评审后再合。"
                    )
                tail = combined.strip().splitlines()
                detail = tail[-1] if tail else f"exit {result.returncode}"
                self.log(CHENSHU, "merge.blocked", mission=mission.id, reason="merge-failed")
                raise ChenshuError(f"ERROR: git merge 失败（{detail}），已回滚——处理后重试。")
            mission.status = "merged"
            self._save()
            self.log(CHENSHU, "merge", mission=mission.id, tip=tip[:10], files=len(files))
            #  合并后提醒：其它未合分支若碰了同一批文件，rebase 后 tip 会动，
            #  评审闸自动强制它们重评
            merged_set = set(files)
            conflicts: list[str] = []
            for other in self.missions:
                if other.id == mission.id or other.kind != "build" or other.status == "merged":
                    continue
                if not other.branch:
                    continue
                other_files = _git_out(
                    ["diff", "--name-only", f"{self.base_branch}...{other.branch}"], root
                )
                overlap = merged_set & set((other_files or "").splitlines())
                if overlap:
                    conflicts.append(f"{other.id}（{len(overlap)} 个文件重叠）")
        lines = [f"✔ {mission.id} 已合并（{len(files)} 个文件）。"]
        if conflicts:
            lines.append(
                "以下 mission 的分支与本次改动有文件重叠，让它们的 owner rebase "
                f"{self.base_branch} 后重走评审：{', '.join(conflicts)}"
            )
        return "\n".join(lines)

    # ---------- spawn / wait ----------

    def spawn(
        self,
        name: str,
        kind: str,
        mission_id: str = "",
        review_target: str = "",
        instructions: str = "",
        resume: bool = False,
    ) -> str:
        self._require_active()
        if not _NAME_RE.match(name or ""):
            raise ChenshuError("ERROR: name 须为小写字母开头的 2-32 位标识。")
        #  保留名：chenshu 是总枢的身份（caller==CHENSHU 是全部特权检查的钥匙），
        #  all 是广播地址——成员顶用这两个名字等于伪造身份/劫持广播
        if name in (CHENSHU, BROADCAST):
            raise ChenshuError(f"ERROR: {name!r} 是保留名（总枢/广播地址），换一个。")
        if kind not in ("worker", "reviewer"):
            raise ChenshuError("ERROR: kind 只能是 worker 或 reviewer。")
        with self.lock:
            existing = self.member(name)
            thread = self._threads.get(name)
            if thread is not None and thread.is_alive():
                raise ChenshuError(f"ERROR: {name} 还在跑——同名成员不能并存。")
            #  退役成员（重启收养后的常态）原名直接重 spawn：init 的提示就是
            #  这么承诺的，不能反手要求一个不存在的存档
            if existing is not None and not resume and existing.status != "retired":
                raise ChenshuError(
                    f"ERROR: {name} 已在花名册（状态 {existing.status}）。要在它的"
                    "上下文上继续，传 resume=true；要新人换个名字。"
                )
            if resume and (existing is None or name not in self._archives):
                raise ChenshuError(
                    f"ERROR: {name} 没有可恢复的存档（重启后线程与存档不跨进程，"
                    "直接不带 resume 重新 spawn 即可，mission 与 worktree 都还在）。"
                )
            if self._live_count() >= max(1, self.config.chenshu_max_workers):
                raise ChenshuError(
                    f"ERROR: 在跑成员已达上限 {self.config.chenshu_max_workers}"
                    "（XIAOYU_CHENSHU_MAX_WORKERS 可调）——chenshu_wait 等人收工。"
                )
            mission: Mission | None = None
            if kind == "worker":
                mission = self.mission(mission_id) if mission_id else None
                if mission is None:
                    raise ChenshuError("ERROR: worker 必须带有效的 mission_id。")
                if mission.status == "merged":
                    raise ChenshuError(f"ERROR: {mission.id} 已合并，无活可干。")
                if mission.owner and mission.owner != name:
                    #  在册且没退役的 owner 才挡人；退役/失踪的 owner（重启收养后）
                    #  允许直接换人接管，否则 mission 永久卡死
                    owner_member = self.member(mission.owner)
                    if owner_member is not None and owner_member.status != "retired":
                        raise ChenshuError(
                            f"ERROR: {mission.id} 已由 {mission.owner} 认领——"
                            "要换人先 spawn 同名 resume，或重新 plan。"
                        )
            else:
                if self._mission_by_ref(review_target) is None:
                    raise ChenshuError("ERROR: reviewer 必须带有效的 review_target（mission id/分支）。")

            workdir = self.config.workspace
            if kind == "worker" and mission is not None and mission.kind == "build":
                if mission.worktree and Path(mission.worktree).is_dir():
                    workdir = Path(mission.worktree)
                else:
                    try:
                        workdir = worktree.create_branch(
                            self.config.workspace, mission.branch,
                            f"cs-{mission.id.lower()}", self.base_branch,
                        )
                    except worktree.WorktreeError as exc:
                        raise ChenshuError(
                            f"ERROR: worktree 创建失败（{exc}）——宸枢不做主工作区"
                            "兜底，隔离是硬前提。"
                        ) from exc
                    mission.worktree = str(workdir)
            if kind == "worker" and mission is not None:
                #  survey 也登记认领：不建 worktree，但双重指派守卫要能生效
                mission.status = "active"
                mission.owner = name
            if existing is None:
                self.members.append(
                    Member(name=name, kind=kind, mission_id=mission.id if mission else "",
                           review_target=review_target, status="running")
                )
            else:
                existing.status = "running"
                existing.handoff = ""
            self._save()
            self.log(CHENSHU, "spawn", name=name, kind=kind,
                     target=mission.id if mission else review_target, resume=resume)

        briefing = self._briefing(name, kind, mission, review_target, instructions)
        seed = self._archives.get(name) if resume else None
        thread = threading.Thread(
            target=self._run_member,
            args=(name, kind, workdir, mission, briefing, seed),
            name=f"chenshu-{name}",
            daemon=True,
        )
        self._threads[name] = thread
        thread.start()
        target_label = mission.id if mission else review_target
        return (
            f"{name}（{kind} → {target_label}）已开工"
            + (f"，worktree：{workdir}" if mission and mission.kind == "build" else "")
            + "。继续指派其它可开工的 mission，全部发完再 chenshu_wait 等事件；"
            "不要空转轮询 status。"
        )

    def _briefing(
        self,
        name: str,
        kind: str,
        mission: Mission | None,
        review_target: str,
        instructions: str,
    ) -> str:
        """briefing 由代码组装，总枢只能通过 instructions 追加——协议段不给改。"""
        extra = f"\n补充指示：\n{instructions}\n" if instructions.strip() else ""
        if kind == "reviewer":
            target = self._mission_by_ref(review_target)
            branch = target.branch if target else review_target
            return (
                f"你是宸枢舰队的 reviewer「{name}」，评审分支 {branch}"
                f"（基于 {self.base_branch}）。\n"
                f"用只读 git 命令看改动：git log {self.base_branch}..{branch}、"
                f"git diff {self.base_branch}...{branch}。绝不修改任何文件。\n"
                "评审优先级：安全 → 数据完整性 → 性能 → 错误处理 → 代码质量。\n"
                "收尾两步缺一不可：1) chenshu_review 提交结论（clean 或 p1-N/p2-N，"
                "正文列出每个问题的位置与理由）；2) chenshu_send 把结论通知作者"
                "（不知道作者就发给 chenshu（总枢））。\n"
                "你无法直接与用户对话；一切沟通走 chenshu_send。" + extra
            )
        assert mission is not None
        scope = ", ".join(mission.scope) if mission.scope else "（survey：只读调研）"
        if mission.kind == "survey":
            return (
                f"你是宸枢舰队的调研 worker「{name}」，负责 {mission.id}：{mission.title}。\n"
                "这是只读调研任务：你没有写工具，交付物是知识。\n"
                "结论走三条道：chenshu_mission 记 notes、chenshu_send 通报相关成员、"
                "值得立项的发现用 chenshu_finding 归档。\n"
                "完成时：chenshu_mission 标记 status=completed，最后一条消息写完整调研结论。"
                + extra
            )
        return (
            f"你是宸枢舰队的 worker「{name}」，负责 {mission.id}：{mission.title}。\n"
            f"工作目录（你的独立 worktree）：{{workspace}}，分支 {mission.branch}"
            f"（基于 {self.base_branch}）。\n"
            f"你的 scope：{scope}。write_file/str_replace 出了 worktree 会被直接拒绝；"
            "scope 之外发现的问题用 chenshu_finding 归档，绝不顺手修。\n"
            "与其他成员/总枢沟通走 chenshu_send / chenshu_inbox；"
            "任务清单与状态走 chenshu_mission（只能改自己这条）。\n"
            "完成四步：1) bash 里 git add -A && git commit（不 commit 等于没干）；"
            "2) chenshu_mission 标记 status=completed；3) chenshu_send 给 chenshu 发"
            " subject=review-request 的消息；4) 最后一条消息写完整交接"
            "（做了什么/改了哪些文件/怎么验证/遗留）。" + extra
        )

    def _run_member(
        self,
        name: str,
        kind: str,
        workdir: Path,
        mission: Mission | None,
        briefing: str,
        seed: list[dict[str, Any]] | None,
    ) -> None:
        from .agent import Agent

        failure = ""
        agent = None
        try:
            tools = (
                REVIEW_TOOLS if kind == "reviewer"
                else SURVEY_TOOLS if mission is not None and mission.kind == "survey"
                else BUILD_TOOLS
            )
            sub_config = Config(
                base_url=self.config.base_url,
                model=self.config.model,
                summary_model=self.config.summary_model,
                explore_model=self.config.explore_model,
                workspace=workdir,
                max_iterations=_WORKER_ITERATIONS,
                max_tool_output=self.config.max_tool_output,
                context_limit_override=self.config.context_limit_override,
                compact_at=self.config.compact_at,
                keep_recent=self.config.keep_recent,
                request_timeout=self.config.request_timeout,
                extra_env=self.config.extra_env,
                bash_timeout=self.config.bash_timeout,
                sandbox=self.config.sandbox,
                sandbox_network=self.config.sandbox_network,
                auto_approve=False,
                enable_explore=False,
                enable_web_search=False,
                enable_skills=False,
                enable_plan=False,
                enable_plugins=False,
                enable_mcp=False,
                enable_hooks=False,
                enable_agents=False,
            )
            toolbox = Toolbox(sub_config, only=list(tools))
            for tool in make_member_tools(self, name, reviewer=kind == "reviewer"):
                toolbox.register(tool)
            guard = _write_guard(workdir) if kind == "worker" and mission is not None and mission.kind == "build" else (lambda n, a: True)
            agent = Agent(
                sub_config,
                toolbox,
                usage=self.usage,
                registry=self.registry,
                quiet=True,
                allow_explore=False,
                approver=guard,
                permissions=self.permissions,
                sink=_SilentSink(),
            )
            with self.lock:
                self._agents[name] = agent
            system = (
                f"你是「{name}」，宸枢舰队的{'评审员' if kind == 'reviewer' else '执行成员'}。"
                "所有 user 消息来自编排方（总枢）。你无法与最终用户对话；最后一条消息"
                "就是你的全部交接，务必自包含。工作区根目录：{workspace}"
            ).format(workspace=workdir)
            if seed:
                agent.messages = [dict(m) for m in seed]
                agent.messages[0] = {"role": "system", "content": system}
            else:
                agent.messages[0]["content"] = system
            agent.send(briefing.replace("{workspace}", str(workdir)))
        except Exception as exc:  # noqa: BLE001 - worker 崩溃不拖垮总枢
            failure = f"{type(exc).__name__}: {exc}"
        finally:
            handoff = ""
            if agent is not None:
                handoff = agent.last_assistant_text()
                with self.lock:
                    self._archives[name] = [dict(m) for m in agent.messages]
                    self._agents.pop(name, None)
            with self.lock:
                member = self.member(name)
                if member is not None:
                    member.status = "failed" if failure else "done"
                    member.handoff = ui.preview(handoff or failure, 500)
                self._save()
            self.log(name, "member.failed" if failure else "member.done",
                     detail=ui.preview(failure or handoff, 80))
            verb = "失败" if failure else "完成"
            event = (
                f"{name} {verb}"
                + (f"：{failure}" if failure else "")
                + (f"\n交接：{ui.preview(handoff, 400)}" if handoff else "")
            )
            self.events.put(event)
            self.notify(f"宸枢：{name} {verb}。chenshu_wait / chenshu_status 查看。",
                        f"chenshu-{name}-{verb}")
            self.sink.emit(Notice(f"  🛰 宸枢：{name} {verb}"))

    def wait(self, timeout: int) -> str:
        self._require_active()
        timeout = max(1, min(int(timeout or _WAIT_DEFAULT), _WAIT_MAX))
        collected: list[str] = []
        with contextlib.suppress(queue.Empty):
            collected.append(self.events.get(timeout=timeout))
            while True:
                collected.append(self.events.get_nowait())
        if not collected:
            live = [name for name, t in self._threads.items() if t.is_alive()]
            return (
                f"{timeout}s 内没有新事件。"
                + (f"在跑：{', '.join(live)}。继续 chenshu_wait 或先处理别的。"
                   if live else "没有在跑的成员——检查是否该 spawn / merge / teardown。")
            )
        return "事件：\n" + "\n---\n".join(collected)

    # ---------- mission 视图/更新（worker 与总枢共用） ----------

    def mission_view(self, caller: str, mission_id: str = "", patch: dict[str, Any] | None = None) -> str:
        self._require_active()
        with self.lock:
            member = self.member(caller)
            if not mission_id and member is not None:
                mission_id = member.mission_id
            mission = self.mission(mission_id)
            if mission is None:
                raise ChenshuError(f"ERROR: 找不到 mission {mission_id!r}。")
            if patch:
                #  worker 只能改自己的；owner/scope 只有总枢能动
                if caller != CHENSHU and (member is None or member.mission_id != mission.id):
                    raise ChenshuError("ERROR: 只能更新自己负责的 mission。")
                status = str(patch.get("status", "")).strip()
                if status:
                    if status not in ("pending", "active", "completed"):
                        raise ChenshuError("ERROR: status 只能改成 pending/active/completed（merged 归 merge 闸）。")
                    mission.status = status
                note = str(patch.get("note", "")).strip()
                if note:
                    mission.notes.append(note)
                if "scope" in patch:
                    if caller != CHENSHU:
                        raise ChenshuError("ERROR: scope 只有总枢能改。")
                    mission.scope = tuple(str(s).strip() for s in patch["scope"] if str(s).strip())
                    self.log(CHENSHU, "mission.scope", mission=mission.id, scope=";".join(mission.scope))
                self._save()
                if status or note:
                    self.log(caller, "mission.update", mission=mission.id,
                             status=mission.status)
            lines = [
                f"{mission.id} [{mission.kind}] {mission.title} · 状态 {mission.status}"
                + (f" · owner {mission.owner}" if mission.owner else ""),
            ]
            if mission.branch:
                lines.append(f"分支 {mission.branch}（基于 {self.base_branch}）")
            if mission.scope:
                lines.append(f"scope: {', '.join(mission.scope)}")
            if mission.deps:
                lines.append(f"deps: {', '.join(mission.deps)}")
            for note in mission.notes[-5:]:
                lines.append(f"note: {note}")
            return "\n".join(lines)

    # ---------- status / teardown ----------

    def status(self) -> str:
        self._require_active()
        root = worktree.git_root(self.config.workspace)
        with self.lock:
            lines = [f"宸枢仪表盘 · base {self.base_branch} · 协议目录 {self.root}"]
            lines.append("missions:")
            all_merged = bool(self.missions)
            for m in self.missions:
                mark = {"pending": "○", "active": "◐", "completed": "●", "merged": "✔"}[m.status]
                if m.status != "merged":
                    all_merged = False
                line = f"  {mark} {m.id} [{m.kind}] {m.title} · {m.status}"
                if m.owner:
                    line += f" · owner {m.owner}"
                if m.kind == "build" and m.branch and root is not None and m.status != "merged":
                    review = self.latest_review(m)
                    if review is None:
                        line += " · 评审闸：无评审"
                    else:
                        tip = _git_out(["rev-parse", m.branch], root) or ""
                        stale = "已过期需重评" if review.get("reviewed_commit") != tip else "盖章有效"
                        line += (
                            f" · 评审闸：r{review.get('round')} {review.get('verdict')}"
                            f"（{stale}）"
                        )
                lines.append(line)
            lines.append("花名册:")
            for member in self.members:
                live = "在跑" if (t := self._threads.get(member.name)) is not None and t.is_alive() else member.status
                target = member.mission_id or member.review_target
                lines.append(f"  {member.name} [{member.kind}→{target}] {live}")
                if member.handoff and member.status in ("done", "failed"):
                    lines.append(f"    交接：{ui.preview(member.handoff, 120)}")
            if not self.members:
                lines.append("  （空）")
            pending_events = self.events.qsize()
            if pending_events:
                lines.append(f"未消费事件 {pending_events} 条（chenshu_wait 取）。")
            log_path = self.root / "activity.log"
            if log_path.is_file():
                with contextlib.suppress(OSError):
                    tail = log_path.read_text(encoding="utf-8").splitlines()[-8:]
                    lines.append("最近活动:")
                    lines += [f"  {line}" for line in tail]
            if all_merged:
                lines.append("全部 mission 已合并 ✔——收尾：确认 inbox 无遗留后立即 chenshu_teardown。")
            return "\n".join(lines)

    def teardown(self, force: bool = False) -> str:
        self._require_active()
        #  三段式：锁内叫停 → **锁外** join（worker 的收尾 finally 也要抢这把
        #  锁存档，持锁 join 必然把每个 join 干等满超时）→ 锁内清理
        with self.lock:
            live = [name for name, t in self._threads.items() if t.is_alive()]
            if live and not force:
                raise ChenshuError(
                    f"ERROR: 还有成员在跑：{', '.join(live)}——chenshu_wait 等收工，"
                    "或确认要硬停就 force=true。"
                )
            for name in live:
                agent = self._agents.get(name)
                if agent is not None:
                    agent.interrupt()
        for name in live:
            self._threads[name].join(timeout=10)
        with self.lock:
            still_alive = {
                m.owner for m in self.missions
                if m.owner and (t := self._threads.get(m.owner)) is not None and t.is_alive()
            }
            kept: list[str] = []
            removed = 0
            for mission in self.missions:
                if not mission.worktree:
                    continue
                path = Path(mission.worktree)
                if not path.is_dir():
                    continue
                #  join 超时还活着的 worker 可能正在这个目录里写——绝不抽地毯
                if mission.owner in still_alive:
                    kept.append(f"{mission.id} → {path}（{mission.owner} 仍未停稳，保留）")
                    self.log(CHENSHU, "worktree.keep", mission=mission.id)
                    continue
                if worktree.dirty(path) and not force:
                    kept.append(f"{mission.id} → {path}（有未提交改动，保留）")
                    self.log(CHENSHU, "worktree.keep", mission=mission.id)
                    continue
                worktree.remove(self.config.workspace, path)
                mission.worktree = ""
                removed += 1
            self.active = False
            self._save()
            self.log(CHENSHU, "teardown", removed=removed, kept=len(kept))
        lines = [f"宸枢已收枢：清理 {removed} 个 worktree。分支与 {self.root} 下的审计轨迹保留。"]
        lines += kept
        return "\n".join(lines)


# ---------- 解析/守卫/静默 ----------


def _parse_front(path: Path) -> tuple[dict[str, str], str]:
    """frontmatter-lite：--- 包裹的 key: value 单行对 + 正文。坏文件当空。"""
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}, ""
    if not raw.startswith("---\n"):
        return {}, raw
    head, sep, body = raw[4:].partition("\n---\n")
    if not sep:
        return {}, raw
    meta: dict[str, str] = {}
    for line in head.splitlines():
        key, colon, value = line.partition(":")
        if colon:
            meta[key.strip()] = value.strip()
    return meta, body


def _write_guard(workdir: Path) -> Callable[[str, dict[str, Any]], Any]:
    """build worker 的审批守卫：写工具限定在自己的 worktree，其余放行。

    bash 不做命令级审查（macOS 有 Seatbelt 兜、其它平台靠 briefing）——
    与"写工具硬拦、其余纪律"的取舍一致，诚实记录在模块 docstring。
    """
    from .agent import Deny

    resolved_root = workdir.resolve()

    def approve(name: str, args: dict[str, Any]) -> Any:
        if name in ("write_file", "str_replace"):
            raw = str(args.get("path", "") or "")
            try:
                target = (Path(raw) if Path(raw).is_absolute() else workdir / raw).resolve()
                inside = target.is_relative_to(resolved_root)
            except (OSError, ValueError):
                inside = False
            if not inside:
                return Deny(
                    f"越界写：{raw} 不在你的 worktree（{workdir}）内。"
                    "scope 外的问题用 chenshu_finding 归档，由总枢分派。"
                )
        return True

    return approve


class _SilentSink:
    """worker 线程的静默 sink：N 个成员的工具刷屏不可读，进度走事件/通知轨道。"""

    def emit(self, event: Any) -> None:  # noqa: ARG002
        pass


# ---------- 工具装配 ----------


def _tool(name: str, description: str, params: dict[str, Any], handler: Any,
          requires_approval: bool = False, check_fn: Any = None) -> Tool:
    return Tool(name=name, description=description, parameters=params,
                handler=handler, requires_approval=requires_approval,
                check_fn=check_fn)


def _wrap(fn: Callable[..., str]) -> Callable[..., str]:
    """ChenshuError → 文本回灌；其余异常交给 Toolbox.run 的兜底。"""

    def inner(**kwargs: Any) -> str:
        try:
            return fn(**kwargs)
        except ChenshuError as exc:
            return str(exc)

    return inner


def make_member_tools(runtime: ChenshuRuntime, name: str, reviewer: bool) -> list[Tool]:
    """挂到成员工具箱上的协作工具（身份在构造期绑定，不靠自报）。"""
    tools = [
        _tool(
            "chenshu_send",
            "给舰队成员/总枢发一条消息（to 可为成员名、chenshu=总枢、all=广播）。",
            {"type": "object", "properties": {
                "to": {"type": "string"}, "subject": {"type": "string"},
                "body": {"type": "string"}},
             "required": ["to", "subject", "body"]},
            _wrap(lambda to, subject, body: runtime.send(name, to, subject, body)),
        ),
        _tool(
            "chenshu_inbox",
            "读自己的收件箱（含广播，倒序，最多 20 条；无已读标记，处理过的自己记住）。",
            {"type": "object", "properties": {}, "required": []},
            _wrap(lambda: runtime.read_inbox(name)),
        ),
        _tool(
            "chenshu_finding",
            "归档一个 scope 之外的发现（bug/improve/vuln/idea）。发现不等于授权：不要动手修。",
            {"type": "object", "properties": {
                "kind": {"type": "string", "enum": ["bug", "improve", "vuln", "idea"]},
                "title": {"type": "string"}, "body": {"type": "string"}},
             "required": ["kind", "title", "body"]},
            _wrap(lambda kind, title, body: runtime.file_finding(name, kind, title, body)),
        ),
        _tool(
            "chenshu_mission",
            "查看/更新 mission（不带 patch 字段=只看；status 可改 completed，note 追加备注）。",
            {"type": "object", "properties": {
                "mission_id": {"type": "string", "description": "省略=自己负责的那条"},
                "status": {"type": "string", "enum": ["active", "completed"]},
                "note": {"type": "string"}},
             "required": []},
            _wrap(lambda mission_id="", status="", note="": runtime.mission_view(
                name, mission_id,
                {k: v for k, v in (("status", status), ("note", note)) if v} or None)),
        ),
    ]
    if reviewer:
        tools.append(
            _tool(
                "chenshu_review",
                "提交评审结论。verdict=clean 或 p1-N/p2-N（p1=必须修，p2=建议修，N=个数）；"
                "summary 列出每个问题的位置与理由。提交时自动盖分支当前 commit。",
                {"type": "object", "properties": {
                    "target": {"type": "string", "description": "mission id 或分支名"},
                    "verdict": {"type": "string"},
                    "summary": {"type": "string"}},
                 "required": ["target", "verdict", "summary"]},
                _wrap(lambda target, verdict, summary: runtime.submit_review(
                    name, target, verdict, summary)),
            )
        )
    return tools


def make_chenshu_tools(runtime: ChenshuRuntime) -> list[Tool]:
    """挂到主 agent 上的宸枢全家：init 常驻，其余 check_fn 门控在 active 后出现。"""
    active = lambda: runtime.active  # noqa: E731

    return [
        _tool(
            "chenshu_init",
            "启动宸枢（统筹织造模式）：总枢规划、分派、监督、汇总——把大工程拆成 scope 互不重叠的 mission，"
            "每个 mission 在独立 git 分支 worktree 里由 worker 并行推进，评审过闸后"
            "合回主干。适合跨多个子系统、需要隔离与评审的大活；小活用七襄或单发委托。"
            "需要 git 仓库且至少一个 commit。重入=收养已有工作区（mission 保留）。",
            {"type": "object", "properties": {}, "required": []},
            _wrap(lambda: runtime.init()),
            requires_approval=True,
        ),
        _tool(
            "chenshu_plan",
            "登记一批 mission。build 必须给 scope（目录/glob，两两不相交，共享文件归"
            "唯一一个 mission）；survey=只读调研无分支。deps 指定合并顺序约束。",
            {"type": "object", "properties": {
                "missions": {"type": "array", "items": {"type": "object", "properties": {
                    "title": {"type": "string"},
                    "kind": {"type": "string", "enum": ["build", "survey"]},
                    "scope": {"type": "array", "items": {"type": "string"}},
                    "deps": {"type": "array", "items": {"type": "string"}}},
                    "required": ["title"]}}},
             "required": ["missions"]},
            _wrap(lambda missions: runtime.plan(list(missions or []))),
            check_fn=active,
        ),
        _tool(
            "chenshu_spawn",
            "起一个成员：worker 带 mission_id（build 自动建分支 worktree），"
            "reviewer 带 review_target。resume=true 在同名成员的上下文上续跑。"
            "把所有依赖已解锁的 mission 背靠背发满，再 chenshu_wait。",
            {"type": "object", "properties": {
                "name": {"type": "string"},
                "kind": {"type": "string", "enum": ["worker", "reviewer"]},
                "mission_id": {"type": "string"},
                "review_target": {"type": "string"},
                "instructions": {"type": "string", "description": "追加给成员的具体指示"},
                "resume": {"type": "boolean"}},
             "required": ["name", "kind"]},
            _wrap(lambda name, kind, mission_id="", review_target="", instructions="",
                  resume=False: runtime.spawn(name, kind, mission_id, review_target,
                                              instructions, bool(resume))),
            check_fn=active,
        ),
        _tool(
            "chenshu_wait",
            "阻塞等待下一个成员事件（完成/失败），最长 600s。有事件立即返回全部积压。"
            "spawn 发满之后用它等，不要轮询 status。",
            {"type": "object", "properties": {
                "timeout": {"type": "integer", "description": f"秒，默认 {_WAIT_DEFAULT}"}},
             "required": []},
            _wrap(lambda timeout=_WAIT_DEFAULT: runtime.wait(int(timeout))),
            check_fn=active,
        ),
        _tool(
            "chenshu_status",
            "仪表盘：mission 表（含评审闸状态）、花名册、未消费事件、最近活动。",
            {"type": "object", "properties": {}, "required": []},
            _wrap(lambda: runtime.status()),
            check_fn=active,
        ),
        _tool(
            "chenshu_merge",
            "唯一合并入口（绝不手工 git merge）。五道闸：deps 已合 → 有评审且最新"
            "一轮 clean → 评审盖的 commit=分支 tip → diff 全部命中 scope → 主 "
            "checkout 在 base 上。survey 直接完结。被拒就按提示修，不要绕。",
            {"type": "object", "properties": {
                "target": {"type": "string", "description": "mission id 或分支名"}},
             "required": ["target"]},
            _wrap(lambda target: runtime.merge(target)),
            requires_approval=True,
            check_fn=active,
        ),
        _tool(
            "chenshu_teardown",
            "收枢：干净 worktree 删除、脏的保留报路径，审计轨迹永久保留。"
            "全部 mission ✔ 且 inbox 无遗留后立即调，不要等用户催。",
            {"type": "object", "properties": {
                "force": {"type": "boolean", "description": "硬停在跑成员并连脏 worktree 一起删"}},
             "required": []},
            _wrap(lambda force=False: runtime.teardown(bool(force))),
            check_fn=active,
        ),
        #  总枢也是通信参与者：同一套 comms，身份绑定为 chenshu
        _tool(
            "chenshu_send",
            "给舰队成员发消息（to=成员名或 all 广播）。总枢是协调者不是内容中继：让成员互相直连。",
            {"type": "object", "properties": {
                "to": {"type": "string"}, "subject": {"type": "string"},
                "body": {"type": "string"}},
             "required": ["to", "subject", "body"]},
            _wrap(lambda to, subject, body: runtime.send(CHENSHU, to, subject, body)),
            check_fn=active,
        ),
        _tool(
            "chenshu_inbox",
            "读全部信件（总枢可见所有人的往来，倒序，最多 20 条）。",
            {"type": "object", "properties": {}, "required": []},
            _wrap(lambda: runtime.read_inbox(CHENSHU)),
            check_fn=active,
        ),
        _tool(
            "chenshu_mission",
            "查看/更新任意 mission（status/note/scope；scope 变更会记审计日志）。",
            {"type": "object", "properties": {
                "mission_id": {"type": "string"},
                "status": {"type": "string", "enum": ["pending", "active", "completed"]},
                "note": {"type": "string"},
                "scope": {"type": "array", "items": {"type": "string"}}},
             "required": ["mission_id"]},
            _wrap(lambda mission_id, status="", note="", scope=None: runtime.mission_view(
                CHENSHU, mission_id,
                {k: v for k, v in (("status", status), ("note", note),
                                   ("scope", scope)) if v} or None)),
            check_fn=active,
        ),
        _tool(
            "chenshu_review",
            "总枢亲自提交评审（通常应 spawn reviewer 评审，总枢不评自己编排的代码；"
            "此通道留给例外情况）。",
            {"type": "object", "properties": {
                "target": {"type": "string"}, "verdict": {"type": "string"},
                "summary": {"type": "string"}},
             "required": ["target", "verdict", "summary"]},
            _wrap(lambda target, verdict, summary: runtime.submit_review(
                CHENSHU, target, verdict, summary)),
            check_fn=active,
        ),
    ]
