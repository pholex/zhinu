"""多 provider 模型路由：把「model 名」解析成「去哪家、用什么名字、走哪个 client」。

为什么需要这一层（小羽从前是单端点：一个 base_url、一个 key、一个 client 贯穿全场）：

1. **开箱可用**——这是公开发布的包，从前用户必须先自建一个 LiteLLM 网关才能跑起来。
   现在填一个 `DEEPSEEK_API_KEY` 就能用。
2. **网关不再是单点**——网关一挂从前是全挂（所有备用模型都在同一个网关后面）。
   现在同名模型有「影子兜底」：直连持续失败会自动落到网关同名模型，会话不断。
3. **少一跳、不加价、key 不过第三方**。

核心规则是**有序合并**：provider 按优先级排队，同名模型先出现者赢。
DeepSeek 官方名（deepseek-v4-pro / deepseek-v4-flash）和网关侧完全一致，
所以 canonical id 直接就是原始 model 名，不需要别名映射表——这是这一层能做薄的关键。
日后遇到两边命名不一致的厂商，别名表加在 Preset 上（models 改成 dict）即可，
调用方无感。
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass, field
from typing import Any

from openai import OpenAI

from .config import GATEWAY_KEY_ENVS, Config, MissingConfig, find_api_key
from .responses import ANTHROPIC, RESPONSES, WILDCARD
from .responses import wrap as wrap_transport
from .scripted import SCRIPTED_ENV, SCRIPTED_PROVIDER
from .textcalls import TEXT as TEXT_TOOLS

#  网关 provider 的固定名字。它是通配的：什么模型名都接（我们不知道网关上有什么，
#  也不做 /v1/models 探测——启动路径上加网络往返，而且"列得出"不等于"调得通"）。
GATEWAY = "gateway"

#  通用兜底 provider 的环境变量前缀：XIAOYU_PROVIDER_<NAME>_{BASE_URL,API_KEY,MODELS}
_GENERIC_PREFIX = "XIAOYU_PROVIDER_"
_GENERIC_SUFFIX = "_BASE_URL"


@dataclass(frozen=True)
class Preset:
    """内置的厂商知识：纯数据，一行一家。"""

    name: str
    base_url: str
    models: tuple[str, ...]
    #  按顺序找 key 的环境变量名
    key_envs: tuple[str, ...]
    #  给人看的来源标注
    label: str
    #  说 Responses 协议的型号（`*` = 整家）。其余走 chat。见 responses.py
    responses_models: tuple[str, ...] = ()
    #  收得下图片输入的型号（`*` = 整家）。声明纪律与 responses_models 完全相同：
    #  **按型号、不按家**——同一家里旗舰能看图、小杯不能是常态，按家切会把
    #  "这张图发不发得出去"押在错误的粒度上。见 media.py / Registry.sees_images
    vision_models: tuple[str, ...] = ()
    #  说 Anthropic Messages 协议的型号（`*` = 整家）。优先级高于 responses_models。
    #  见 messages.py（为什么原生协议优于官方 OpenAI 兼容端点）
    anthropic_models: tuple[str, ...] = ()
    #  不会 function calling、工具调用要走文本协议的型号（`*` = 整家）。
    #  内置厂商目前没有一家需要（名额纪律只收旗舰，旗舰都会 function calling）；
    #  字段留着是让 preset 与通用 env 兜底形状一致。见 textcalls.py
    text_tool_models: tuple[str, ...] = ()


#  ⚠️ 只内置能确认的厂商。猜错的代价是用户配好了却 404——这类数据宁可缺也不能错
#  （DeepSeek 自己就把 deepseek-chat / deepseek-reasoner 淘汰掉了，旧名字现在是错的）。
#  补一家 = 加一行；在此之前，未内置的厂商走下面的通用 env 兜底。
#
#  ⚠️ 名额纪律（2026-08-13 定）：直连内置总量 ≤16 个 model，单家 ≤3 个
#  （同代 family 如 gpt-5.6 三兄弟是上限情形），绝大多数厂商只留最新旗舰 1-2 个，
#  **滚动替换而非累积**——新旗舰入册、旧型号下架（下架 ≠ 不能用，网关通配仍转发，
#  见 anthropic 条目里 haiku 的先例）。小名额是逐型号实测纪律成立的前提：
#  每个在册型号的协议选边 / vision / reasoning 回传都要有实测数字，500 家的
#  everything connector 做不到这一点，这正是"内置"二字的含金量。
#
#  ⚠️ `vision_models` 同样只写实测过的（`experiments/vision_probe.py`，2026-08-12
#  跑过全部 11 个内置型号）。判据是**绿、紫两张纯色图都答对**：不看 HTTP 200
#  （deepseek-v4-flash 会 200 收下再说"无法确定"），不用红色（蒙也蒙得中），
#  **更不能在提示词里给"看不到就明说"的逃生舱**——那半句让 claude 两个型号
#  100% 自称看不见，第一版据此把 anthropic 错记成"不收图"。补新型号照跑一遍。
PRESETS: dict[str, Preset] = {
    "deepseek": Preset(
        name="deepseek",
        base_url="https://api.deepseek.com/v1",
        models=("deepseek-v4-pro", "deepseek-v4-flash"),
        #  键名就用厂商原生名，.env / 环境变量 / Keychain 三处同名，用户只记一个
        key_envs=("DEEPSEEK_API_KEY",),
        label="直连 deepseek",
        #  ⚠️ 只有 flash：v4-pro 的 /responses 实测回"尚未开放"类明确报错，
        #  而 v4-pro 是默认主模型——这正是协议必须按型号声明、不能按家切的原因。
        #  实测两边 token 计数完全一致（长短输入都 0 差），换协议不多花钱
        responses_models=("deepseek-v4-flash",),
        #  ⚠️ 两个型号都不收图，且**失败方式不同**：v4-pro 的 chat 端点直接 400
        #  （unknown variant `image_url`），v4-flash 的 /responses 却 200 收下、
        #  prompt_tokens 只涨 5 个（92→107）、然后答"无法确定"——图被静默丢弃。
        #  后者正是 vision_probe 不能只看状态码的原因
    ),
    #  以下三家 2026-08-11 实测过 chat completions 通、模型名正确
    "moonshot": Preset(
        name="moonshot",
        base_url="https://api.moonshot.cn/v1",
        models=("kimi-k3",),
        key_envs=("MOONSHOT_API_KEY",),
        label="直连 moonshot",
        #  2026-08-12 实测收图：红/绿两轮都答对
        vision_models=(WILDCARD,),
    ),
    "qwen": Preset(
        name="qwen",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        models=("qwen3.8-max",),
        #  阿里官方生态惯用 DASHSCOPE_API_KEY，两个名字都认（同网关认 LITELLM_API_KEY 的理由）
        key_envs=("QWEN_API_KEY", "DASHSCOPE_API_KEY"),
        label="直连 qwen",
        #  ⚠️ 刻意留在 chat：它的 /responses 调得通、server-side state 也真，但
        #  reasoning 只给明文 summary、没有可回放的加密状态（回传实测不被服务端消费），
        #  也就是说切过去零收益；而 responses 侧每次请求还固定多 32 个 input token
        #  2026-08-12 实测收图：红/绿两轮都答对。⚠️ 图别太小——8×8 会被回
        #  "image length and width do not meet the model restrictions"，那是尺寸
        #  限制不是不支持（探测脚本因此用 64×64）
        vision_models=(WILDCARD,),
    ),
    "zhipu": Preset(
        name="zhipu",
        base_url="https://open.bigmodel.cn/api/paas/v4",
        #  2026-08-24 换代 5.2 → 5.3（滚动替换，同底座后训练升级）。实测：
        #  tool_calls / 流式 reasoning_content 均通。注意 5.3 思考不可关，
        #  reasoning_effort 只认 low/high/max（无 medium），不传=服务端默认 max
        models=("glm-5.3",),
        key_envs=("ZHIPU_API_KEY",),
        label="直连 zhipu",
        #  ⚠️ glm-5.3 仍不收图（2026-08-24 复测）：chat 端点同款 400「messages.
        #  content.type 参数非法，取值范围 ['text']」。zhipu 的视觉能力在另外的
        #  型号上，我们没内置
    ),
    #  —— 海外直连 ——
    "anthropic": Preset(
        name="anthropic",
        base_url="https://api.anthropic.com/v1",
        #  只留旗舰与主力两档。claude-haiku-4-5 官方仍在售，但我们不用它（上一代小杯，
        #  便宜档已由 deepseek-v4-flash 覆盖）——不内置不等于不能用：
        #  网关通配仍能转发，config.CONTEXT_WINDOWS 里的 haiku 200K 例外因此保留
        models=("claude-opus-5", "claude-sonnet-5"),
        key_envs=("ANTHROPIC_API_KEY",),
        label="直连 anthropic",
        #  2026-08-12 实测两个型号都收图：绿/紫两轮都答对。
        #  ⚠️ 这一条的第一版是**错的**（记成"官方 OpenAI 兼容层丢掉 image 部件"），
        #  根因在探测脚本而不在端点：提示词里加了"看不到就回答：看不到图"，
        #  claude 两个型号就 100% 照着答，尽管换个问法它们能准确描述出
        #  "纯蓝 #0000FF"。教训记在 vision_probe.py 的 QUESTION 上方——
        #  **给模型逃生舱等于给假阴性开门**
        vision_models=(WILDCARD,),
        #  走原生 Messages 协议（此前走官方 OpenAI 兼容端点——Anthropic 自己
        #  定位它"评估用、非生产"：不支持 prompt caching、reasoning_effort
        #  被静默忽略、thinking 不回传。三样对 agent 负载都值钱，见 messages.py）
        anthropic_models=(WILDCARD,),
    ),
    "openai": Preset(
        name="openai",
        base_url="https://api.openai.com/v1",
        #  5.6 线三兄弟（sol / terra / luna）为现役，旧的 5.5 / 5.4-mini 不再内置
        models=("gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"),
        key_envs=("OPENAI_API_KEY",),
        label="直连 openai",
        #  ⚠️ 必须走 Responses：5.6 线在 /v1/chat/completions 上带 tools 直接 400
        #  （"use /v1/responses or set reasoning_effort to 'none'"）。小羽每轮都带
        #  工具，关推理不可接受，所以整个 openai 直连改走 Responses——翻译在
        #  responses.py，内核与历史形态不变
        responses_models=(WILDCARD,),
        #  2026-08-12 实测三兄弟全部收图：红/绿两轮都答对
        vision_models=(WILDCARD,),
    ),
    #  2026-08-07 实测过 chat completions 与 /responses 都通、模型名正确
    "xai": Preset(
        name="xai",
        base_url="https://api.x.ai/v1",
        #  2026-08-13 由 grok-4.5 滚动替换为 4.6（同代升级款，按纪律换不叠）：
        #  实测 /responses 通、内核端到端 3 轮工具调用通
        models=("grok-4.6",),
        key_envs=("XAI_API_KEY",),
        label="直连 xai",
        #  实测是完整实现：加密 reasoning（4.5 是 668~855 字符，4.6 约 1300）与
        #  server-side state 都真——除 openai 外唯一吃得满 reasoning 回传的一家
        responses_models=(WILDCARD,),
        #  2026-08-12 实测收图（4.5 红/绿两轮都答对），2026-08-13 在 4.6 上复测
        #  绿/紫两轮仍都答对。⚠️ 图别小于 512 像素，8×8 会被明确回绝
        #  （"below the minimum of 512 pixels"）
        vision_models=(WILDCARD,),
    ),
    #  ⚠️ AWS Bedrock Mantle 刻意不内置（2026-08-11 实测后撤销）：
    #  其 OpenAI 兼容端点 https://bedrock-mantle.{region}.api.aws/v1 确实存在
    #  （API key 鉴权），但 Claude 系与 openai.gpt-5.x 都只挂各自原生协议
    #  （/v1/chat/completions 与 /v1/responses 均明确拒绝）；chat completions
    #  只对开放权重/第三方模型开放（gpt-oss、deepseek.v3.2、kimi-k2.5 实测通），
    #  而那些都有更优直连。等内核会说 Anthropic Messages 协议再回来；
    #  在那之前 bedrock Claude 经 OpenAI 兼容网关转发。
}


@dataclass(frozen=True)
class Provider:
    """一个已配置好的上游：端点 + key + 它认领的模型清单。"""

    name: str
    base_url: str
    #  永不打印、永不写日志（repr 里也不要，异常回溯会带出来）
    api_key: str = field(repr=False)
    #  空 = 通配：什么模型名都接。网关就是这种。
    models: tuple[str, ...] = ()
    label: str = ""
    #  说 Responses 协议的型号（`*` = 整家）。其余走 chat
    responses_models: tuple[str, ...] = ()
    #  收得下图片输入的型号（`*` = 整家）
    vision_models: tuple[str, ...] = ()
    #  说 Anthropic Messages 协议的型号（`*` = 整家），优先级高于 responses_models
    anthropic_models: tuple[str, ...] = ()
    #  工具调用走文本协议的型号（`*` = 整家）。见 textcalls.py
    text_tool_models: tuple[str, ...] = ()

    @property
    def wildcard(self) -> bool:
        return not self.models

    def accepts(self, model: str) -> bool:
        return self.wildcard or model in self.models

    def sees_images(self, model: str) -> bool:
        return WILDCARD in self.vision_models or model in self.vision_models

    @property
    def display(self) -> str:
        return self.label or self.name


@dataclass(frozen=True)
class Route:
    """一次请求要用的三件套：哪家、发什么名字、走哪个 client。"""

    provider: str
    #  发给上游的真名（不带 provider 前缀）
    model: str
    #  client 不参与相等性比较：Route 要能按 (provider, model) 去重
    client: Any = field(compare=False, repr=False, default=None)

    @property
    def qualified(self) -> str:
        """全限定名 `provider/model`。用于记账、粘性降级、显式寻址。"""
        return f"{self.provider}/{self.model}"


class UnknownModel(MissingConfig):
    """没有任何 provider 认领这个模型名。"""


class Registry:
    """有序 provider 表 + 解析。client 按 provider 缓存，连接不重开。"""

    def __init__(
        self,
        providers: list[Provider],
        timeout: float = 600.0,
        clients: dict[str, Any] | None = None,
    ) -> None:
        if not providers:
            raise MissingConfig(NO_PROVIDER_HINT)
        self.providers = providers
        self._timeout = timeout
        #  预置 client（测试注入假 client 用）；其余按需惰性构造并缓存。
        #  惰性构造上锁：qixiang 的工作线程可能同时首访同一家 provider，
        #  竞态下 ScriptedClient（e2e 进程内单例队列）会被建出两份，
        #  "总共调了几次模型"的断言就假了——普通 OpenAI client 重复构造
        #  虽无害，也一并锁掉图个确定性。
        self._clients: dict[str, Any] = dict(clients or {})
        self._client_lock = threading.Lock()
        #  remote_models 探测到的通配 provider 清单（会话级）：补全候选用。
        #  逐键补全不能做网络请求，所以只有探测过一次之后才补得出网关模型
        self._remote_cache: dict[str, list[str]] = {}
        #  Models API 能力缓存（qualified 名 → {max_input_tokens, image_input}）：
        #  probe_model_caps 按需填，零启动往返；探到的 max_input_tokens 是上下文
        #  上限的权威值，硬编码 CONTEXT_WINDOWS 只是它未探测时的兜底
        self._capabilities: dict[str, dict[str, Any]] = {}

    @classmethod
    def for_client(cls, client: Any, name: str = GATEWAY) -> "Registry":
        """拿一个现成的 client 造单 provider registry：测试注入假 client 的入口。

        它也是"只有网关"这个既有形态的最小构造，行为等价性测试直接拿它当基准。
        """
        return cls([Provider(name, "", "", (), name)], clients={name: client})

    #  —— 解析 ——

    def get(self, name: str) -> Provider | None:
        for provider in self.providers:
            if provider.name == name:
                return provider
        return None

    def _pinned(self, name: str) -> tuple[Provider | None, str]:
        """显式寻址：`deepseek/deepseek-v4-pro` 强制指定去哪家。

        前缀只有**匹配到已注册 provider 名**才算寻址，否则整串透传——
        网关上真有 `anthropic/claude-x` 这种自带斜杠的模型名，不能误切。
        """
        prefix, sep, rest = name.partition("/")
        if sep and rest:
            if (provider := self.get(prefix)) is not None:
                return provider, rest
        return None, name

    def resolve(self, name: str) -> Route:
        """model 名 → Route。严格按序，第一个接得住的赢；没人接就抛 UnknownModel。

        规则只有这一条，通配 provider 不搞特殊：它"接得住一切"，所以谁把它排在前面，
        它就吃掉一切。默认顺序里网关垫底正是因为这个——而 XIAOYU_PROVIDERS 把网关
        提前，也就真的成了"临时全走网关"的调试开关（若通配另有优待，这个开关就是假的）。
        """
        pinned, model = self._pinned(name)
        if pinned is not None:
            return self._route(pinned, model)
        for provider in self.providers:
            if provider.accepts(model):
                return self._route(provider, model)
        raise UnknownModel(
            f"没有 provider 提供模型 {name}。已知模型："
            + "、".join(entry.model for entry in self.listing())
            + "\n（配上网关 XIAOYU_BASE_URL 后，任何模型名都会转发过去）"
        )

    def backups(self, name: str) -> list[Route]:
        """影子兜底：主 provider **之后**同样能接这个名字的路由，按序返回。

        这就是"同名去重"的另一半——网关上的同名模型不出现在清单里，
        但直连持续失败时它是退路。显式寻址（provider/model）不给兜底：
        用户既然点名了哪一家，就不该被偷偷换掉。
        """
        pinned, model = self._pinned(name)
        if pinned is not None:
            return []
        primary = self.resolve(model)
        routes: list[Route] = []
        after_primary = False
        for provider in self.providers:
            if provider.name == primary.provider:
                after_primary = True
                continue
            if after_primary and provider.accepts(model):
                routes.append(self._route(provider, model))
        return routes

    def sees_images(self, name: str) -> bool:
        """这个模型名收不收得下图片。解析不了的名字一律 False。

        **fail-closed 是有意的**：判断错的两种代价不对称——漏判只是图片降级成
        一行文字说明（模型照样干活），误判是每一轮请求都被上游 400，整个会话卡死。
        所以未声明即"不能看图"，通配 provider（网关）也不例外：网关后面挂的是
        什么模型我们无从知道，猜"能"等于把整条链路押在运气上。

        网关上确实挂着视觉模型时，用 XIAOYU_VISION_MODELS 点名放行（见 _vision_override）。
        """
        if _vision_override(name):
            return True
        #  刻意不走 resolve()：那条路会顺手构造 client（连接池、鉴权），
        #  而这里只是问一句"能不能"，一轮里会被问好几次
        provider, model = self._pinned(name)
        if provider is None:
            provider = next((p for p in self.providers if p.accepts(model)), None)
        return provider is not None and provider.sees_images(model)

    def sticky_name(self, route: Route) -> str:
        """粘性降级写回 config.model 用：能用裸名表达就用裸名，否则用全限定名。

        自证式判断——裸名再解析一次还落回同一家才敢用裸名，
        否则 `/model` 显示的东西和实际跑的就对不上了。
        """
        try:
            if self.resolve(route.model).provider == route.provider:
                return route.model
        except UnknownModel:
            pass
        return route.qualified

    def _route(self, provider: Provider, model: str) -> Route:
        return Route(provider.name, model, self.client(provider.name))

    def client(self, name: str) -> Any:
        """按 provider 缓存 client：同一家的所有请求复用一条连接池。
        返回鸭子型对象（OpenAI 或 e2e 的 ScriptedClient），调用面只有
        `chat.completions.create`。

        max_retries=0 沿用旧约定：重试全部收归 agent._stream_with_recovery 一层，
        否则两层叠加，用户看到的"第 1/2 次重试"是假的。
        """
        if name not in self._clients:
            with self._client_lock:
                if name not in self._clients:
                    self._build_client(name)
        return self._clients[name]

    def _build_client(self, name: str) -> None:
        """构造并缓存一家 provider 的 client（仅 client() 持锁调用）。"""
        provider = self.get(name)
        if provider is None:  # pragma: no cover - 只可能是内部调用写错
            raise UnknownModel(f"未注册的 provider：{name}")
        if name == SCRIPTED_PROVIDER:
            #  确定性 e2e 桩：进程内单例队列（主循环/摘要/explore 共用），
            #  "总共调了几次模型"因此可断言
            from .scripted import ScriptedClient

            self._clients[name] = ScriptedClient.from_file(
                os.environ[SCRIPTED_ENV].strip()
            )
            return
        #  一律包一层 Transport（responses.py）：它是唯一的出网口，
        #  按型号分派协议、并摘掉内核私有键。包在缓存内侧，
        #  所有拿到这个 client 的地方看到的都是同一只。
        #  anthropic client 走懒工厂：不碰 Claude 直连就不构造、不 import
        factory = None
        if provider.anthropic_models:

            def factory(p: Provider = provider, t: float = self._timeout) -> Any:
                from . import messages

                return messages.client(p.base_url, p.api_key, t)

        #  snapshot 录制（XIAOYU_SNAPSHOT_RECORD）包在最外侧：录到的
        #  是传输层抹平协议差异之后、内核实际消费的 chunk 面
        from .snapshot import maybe_record

        self._clients[name] = maybe_record(
            wrap_transport(
                OpenAI(
                    base_url=provider.base_url,
                    api_key=provider.api_key,
                    timeout=self._timeout,
                    max_retries=0,
                ),
                provider.responses_models,
                provider.anthropic_models,
                factory,
                provider=name,
                text_tool_models=provider.text_tool_models,
            )
        )

    #  —— 展示 ——

    def listing(self) -> list[Listing]:
        """合并后的模型清单：只有被显式声明的模型能枚举（通配 provider 无从列举）。

        归属一律回头问 resolve，不在这里自己判断——两处各写一遍判断逻辑，
        顺序被 XIAOYU_PROVIDERS 覆盖时清单就会和实际跑的路由对不上。
        """
        entries: list[Listing] = []
        seen: set[str] = set()
        for provider in self.providers:
            for model in provider.models:
                if model in seen:
                    continue
                seen.add(model)
                owner = self.get(self.resolve(model).provider)
                entries.append(
                    Listing(
                        model=model,
                        owner=owner.name if owner else provider.name,
                        owner_label=owner.display if owner else provider.display,
                        backups=tuple(route.provider for route in self.backups(model)),
                    )
                )
        return entries

    @property
    def wildcards(self) -> list[Provider]:
        return [provider for provider in self.providers if provider.wildcard]

    def remote_models(
        self, timeout: float = 10.0
    ) -> list[tuple[str, list[str] | None, str]]:
        """现场探测通配 provider 的 /v1/models，返回 (显示名, 模型列表, 失败原因)。

        模型列表为 None 即失败，失败原因才有值——带上一句原因是因为
        "获取失败"可能是网关挂了、也可能是 key 预算用完（429），
        用户当场就该分得清。启动路径上刻意不做这个探测（加网络往返，
        且"列得出"不等于"调得通"）；/model 无参是用户显式想看清单的时刻，
        这一次往返花得其所。短超时独立于 request_timeout：清单是锦上添花，
        挂掉的网关不该把 REPL 卡住十分钟；失败只降级，绝不抛进 REPL。
        """
        results: list[tuple[str, list[str] | None, str]] = []
        for provider in self.wildcards:
            client = self.client(provider.name)
            try:
                page = client.with_options(timeout=timeout).models.list()
                models = sorted(model.id for model in page)
                self._remote_cache[provider.name] = models
                results.append((provider.display, models, ""))
            except Exception as exc:
                reason = str(exc).splitlines()[0][:160] or type(exc).__name__
                results.append((provider.display, None, reason))
        return results

    def remote_cached(self) -> list[str]:
        """已探测到的通配 provider 模型名，去重保序。

        探测失败不清空旧缓存——补全候选宁可略旧也别突然清零。"""
        names: list[str] = []
        for models in self._remote_cache.values():
            for name in models:
                if name not in names:
                    names.append(name)
        return names

    @staticmethod
    def _extract_caps(model_obj: Any) -> dict[str, Any]:
        """从一个 /v1/models 的模型对象里挖 max_input_tokens 与 image_input。

        Anthropic 原生返回富 capabilities（image_input.supported 等）+
        max_input_tokens；OpenAI 兼容网关多半没有这些字段——挖不到就留 None，
        绝不臆造（None = 不知道，走硬编码兜底）。
        """
        data: dict[str, Any] = {}
        if hasattr(model_obj, "model_dump"):
            try:
                data = model_obj.model_dump()
            except Exception:  # noqa: BLE001
                data = {}
        if not data:
            data = {k: getattr(model_obj, k) for k in dir(model_obj) if not k.startswith("_")}
        raw_limit = data.get("max_input_tokens")
        try:
            limit = int(raw_limit) if raw_limit else None
        except (TypeError, ValueError):
            limit = None
        image = None
        caps = data.get("capabilities")
        if isinstance(caps, dict) and isinstance(caps.get("image_input"), dict):
            image = bool(caps["image_input"].get("supported"))
        return {"max_input_tokens": limit, "image_input": image}

    def probe_model_caps(self, model: str, timeout: float = 10.0) -> dict[str, Any]:
        """按需拉一个模型的 Models API 能力（缓存、绝不抛、绝不启动期调用）。

        走对应 provider 的 client：说 Anthropic 协议的用原生 SDK client（它才有
        富 capabilities），其余用被包 client 的 `.models`。失败/无此字段 → {}。
        """
        try:
            route = self.resolve(model)
        except UnknownModel:
            return {}
        transport = route.client
        try:
            speaks = getattr(transport, "speaks_anthropic", None)
            build = getattr(transport, "anthropic_client", None)
            if callable(speaks) and callable(build) and speaks(route.model):
                #  Anthropic 原生 SDK client 才有富 capabilities（OpenAI 兼容面没有）
                api = build().with_options(timeout=timeout).models
            elif hasattr(transport, "with_options"):
                api = transport.with_options(timeout=timeout).models
            else:
                api = transport.models
            obj = api.retrieve(route.model)
            caps = self._extract_caps(obj)
        except Exception:  # noqa: BLE001 - 能力探测是锦上添花，任何失败都静默降级
            return {}
        self._capabilities[route.qualified] = caps
        return caps

    def cached_caps(self, model: str) -> dict[str, Any] | None:
        try:
            route = self.resolve(model)
        except UnknownModel:
            return None
        return self._capabilities.get(route.qualified)

    def describe(self) -> str:
        """`/model` 无参时的合并清单。让"钱花在哪、请求走哪条路"看得见——
        直连是靠环境变量静默启用的，不显示出来用户根本不知道路由变了。"""
        from . import ui

        rows: list[tuple[str, str]] = []
        for entry in self.listing():
            backups = "、".join(self._display(name) for name in entry.backups)
            note = f"（同名可兜底：{backups}）" if backups else ""
            rows.append((entry.model, f"{entry.owner_label}{note}"))
        for provider in self.wildcards:
            rows.append(("其余任意模型名", f"{provider.display}（转发，不枚举）"))
        #  中文占两列，不能用 str.ljust 对齐（ui.pad 按显示宽度补）
        width = max((ui.display_width(name) for name, _ in rows), default=0)
        return "\n".join(f"  {ui.pad(name, width)}  ← {source}" for name, source in rows)

    def _display(self, name: str) -> str:
        provider = self.get(name)
        return provider.display if provider else name


@dataclass(frozen=True)
class Listing:
    model: str
    owner: str
    owner_label: str
    backups: tuple[str, ...]


NO_PROVIDER_HINT = (
    "没有可用的模型端点。任选一种：\n"
    "  1. 运行 `xiaoyu config` 走配置向导（推荐，全平台）\n"
    "  2. 直连大模型厂商——填一个 key 即可，不需要网关：\n"
    "       DEEPSEEK_API_KEY=<你的-deepseek-key>\n"
    "       （同理：MOONSHOT_API_KEY / QWEN_API_KEY / ZHIPU_API_KEY\n"
    "         / ANTHROPIC_API_KEY / OPENAI_API_KEY / XAI_API_KEY）\n"
    "  3. 走 OpenAI 兼容网关（LiteLLM、vLLM、各家官方 API…）：\n"
    "       XIAOYU_BASE_URL=https://<你的网关>/v1  +  XIAOYU_API_KEY=<key>\n"
    "  4. 命令行 --base-url https://<你的网关>/v1\n"
    "（以上可同时配：直连优先，网关自动作为同名模型的兜底）"
)


def build(config: Config) -> Registry:
    """按优先级组装 Registry。key 缺失的 provider 直接不注册——

    "注册了但没 key"会把配置错误推迟到运行期才炸，而且是在用户已经开始对话之后。
    宁可启动时清单里就没有它。
    """
    providers: list[Provider] = []
    for name in _order():
        if (provider := _make(name, config)) is not None:
            providers.append(provider)
    return Registry(providers, timeout=config.request_timeout)


def _order() -> list[str]:
    """provider 的优先顺序。默认：内置直连 → 通用兜底 → 网关。

    直连排前面是这次改动的全部意义；网关垫底，因为它通配，放前面会把所有名字都吃掉。
    XIAOYU_PROVIDERS 可整体覆盖（调试时把网关提前）。

    确定性 e2e 桩（XIAOYU_SCRIPTED_SCRIPTS）优先级最高且**独占**：
    测试进程里只有它一个 provider，任何模型名都路由过去，绝无真实网络——
    比"排第一"更强的保证是"别家根本不注册"。
    """
    if os.environ.get(SCRIPTED_ENV, "").strip():
        return [SCRIPTED_PROVIDER]
    if raw := os.environ.get("XIAOYU_PROVIDERS", "").strip():
        return [name.strip() for name in raw.split(",") if name.strip()]
    return [*PRESETS, *_generic_names(), GATEWAY]


def _vision_override(name: str) -> bool:
    """`XIAOYU_VISION_MODELS=glm-5.3,my-gw-model`（`*` = 一律放行）。

    内置 preset 的 vision_models 必须实测过才敢写（见 PRESETS 上的 ⚠️），
    而"我的网关后面挂着视觉模型"是用户当场就知道、我们无从知道的事实。
    没有这个开关，这类用户就只能等我们发版——一条纯环境变量的逃生口比
    "先猜一个默认值"安全得多：猜错是每轮 400，这里错是用户自己点的名。
    """
    raw = os.environ.get("XIAOYU_VISION_MODELS", "")
    allowed = {item.strip() for item in raw.split(",") if item.strip()}
    return WILDCARD in allowed or name in allowed or name.rpartition("/")[2] in allowed


def _generic_names() -> list[str]:
    """从环境变量里发现未内置的厂商。排序固定，保证顺序可预测。"""
    found = set()
    for key in os.environ:
        if key.startswith(_GENERIC_PREFIX) and key.endswith(_GENERIC_SUFFIX):
            name = key[len(_GENERIC_PREFIX) : -len(_GENERIC_SUFFIX)].lower()
            if name and name not in PRESETS and name != GATEWAY:
                found.add(name)
    return sorted(found)


def _make(name: str, config: Config) -> Provider | None:
    """按名字造 provider。配不全（缺端点或缺 key）就返回 None。"""
    if name == SCRIPTED_PROVIDER:
        if not os.environ.get(SCRIPTED_ENV, "").strip():
            return None
        #  通配 + 假 key：e2e 里配什么模型名都接，且不需要任何真实凭据
        return Provider(SCRIPTED_PROVIDER, "scripted://", "scripted", (), "scripted 测试桩")
    if name == GATEWAY:
        if not config.base_url:
            return None
        key = find_api_key(GATEWAY_KEY_ENVS)
        if not key:
            return None
        return Provider(GATEWAY, config.base_url, key, (), "网关")

    if preset := PRESETS.get(name):
        key = find_api_key(preset.key_envs)
        if not key:
            return None
        return Provider(
            preset.name,
            preset.base_url,
            key,
            preset.models,
            preset.label,
            preset.responses_models,
            preset.vision_models,
            preset.anthropic_models,
            preset.text_tool_models,
        )

    #  通用兜底：XIAOYU_PROVIDER_<NAME>_{BASE_URL,API_KEY,MODELS,PROTOCOL,VISION,TOOLS}
    upper = name.upper()
    base_url = os.environ.get(f"{_GENERIC_PREFIX}{upper}{_GENERIC_SUFFIX}", "").strip()
    if not base_url:
        return None
    key = find_api_key((f"{_GENERIC_PREFIX}{upper}_API_KEY",))
    if not key:
        return None
    raw_models = os.environ.get(f"{_GENERIC_PREFIX}{upper}_MODELS", "")
    models = tuple(item.strip() for item in raw_models.split(",") if item.strip())
    #  未内置的厂商若说的不是 chat，用 PROTOCOL=responses / PROTOCOL=anthropic
    #  自救（后者也是日后 Bedrock Mantle 一类"Claude 只挂原生协议"端点的逃生舱），
    #  不必等我们补 preset。env 这一档是按家开关（要按型号区分就该来提 preset）
    protocol = os.environ.get(f"{_GENERIC_PREFIX}{upper}_PROTOCOL", "").strip().lower()
    speaks_responses = (WILDCARD,) if protocol == RESPONSES else ()
    speaks_anthropic = (WILDCARD,) if protocol == ANTHROPIC else ()
    #  VISION=* 整家、或逗号分隔点名。默认空 = 不发图（见 Registry.sees_images 的 fail-closed）
    raw_vision = os.environ.get(f"{_GENERIC_PREFIX}{upper}_VISION", "")
    vision = tuple(item.strip() for item in raw_vision.split(",") if item.strip())
    #  TOOLS=text：端点不会 function calling（本地小模型 / 老端点），工具调用改走
    #  文本协议（textcalls.py）。默认 native。与 PROTOCOL 正交，可以同时设
    tool_mode = os.environ.get(f"{_GENERIC_PREFIX}{upper}_TOOLS", "").strip().lower()
    text_tools = (WILDCARD,) if tool_mode == TEXT_TOOLS else ()
    return Provider(
        name,
        base_url,
        key,
        models,
        f"直连 {name}",
        speaks_responses,
        vision,
        speaks_anthropic,
        text_tools,
    )
