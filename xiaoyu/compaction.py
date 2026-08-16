"""上下文压缩。

思路：上下文快满时，把早期对话交给模型自己总结成一段"交接说明"，
用摘要替换掉那一段原文，保留 system prompt 和最近若干条消息。

两个必须守住的不变量：
1. **不能切开 tool_calls 和它的 tool 结果**。assistant 带 tool_calls 却找不到对应
   tool 消息，下一次请求会直接 400。所以切点只能落在 role=user 的消息上。
2. **压缩失败不能吞掉历史**。摘要调用失败就放弃这次压缩，宁可继续带着长上下文
   撞上限，也不能把内容丢了。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable

from . import media, tokens

#  开头的"无工具"声明：实测不加这段时部分模型会把唯一一轮输出浪费在
#  调工具上——摘要走的是便宜模型，更容易犯这个错。固定分节结构
#  （含"用户消息清单"节）同理：用户原话是接手时最不可丢的信息。
#  结尾的"代价不对等"声明：判断任务的两个
#  错误方向代价从不对等，但模型猜不到哪边重——不明说它就在模糊处随机摇摆。
SUMMARY_INSTRUCTION = """你这次调用没有任何工具可用。不要调用工具、不要输出工具调用语法，
直接输出摘要正文——输出被浪费在工具调用上，这次压缩就整个失败了。

下面是一段编码会话的前半部分。请把它压缩成一份"交接说明"，
让接手的人只看这份说明就能继续干活。按以下固定结构输出，每节都要有（没有内容就写"无"）：

1. 任务目标：用户的原始目标和后续追加的要求
2. 关键事实：项目结构、命令怎么跑、测试结果、环境限制等已确认的结论
3. 文件与改动：动过/读过的关键文件（带路径），改了什么、为什么
4. 错误与修复：踩过的坑、试过不行的方向（避免接手的人重复试）
5. 用户消息清单：用户说过的每条要求逐条列出——用户原话是最高优先级的信息，一条都不能漏
6. 未完成事项：还没做完的事
7. 当前状态：此刻正做到哪一步
8. 下一步：会话里已明确的下一步；没有就写"无"

只写会话里确实出现过的内容，不要推测、不要补充你认为应该有的东西。
两类错误的代价不对等：漏写细节可以接受——文件都还在磁盘上，接手的人随时能重读补回来；
写入没发生过的"事实"不可接受——接手的人会直接采信、无从核对。宁可短，不可编。
不确定的地方标注"未确认"。用中文，条目化，不要客套话。"""

#  前缀重放式摘要的指令尾（摘要调用
#  逐字重放会话自己的 system prompt + 工具 schema + 历史消息，把压缩指令作为
#  **最后一条 user 消息**追加——复用 provider 的 KV 前缀缓存，而不是另起一个
#  会作废缓存的精简 prompt；同时摘要模型看到的是全保真历史，不是被 600 字符
#  截断的渲染转写）。只用于路由到主模型那一腿；措辞因此改成"以上对话"。
PREFIX_SUMMARY_INSTRUCTION = """现在暂停手头的任务。不要调用工具、不要输出工具调用语法，
直接输出摘要正文——输出被浪费在工具调用上，这次压缩就整个失败了。

请把以上对话（本次编码会话的前半部分）压缩成一份"交接说明"，
让接手的人只看这份说明就能继续干活。按以下固定结构输出，每节都要有（没有内容就写"无"）：

1. 任务目标：用户的原始目标和后续追加的要求
2. 关键事实：项目结构、命令怎么跑、测试结果、环境限制等已确认的结论
3. 文件与改动：动过/读过的关键文件（带路径），改了什么、为什么
4. 错误与修复：踩过的坑、试过不行的方向（避免接手的人重复试）
5. 用户消息清单：用户说过的每条要求逐条列出——用户原话是最高优先级的信息，一条都不能漏
6. 未完成事项：还没做完的事
7. 当前状态：此刻正做到哪一步
8. 下一步：会话里已明确的下一步；没有就写"无"

