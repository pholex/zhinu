"""把 OpenAI 的 Responses API 包成 chat completions 的形状。

**为什么必须有这一层**：gpt-5.6 线（sol / terra / luna）在 `/v1/chat/completions`
上「带 tools 就 400」，报错原文明确给了两条路——改走 `/v1/responses`，或者
`reasoning_effort='none'`。小羽是 agent，每一轮都带工具 schema，第二条等于把 5.6
永久降级成非推理档；所以只剩第一条。

**为什么不动内核**：`Registry.client()` 返回的本来就是鸭子型对象（调用面只有
`chat.completions.create`，见 providers.py 的注释），scripted 测试桩早就是第二只
鸭子。这里加的是第三只：外面看是 chat completions，里面说 Responses。于是

- `agent.messages` 仍是 chat-completions 形态（tool_calls 配对不变量、压缩切点、
  历史修复、会话日志、网关同名兜底全部照旧）；
- 同一份历史可以在两种 wire format 之间来回切——因为翻译发生在**发出去的那一刻**，
  不落进历史。websearch.py 当年判「双 transport 不值」，说的是让内核学第二种消息
  形态；翻译层不改内核形态，所以那个结论不适用于这里。

**reasoning item 回传**：这是 Responses 相对 chat completions 唯一的**能力增量**，
也是唯一需要内核配合的地方。推理模型在多轮工具循环里，若每轮都从零重新推理，
既烧 reasoning token 又容易在长任务里丢线索。`store=False` 下拿回它的办法是
`include=["reasoning.encrypted_content"]`——服务端把推理状态加密成一个不透明串
交给客户端保管，下一轮原样送回即可续上。

存放方式是**挂在 assistant 消息上的私有键 `_reasoning`**（不是另起一张边表）：
消息被压缩丢弃时它跟着一起走、会话日志落盘时它跟着一起存、fork/resume 天然带上，
一个"记得同步清理"的地方都不需要。代价是历史里多了一个只有 Responses 认识的键，
所以出网前必须摘掉——这正是 `Transport` 把 chat 协议也一并包住的原因：
出网口只有一个，就不存在"哪天新加一条路径忘了摘"。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterator

from . import media

#  协议名。也是通用兜底 provider 的 XIAOYU_PROVIDER_<NAME>_PROTOCOL 取值
CHAT = "chat"
RESPONSES = "responses"
ANTHROPIC = "anthropic"

#  "整家都说 Responses" 的写法。协议是**按型号**声明的，因为同一家常常只有部分
#  型号开了这条路——deepseek 就是 v4-flash 通、v4-pro 明确回"稍后开放"，
#  而 v4-pro 恰恰是默认主模型，按家切会当场切死。
WILDCARD = "*"

#  assistant 消息上的私有键：存这一轮的 reasoning item。下划线开头 = 内核私有，
#  绝不出网（见 strip_private）。值的形状是 {"model": 产出它的模型, "items": [...]}
REASONING_KEY = "_reasoning"

#  拿回 reasoning 状态所必须的 include。实测 xai / qwen / deepseek 的 Responses
#  端点都容得下这个参数（不支持推理的模型只是不回而已），所以无条件发。
_REASONING_INCLUDE = ["reasoning.encrypted_content"]


def strip_private(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """摘掉所有下划线私有键。没有私有键的消息原样复用，不做无谓的拷贝。

    为什么必须摘（2026-08-12 实测：openai / anthropic / deepseek / zhipu /
    moonshot / xai / qwen 的 chat 端点**目前都容忍**这个未知键，所以理由不是"会 400"）：

    1. **钱**——一个 reasoning item 的加密串实测约 1000 字符，对 chat 端点是纯废话，
       却要作为 input token 在**其后每一轮**重复计费。这是确定的浪费，不是假设；
    2. **各家宽严不一且会变**，赌"大家都宽容"没有好处；
    3. 私有键就该止于内核边界——历史是我们的数据结构，wire 是协议。
    """
    cleaned: list[dict[str, Any]] = []
    for message in messages:
        if any(key.startswith("_") for key in message):
            message = {k: v for k, v in message.items() if not k.startswith("_")}
        cleaned.append(message)
    return cleaned


# ---------- 请求方向：chat completions → Responses ----------


def to_input(
    messages: list[dict[str, Any]], model: str = "", provider: str = ""
) -> list[dict[str, Any]]:
    """messages → Responses 的 input 列表。

    形态差异只有四处，其余角色原样透传：
    1. assistant 的 `tool_calls` 不是消息的属性，而是**平级的 function_call item**；
    2. `role: tool` 的结果同样是平级 item（function_call_output）；
    3. 两者靠 `call_id` 配对——正好就是 chat 侧的 `tool_calls[].id` / `tool_call_id`，
       所以往返无损，不需要额外的 id 映射表；
    4. `_reasoning` 里的 reasoning item 还原到**这条消息的最前面**——服务端要求
       reasoning item 排在它引出的 message / function_call 之前，顺序反了就不认。

    reasoning 只在**产出它的那条路由**上回放（provider+model 都对上才回，
    任一对不上整段跳过）：加密串是模型/服务侧的私有状态，`/model` 切了模型
    不能塞回去；降级链让**同名模型跑在两家上**（直连 vs 网关），跨家回放
    同样是喂错东西。旧会话的存量 reasoning 没有 provider 标签，按不匹配
    处理（安全方向：丢 reasoning 永远无害，只是少省一点）。
    """
    items: list[dict[str, Any]] = []
    for message in messages:
        role = message.get("role")
        content = message.get("content")
        reasoning = message.get(REASONING_KEY) or {}
        if reasoning.get("model") == model and (reasoning.get("provider") or "") == provider:
            items.extend(reasoning.get("items") or [])
        if role == "tool":
            items.append(
                {
                    "type": "function_call_output",
                    "call_id": message.get("tool_call_id", ""),
                    "output": content or "",
                }
            )
            continue
        #  content 为 None 是合法的（只带 tool_calls 的 assistant），此时不发消息 item：
        #  Responses 不接受空 content 的 assistant 消息
        if content:
            items.append({"role": role, "content": _to_parts(role, content)})
        for call in message.get("tool_calls") or []:
            function = call.get("function") or {}
            items.append(
                {
                    "type": "function_call",
                    "call_id": call.get("id", ""),
                    "name": function.get("name", ""),
                    "arguments": function.get("arguments") or "{}",
                }
            )
    return items


def _to_parts(role: Any, content: Any) -> Any:
    """chat 的内容部件 → Responses 的内容部件。纯文本原样返回（不无谓地展开）。

    两边的部件名不同：`text` → `input_text` / `output_text`（按角色分，
    assistant 的历史正文必须是 output_text，用 input_text 会被拒），
    `image_url` 从"嵌套对象"拍平成"URL 字符串"。图片只可能出现在 user 侧。
    """
    if not isinstance(content, list):
        return content
    text_type = "output_text" if role == "assistant" else "input_text"
    parts: list[dict[str, Any]] = []
    for part in content:
        if not isinstance(part, dict):
            continue
        if part.get("type") == "image_url":
            url = (part.get("image_url") or {}).get("url", "")
            if url:
                parts.append({"type": "input_image", "image_url": url})
        else:
            parts.append({"type": text_type, "text": str(part.get("text") or "")})
    return parts


def to_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """工具 schema：chat 的嵌套 `{"function": {...}}` 拍平成 Responses 的平铺形态。

    `strict` 显式给 False：Responses 侧它默认为 True，而 True 要求 schema 满足
    结构化输出的全部约束（每个对象都要 `additionalProperties: false`、required 必须
    列全）。小羽的工具 schema 是按 chat 的宽松规则写的，不显式关掉会整批被拒。
    """
    converted: list[dict[str, Any]] = []
    for tool in tools:
        if tool.get("type") != "function" or "function" not in tool:
            #  已经是 Responses 形态（或 web_search 这类内置工具）：原样透传
            converted.append(tool)
            continue
        function = tool["function"]
        converted.append(
            {
                "type": "function",
                "name": function.get("name", ""),
                "description": function.get("description", ""),
                "parameters": function.get("parameters") or {},
                "strict": function.get("strict", False),
            }
        )
    return converted


#  chat completions 参数名 → Responses 参数名。没列出的原样透传：
#  与其静默丢弃一个调用方以为生效了的参数，不如让上游 400 出来。
_PARAM_ALIASES = {
    "max_completion_tokens": "max_output_tokens",
    "max_tokens": "max_output_tokens",
}


def to_request(
    model: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None,
    extra: dict[str, Any],
    provider: str = "",
) -> dict[str, Any]:
    """拼出 responses.create 的完整参数。

    `store=False`：与 chat completions 行为对齐（不在服务端留存这次对话）。
    Responses 默认 store=True，不显式关掉等于偷偷改变了用户的数据留存面。
    """
    request: dict[str, Any] = {
        "model": model,
        "input": to_input(messages, model, provider),
        "store": False,
        "include": _REASONING_INCLUDE,
    }
    if tools:
        request["tools"] = to_tools(tools)
    for key, value in extra.items():
        request[_PARAM_ALIASES.get(key, key)] = value
    return request


# ---------- 响应方向：Responses → chat completions ----------
#
#  下面这几个是刻意手写的最小鸭子型：只长出 agent._consume_stream 与 _summarize
#  真正读到的那几个属性。用 SDK 的 ChatCompletionChunk 反而要凑满一堆必填字段
#  （id/created/object/…），噪音大于收益。


@dataclass
class Function:
    name: str | None = None
    arguments: str | None = None


@dataclass
class ToolCallFragment:
    index: int
    id: str | None = None
    function: Function | None = None


@dataclass
class Delta:
    content: str | None = None
    tool_calls: list[ToolCallFragment] | None = None


@dataclass
class Choice:
    delta: Delta


@dataclass
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0


@dataclass
class Chunk:
    choices: list[Choice] = field(default_factory=list)
    usage: Usage | None = None
    #  chat 协议没有的东西，内核用 getattr 取（真 SDK 的 chunk 上取不到 = None）
    reasoning: list[dict[str, Any]] | None = None


@dataclass
class Message:
    content: str | None = None
    role: str = "assistant"
    tool_calls: list[dict[str, Any]] | None = None


@dataclass
class NonStreamChoice:
    message: Message


@dataclass
class Completion:
    choices: list[NonStreamChoice]
    usage: Usage | None = None


def _dump(item: Any) -> dict[str, Any]:
    """SDK 的 item → 纯 dict（原样回传用）。已经是 dict 的直接过。"""
    if isinstance(item, dict):
        return item
    if hasattr(item, "model_dump"):
        return item.model_dump(exclude_none=True)
    return dict(item)


def _replayable(item: dict[str, Any]) -> dict[str, Any] | None:
    """只留**能回放**的 reasoning item：带非空 `encrypted_content` 的那种。

    各家给的东西不一样（2026-08-12 实测）：openai / xai 给加密状态（回传能真的
    接上思路）；deepseek 给明文 `content`、qwen 给明文 `summary`，都没有加密串。
    明文那种不留，两个理由：

    1. **回传了也没用**——实测把明文 reasoning item 塞回去，input_tokens 只从
       97 涨到 101，服务端根本没吃它；
    2. **不该落盘**——那是模型的原始思维链，存进会话日志只是让日志变大。

    于是"哪家该开回传"这个判断不需要写死名单：给得出加密状态的自然生效，
    给不出的自然为空，日后哪家补上了也自动跟着生效。
    """
    return item if item.get("encrypted_content") else None


def _usage(raw: Any) -> Usage | None:
    """Responses 的 usage 字段名与 chat 不同（input/output_tokens）。"""
    if raw is None:
        return None
    return Usage(
        prompt_tokens=getattr(raw, "input_tokens", 0) or 0,
        completion_tokens=getattr(raw, "output_tokens", 0) or 0,
    )


def _text_chunk(text: str) -> Chunk:
    return Chunk(choices=[Choice(Delta(content=text))])


def _tool_chunk(index: int, **kwargs: Any) -> Chunk:
    return Chunk(choices=[Choice(Delta(tool_calls=[ToolCallFragment(index, **kwargs)]))])


def stream_chunks(events: Iterator[Any]) -> Iterator[Chunk]:
    """Responses 事件流 → chat completions chunk 流。

    分片下标用 `output_index`：Responses 的 output 是一串 item（reasoning、message、
    function_call 混排），function_call 的 output_index 天然唯一且保序，正好当
    `tool_calls[].index` 使——内核那边按 index 攒分片、按 index 排序，语义一致。

    reasoning / 内置工具等其它事件一律忽略：内核没有展示位，多一路事件只会让
    「模型说了什么」这件事变得不确定。
    """
    for event in events:
        kind = getattr(event, "type", "")
        if kind == "response.output_text.delta":
            yield _text_chunk(event.delta)
        elif kind == "response.output_item.added":
            item = event.item
            if getattr(item, "type", "") == "function_call":
                #  name 在 item 加进来时就给全了（chat 侧也是一次给全），
                #  arguments 随后分片累加
                yield _tool_chunk(
                    event.output_index,
                    id=getattr(item, "call_id", "") or "",
                    function=Function(name=getattr(item, "name", "") or "", arguments=""),
                )
        elif kind == "response.output_item.done":
            item = getattr(event, "item", None)
            if getattr(item, "type", "") == "reasoning":
                #  必须等 done：added 时 encrypted_content 还是半截的（实测 932 vs
                #  1124 字符）。存 dict 而不是 pydantic 对象——会话日志要能 json 落盘
                if dumped := _replayable(_dump(item)):
                    yield Chunk(reasoning=[dumped])
        elif kind == "response.function_call_arguments.delta":
            yield _tool_chunk(
                event.output_index, function=Function(arguments=event.delta)
            )
        elif kind in ("response.completed", "response.incomplete"):
            #  usage 只在收尾事件里给一次，choices 留空——内核见空 choices 就跳过，
            #  和 OpenAI chat 流最后那个纯 usage chunk 形状一致。
            #  incomplete（截断、内容过滤）同样走这里而不是抛异常：chat completions
            #  遇到 finish_reason=length 也是正常返回半截正文，抛了反而把已经吐出来的
            #  内容全丢掉——两种协议在这一点上必须表现一致
            yield Chunk(usage=_usage(getattr(event.response, "usage", None)))
        elif kind in ("response.failed", "error"):
            #  真失败才抛。落到 errors.classify 多半是 fatal（没有 HTTP 状态码可判），
            #  于是不换模型、直接报到 REPL——会话保留，用户重发即可。把上游原文带上，
            #  是因为这条路径上"到底哪儿炸了"只有这一句话可看
            raise RuntimeError(f"Responses 流失败：{_failure(event)}")


def _failure(event: Any) -> str:
    if message := getattr(event, "message", None):  # error 事件自带 message
        return str(message)
    response = getattr(event, "response", None)
    for attr in ("error", "incomplete_details"):
        if detail := getattr(response, attr, None):
            return str(getattr(detail, "message", None) or getattr(detail, "reason", detail))
    return str(getattr(event, "type", "unknown"))


def to_completion(response: Any) -> Completion:
    """非流式响应 → chat completions 形状（`_summarize` 走这条路）。"""
    return Completion(
        choices=[NonStreamChoice(Message(content=getattr(response, "output_text", "") or ""))],
        usage=_usage(getattr(response, "usage", None)),
    )


# ---------- 鸭子型 client ----------


class _Completions:
    def __init__(
        self,
        inner: Any,
        speaks_responses: Any,
        speaks_anthropic: Any,
        anthropic_client: Any,
        provider: str = "",
        text_tools: Any = None,
    ) -> None:
        self._inner = inner
        self._speaks_responses = speaks_responses
        self._speaks_anthropic = speaks_anthropic
        #  型号 → 是否走文本工具协议（见 textcalls.py）。None = 全部原生
        self._text_tools = text_tools or (lambda _model: False)
        #  零参 callable：懒构造并缓存（见 Transport.anthropic_client）
        self._anthropic_client = anthropic_client
        #  本传输层所属的 provider：reasoning 回放判定要用（provider+model
        #  都对上才回，见 to_input 的注释）
        self._provider = provider

    def create(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        stream: bool = False,
        stream_options: dict[str, Any] | None = None,
        tools: list[dict[str, Any]] | None = None,
        **extra: Any,
    ) -> Any:
        #  媒体引用展开成 data URL 也在这一层，和私有键净化同一个理由：出网口只有
        #  一个，"记得展开"就不必成为一条要人记住的纪律。三条协议都要，所以在分支之前
        messages = media.inline(messages)
        #  文本工具协议（textcalls.py）包在协议分派**外面**：它改的是消息内容与
        #  tools 的有无，不挑 wire protocol——哪条协议都能跑。出网前把历史改写成
        #  纯文本、tools 拿掉；回来时把正文里的调用块翻回 tool_calls 分片
        if self._text_tools(model):
            from . import textcalls

            messages = textcalls.to_text_messages(messages, tools)
            result = self._dispatch(model, messages, stream, stream_options, None, extra)
            return textcalls.stream_chunks(result) if stream else textcalls.to_completion(result)
        return self._dispatch(model, messages, stream, stream_options, tools, extra)

    def _dispatch(
        self,
        model: str,
        messages: list[dict[str, Any]],
        stream: bool,
        stream_options: dict[str, Any] | None,
        tools: list[dict[str, Any]] | None,
        extra: dict[str, Any],
    ) -> Any:
        if self._speaks_anthropic(model):
            #  ⚠️ 和 Responses 一路同理，**不能**先 strip_private：`_reasoning`
            #  正是 to_request 要消费的东西。净化是结构性的——逐块重建，
            #  私有键没有出口。懒 import：不配 Claude 直连的进程不付这份成本
            from . import messages as anthro

            request = anthro.to_request(model, messages, tools, stream, extra, self._provider)
            client = self._anthropic_client()
            if not stream:
                return anthro.to_completion(client.messages.create(**request))
            return anthro.stream_chunks(client.messages.create(stream=True, **request))
        if not self._speaks_responses(model):
            #  这里是内核私有键唯一的净化点（为什么必须摘：见 strip_private）
            return self._chat(model, strip_private(messages), stream, stream_options, tools, extra)
        #  ⚠️ Responses 这一路**不能**先 strip：`_reasoning` 正是 to_input 要消费的
        #  东西，先摘掉等于回传功能整个失效（而且不报错，只是悄悄变慢变贵）。
        #  它的净化是结构性的——to_input 逐字段重建 item，私有键根本没有出口。
        #  stream_options 只有 include_usage 一个用途，Responses 的 usage 一定随
        #  response.completed 回来，不需要开关；吃掉即可
        request = to_request(model, messages, tools, extra, self._provider)
        if not stream:
            return to_completion(self._inner.responses.create(**request))
        return stream_chunks(iter(self._inner.responses.create(stream=True, **request)))

    def _chat(
        self,
        model: str,
        messages: list[dict[str, Any]],
        stream: bool,
        stream_options: dict[str, Any] | None,
        tools: list[dict[str, Any]] | None,
        extra: dict[str, Any],
    ) -> Any:
        """原路直发。可选参数为空就**不传**，而不是传 None——
        真 SDK 把显式 None 当"发个 null 上去"，有的端点会因此 400。"""
        request: dict[str, Any] = {"model": model, "messages": messages, **extra}
        if stream:
            request["stream"] = True
        if stream_options is not None:
            request["stream_options"] = stream_options
        if tools is not None:
            request["tools"] = tools
        return self._inner.chat.completions.create(**request)


class _Chat:
    def __init__(
        self,
        inner: Any,
        speaks_responses: Any,
        speaks_anthropic: Any,
        anthropic_client: Any,
        provider: str = "",
        text_tools: Any = None,
    ) -> None:
        self.completions = _Completions(
            inner, speaks_responses, speaks_anthropic, anthropic_client, provider, text_tools
        )


class Transport:
    """**唯一的出网口**：外面看是 OpenAI client，里面按型号决定说哪种协议。

    chat 协议也包（而不是"只包 responses、chat 用裸 client"）：净化私有键这件事
    必须是结构性的。留一条不经过这里的出网路径，就等于把"记得摘掉 `_reasoning`"
    变成一条需要人记住的纪律——而它一旦被忘掉，症状是**每轮多付一千字符的钱、
    不报任何错**，最不容易被发现的那一类。

    第三条协议（Anthropic Messages，见 messages.py）的 client 是懒构造的：
    工厂由 providers.Registry 注入，首次遇到 Messages 协议请求才建、建完缓存。

    其余属性（`responses`、`models`…）直接委托给被包的真 client：
    web_search 工具就是拿 `client.responses.create` 直接发的，不能被这层挡住。
    """

    def __init__(
        self,
        inner: Any,
        responses_models: tuple[str, ...] = (),
        anthropic_models: tuple[str, ...] = (),
        anthropic_factory: Any = None,
        provider: str = "",
        text_tool_models: tuple[str, ...] = (),
    ) -> None:
        self._inner = inner
        self.responses_models = tuple(responses_models)
        self.anthropic_models = tuple(anthropic_models)
        #  走文本工具协议的型号（`*` = 整家）。与 wire protocol 正交：它说的是
        #  "这个型号不会 function calling"，不是"说哪种协议"。见 textcalls.py
        self.text_tool_models = tuple(text_tool_models)
        self._anthropic_factory = anthropic_factory
        self._anthropic: Any = None
        self.chat = _Chat(
            inner,
            self.speaks_responses,
            self.speaks_anthropic,
            self.anthropic_client,
            provider,
            self.uses_text_tools,
        )

    def uses_text_tools(self, model: str) -> bool:
        """这个型号的工具调用走不走文本协议。声明纪律同 responses_models。"""
        return WILDCARD in self.text_tool_models or model in self.text_tool_models

    def tool_mode_for(self, model: str) -> str:
        from .textcalls import NATIVE, TEXT

        return TEXT if self.uses_text_tools(model) else NATIVE

    def speaks_responses(self, model: str) -> bool:
        """这个型号走不走 Responses。裸名比对——全限定名在解析时早已剥掉前缀。"""
        return WILDCARD in self.responses_models or model in self.responses_models

    def speaks_anthropic(self, model: str) -> bool:
        """这个型号走不走 Anthropic Messages。声明纪律同 responses_models。"""
        return WILDCARD in self.anthropic_models or model in self.anthropic_models

    def protocol_for(self, model: str) -> str:
        if self.speaks_anthropic(model):
            return ANTHROPIC
        return RESPONSES if self.speaks_responses(model) else CHAT

    def anthropic_client(self) -> Any:
        """懒构造 + 缓存。没配工厂却声明了 anthropic_models 是装配 bug，
        当场炸出来比发一个必然失败的请求好排查。"""
        if self._anthropic is None:
            if self._anthropic_factory is None:  # pragma: no cover - 装配错误
                raise RuntimeError("该 provider 未注入 anthropic client 工厂")
            self._anthropic = self._anthropic_factory()
        return self._anthropic

    def with_options(self, **kwargs: Any) -> "Transport":
        #  超时等选项要落到内层，但返回的仍须是包过的——否则 with_options 之后
        #  就悄悄退回裸 client 了。协议表和工厂都要带上（懒 client 在副本上重建，
        #  可接受：with_options 只用于通配 provider 的 models.list 探测）
        return Transport(
            self._inner.with_options(**kwargs),
            self.responses_models,
            self.anthropic_models,
            self._anthropic_factory,
            text_tool_models=self.text_tool_models,
        )

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def wrap(
    client: Any,
    responses_models: tuple[str, ...],
    anthropic_models: tuple[str, ...] = (),
    anthropic_factory: Any = None,
    provider: str = "",
    text_tool_models: tuple[str, ...] = (),
) -> Transport:
    return Transport(
        client, responses_models, anthropic_models, anthropic_factory, provider, text_tool_models
    )
