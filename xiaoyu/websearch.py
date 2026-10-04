"""web_search 工具：借厂商接口的内置联网搜索。

为什么做成工具而不是把主循环切到 Responses 协议：
- 各家 Responses 支持面参差（上一代 deepseek-v4-pro 就没开放），切协议会让
  部分主模型当场不可用；
- 内核的消息形态（tool_calls 配对不变量、压缩、网关同名兜底）全是 chat-completions
  形态，同一份历史没法在两种 wire format 之间无缝切换，双 transport 不值；
- 做成工具后**任何主模型**都能用上联网搜索，且搜索模型独立于主模型选择。

一次性调用：把查询交给搜索后端（模型 + 服务端 web_search），拿回带来源的结论。
搜索、抓取、筛选全在厂商服务端发生，本地不落任何中间结果。

后端按 XIAOYU_SEARCH_PROVIDER 选（config.search_provider）：默认 deepseek-flash
走 Anthropic 兼容接口，xai/grok-4.7 与 bedrock/openai.gpt-5.6-luna 走 Responses。
DeepSeek 的 Responses 接口忽略内置搜索，搜索必须单独走 /anthropic；
不改变主对话的协议。2026-10-03 实测返回服务端搜索调用与结果块。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from . import ui
from .config import Config
from .events import Notice, UISink
from .mcp import _redact
from .providers import DEFAULT_BEDROCK_REGION, IAM_PLACEHOLDER_KEY, PRESETS, Registry, request_timeout
from .render import PlainSink
from .tools import Tool


@dataclass(frozen=True)
class SearchBackend:
    """一个可用的搜索后端：registry 里的 provider 名 + 跑搜索的模型。"""

    provider: str
    model: str


#  模型选各家里"够用且便宜"的档：搜索是有界辅助任务，不需要旗舰。
SEARCH_BACKENDS: dict[str, SearchBackend] = {
    "deepseek": SearchBackend("deepseek", "deepseek-flash"),
    "xai": SearchBackend("xai", "grok-4.7"),
    "bedrock": SearchBackend("bedrock", "openai.gpt-5.6-luna"),
}


def search_command(config: Config, registry: Registry, args: str) -> str:
    """会话内查看或切换后端；只改内存配置，不探测网络、不改持久配置。"""
    parts = args.split()
    if not parts:
        state = "" if config.enable_web_search else "（联网搜索已禁用）"
        lines = [f"当前搜索后端：{config.search_provider}{state}"]
        for name, backend in SEARCH_BACKENDS.items():
            available = "已配置" if registry.get(backend.provider) is not None else "未配置"
            lines.append(f"  {name} · {backend.model} · {available}")
        lines.append("用 /search deepseek|xai|bedrock 切换，仅当前会话生效。")
        return "\n".join(lines)
    if len(parts) != 1 or parts[0] not in SEARCH_BACKENDS:
        return "用法：/search [deepseek|xai|bedrock]；原搜索后端未改变。"
    if not config.enable_web_search:
        return "联网搜索已禁用；需先启用 XIAOYU_ENABLE_WEB_SEARCH，原搜索后端未改变。"
    name = parts[0]
    backend = SEARCH_BACKENDS[name]
    if registry.get(backend.provider) is None:
        hint = ("XIAOYU_BEDROCK_REGION 与 AWS 凭证链，或 AWS_BEARER_TOKEN_BEDROCK"
                if name == "bedrock" else PRESETS[backend.provider].key_envs[0])
        return f"未配置 {name}（需要 {hint}）；原搜索后端未改变。"
    config.search_provider = name
    return f"已切换搜索后端：{name}（{backend.model}），下一次搜索生效，仅当前会话。"

#  搜索结论太长就挤占主上下文，与 explore 同一约束
MAX_ANSWER_CHARS = 4000

SEARCH_INSTRUCTIONS = (
    "你是联网搜索助手，用 web_search 查证问题后回答。要求："
    "结论先行；每个关键事实在正文中标注来源（站点名或 URL）；"
    "时效性信息以搜索结果为准，不要用你的训练知识兜底；"
    "查不到就明说查不到，说明搜了什么关键词，不要编。回答保持简洁。"
    "优先官方或一手来源。天气等时效信息核对地点、日期与发布时间，"
    "以同一官方来源的数据为准；不同来源不一致时明确标注，不拼成一份实况。"
)


def make_web_search_tool(
    config: Config, registry: Registry, usage, sink: UISink | None = None
) -> Tool:
    """造一个 web_search 工具挂到主 agent 上。usage 记在父级同一本账上；
    xai 复用 registry 的 client；DeepSeek 搜索单独使用 Messages client，调用后关闭。
    """
    sink = sink or PlainSink()

    def _backend() -> SearchBackend | None:
        return SEARCH_BACKENDS.get(config.search_provider)

    def web_search(query: str) -> str:
        if not config.enable_web_search:
            return "ERROR: 联网搜索已禁用（XIAOYU_ENABLE_WEB_SEARCH）。"
        if not isinstance(query, str) or not query.strip():
            return "ERROR: query 必须是非空搜索词。"
        backend = _backend()
        if backend is None:
            return (
                f"ERROR: XIAOYU_SEARCH_PROVIDER={config.search_provider!r} 不认识，"
                f"可选：{'、'.join(SEARCH_BACKENDS)}。请提示用户改配置。"
            )
        if registry.get(backend.provider) is None:
            key_hint = ""
            if backend.provider == "bedrock":
                key_hint = "（设置 XIAOYU_BEDROCK_REGION 并配置 AWS 凭证链，或 AWS_BEARER_TOKEN_BEDROCK）"
            elif preset := PRESETS.get(backend.provider):
                key_hint = f"（缺 {preset.key_envs[0]}）"
            return (
                f"ERROR: 未配置 {backend.provider} 直连{key_hint}，联网搜索不可用。"
                "请改用其它途径，或提示用户配置。"
            )
        sink.emit(Notice(f"  🌐 web_search（{backend.model}）：{ui.preview(query, 90)}"))

        try:
            instructions = f"当前日期：{date.today().isoformat()}。{SEARCH_INSTRUCTIONS}"
            if backend.provider == "deepseek":
                from . import messages

                provider = registry.get(backend.provider)
                base = provider.base_url.rstrip("/").removesuffix("/v1")
                if not base.endswith("/anthropic"):
                    base += "/anthropic"
                with messages.client(base, provider.api_key, request_timeout(config.request_timeout)) as client:
                    response = client.messages.create(
                        model=backend.model,
                        system=instructions,
                        max_tokens=4096,
                        tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 3}],
                        messages=[{"role": "user", "content": query}],
                    )
            else:
                request = dict(model=backend.model, instructions=instructions, input=query,
                               tools=[{"type": "web_search"}])
                if backend.provider == "bedrock":
                    from . import bedrock_search

                    provider = registry.get(backend.provider)
                    key = None if provider.api_key == IAM_PLACEHOLDER_KEY else provider.api_key
                    request["tools"] = [{"type": "web_search", "external_web_access": False}]
                    request["max_output_tokens"] = 4096
                    with bedrock_search.client(provider.aws_region or DEFAULT_BEDROCK_REGION,
                                               key, request_timeout(config.request_timeout)) as client:
                        response = client.responses.create(**request)
                else:
                    response = registry.client(backend.provider).responses.create(**request)
        except Exception as exc:  # noqa: BLE001 - 搜索失败不该打断主流程
            return (
                f"ERROR: 联网搜索失败（{type(exc).__name__}: {_redact(str(exc))}）。"
                "可以换个问法重试一次；再失败就基于已有信息继续，并向用户说明。"
            )

        #  Responses 的 usage 字段名与 chat completions 不同（input/output_tokens）
        if resp_usage := getattr(response, "usage", None):
            prompt_tokens = int(getattr(resp_usage, "input_tokens", 0) or 0)
            if backend.provider == "deepseek":
                #  Messages 的缓存读写 token 不含在 input_tokens 里，与主对话计账一致。
                prompt_tokens += int(getattr(resp_usage, "cache_read_input_tokens", 0) or 0)
                prompt_tokens += int(getattr(resp_usage, "cache_creation_input_tokens", 0) or 0)
            usage.add(
                f"{backend.provider}/{backend.model}",
                prompt_tokens,
                int(getattr(resp_usage, "output_tokens", 0) or 0),
            )

        if backend.provider == "deepseek":
            answer, sources, errors = _messages_search_result(response)
            if getattr(response, "stop_reason", None) in ("pause_turn", "max_tokens"):
                return "ERROR: 联网搜索尚未完成，不能把当前内容作为完整结论。请缩小查询范围重试。"
            if not sources:
                detail = "、".join(errors) if errors else "未返回服务端搜索结果"
                return f"ERROR: 联网搜索未获得可核实来源（{detail}）。请重试或改用其它途径。"
            if errors and answer:
                answer += "\n[部分搜索失败：" + "、".join(errors) + "；以下结论仅基于已返回的来源。]"
        else:
            answer = (getattr(response, "output_text", "") or "").strip()
            sources = _citations(response)
            if backend.provider == "bedrock":
                if getattr(response, "status", "completed") != "completed":
                    return "ERROR: Bedrock 联网搜索未完成，请缩小查询范围重试。"
                if not sources or not any(getattr(item, "type", None) == "web_search_call"
                                          for item in getattr(response, "output", None) or []):
                    return "ERROR: Bedrock 未返回搜索调用与来源，不能把模型正文作为已核实的搜索结论。"
        if not answer:
            return "联网搜索没有返回内容。请换个问法，或基于已有信息继续。"

        if sources:
            answer += "\n来源：\n" + "\n".join(f"- {item}" for item in sources)
        return f"[联网搜索结论 · 由 {backend.model} 服务端搜索得出，时效信息以此为准]\n{answer}"

    return Tool(
        name="web_search",
        #  超出的部分落盘可召回，不直接切掉（来源列表排在结论最后）
        output_limit=MAX_ANSWER_CHARS + 500,
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
        untrusted=True,
        #  只读互联网、不改本地；查询发往用户自己配了 key 的厂商官方端点，
        #  信任级别与主对话相同，逐次确认只会让模型不用它。
        requires_approval=False,
        #  选中的后端没配好（名字不对/缺 key）就不进 schemas
        #  （handler 里再兜一次底，防注册后 key 失效，且错误文案更具体）
        check_fn=lambda: config.enable_web_search and (b := _backend()) is not None
        and registry.get(b.provider) is not None,
    )


def _messages_search_result(response) -> tuple[str, list[str], list[str]]:
    """只取最终正文和搜索结果，不把思考或加密内容回灌主模型。

    兼容接口的错误结果可能是字典，也可能混在结果列表里，逐块读取即可，
    不做整份响应序列化（SDK 对混合形态会报格式警告）。
    """
    def field(obj, name, default=None):
        return obj.get(name, default) if isinstance(obj, dict) else getattr(obj, name, default)

    texts, sources, errors = [], [], []
    for block in getattr(response, "content", None) or []:
        kind = field(block, "type")
        if kind == "text":
            text = field(block, "text", "")
            if isinstance(text, str) and text:
                texts.append(text)
        elif kind == "web_search_tool_result":
            content = field(block, "content", [])
            if not isinstance(content, (list, tuple)):
                content = [content]
            for result in content:
                if field(result, "type") == "web_search_tool_result_error":
                    code = str(field(result, "error_code", "未知错误"))
                    if code not in errors:
                        errors.append(code)
                elif field(result, "type") == "web_search_result":
                    url = field(result, "url")
                    if isinstance(url, str) and url and url not in sources:
                        sources.append(url)
    return "\n".join(texts).strip(), sources, errors


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