只写会话里确实出现过的内容，不要推测、不要补充你认为应该有的东西。
两类错误的代价不对等：漏写细节可以接受——文件都还在磁盘上，接手的人随时能重读补回来；
写入没发生过的"事实"不可接受——接手的人会直接采信、无从核对。宁可短，不可编。
不确定的地方标注"未确认"。用中文，条目化，不要客套话。"""

#  措辞要点：把摘要明确定位成"另一个模型的交接产物"
#  而非自己的记忆，并提示"文件系统还在"——会话早期做过的改动可以用工具直接
#  查看现状，不必怀疑摘要、也不必重做。
CONTEXT_PREFIX = (
    "[以下是另一个模型对本会话早期内容做的交接摘要，原文已省略。"
    "早期改动过的文件都还在磁盘上，可用工具直接查看当前状态；"
    "请基于已完成的工作继续，避免重复劳动]\n\n"
)

#  摘要退化下限（500 英文字符按中文信息密度折半得 200）：
#  八节交接说明连这个长度都不到，不可能承载它要替换的
#  任务状态——多半是模型敷衍、截断或复读指令。已有的"空摘要"检查抓不到这种
#  "非空但等于没写"的输出。调用方应当作瞬时失败处理（换模型重试）。
MIN_SUMMARY_CHARS = 200


def is_degenerate_summary(summary: str) -> bool:
    """清洗后过短的摘要视为退化：接受它等于拿几十个字换掉整段历史。"""
    return len(summary.strip()) < MIN_SUMMARY_CHARS


#  分界标记消毒（插零宽空格打断，不删内容）：
#  摘要正文若原样复读了 CONTEXT_PREFIX（复述上一份摘要的开头、引用指令），
#  下次压缩 split_head 会把它认成真的分界标记、从假标记处切开历史。
_DEFUSED_PREFIX = "[\u200b" + CONTEXT_PREFIX[1:]


def sanitize_summary(summary: str) -> str:
    """把摘要正文里混入的分界标记打断，使它永远不会被当成活标记解析。"""
    return summary.replace(CONTEXT_PREFIX, _DEFUSED_PREFIX)

# ---------- microcompact：清理旧工具输出（全量摘要之前更便宜的一层） ----------

#  可清理的工具：结果是"可重新获取的原始数据"，清了随时能再拿。
#  explore 的结论、skill 的说明是蒸馏产物 / 行为指令，清了拿不回来，不碰。
CLEARABLE_TOOLS = frozenset({"read_file", "grep", "list_files", "bash"})

#  小于这个字符数的结果不值得清（换出来的 token 抵不过 stub 占位）
CLEAR_MIN_CHARS = 500

_CLEARED_MARKER = "[结果已清理"


def microcompact(
    messages: list[dict[str, Any]],
    keep_recent: int,
    min_chars: int = CLEAR_MIN_CHARS,
) -> tuple[list[dict[str, Any]], int, int]:
    """把较早的大块工具输出替换成占位符。返回 (新消息列表, 清理条数, 省下的字符数)。

    microcompact：只按 tool_call_id 找到白名单工具的旧结果、
    整条替换，不看内容、不改消息结构——所以它和全量摘要压缩可以干净地组合
    （摘要看到的是占位符，占位符本身也说明了内容去了哪）。

    最近 keep_recent 条消息受保护：近期的读取/测试输出往往正在被使用。
    """
    #  tool_call_id → 工具名，从 assistant 的 tool_calls 里反查
    call_names: dict[str, str] = {}
    for message in messages:
        for call in message.get("tool_calls") or []:
            call_names[call.get("id", "")] = call.get("function", {}).get("name", "")

    boundary = max(0, len(messages) - keep_recent)
    result: list[dict[str, Any]] = []
    cleared = saved = 0
    for index, message in enumerate(messages):
        content = media.text_of(message.get("content"))
        name = call_names.get(message.get("tool_call_id", ""))
        if (
            index >= boundary
            or message.get("role") != "tool"
            or name not in CLEARABLE_TOOLS
            or len(content) < min_chars
            or content.startswith(_CLEARED_MARKER)
        ):
            result.append(message)
            continue
        stub = (
            f"{_CLEARED_MARKER}：这条较早的 {name} 输出（原 {len(content)} 字符）"
            f"已在上下文回收时移除。若还需要这份内容，请重新调用 {name}。]"
        )
        result.append({**message, "content": stub})
        cleared += 1
        saved += len(content) - len(stub)
    return result, cleared, saved


# ---------- 机械锚点索引（摘要旁的逐字标识符备份） ----------

#  摘要是有损的，而最容易被摘要"释义"掉的恰是必须逐字才有用的标识符：
#  文件路径、提交号、URL、错误类名、工单编号。这一节用纯正则从被压原文里
#  机械收割（零模型调用、零释义风险），作为有界索引附在摘要旁——后续轮次
#  要"找回当时那个文件/那次提交"时，逐字锚点还在。
_ANCHOR_PATTERNS: tuple[tuple[str, str], ...] = (
    #  带目录分隔的相对/绝对路径（要求至少一层目录，裸文件名不收：噪声太大）
    ("路径", r"(?:~/|\.{1,2}/|/)?[\w.\-]+(?:/[\w.\-]+)+"),
    ("URL", r"https?://[^\s)\"'>\]]+"),
    #  7~40 位十六进制且至少含一个字母：排除纯数字串误报
    ("提交", r"\b(?=[0-9a-f]*[a-f])[0-9a-f]{7,40}\b"),
    ("编号", r"#\d{2,}"),
    ("错误", r"\b[A-Z][A-Za-z]{2,}(?:Error|Exception|Warning)\b"),
)
_ANCHORS_PER_KIND = 15
_ANCHORS_CHAR_CAP = 1200


def anchor_index(messages: list[dict[str, Any]]) -> str:
    """被压区的逐字标识符索引；一个都没收到返回空串。

    扫描消息正文与 tool_calls 参数（路径最常出现在调用参数里）。
    每类去重保序、各限 15 条，总量封顶 1200 字符——索引是保险不是转录，
    超限宁可截断。
    """
    pieces: list[str] = []
    for message in messages:
        pieces.append(media.text_of(message.get("content")))
        for call in message.get("tool_calls") or []:
            function = call.get("function") or {}
            pieces.append(str(function.get("arguments") or ""))
    text = "\n".join(pieces)
    lines: list[str] = []
    for label, pattern in _ANCHOR_PATTERNS:
        found: list[str] = []
        for match in re.findall(pattern, text):
            if match not in found:
                found.append(match)
                if len(found) >= _ANCHORS_PER_KIND:
                    break
        if found:
            lines.append(f"{label}: {'  '.join(found)}")
    block = "\n".join(lines)
    if len(block) > _ANCHORS_CHAR_CAP:
        block = block[:_ANCHORS_CHAR_CAP] + "…"
    return block


def split_head(content: str) -> tuple[str, str]:
    """把首条用户消息拆成 (原始任务, 上一次的摘要)。

    多次压缩时摘要不能层层累加——旧摘要要拆出来重新参与摘要，
    而原始任务永远原文保留。
    """
    if CONTEXT_PREFIX in content:
        original, _, previous = content.partition(CONTEXT_PREFIX)
        return original.rstrip(), previous.strip()
    return content, ""

#  喂给摘要模型时，单条工具输出最多保留这么多字符
_TOOL_OUTPUT_CAP = 600

#  整段 transcript 的字符上限。
#  ⚠️ 摘要走的是便宜模型，它的上下文窗口通常比主模型小得多——
#  主模型 180k 的历史直接丢过去会超窗。超了就砍中段：
#  开头保留（原始目标），结尾保留（最近进展），中间省略。
MAX_TRANSCRIPT_CHARS = 60_000

_ELLIPSIS = "\n\n……（中段省略，仅保留开头的目标与最近的进展）……\n\n"


def clamp(text: str, cap: int = MAX_TRANSCRIPT_CHARS) -> str:
    """超长 transcript 砍中段，保头保尾。"""
    if len(text) <= cap:
        return text
    head = int(cap * 0.4)
    tail = cap - head - len(_ELLIPSIS)
    return text[:head] + _ELLIPSIS + text[-tail:]


#  单次压缩至少要省出这个比例才算"有效"
MIN_SAVING_RATIO = 0.10

#  压缩后保留的"用户原话"预算（估算 token，约为上下文窗口的 7%，
#  按小羽默认 180k 窗口折算）：摘要最容易丢失的就是用户的原话、偏好和约束，
#  从被压缩区间里把 user 消息原文备份下来，成本极低、保真度提升巨大。
USER_VOICE_TOKENS = 12_000


def collect_user_voice(
    older: list[dict[str, Any]],
    budget_tokens: int = USER_VOICE_TOKENS,
    synthetic_texts: frozenset[str] = frozenset(),
) -> str:
    """从被压缩区间收集用户消息原文，从最新往回装、装满为止。

    synthetic_texts 是 harness 注入的伪 user 消息（收尾指令等），不算用户原话。
    最后一条装不下的砍中段保留（middle-truncate），而不是整条丢弃。
    """
    picked: list[str] = []
    remaining = budget_tokens
    for message in reversed(older):
        if message.get("role") != "user":
            continue
        content = media.text_of(message.get("content")).strip()
        if not content or content in synthetic_texts:
            continue
        cost = tokens.estimate_text(content)
        if cost <= remaining:
            picked.append(content)
            remaining -= cost
        else:
            if remaining > 200:
                #  token 预算粗换算成字符预算（混合中英文按 ~3 字符/token）
                picked.append(clamp(content, cap=remaining * 3))
            break
    if not picked:
        return ""
    picked.reverse()
    body = "\n---\n".join(picked)
    return (
        "\n\n[被压缩区间内用户消息的原文备份（按时间顺序，逐条以 --- 分隔）。"
        "用户原话是最高优先级的信息，摘要与此有出入时以这里为准]\n" + body
    )


@dataclass
class CompactionState:
    count: int = 0
    #  上一次压缩省下来的估算 token
    saved_tokens: int = 0
    #  连续失败次数，失败太多就不再自动尝试，免得每轮都白烧一次调用
    failures: int = 0
    #  断路器：连续几次压缩都省不到 MIN_SAVING_RATIO 就停止自动压缩。
    #  这种情况说明剩下的都是压不动的内容（近期消息 + 已有摘要），
    #  再压只是白烧摘要调用还磨损细节。手动 /compact 不受限。
    ineffective: int = 0


class Compactor:
    def __init__(
        self,
        context_limit: int,
        compact_at: float,
        keep_recent: int,
        summarizer: Callable[[str, list[dict[str, Any]]], str],
        transcript_cap: int = MAX_TRANSCRIPT_CHARS,
        synthetic_user_texts: frozenset[str] = frozenset(),
        user_voice_tokens: int = USER_VOICE_TOKENS,
    ) -> None:
        self.context_limit = context_limit
        self.compact_at = compact_at
        self.keep_recent = keep_recent
        self.summarizer = summarizer
        self.transcript_cap = transcript_cap
        #  harness 注入的伪 user 消息（收尾指令等）：不算"用户原话"
        self.synthetic_user_texts = synthetic_user_texts
        #  用户原话备份的预算（0 = 关闭）
        self.user_voice_tokens = user_voice_tokens
        self.state = CompactionState()

    # ---------- 判断 ----------

    def budget(self) -> int:
        return int(self.context_limit * self.compact_at)

    def should_compact(self, estimated: int) -> bool:
        if self.state.failures >= 2:
            return False
        if self.state.ineffective >= 2:
            return False
        return estimated >= self.budget()

    # ---------- 切点 ----------

    def find_cut(self, messages: list[dict[str, Any]], min_index: int = 1) -> int:
        """返回安全切点下标；返回 -1 表示这次没法安全压缩。

        唯一的硬约束：**保留段不能以 tool 消息开头**——它的 assistant 被压掉了就成了
        孤儿 tool_call_id，下一次请求直接 400。

        反过来，切在 assistant（哪怕它带 tool_calls）上是安全的：它的 tool 结果排在
        它后面，会跟它一起被保留。

        ⚠️ 早期版本要求切点必须是 user 消息，那是错的：编码 agent 的上下文主要是在
        单个 turn 内烧掉的（一路读文件、跑命令），整段可能只有一条 user 消息，
        那个规则会让压缩永远不触发。

        min_index 之前的消息受保护（system prompt 和原始任务描述）。
        """
        target = max(min_index, len(messages) - self.keep_recent)
        for index in range(target, len(messages)):
            if messages[index].get("role") != "tool":
                #  切点等于 min_index 意味着一条都没压到，没意义
                return index if index > min_index else -1
        return -1

    # ---------- 执行 ----------

    def _fail(
        self, messages: list[dict[str, Any]], reason: str
    ) -> tuple[list[dict[str, Any]], str]:
        """压缩失败的统一出口：历史原样保留，失败计数 +1。

        撞到断路器阈值时给出自救命令清单（/compact /usage /clear
        三条指令）：自动路径断了，用户得知道手动出口在哪。
        """
        self.state.failures += 1
        note = f"压缩失败（历史已保留）：{reason}"
        if self.state.failures >= 2:
            note += (
                "。自动压缩已暂停——可 /compact 手动重试、/context 查看占用、"
                "/clear 清空对话，或换个摘要模型再试"
            )
        return messages, note

    def compact(self, messages: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], str]:
        """返回 (新消息列表, 结果说明)。失败或无益时原样返回。

        压缩后的形状固定为：[system, user(原始任务 + 摘要), ...最近若干条]
        —— 只有一条 user 消息，和压缩前一样，不会产生连续 user 消息
        （Anthropic 系要求角色交替，连续 user 有被拒或被静默合并的风险）。
        """
        #  首条 user 消息是任务定义，最不该丢，永远原文保留
        has_task = len(messages) > 1 and messages[1].get("role") == "user"
        head_end = 2 if has_task else 1

        cut = self.find_cut(messages, min_index=head_end)
        if cut < 0:
            return messages, f"跳过：除最近 {self.keep_recent} 条外没有可压缩的历史"

        older = messages[head_end:cut]
        before = tokens.estimate_messages(messages)

        #  不划算早退：摘要正文 + 分界标记 + 原话备份 + 锚点索引自身就有固定
        #  开销，可压区间比这个量级还小时，十有八九落进后面"反而更大"的放弃
        #  路径——那就别花这次注定白费的摘要调用。阈值随窗口缩放（2%）并
        #  封顶：小窗口按比例、大窗口不至于把明明可压的区间也拦掉。
        region = tokens.estimate_messages(older)
        floor = min(2_000, int(self.context_limit * 0.02))
        if region <= floor:
            return messages, (
                f"跳过：可压区间仅约 {region} tok，低于摘要固定开销的量级（{floor}）"
            )

        original, previous_summary = (
            split_head(media.text_of(messages[1].get("content"))) if has_task else ("", "")
        )
        transcript = render(older)
        if previous_summary:
            #  上一次的摘要要一起重新摘要，否则多次压缩会层层累加
            transcript = f"【此前的压缩摘要】\n{previous_summary}\n\n【之后的新内容】\n{transcript}"

        #  前缀 = 被压缩区间的**逐字消息**（含 system 与任务头；find_cut 保证
        #  切点不劈开 tool 配对）。summarizer 路由到主模型那一腿时用它做
        #  前缀重放，其余腿用渲染转写——两种姿势由 summarizer 自己按路由选。
        prefix = messages[:cut]
        try:
            summary = self.summarizer(clamp(transcript, self.transcript_cap), prefix)
        except Exception:  # noqa: BLE001 - 压缩失败必须保住历史
            #  降级阶梯：失败最常见的原因是
            #  transcript 对便宜摘要模型仍太大（clamp 上限按主模型窗口拍的）——
            #  减半再试一次，还失败才放弃。摘要模型链的回退在 summarizer 内部，
            #  走到这里说明整条链都没接住，只能从输入侧想办法。
            try:
                summary = self.summarizer(clamp(transcript, self.transcript_cap // 2), prefix)
            except Exception as exc:  # noqa: BLE001
                return self._fail(messages, f"{type(exc).__name__}: {exc}")

        if not summary.strip():
            return self._fail(messages, "摘要为空")

        #  用户原话备份跟在摘要后面（同一条 user 消息里，不引入新的消息角色问题）。
        #  摘要是有损的，用户消息是最不可丢的——原文备份的成本远低于丢失的代价。
        voice = (
            collect_user_voice(
                older, self.user_voice_tokens, synthetic_texts=self.synthetic_user_texts
            )
            if self.user_voice_tokens > 0
            else ""
        )
        #  逐字锚点索引附在摘要后：摘要可以释义，索引不许（机械提取）。
        #  它也一并过消毒——原文里万一含分界标记，不能借索引复活
        anchors = anchor_index(older)
        if anchors:
            anchors = (
                "\n\n【索引】以下标识符逐字取自被压缩的原文（机械提取，未经改写）：\n"
                + sanitize_summary(anchors)
            )
        #  消毒后再拼分界标记：正文里复读的标记被打断，split_head 永远只认这里拼的这一个
        summary_block = CONTEXT_PREFIX + sanitize_summary(summary.strip()) + anchors + voice
        if has_task:
            head = {"role": "user", "content": f"{original}\n\n{summary_block}"}
        else:
            head = {"role": "user", "content": summary_block}
        compacted = merge_consecutive_users([messages[0], head, *messages[cut:]])

        after = tokens.estimate_messages(compacted)
        if after >= before:
            #  摘要比原文还长 → 压缩没有意义，退回原状。
            #  不做这道检查的话会出现"压缩后上下文反而变大"，而且会白丢历史细节。
            self.state.failures += 1
            return messages, (
                f"跳过：摘要后反而更大（{before} → {after} tok），已放弃本次压缩"
            )

        self.state.count += 1
        self.state.saved_tokens = before - after
        self.state.failures = 0

        note = (
            f"已压缩 {len(older)} 条历史消息，估算 {before} → {after} tok"
            f"（省 {self.state.saved_tokens}）"
        )
        if before > 0 and self.state.saved_tokens < before * MIN_SAVING_RATIO:
            self.state.ineffective += 1
            if self.state.ineffective >= 2:
                note += "；连续两次收效甚微，暂停自动压缩（手动 /compact 仍可用）"
        else:
            self.state.ineffective = 0
        return compacted, note


def _joined(first: Any, second: Any) -> Any:
    """两条 user 消息的内容拼成一条。

    任一侧带图片就走部件列表拼接——按原文拼接是这个函数的承诺（"不丢东西"），
    图片当然也在承诺之内；两侧都是纯文本时仍返回字符串，历史形态不无谓地变复杂。
    """
    if media.is_parts(first) or media.is_parts(second):
        return media.as_parts(first) + media.as_parts(second)
    return f"{first or ''}\n\n{second or ''}".strip()


def merge_consecutive_users(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把相邻的 user 消息并成一条。

    切点如果正好落在 user 消息上，压缩后的头部就会和它相邻，产生连续两条 user
    消息 —— Anthropic 系要求角色严格交替，这种序列会被拒（或被网关静默合并，
    行为不可控）。内容按原文拼接，不丢东西。
    """
    merged: list[dict[str, Any]] = []
    for message in messages:
        if (
            merged
            and message.get("role") == "user"
            and merged[-1].get("role") == "user"
            and not merged[-1].get("tool_calls")
        ):
            previous = merged[-1]
            merged[-1] = {
                "role": "user",
                "content": _joined(previous.get("content"), message.get("content")),
            }
            continue
        merged.append(message)
    return merged


def render(messages: list[dict[str, Any]]) -> str:
    """把消息列表渲染成给摘要模型看的纯文本。"""
    lines: list[str] = []
    for message in messages:
        role = message.get("role")
        content = media.text_of(message.get("content")).strip()

        if role == "user":
            lines.append(f"【用户】{content}")
        elif role == "assistant":
            if content:
                lines.append(f"【小羽】{content}")
            for call in message.get("tool_calls") or []:
                function = call.get("function", {})
                args = (function.get("arguments") or "").replace("\n", " ")
                lines.append(f"【调用工具】{function.get('name')}({args[:200]})")
        elif role == "tool":
            snippet = content[:_TOOL_OUTPUT_CAP]
            if len(content) > _TOOL_OUTPUT_CAP:
                snippet += f"…（原输出 {len(content)} 字符，此处截断）"
            lines.append(f"【工具结果】{snippet}")
    return "\n".join(lines)
