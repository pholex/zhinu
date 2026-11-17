"""Gemini Interactions 后台研究：提交、查询与取消各是一次同步 HTTP 请求；
完成通知由本模块自己的 watcher 线程经通知轨道送达。

为什么要 watcher：工具结果只说「稍后查询」时，模型没有「结束本轮、等事件再来」
的路径，唯一能等的手段就是 bash sleep——实测模型把 sleep 从 45s 逐级加到 300s，
整轮卡死在那里。后台命令 / monitor 的契约是「完成时自动通知、不要轮询」，研究
任务沿用同一契约：提交即返回，watcher 定期查状态，终态时 notify，模型收到
通知再读报告。
"""

from __future__ import annotations

import base64
import json
import re
import threading
import time
from pathlib import Path
from typing import Callable

from . import media, netproxy
from .config import Config, user_config_dir
from .providers import Registry, request_timeout
from .tools import Tool

ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/interactions"
AGENTS = {
    "standard": "deep-research-preview-04-2026",
    "max": "deep-research-max-preview-04-2026",
}
_TERMINAL = {"completed", "failed", "cancelled"}
#  watcher 的轮询间隔与寿命：研究通常几分钟到几十分钟，30s 一查足够及时；
#  超过寿命就不再盯（任务照样可以手动查），别让一个忘了的任务永远占着线程
POLL_SECONDS = 30.0
WATCH_MAX_SECONDS = 3 * 3600
#  连续这么多次查询失败就收手（网络断了之类），改由手动查询
MAX_CONSECUTIVE_FAILURES = 5


def _valid_id(value) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,2048}", value) is not None


def _report(data: dict, figure: Callable[[int, str], str] | None = None) -> str:
    """最终报告 = steps 末尾连续的 model_output 段拼起来；思考、工具执行及签名不进入。

    服务端会把长报告切成多个相邻的 model_output 步骤（实测：首段带标题、摘要
    与全部引用注解，次段是一张图加正文），只取最后一段会丢掉标题和全部来源。
    被 thought / 工具步骤隔开的更早输出是中间稿，不算报告。

    附图按原位置替换成 `figure(序号, base64)` 返回的文字（落盘路径等）；没给
    figure 时只在末尾报数量。没有任何文本的报告不算报告，哪怕有图。
    """
    def items(value):
        return value if isinstance(value, list) else []

    final = []
    for step in reversed(items(data.get("steps"))):
        if not isinstance(step, dict) or step.get("type") != "model_output":
            break
        final.append(step)
    final.reverse()
    parts, urls, images, has_text = [], [], 0, False
    for step in final:
        for content in items(step.get("content")):
            if not isinstance(content, dict):
                continue
            if content.get("type") == "image":
                images += 1
                if figure is not None:
                    parts.append(figure(images, content.get("data") if isinstance(content.get("data"), str) else ""))
                continue
            if content.get("type") != "text":
                continue
            if isinstance(content.get("text"), str) and content["text"].strip():
                parts.append(content["text"])
                has_text = True
            for annotation in items(content.get("annotations")):
                if not isinstance(annotation, dict):
                    continue
                url = annotation.get("url")
                if isinstance(url, str) and url.startswith(("https://", "http://")) and url not in urls:
                    urls.append(url)
    if not has_text:
        return ""
    report = "\n".join(parts)
    if images and figure is None:
        report += f"\n（报告另含 {images} 张图，文本结果里不展示）"
    if urls:
        report += "\n来源：\n" + "\n".join(f"- {url}" for url in urls)
    return report


def figures_dir(interaction_id: str) -> Path:
    """某个研究任务附图的持久目录：用户配置目录下按任务 ID 分开。

    不放工作区（研究报告不是代码改动，不该弄脏仓库），也不放会话临时目录
    （进程退出就删，而任务 ID 本来就承诺跨会话可查）。
    """
    return user_config_dir() / "deep-research" / interaction_id


