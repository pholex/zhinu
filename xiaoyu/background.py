"""后台任务：bash run_in_background 与 monitor 的进程表、完成通知、事件限流。

后台三件套，按小羽的同步架构裁剪：

- **后台命令**：bash 工具的 `run_in_background=true`。立即返回 task id，输出
  落盘到日志文件，watcher 线程等进程退出后经通知轨道（Agent.notify →
  下一条工具结果的 <system-reminder>）告知模型——模型不用轮询。
- **monitor**：长驻观察进程，stdout 每行是一个事件，tail 线程把新行批量转成
  通知。没有 until-condition——条件语义写在命令自己的脚本里（工具描述
  强约束"只打印 DONE/FAILED"来代替条件表达式）。
- **事件限流**：令牌桶 10 个、每 2s 补 1 个；被压制的
  事件计数，恢复时补一条说明；持续压制超 30s 直接击杀 monitor——
  刷屏的监控脚本只会把上下文灌满，救不回来。

进程策略（argv 怎么拼、套不套沙箱、环境白名单）都留在 tools.py：这里只收
现成的 spawn 参数，管生命周期。kill_tree 定义在本模块（tools 也用它）——
安全函数只此一份，不复制——复制出去的副本迟早跟不上加固。
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

#  单行/单批事件的字符上限
_LINE_CAP = 500
_BATCH_CAP = 3000
#  monitor tail 的轮询间隔
_POLL_SECONDS = 0.2
#  令牌桶：容量 / 补充间隔（秒/个）
_BUCKET_CAPACITY = 10
_BUCKET_REFILL_SECONDS = 2.0
#  持续压制这么久还在刷 → 击杀 monitor
_AUTO_KILL_SECONDS = 30.0
#  monitor 默认寿命（10 小时）
MONITOR_DEFAULT_TIMEOUT = 36_000


def kill_tree(proc: subprocess.Popen) -> None:
    """整树终止（从 tools.py 移入，那边 import 这里的）。

    只杀 shell 会留下孙进程握着管道不放；Windows 用 taskkill /T 按树杀，
    POSIX 靠 start_new_session 建立的进程组整组杀。
    """
    if os.name == "nt":
        with contextlib.suppress(Exception):
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                capture_output=True,
                timeout=10,
            )
    else:
        import signal

        with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
            pgid = os.getpgid(proc.pid)
            #  子进程与我们同组（调用方没开 start_new_session）时绝不能 killpg——
            #  那会把自己整组带走。这种情况下退化为只杀直接子进程。
            if pgid != os.getpgid(0):
                os.killpg(pgid, signal.SIGKILL)
    with contextlib.suppress(Exception):
        proc.kill()


@dataclass
class BackgroundTask:
    task_id: str
    kind: str  # "command" | "monitor"
    command: str
    description: str
    log_path: Path
    proc: subprocess.Popen
    started: float = field(default_factory=time.monotonic)
    done: threading.Event = field(default_factory=threading.Event)
    exit_code: int | None = None
    #  kill_task 明确终止的任务不再发完成通知（kill 的返回值已经告知模型）
    killed: bool = False

    @property
    def status(self) -> str:
        if not self.done.is_set():
            return "running"
        if self.killed:
            return "cancelled"
        return "completed" if self.exit_code == 0 else "failed"

    def elapsed(self) -> float:
        return time.monotonic() - self.started


class _RateLimiter:
    """monitor 事件的令牌桶。allow() 返回 (放行?, 恢复时要补的说明)。"""

    def __init__(self) -> None:
        self.tokens = float(_BUCKET_CAPACITY)
        self.last_refill = time.monotonic()
        self.suppressed = 0
        self.suppressing_since: float | None = None

    def allow(self) -> tuple[bool, str]:
        now = time.monotonic()
        self.tokens = min(
            float(_BUCKET_CAPACITY),
            self.tokens + (now - self.last_refill) / _BUCKET_REFILL_SECONDS,
        )
        self.last_refill = now
        if self.tokens < 1.0:
            self.suppressed += 1
            if self.suppressing_since is None:
                self.suppressing_since = now
            return False, ""
        self.tokens -= 1.0
        note = ""
        if self.suppressed:
            note = (
                f"[输出过快，期间有 {self.suppressed} 条事件被丢弃。"
                "考虑 kill_task 后换一条过滤更狠的 monitor 命令。]\n"
            )
        self.suppressed = 0
        self.suppressing_since = None
        return True, note

    def should_auto_kill(self) -> bool:
        return (
            self.suppressing_since is not None
            and time.monotonic() - self.suppressing_since > _AUTO_KILL_SECONDS
        )


class TaskManager:
    """一个会话的后台任务表。notify 由 Agent 注入（见 agent.__init__）。

    notify 为 None 时任务照跑，只是没有"搭便车"通知——task_output 仍可查询
    （headless 嵌入宿主不接通知轨道时的退化形态）。
    """

    def __init__(self) -> None:
        self.notify: Callable[[str, str], None] | None = None
        self._tasks: dict[str, BackgroundTask] = {}
        self._lock = threading.Lock()
        self._counter = 0
        self._log_dir: Path | None = None
        self._atexit_registered = False

    # ---------- 启动 ----------

    def _log_file(self, task_id: str) -> Path:
        if self._log_dir is None:
            self._log_dir = Path(tempfile.mkdtemp(prefix="xiaoyu-bg-"))
        return self._log_dir / f"{task_id}.log"

    def start(
        self,
        argv: list[str],
        *,
        command: str,
        kind: str = "command",
        description: str = "",
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout: float | None = None,
        popen_extra: dict[str, Any] | None = None,
    ) -> "BackgroundTask | str":
        """拉起后台进程。成功返回任务；失败返回错误文本（交给模型自愈）。"""
        with self._lock:
            self._counter += 1
            task_id = f"task-{self._counter}"
            if not self._atexit_registered:
                import atexit

                atexit.register(self.shutdown)
                self._atexit_registered = True
        log_path = self._log_file(task_id)
        try:
            log_handle = log_path.open("wb")
        except OSError as exc:
            return f"ERROR: 无法创建后台任务日志文件：{exc}"
        try:
            proc = subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                cwd=cwd,
                env=env,
                **(popen_extra or {}),
            )
        except OSError as exc:
            log_handle.close()
            return f"ERROR: 无法启动后台任务：{exc}"
        finally:
            #  子进程已继承文件描述符，父进程这份立即关掉（Windows 上不关会锁文件）
            with contextlib.suppress(OSError):
                log_handle.close()
        task = BackgroundTask(
            task_id=task_id,
            kind=kind,
            command=command,
            description=description or command,
            log_path=log_path,
            proc=proc,
        )
        with self._lock:
            self._tasks[task_id] = task
        threading.Thread(
            target=self._watch, args=(task, timeout), daemon=True, name=f"xiaoyu-{task_id}"
        ).start()
        if kind == "monitor":
            threading.Thread(
                target=self._tail_monitor, args=(task,), daemon=True,
                name=f"xiaoyu-{task_id}-tail",
            ).start()
        return task

    # ---------- 生命周期 ----------

    def _watch(self, task: BackgroundTask, timeout: float | None) -> None:
        try:
            task.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            kill_tree(task.proc)
            with contextlib.suppress(Exception):
                task.proc.wait(timeout=10)
        except Exception:  # noqa: BLE001 - watcher 线程绝不能带着异常死掉
            pass
        task.exit_code = task.proc.returncode
        task.done.set()
        self._announce_completion(task)

    def _announce_completion(self, task: BackgroundTask) -> None:
        if self.notify is None or task.killed:
            return
        seconds = task.elapsed()
        if task.kind == "monitor":
            text = (
                f'monitor "{task.task_id}" 已结束（exit {task.exit_code}）。\n'
                f"描述：{task.description}\n"
                f'用时 {seconds:.1f}s。用 task_output(task_ids=["{task.task_id}"]) 查看完整输出。'
            )
        else:
            #  自伤诊断：秒退多半是 pkill 匹配到了自己之类的问题
            hint = ""
            if task.exit_code not in (0, None) and seconds < 1.0:
                hint = "\n（不到 1 秒就退出了：检查命令是否有误、或 kill 模式是否匹配到了自己。）"
            text = (
                f'后台任务 "{task.task_id}" 已完成（exit {task.exit_code}）。\n'
                f"命令：{task.command} | 用时 {seconds:.1f}s\n"
                f'用 task_output(task_ids=["{task.task_id}"]) 查看完整输出。{hint}'
            )
        self.notify(text, f"task-done-{task.task_id}")

    def _tail_monitor(self, task: BackgroundTask) -> None:
        """轮询日志文件，把新行批量转成通知（文件游标是唯一的增量语义）。"""
        offset = 0
        pending = ""
        limiter = _RateLimiter()
        while True:
            finished = task.done.is_set()
            chunk, offset = self._read_new(task.log_path, offset)
            pending += chunk
            lines, pending = self._split_lines(pending, flush=finished)
            if lines and self.notify is not None:
                allowed, note = limiter.allow()
                if allowed:
                    batch = "\n".join(lines)
                    if len(batch) > _BATCH_CAP:
                        batch = batch[:_BATCH_CAP] + "\n…（本批过长已截断）"
                    self.notify(
                        f'<monitor-event task_id="{task.task_id}" '
                        f'description="{_sanitize(task.description)}">\n'
                        f"{note}{batch}\n</monitor-event>",
                        "",
                    )
                elif limiter.should_auto_kill():
                    task.killed = True
                    kill_tree(task.proc)
                    self.notify(
                        f'[monitor "{task.task_id}" 已被自动终止——脚本输出太快'
                        f"（{limiter.suppressed} 条事件被丢弃）。请换一条过滤更狠的命令："
                        "管道接 grep --line-buffered / awk，只输出你真正要等的那几行。]",
                        "",
                    )
                    return
            if finished:
                return
            time.sleep(_POLL_SECONDS)

    @staticmethod
    def _read_new(path: Path, offset: int) -> tuple[str, int]:
        try:
            with path.open("rb") as handle:
                handle.seek(offset)
                data = handle.read(1_048_576)
        except OSError:
            return "", offset
        return data.decode("utf-8", errors="replace"), offset + len(data)

    @staticmethod
    def _split_lines(pending: str, flush: bool) -> tuple[list[str], str]:
        """完整行出队（单行截断），残行留在缓冲；flush=True 时残行也算一行。"""
        parts = pending.split("\n")
        remainder = parts.pop()
        if flush and remainder.strip():
            parts.append(remainder)
            remainder = ""
        lines = []
        for line in parts:
            line = line.strip()
            if not line:
                continue
            if len(line) > _LINE_CAP:
                line = line[:_LINE_CAP] + "…（单行过长已截断）"
            lines.append(line)
        return lines, remainder

    # ---------- 查询 / 终止 ----------

    def get(self, task_id: str) -> BackgroundTask | None:
        with self._lock:
            return self._tasks.get(task_id)

    def all(self) -> list[BackgroundTask]:
        with self._lock:
            return list(self._tasks.values())

    def running(self) -> list[BackgroundTask]:
        return [task for task in self.all() if not task.done.is_set()]

    def known_ids(self) -> list[str]:
        with self._lock:
            return list(self._tasks)

    def kill(self, task_id: str) -> str:
        task = self.get(task_id)
        if task is None:
            known = ", ".join(self.known_ids()) or "（本会话还没有后台任务）"
            return f"ERROR: 没有名为 {task_id!r} 的后台任务。已知：{known}"
        if task.done.is_set():
            return f"{task_id} 早已结束（exit {task.exit_code}），无需终止。"
        task.killed = True
        kill_tree(task.proc)
        return f"已终止 {task_id}（{task.description}）。"

    def output_of(self, task: BackgroundTask) -> str:
        try:
            text = task.log_path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return f"（日志读取失败：{exc}）"
        return text if text.strip() else "（暂无输出）"

    def still_running_line(self) -> str:
        """轮次结束后的状态行；没有在跑的返回空串。"""
        running = self.running()
        if not running:
            return ""
        commands = sum(1 for task in running if task.kind == "command")
        monitors = len(running) - commands
        parts = []
        if commands:
            parts.append(f"{commands} 个后台任务")
        if monitors:
            parts.append(f"{monitors} 个 monitor")
        return " · ".join(parts) + " 仍在运行（/tasks 查看，task_output 取输出）"

    def shutdown(self) -> None:
        """会话结束整体回收（atexit / 显式调用均幂等）。"""
        for task in self.all():
            if not task.done.is_set():
                task.killed = True
                kill_tree(task.proc)


def _sanitize(description: str) -> str:
    """事件标签里的描述：引号与换行替换掉，别把 XML 形状搅坏。"""
    return description.replace('"', "'").replace("\n", " ").replace("\r", " ")
