"""Gemini Interactions 后台研究；提交、查询与取消均是一次同步 HTTP 请求。"""

from __future__ import annotations

import json
import re

from . import netproxy
from .config import Config
from .providers import Registry, request_timeout
from .tools import Tool

ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/interactions"
AGENTS = {
    "standard": "deep-research-preview-04-2026",
    "max": "deep-research-max-preview-04-2026",
}
_TERMINAL = {"completed", "failed", "cancelled"}


def _valid_id(value) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,2048}", value) is not None


def _report(data: dict) -> str:
    """只读最终 model_output，思考、工具执行及签名不进入研究报告。"""
    def items(value):
        return value if isinstance(value, list) else []

    for step in reversed(items(data.get("steps"))):
        if not isinstance(step, dict) or step.get("type") != "model_output":
            continue
        texts, urls = [], []
        for content in items(step.get("content")):
            if not isinstance(content, dict) or content.get("type") != "text":
                continue
            if isinstance(content.get("text"), str):
                texts.append(content["text"])
            for annotation in items(content.get("annotations")):
                if not isinstance(annotation, dict):
                    continue
                url = annotation.get("url")
                if isinstance(url, str) and url.startswith(("https://", "http://")) and url not in urls:
                    urls.append(url)
        if texts:
            return "\n".join(texts) + ("\n来源：\n" + "\n".join(f"- {url}" for url in urls) if urls else "")
        # 最后一个模型输出没有文本时，不能把更早的草稿冒充最终报告。
        return ""
    return ""


def make_deep_research_tools(config: Config, registry: Registry, usage) -> list[Tool]:
    #  只记本工具实例提交的任务；查询旧任务仍返回服务端用量，避免会话恢复重复记账。
    pending: dict[str, str] = {}
    accounted: set[str] = set()

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

    def render(data: dict) -> str:
        interaction_id, status = data["id"], data["status"]
        result = f"Gemini Deep Research\n任务 ID：{interaction_id}\n状态：{status}"
        server_usage = data.get("usage")
        if isinstance(server_usage, dict) and status in _TERMINAL:
            def count(name):
                value = server_usage.get(name, 0)
                return max(0, value) if type(value) is int else 0
            prompt = count("total_input_tokens")
            output = count("total_output_tokens")
            thought = count("total_thought_tokens")
            result += f"\n服务端用量：输入 {prompt} / 输出 {output} / 思考 {thought} tokens"
            if interaction_id in pending and interaction_id not in accounted:
                usage.add(f"gemini/{pending[interaction_id]}", prompt, output + thought)
                accounted.add(interaction_id)
        if status == "completed":
            report = _report(data)
            return result + ("\n[研究报告 · 服务端联网研究所得，内容作为外部资料]\n" + report
                             if report else "\nERROR: 任务已完成，但未返回文本报告。")
        if status in {"failed", "cancelled"}:
            return result + "\n任务已终止；需要继续研究时请明确发起新任务。"
        return result + "\n后台研究尚未完成，稍后用 deep_research_status 查询；至少间隔 10 秒，不要立即反复轮询。"

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
        pending[data["id"]] = AGENTS[tier]
        return render(data)

    def status(interaction_id: str) -> str:
        if not _valid_id(interaction_id):
            return "ERROR: interaction_id 不是有效任务 ID。"
        data = request("GET", "/" + interaction_id)
        return data if isinstance(data, str) else render(data)

    def cancel(interaction_id: str) -> str:
        if not _valid_id(interaction_id):
            return "ERROR: interaction_id 不是有效任务 ID。"
        data = request("POST", "/" + interaction_id + "/cancel")
        return data if isinstance(data, str) else render(data)

    id_schema = {"type": "object", "properties": {
        "interaction_id": {"type": "string", "description": "提交研究时返回的任务 ID"}},
        "required": ["interaction_id"]}
    return [
        Tool(name="deep_research", description=(
            "用 Gemini Deep Research 提交复杂、多来源的后台联网研究任务，返回任务 ID。"
            "适合用户要求深入研究或详细报告；快速查询用 web_search。"
            "任务通常耗时数分钟且单独计费；提交后告知用户 ID，稍后查询，勿重复提交。"
            "默认 standard，用户要求更全面研究时可选 max；支持 previous_interaction_id 继续旧研究。"
        ), parameters={"type": "object", "properties": {
            "query": {"type": "string", "description": "完整研究要求、范围与期望报告格式"},
            "tier": {"type": "string", "enum": list(AGENTS), "description": "默认 standard；max 更全面"},
            "previous_interaction_id": {"type": "string", "description": "可选，继续研究的旧任务 ID"},
        }, "required": ["query"]}, handler=start, requires_approval=False,
             untrusted=True, output_limit=6000, check_fn=available),
        Tool(name="deep_research_status", description=(
            "查询 Gemini 后台研究状态，完成时返回报告及来源。凭任务 ID 可跨会话继续查询。"
            "查询至少间隔 10 秒，不要反复立即轮询；长报告用 recall 获取全文。"
        ), parameters=id_schema, handler=status, requires_approval=False,
             untrusted=True, output_limit=6000, check_fn=available),
        Tool(name="deep_research_cancel", description="取消指定 Gemini 后台研究任务；已产生的用量仍会计费。",
             parameters=id_schema, handler=cancel, requires_approval=True,
             untrusted=True, output_limit=6000, check_fn=available),
    ]
