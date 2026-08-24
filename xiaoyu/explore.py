"""explore 子 agent：用便宜模型做只读检索。

为什么值得做（这是自建 harness 相对现成工具的实际收益之一）：
- **省钱**：翻代码找东西是最费 token 的动作，交给 1× 单价的模型，主模型只看结论。
- **省上下文**：几十个 grep 结果、几百行文件内容全留在子 agent 里，用完即弃，
  主上下文只增加一段结论。这和上下文压缩是同一个方向的收益，而且更早生效。

两条硬约束：
1. **子 agent 只拿只读工具**（read_file / grep / list_files）。绝不给 bash ——
   bash 能写文件、能 pip install（已经真实发生过），"只读检索"就成了空话。
2. **子 agent 不再挂 explore**，避免无限套娃。
"""

from __future__ import annotations

from . import ui
from .config import Config
from .events import Notice, UISink
from .providers import Registry
from .render import PlainSink
from .tools import Tool, Toolbox

#  "代价不对等"与"形似不算命中"两条反垃圾 prompt 写法
#  （显式错误代价声明 + 负样本清单）；负样本的素材来自 explore 实验 D 组的
#  诱饵常量——模型当时不信任检索结论的理由正是分不清"出现过"和"被使用"。
EXPLORE_PROMPT = """你是代码检索助手，只负责查清事实，不做修改、不给方案。

你只有只读工具：grep（按正则搜）、list_files（列文件）、read_file（读文件，支持 offset/limit）。

工作方式：
- 先用 grep / list_files 定位，再只读真正需要的文件，别整个仓库通读。
- 一次搜不到就换关键词、换大小写、换更短的词根，不要反复用同一个模式。

回答要求（这份回答会直接交给主 agent 当依据，它不会再去核对，所以必须自证）：
- 结论先行，一句话说清答案。
- **每个关键事实都要带证据**：`路径:行号` + 那一行的**原文**（照抄，不要改写）。
  只给位置不给原文，主 agent 无法判断真假，就只能自己重读一遍 —— 那就白费了。
- **两类错误的代价不对等**：写错一个事实比漏掉一个位置严重得多——漏了主 agent
  还能自己补查，错了它会直接照着改代码。拿不准的标注「未确认」，
  不要为了显得完整把推测写成事实。
- **形似不算命中**：同名但未被实际引用的常量、废弃的入口、注释或字符串里的示例
  都不是答案，判断标准是"谁真正被使用"。存在这类干扰项时，
  **明确说清你排除了什么、依据是哪一行**。
- 只写你实际读到的内容。没查到就明确说"没找到"，并说明搜过哪些关键词 —— 不要猜、不要编。
- 简洁：证据行只贴关键的那几行，不要整段复制文件。不要给改进建议，不要写代码。

工作区根目录：{workspace}"""

#  子 agent 的回答如果太长就失去意义了（省上下文是它存在的理由）
MAX_ANSWER_CHARS = 4000


def make_explore_tool(
    config: Config, registry: Registry, usage, sink: UISink | None = None
) -> Tool:
    """造一个 explore 工具挂到主 agent 上。usage 与 registry 都传父级的：
    成本记同一本账，client 也按 provider 复用（explore_model 可能落在另一家）。

    sink 是父 agent 的表现层出口：explore 的进度提示随父级的界面走。
    """
    sink = sink or PlainSink()

    def explore(question: str) -> str:
        #  延迟导入，避免和 agent 模块循环引用
        from .agent import Agent

        sub_config = Config(
            base_url=config.base_url,
            model=config.explore_model,
            summary_model=config.summary_model,
            explore_model=config.explore_model,
            vision_fallback_model=config.vision_fallback_model,
            workspace=config.workspace,
            max_iterations=config.explore_iterations,
            max_tool_output=config.max_tool_output,
            #  只传显式覆写：不覆写时子 agent 按自己的 explore_model 查表取上限
            context_limit_override=config.context_limit_override,
            compact_at=config.compact_at,
            keep_recent=config.keep_recent,
            request_timeout=config.request_timeout,
            extra_env=config.extra_env,
            #  只读工具，不需要人工确认
            auto_approve=True,
            enable_explore=False,
            #  检索子 agent 只查代码，不上网
            enable_web_search=False,
            #  检索子 agent 不需要技能，也不该受用户机器上技能库的影响
            enable_skills=False,
            #  检索任务有界且只读，不需要计划工具
            enable_plan=False,
            #  只读子集用不上插件工具，也不为它花插件发现的时间
            enable_plugins=False,
            #  MCP 同理：受限子集不挂第三方 server 工具
            enable_mcp=False,
        )

        sink.emit(Notice(f"  🔍 explore（{config.explore_model}）：{ui.preview(question, 90)}"))

        #  子 agent 的 sink 从父 sink 派生（duck-typed）：RichSink 场景下必须
        #  共用同一个 Console，rich 的活区 spinner 才能把嵌套的工具行抬到
        #  自己上方；裸 print（PlainSink 默认路径）会把活区搅花。
        #  父 sink 不支持派生（PlainSink/NullSink）就传 None，走原有默认。
        child_maker = getattr(sink, "quiet_child", None)
        sub_agent = Agent(
            sub_config,
            Toolbox(sub_config, only=Toolbox.READONLY),
            usage=usage,
            registry=registry,
            quiet=True,
            allow_explore=False,
            sink=child_maker() if callable(child_maker) else None,
        )
        sub_agent.system_prompt_override = EXPLORE_PROMPT.format(workspace=config.workspace)
        sub_agent.messages[0]["content"] = sub_agent.system_prompt_override

        try:
            sub_agent.send(question)
        except Exception as exc:  # noqa: BLE001 - 检索失败不该打断主流程
            return (
                f"ERROR: 检索子 agent 失败（{type(exc).__name__}: {exc}）。"
                "可以自己用 grep / read_file 继续查。"
            )

        answer = sub_agent.last_assistant_text()
        tools_used = len(sub_agent.trace)
        sink.emit(Notice(f"  🔍 explore 完成：{tools_used} 次只读工具调用"))

        if not answer:
            return "检索子 agent 没有给出结论（可能是轮次用尽）。请自己用 grep / read_file 继续查。"
        if len(answer) > MAX_ANSWER_CHARS:
            answer = answer[:MAX_ANSWER_CHARS] + "\n…（结论过长已截断）"
        return (
            f"[检索结论 · 由 {config.explore_model} 只读检索 {tools_used} 次得出。"
            "结论里的 路径:行号 + 原文行是实际读到的，可直接采信，"
            "不需要为了核对再把这些文件读一遍]\n"
            f"{answer}"
        )

    return Tool(
        name="explore",
        description=(
            "把一个检索问题交给便宜模型的只读子 agent，返回带证据位置的结论。"
            "适合：摸清项目结构、找某个符号定义在哪、找所有调用点、"
            "确认某个配置项/常量的值、搞清一段逻辑的来龙去脉。"
            "优先用它代替自己连续 grep + 读一堆文件 —— 中间过程不会占用你的上下文。"
            "问题要具体，并说明你想要什么形式的答案（例如「给出文件和行号」）。"
            "它只读，不会改任何文件；要改代码仍然由你自己做。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "具体的检索问题，越具体越好",
                }
            },
            "required": ["question"],
        },
        handler=explore,
        #  子 agent 只有只读工具，委托本身不需要确认
        requires_approval=False,
    )
