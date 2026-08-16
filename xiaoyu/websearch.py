"""web_search 工具：借厂商 Responses 接口的内置联网搜索。

为什么做成工具而不是把主循环切到 Responses 协议：
- deepseek 的 Responses 目前只支持 deepseek-v4-flash（v4-pro 计划中），
  切协议默认模型当场不可用；
- 内核的消息形态（tool_calls 配对不变量、压缩、网关同名兜底）全是 chat-completions
  形态，同一份历史没法在两种 wire format 之间无缝切换，双 transport 不值；
- 做成工具后**任何主模型**都能用上联网搜索，且搜索模型独立于主模型选择。

一次性调用：把查询交给搜索后端（模型 + 服务端 web_search），拿回带来源的结论。
搜索、抓取、筛选全在厂商服务端发生，本地不落任何中间结果。

后端按 XIAOYU_SEARCH_PROVIDER 选（config.search_provider）。两家请求形态相同
（/responses + tools:[{"type":"web_search"}]），差在质量与价格——2026-08 五题
对比实测：grok-4.5 正确性 5/5、每题都带结构化引用 URL，但单次约 0.65 元；
deepseek-v4-flash 4/5（时效敏感题失手）、无结构化引用，单次约 0.02 元。
（xai 侧 2026-08-13 起由 grok-4.5 换成同代升级款 grok-4.6，形态与价位不变。）
"""

from __future__ import annotations

from dataclasses import dataclass

from . import ui
from .config import Config
from .events import Notice, UISink
from .providers import PRESETS, Registry
from .render import PlainSink
from .tools import Tool


@dataclass(frozen=True)
class SearchBackend:
    """一个可用的搜索后端：registry 里的 provider 名 + 跑搜索的模型。"""

    provider: str
    model: str


#  模型选各家里"够用且便宜"的档：搜索是有界辅助任务，不需要旗舰。
#  ⚠️ deepseek 的 Responses 目前只认 flash——v4-pro 上线后也未必要换。
SEARCH_BACKENDS: dict[str, SearchBackend] = {
    "deepseek": SearchBackend("deepseek", "deepseek-v4-flash"),
    "xai": SearchBackend("xai", "grok-4.6"),
}

#  搜索结论太长就挤占主上下文，与 explore 同一约束
MAX_ANSWER_CHARS = 4000

SEARCH_INSTRUCTIONS = (
    "你是联网搜索助手，用 web_search 查证问题后回答。要求："
    "结论先行；每个关键事实在正文中标注来源（站点名或 URL）；"
    "时效性信息以搜索结果为准，不要用你的训练知识兜底；"
    "查不到就明说查不到，说明搜了什么关键词，不要编。回答保持简洁。"
)


def make_web_search_tool(
    config: Config, registry: Registry, usage, sink: UISink | None = None
) -> Tool:
    """造一个 web_search 工具挂到主 agent 上。usage 记在父级同一本账上；
    client 走 registry 的缓存（与该 provider 的主对话复用同一连接池）。
    """
    sink = sink or PlainSink()

    def _backend() -> SearchBackend | None:
        return SEARCH_BACKENDS.get(config.search_provider)

    def web_search(query: str) -> str:
        backend = _backend()
        if backend is None:
            return (
                f"ERROR: XIAOYU_SEARCH_PROVIDER={config.search_provider!r} 不认识，"
                f"可选：{'、'.join(SEARCH_BACKENDS)}。请提示用户改配置。"
            )
        if registry.get(backend.provider) is None:
            key_hint = ""
            if preset := PRESETS.get(backend.provider):
                key_hint = f"（缺 {preset.key_envs[0]}）"
            return (
                f"ERROR: 未配置 {backend.provider} 直连{key_hint}，联网搜索不可用。"
                "请改用其它途径，或提示用户配置。"
            )
        sink.emit(Notice(f"  🌐 web_search（{backend.model}）：{ui.preview(query, 90)}"))

        client = registry.client(backend.provider)
        try:
            response = client.responses.create(
                model=backend.model,
                instructions=SEARCH_INSTRUCTIONS,
                input=query,
                tools=[{"type": "web_search"}],
            )
        except Exception as exc:  # noqa: BLE001 - 搜索失败不该打断主流程
            return (
                f"ERROR: 联网搜索失败（{type(exc).__name__}: {exc}）。"
                "可以换个问法重试一次；再失败就基于已有信息继续，并向用户说明。"
            )

        #  Responses 的 usage 字段名与 chat completions 不同（input/output_tokens）
        if resp_usage := getattr(response, "usage", None):
            usage.add(
                f"{backend.provider}/{backend.model}",
                int(getattr(resp_usage, "input_tokens", 0) or 0),
                int(getattr(resp_usage, "output_tokens", 0) or 0),
            )

        answer = (getattr(response, "output_text", "") or "").strip()
        if not answer:
            return "联网搜索没有返回内容。请换个问法，或基于已有信息继续。"

        #  引用两处都收：顶层 citations（xai 形态）+ url_citation 注解（标准形态，
        #  deepseek 实测常为空，来源多写在正文里）——有就附上
        if sources := _citations(response):
            answer += "\n来源：\n" + "\n".join(f"- {item}" for item in sources)
        if len(answer) > MAX_ANSWER_CHARS:
            answer = answer[:MAX_ANSWER_CHARS] + "\n…（结论过长已截断）"
        return f"[联网搜索结论 · 由 {backend.model} 服务端搜索得出，时效信息以此为准]\n{answer}"

    return Tool(
        name="web_search",
        description=(
            "联网搜索并返回带来源的结论。适合：查时效性信息（版本号、新闻、价格、"
            "文档更新）、核实你不确定的事实、找报错信息的解法。"
            "查询要具体，像对搜索引擎提问一样。"
            "它只读互联网，不访问本地文件；查代码库内部的问题用 explore。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "要查证的问题或搜索词，越具体越好",
                }
            },
            "required": ["query"],
        },
        handler=web_search,
        #  只读互联网、不改本地；查询发往用户自己配了 key 的厂商官方端点，
        #  信任级别与主对话相同，逐次确认只会让模型不用它。
        requires_approval=False,
        #  选中的后端没配好（名字不对/缺 key）就不进 schemas
        #  （handler 里再兜一次底，防注册后 key 失效，且错误文案更具体）
        check_fn=lambda: (b := _backend()) is not None
        and registry.get(b.provider) is not None,
    )


def _citations(response) -> list[str]:
    """收集引用 URL，去重保序。字段全部防御式访问——两家的兼容实现
    不保证每个响应都带引用：xai 给顶层 citations 列表，标准形态是
    output 里的 url_citation 注解。"""
    found: list[str] = []
    for url in getattr(response, "citations", None) or []:
        if isinstance(url, str) and url and url not in found:
            found.append(url)
    for item in getattr(response, "output", None) or []:
        for part in getattr(item, "content", None) or []:
            for ann in getattr(part, "annotations", None) or []:
                if getattr(ann, "type", "") != "url_citation":
                    continue
                url = getattr(ann, "url", "") or ""
                if not url or url in found:
                    continue
                found.append(url)
    return found
