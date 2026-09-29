"""并发委托的共享引擎（七襄与斗巧同用）。

一份代码管三件事：错峰启动、超时巡检（逐 tick 补发中断）、用户中止的
存档保全。曾经在两个模块里各抄一份，漂移立刻发生（notes 丢失、
未开始席位无说明、超时误伤刚完赛的项）——并发正确性的修补必须只落一处。

调用方只提供两个闭包：primary（跑一次委托）与可选的 follow_up
（结论太短时借 resume 追问一轮）。引擎不认识 spec/report/判官，
它只负责"把 N 个尝试并发跑完、每个尝试的收束状态记清楚"。
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass
from typing import Any, Callable

from .agents import DelegationResult
from .events import ObservingSink  # noqa: F401 - 批量工具从这里取
from .errors import Interrupted, attach_partial  # noqa: F401 - 批量工具从这里取

#  错峰间隔：首批并发槽位依次延后起步，避免同一瞬间打满 provider
STAGGER_SECONDS = 0.3
#  短结论追问阈值：批量/竞赛模式下父 agent 无法逐个便宜追问，
#  收束前把太短的交接补全一轮（200 字符以下基本不可能是完整交接）
MIN_ANSWER_CHARS = 200

#  多久没有任何一项收束就报一次"都在干什么"
HEARTBEAT_SECONDS = 30.0

#  连续这么多项都因为同一个换谁来都一样的原因失败，余下没起步的就不再起步
BREAKER_STREAK = 3
#  换一项重试也不会好的失败：鉴权不过、额度用尽
_HOPELESS_KINDS = ("auth", "quota")

OnAgent = Callable[[Any], None]


@dataclass
class Attempt:
    """一次尝试的全程状态（引擎写，调用方读）。"""

    index: int  # 0 起，也是错峰顺位
    primary: Callable[[OnAgent], DelegationResult]
    #  (上一轮 run_id, register) -> 追问轮结果；None = 不追问
    follow_up: Callable[[str, OnAgent], DelegationResult] | None = None
    result: DelegationResult | None = None
    crash: str = ""
    timed_out: bool = False
    started_at: float | None = None
    never_started: bool = False


def run_attempts(
    attempts: list[Attempt],
    *,
    concurrency: int,
    timeout_s: int = 0,
    min_answer_chars: int = MIN_ANSWER_CHARS,
    on_settled: Callable[[Attempt, int, int], None] | None = None,
    stop_requested: Callable[[], bool] | None = None,
    on_tick: Callable[[], None] | None = None,
    breaker: int = 0,
) -> str:
    """并发跑完全部尝试；结果写回各 Attempt。

    - 超时从**实际启动**起算（排队不计），巡检逐 tick 补发中断——中断
      信号可能落在两代 agent 之间（primary 刚收尾、追问轮刚接手），
      只发一次会让换代后的 agent 无界跑下去。
    - primary 一收束立刻落座 result，追问轮成功才覆盖——否则刚好在
      deadline 边上完赛的尝试会被巡检误标超时、好答案被藏。
    - 用户中止（BaseException）：叫停所有在飞 agent、等一小段让存档
      落地（resume 句柄仍有效）后原样上抛。
    - breaker > 0：连续这么多项都因鉴权 / 额度失败时，余下没起步的不再起步
      （记成 never_started），返回值说明原因；正常跑完返回空串。已经在跑的
      不打断。各项用的模型不同时不要开（一项的 key 失效说明不了别的项）。
    - on_tick 每个 tick 在调用方线程里叫一次，给调用方转述旁观到的告警。
    - stop_requested（父级的"我被打断了吗"）每个 tick 问一次：宿主经
      interrupt() 叫停父级时没有异常会落到这个线程上，得自己去看。
    """
    cancel_event = threading.Event()
    live: dict[int, Any] = {}
    live_lock = threading.Lock()

    breaker_state: dict[str, Any] = {"streak": [], "tripped": ""}
    breaker_lock = threading.Lock()

    def note_outcome(attempt: Attempt) -> None:
        if not breaker:
            return
        kind = attempt.result.failure_kind if attempt.result is not None else ""
        with breaker_lock:
            if kind not in _HOPELESS_KINDS:
                breaker_state["streak"] = []
                return
            streak = [*breaker_state["streak"], kind][-breaker:]
            breaker_state["streak"] = streak
            if breaker_state["tripped"] or len(streak) < breaker or len(set(streak)) != 1:
                return
            #  建一个完整的工作副本再失败、再建下一个——几十项就是几十次白建，
            #  外加一份几十条同因失败的报告
            reason = {"auth": "鉴权失败", "quota": "额度用尽"}[kind]
            breaker_state["tripped"] = (
                f"连续 {breaker} 项都因{reason}而失败，余下没起步的项不再起步"
            )
            cancel_event.set()

    def runner(attempt: Attempt) -> None:
        delay = (
            attempt.index * STAGGER_SECONDS if attempt.index < concurrency else 0.0
        )
        if delay and cancel_event.wait(delay):
            attempt.never_started = True
            return
        if cancel_event.is_set():
            attempt.never_started = True
            return
        attempt.started_at = time.monotonic()

        def register(agent: Any) -> None:
            with live_lock:
                live[attempt.index] = agent

        try:
            result = attempt.primary(register)
            #  先落座：巡检以 result 是否就位判断"还在跑"
            attempt.result = result
            if (
                attempt.follow_up is not None
                and not result.error
                and not result.failure
                and result.answer
                and len(result.answer) < min_answer_chars
                and not cancel_event.is_set()
                and not attempt.timed_out
            ):
                follow = attempt.follow_up(result.run_id, register)
                if (
                    not follow.error
                    and not follow.failure
                    and len(follow.answer) > len(result.answer)
                ):
                    attempt.result = follow
        except BaseException as exc:  # noqa: BLE001 - 单个尝试炸了不搅局
            attempt.crash = f"{type(exc).__name__}: {exc}"
        finally:
            with live_lock:
                live.pop(attempt.index, None)
            #  熔断判定放在工作线程里、交还线程之前：放到调度循环里判的话，线程一
            #  空出来下一项就已经起步了，总是多跑一项
            note_outcome(attempt)

    pool = ThreadPoolExecutor(
        max_workers=max(1, concurrency), thread_name_prefix="fanout"
    )
    futures = {pool.submit(runner, attempt): attempt for attempt in attempts}
    try:
        pending = set(futures)
        settled = 0
        while pending:
            if stop_requested is not None and stop_requested():
                #  走下面的中止分支：叫停在飞的、保住存档、原样上抛
                raise Interrupted("宿主请求打断")
            finished, pending = wait(pending, timeout=1.0, return_when=FIRST_COMPLETED)
            if on_tick is not None:
                #  调用方线程里的例行回调（转述告警、报心跳）；它出错不能打断调度
                try:
                    on_tick()
                except Exception:  # noqa: BLE001
                    pass
            for future in finished:
                settled += 1
                if on_settled is not None:
                    on_settled(futures[future], settled, len(attempts))
            if timeout_s:
                now = time.monotonic()
                for attempt in attempts:
                    if (
                        attempt.started_at is not None
                        and attempt.result is None
                        and not attempt.crash
                        and now - attempt.started_at > timeout_s
                    ):
                        attempt.timed_out = True
                        with live_lock:
                            agent = live.get(attempt.index)
                        if agent is not None:
                            agent.interrupt()
        pool.shutdown(wait=True)
        return breaker_state["tripped"]
    except BaseException:
        cancel_event.set()
        with live_lock:
            for agent in live.values():
                agent.interrupt()
        wait(set(futures), timeout=10)
        pool.shutdown(wait=False, cancel_futures=True)
        raise