def make_deep_research_tools(config: Config, registry: Registry, usage, notify=None,
                             push_media=None) -> list[Tool]:
    """三件工具。`notify(text, key)` 是宿主的通知轨道（Agent.notify）：给了就为本会话
    提交的任务起 watcher，终态时推一条通知；没给（headless 嵌入）退回「稍后手动查询」。
    `push_media(part)` 是 Toolbox.push_media：给了就把报告附图交给模型看（随下一条
    消息附上）；没给只落盘给路径。"""
    #  只记本工具实例提交的任务；查询旧任务仍返回服务端用量，避免会话恢复重复记账。
    pending: dict[str, str] = {}
    accounted: set[str] = set()
    submitted: dict[str, float] = {}
    #  watcher 拿到的终态响应：之后的 status 查询直接用，不再打网络
    finished: dict[str, dict] = {}
    #  正在被 watcher 盯着的任务 → 它的停止信号（取消时叫停，不再通知）
    watching: dict[str, threading.Event] = {}
    #  已经交给模型看过的附图：同一任务反复查状态不重复附图
    shown: set[tuple[str, int]] = set()
    lock = threading.Lock()

    def save_figure(interaction_id: str, index: int, payload: str) -> str:
        """一张附图 → 报告里的一行占位：落盘路径 + 尺寸，并交给模型看。

        字节先过 media.accept（格式嗅探、体积上限，与剪贴板/MCP 的图同一道闸），
        再在用户配置目录留一份按任务 ID 归档的持久副本——媒体缓存是内容寻址的
        内部存储，用户不该去那里找图。落盘失败不影响报告正文。
        """
        try:
            data = base64.b64decode(payload, validate=True)
        except (ValueError, TypeError):
            return f"[附图 {index} 已省略：数据不是有效的 base64]"
        ref, problem = media.accept(data, f"研究报告附图 {index}")
        if not ref:
            return f"[附图 {index} 已省略：{problem}]"
        ext = ref.rsplit(".", 1)[-1]
        target = figures_dir(interaction_id) / f"figure-{index}.{ext}"
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists():
                target.write_bytes(data)
            location = str(target)
        except OSError:
            location = str(media.path_of(ref) or ref)
        note = f"[附图 {index}：{location}（{media.label(ref)}）"
        if push_media is not None:
            with lock:
                first = (interaction_id, index) not in shown
                shown.add((interaction_id, index))
            if first:
                push_media(media.image_part(ref))
                note += "，图片见下一条"
        return note + "]"

    def available() -> bool:
        provider = registry.get("gemini")
        return config.enable_deep_research and provider is not None and bool(provider.api_key)

    def request(method: str, suffix: str = "", body=None):
        if not config.enable_deep_research:
            return "ERROR: Deep Research 已禁用（XIAOYU_ENABLE_DEEP_RESEARCH）。"
        provider = registry.get("gemini")
        if provider is None or not provider.api_key:
            return "ERROR: 未配置 Gemini 直连（需要 GEMINI_API_KEY 或 GOOGLE_API_KEY）。"
        try:
            with netproxy.http_client() as client:
                response = client.request(
                    method, ENDPOINT + suffix, json=body,
                    headers={"x-goog-api-key": provider.api_key},
                    timeout=request_timeout(config.request_timeout), follow_redirects=False,
                )
            if not response.is_success:
                #  不回显错误体或异常消息：服务端/代理可能把凭据写进其中。
                return (f"ERROR: Gemini Deep Research HTTP {response.status_code}。"
                        "请检查当前 key 的 Interactions 权限、计费与配额；提交失败请勿盲目重复提交。")
            data = json.loads(response.content.decode("utf-8", errors="replace"))
            if not isinstance(data, dict) or not _valid_id(data.get("id")) or not isinstance(data.get("status"), str):
                return "ERROR: Gemini Deep Research 响应缺少有效任务 ID 或状态。"
            return data
        except Exception as exc:  # noqa: BLE001 - 辅助研究失败不打断会话
            return (f"ERROR: Gemini Deep Research 请求失败（{type(exc).__name__}）。"
                    "提交请求可能已被服务端接收，请勿自动重复提交。")

    def account(data: dict) -> str:
        """终态响应的服务端用量：渲染成一行，本实例提交的任务只记一次账。"""
        server_usage = data.get("usage")
        if not isinstance(server_usage, dict) or data["status"] not in _TERMINAL:
            return ""

        def count(name):
            value = server_usage.get(name, 0)
            return max(0, value) if type(value) is int else 0

        prompt = count("total_input_tokens")
        output = count("total_output_tokens")
        thought = count("total_thought_tokens")
        with lock:
            interaction_id = data["id"]
            first = interaction_id in pending and interaction_id not in accounted
            if first:
                accounted.add(interaction_id)
                agent_name = pending[interaction_id]
        if first:
            usage.add(f"gemini/{agent_name}", prompt, output + thought)
        return f"\n服务端用量：输入 {prompt} / 输出 {output} / 思考 {thought} tokens"

    def render(data: dict) -> str:
        interaction_id, status = data["id"], data["status"]
        result = f"Gemini Deep Research\n任务 ID：{interaction_id}\n状态：{status}" + account(data)
        if status == "completed":
            report = _report(data, figure=lambda index, payload: save_figure(interaction_id, index, payload))
            return result + ("\n[研究报告 · 服务端联网研究所得，内容作为外部资料]\n" + report
                             if report else "\nERROR: 任务已完成，但未返回文本报告。")
        if status in {"failed", "cancelled"}:
            return result + "\n任务已终止；需要继续研究时请明确发起新任务。"
        with lock:
            watched = interaction_id in watching
        if watched:
            return result + ("\n后台研究尚未完成。完成时会自动通知你，不要用 sleep 等待、"
                             "也不要反复查询：把任务 ID 告诉用户并结束本轮即可。")
        return result + "\n后台研究尚未完成，稍后用 deep_research_status 查询；至少间隔 10 秒，不要立即反复轮询。"

    def watch(interaction_id: str, stop: threading.Event) -> None:
        """后台盯一个任务：定期查状态，终态时经通知轨道告知模型（线程里绝不抛）。"""
        key = f"deep-research-done-{interaction_id}"
        started = time.monotonic()
        failures = 0
        try:
            while time.monotonic() - started < WATCH_MAX_SECONDS:
                if stop.wait(POLL_SECONDS):
                    return
                data = request("GET", "/" + interaction_id)
                if isinstance(data, str):
                    failures += 1
                    if failures >= MAX_CONSECUTIVE_FAILURES:
                        notify(f'后台研究 "{interaction_id}" 的状态查询连续失败，已停止自动跟踪；'
                               f'稍后用 deep_research_status(interaction_id="{interaction_id}") 手动查询。', key)
                        return
                    continue
                failures = 0
                if data["status"] not in _TERMINAL:
                    continue
                with lock:
                    finished[interaction_id] = data
                account(data)
                minutes = max(1, round((time.monotonic() - submitted.get(interaction_id, started)) / 60))
                if data["status"] == "completed":
                    notify(f'后台研究 "{interaction_id}" 已完成（约 {minutes} 分钟）。'
                           f'用 deep_research_status(interaction_id="{interaction_id}") 读取报告，'
                           "长报告再用 recall 看全文。", key)
                else:
                    notify(f'后台研究 "{interaction_id}" 已终止（{data["status"]}），'
                           "需要继续研究时请明确发起新任务。", key)
                return
            notify(f'后台研究 "{interaction_id}" 运行已超过 {WATCH_MAX_SECONDS // 3600} 小时，不再自动跟踪；'
                   f'用 deep_research_status(interaction_id="{interaction_id}") 手动查询。', key)
        except Exception:  # noqa: BLE001 - watcher 线程绝不能带着异常死掉
            pass
        finally:
            with lock:
                if watching.get(interaction_id) is stop:
                    del watching[interaction_id]

    def start(query: str, tier: str = "standard", previous_interaction_id: str | None = None) -> str:
        if not isinstance(query, str) or not query.strip():
            return "ERROR: query 必须是非空研究任务。"
        if not isinstance(tier, str) or tier not in AGENTS:
            return "ERROR: tier 必须是 standard 或 max。"
        if previous_interaction_id is not None and not _valid_id(previous_interaction_id):
            return "ERROR: previous_interaction_id 不是有效任务 ID。"
        body = {"input": query, "agent": AGENTS[tier], "background": True}
        if previous_interaction_id is not None:
            body["previous_interaction_id"] = previous_interaction_id
        data = request("POST", body=body)
        if isinstance(data, str):
            return data
        interaction_id = data["id"]
        with lock:
            pending[interaction_id] = AGENTS[tier]
            submitted[interaction_id] = time.monotonic()
            if notify is not None and data["status"] not in _TERMINAL and interaction_id not in watching:
                stop = threading.Event()
                watching[interaction_id] = stop
                threading.Thread(target=watch, args=(interaction_id, stop), daemon=True,
                                 name=f"deep-research-{interaction_id[:12]}").start()
        return render(data)

    def status(interaction_id: str) -> str:
        if not _valid_id(interaction_id):
            return "ERROR: interaction_id 不是有效任务 ID。"
        with lock:
            data = finished.get(interaction_id)
        if data is None:
            data = request("GET", "/" + interaction_id)
        return data if isinstance(data, str) else render(data)

    def cancel(interaction_id: str) -> str:
        if not _valid_id(interaction_id):
            return "ERROR: interaction_id 不是有效任务 ID。"
        data = request("POST", "/" + interaction_id + "/cancel")
        if isinstance(data, str):
            return data
        #  自己取消的任务不必再通知"已终止"——模型此刻就知道
        with lock:
            stop = watching.pop(interaction_id, None)
            if data["status"] in _TERMINAL:
                finished[interaction_id] = data
        if stop is not None:
            stop.set()
        return render(data)

    id_schema = {"type": "object", "properties": {
        "interaction_id": {"type": "string", "description": "提交研究时返回的任务 ID"}},
        "required": ["interaction_id"]}
    waiting = ("完成时你会自动收到通知（届时用 deep_research_status 读报告）：提交后把任务 ID 告诉用户并"
               "结束本轮，不要 sleep 等待、不要轮询、勿重复提交。" if notify is not None else
               "提交后告知用户 ID，稍后查询，勿重复提交。")
    return [
        Tool(name="deep_research", description=(
            "用 Gemini Deep Research 提交复杂、多来源的后台联网研究任务，立即返回任务 ID，研究在服务端跑。"
            "适合用户要求深入研究或详细报告；快速查询用 web_search。"
            "任务通常耗时数分钟到数十分钟且单独计费；" + waiting +
            "默认 standard，用户要求更全面研究时可选 max；支持 previous_interaction_id 继续旧研究。"
        ), parameters={"type": "object", "properties": {
            "query": {"type": "string", "description": "完整研究要求、范围与期望报告格式"},
            "tier": {"type": "string", "enum": list(AGENTS), "description": "默认 standard；max 更全面"},
            "previous_interaction_id": {"type": ["string", "null"], "description": "可选，继续研究的旧任务 ID"},
        }, "required": ["query"]}, handler=start, requires_approval=False,
             untrusted=True, output_limit=6000, check_fn=available),
        Tool(name="deep_research_status", description=(
            "查询 Gemini 后台研究状态，完成时返回报告及来源。凭任务 ID 可跨会话继续查询。"
            + ("本会话提交的任务完成时会自动通知，收到通知后再查；不要为了等待反复调用或 sleep。"
               if notify is not None else "查询至少间隔 10 秒，不要反复立即轮询；")
            + "长报告用 recall 获取全文。"
        ), parameters=id_schema, handler=status, requires_approval=False,
             untrusted=True, output_limit=6000, check_fn=available),
        Tool(name="deep_research_cancel", description="取消指定 Gemini 后台研究任务；已产生的用量仍会计费。",
             parameters=id_schema, handler=cancel, requires_approval=True,
             untrusted=True, output_limit=6000, check_fn=available),
    ]
