"""token 估算与上下文压缩的测试。不打网络：摘要器用桩替换。"""

from __future__ import annotations

import unittest

from xiaoyu import tokens
from xiaoyu.compaction import (
    CONTEXT_PREFIX,
    MIN_SUMMARY_CHARS,
    Compactor,
    anchor_index,
    collect_user_voice,
    is_degenerate_summary,
    render,
    sanitize_summary,
    split_head,
)


def conversation() -> list[dict]:
    """一段有代表性的历史：含 tool_calls 配对、单调用与并行双调用。"""
    return [
        {"role": "system", "content": "你是小羽"},
        {"role": "user", "content": "看一下 calc.py"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "read_file", "arguments": '{"path":"calc.py"}'}}
            ],
        },
        {"role": "tool", "tool_call_id": "c1", "content": "def add(a, b): ..."},
        {"role": "assistant", "content": "看到了，两个函数"},
        {"role": "user", "content": "把 div 的除零补上"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {"id": "c2", "type": "function", "function": {"name": "str_replace", "arguments": '{"path":"calc.py"}'}},
                {"id": "c3", "type": "function", "function": {"name": "bash", "arguments": '{"command":"pytest"}'}},
            ],
        },
        {"role": "tool", "tool_call_id": "c2", "content": "已替换"},
        {"role": "tool", "tool_call_id": "c3", "content": "exit_status: 0"},
        {"role": "assistant", "content": "改完了，测试通过"},
    ]


def assert_valid_sequence(case: unittest.TestCase, messages: list[dict]) -> None:
    """校验消息序列不会被 API 拒收：

    1. tool_calls 与 tool 结果必须一一配对
    2. 不能出现连续两条 user 消息（Anthropic 系要求角色交替）
    """
    case.assertEqual(messages[0]["role"], "system", "system prompt 必须在首位")

    expected: list[str] = []
    previous_role: str | None = None
    for message in messages:
        role = message.get("role")
        if role == "user":
            case.assertNotEqual(previous_role, "user", "出现了连续两条 user 消息")
        if role == "assistant":
            case.assertEqual(expected, [], f"上一批 tool_calls 缺少结果：{expected}")
            expected = [call["id"] for call in message.get("tool_calls") or []]
        elif role == "tool":
            case.assertIn(
                message["tool_call_id"], expected, "出现了没有对应 tool_calls 的孤儿 tool 消息"
            )
            expected.remove(message["tool_call_id"])
        elif role == "user":
            case.assertEqual(expected, [], f"tool_calls 未闭合就开始新一轮：{expected}")
        previous_role = role
    case.assertEqual(expected, [], f"末尾还有未闭合的 tool_calls：{expected}")


class TestEstimate(unittest.TestCase):
    def test_empty(self) -> None:
        self.assertEqual(tokens.estimate_text(""), 0)
        self.assertEqual(tokens.estimate_text(None), 0)

    def test_cjk_costs_more_per_char_than_ascii(self) -> None:
        chinese = tokens.estimate_text("中文字符测试内容一二三四五")
        ascii_text = tokens.estimate_text("a" * 13)
        self.assertGreater(chinese, ascii_text)

    def test_monotonic(self) -> None:
        short = tokens.estimate_text("def add(a, b): return a + b")
        long = tokens.estimate_text("def add(a, b): return a + b" * 10)
        self.assertGreater(long, short)

    def test_tool_calls_are_counted(self) -> None:
        plain = {"role": "assistant", "content": "hi"}
        with_calls = {
            "role": "assistant",
            "content": "hi",
            "tool_calls": [
                {"id": "x", "function": {"name": "bash", "arguments": '{"command":"ls -la /tmp"}'}}
            ],
        }
        self.assertGreater(tokens.estimate_message(with_calls), tokens.estimate_message(plain))

    def test_tool_schemas_are_counted(self) -> None:
        self.assertEqual(tokens.estimate_tools([]), 0)
        self.assertGreater(
            tokens.estimate_tools([{"type": "function", "function": {"name": "bash"}}]), 0
        )


def single_turn_conversation(rounds: int = 6) -> list[dict]:
    """编码 agent 最常见的形态：**只有一条 user 消息**，后面全是工具往复。

    上下文主要就是在这种单 turn 里烧掉的。压缩必须能在这里工作。
    """
    messages: list[dict] = [
        {"role": "system", "content": "你是小羽"},
        {"role": "user", "content": "读完三个模块再写个汇总"},
    ]
    for n in range(rounds):
        messages.append(
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": f"t{n}",
                        "type": "function",
                        "function": {"name": "read_file", "arguments": f'{{"path":"m{n}.py"}}'},
                    }
                ],
            }
        )
        messages.append({"role": "tool", "tool_call_id": f"t{n}", "content": f"VERSION = 'v{n}'"})
    return messages


class TestFindCut(unittest.TestCase):
    def build(self, keep_recent: int) -> Compactor:
        return Compactor(
            context_limit=1000, compact_at=0.7, keep_recent=keep_recent, summarizer=lambda _t, _p: "摘要"
        )

    def test_cuts_at_user_boundary(self) -> None:
        self.assertEqual(self.build(5).find_cut(conversation()), 5)

    def test_can_cut_at_assistant_with_tool_calls(self) -> None:
        #  切在 assistant(tool_calls) 上是安全的：它的 tool 结果跟着一起保留
        messages = conversation()
        cut = self.build(4).find_cut(messages)
        self.assertEqual(cut, 6)
        self.assertEqual(messages[cut]["role"], "assistant")

    def test_never_starts_retained_segment_with_tool(self) -> None:
        #  keep_recent=3 时 target 落在 tool 上，必须往后挪到非 tool 消息
        messages = conversation()
        cut = self.build(3).find_cut(messages)
        self.assertNotEqual(messages[cut]["role"], "tool")

    def test_works_inside_a_single_turn(self) -> None:
        """回归测试：早期版本要求切点是 user 消息，导致单 turn 内永远压不了。"""
        messages = single_turn_conversation(rounds=6)
        self.assertEqual(sum(1 for m in messages if m["role"] == "user"), 1)
        cut = self.build(4).find_cut(messages)
        self.assertGreater(cut, 1, "单 turn 会话里也必须能找到切点")
        self.assertNotEqual(messages[cut]["role"], "tool")

    def test_no_history_to_compact(self) -> None:
        messages = [{"role": "system", "content": "s"}, {"role": "user", "content": "hi"}]
        self.assertEqual(self.build(1).find_cut(messages), -1)


class TestCollectUserVoice(unittest.TestCase):
    def test_picks_user_messages_in_order(self) -> None:
        older = [
            {"role": "user", "content": "第一条要求"},
            {"role": "tool", "tool_call_id": "x", "content": "噪声"},
            {"role": "assistant", "content": "好的"},
            {"role": "user", "content": "第二条要求"},
        ]
        voice = collect_user_voice(older, budget_tokens=1000)
        self.assertIn("第一条要求", voice)
        self.assertIn("第二条要求", voice)
        self.assertNotIn("噪声", voice)
        self.assertLess(voice.index("第一条要求"), voice.index("第二条要求"))

    def test_budget_prefers_recent_and_truncates_overflow(self) -> None:
        older = [
            {"role": "user", "content": "早期长要求" + "x" * 4000},
            {"role": "user", "content": "最近的要求"},
        ]
        voice = collect_user_voice(older, budget_tokens=300)
        self.assertIn("最近的要求", voice)  # 从最新往回装
        #  溢出的那条砍中段保留（保头保尾），不是整条丢弃
        self.assertIn("早期长要求", voice)
        self.assertIn("中段省略", voice)

    def test_synthetic_texts_excluded(self) -> None:
        older = [{"role": "user", "content": "收尾指令"}]
        self.assertEqual(
            collect_user_voice(older, synthetic_texts=frozenset({"收尾指令"})), ""
        )

    def test_empty_when_no_user_messages(self) -> None:
        self.assertEqual(collect_user_voice([{"role": "assistant", "content": "x"}]), "")


class TestCompact(unittest.TestCase):
    def test_summarizer_receives_verbatim_prefix(self) -> None:
        """summarizer 收到的前缀 = 被压缩区间的逐字消息（前缀重放）。

        前缀必须以 system 开头、与原消息逐字一致、且不劈开 tool 配对——
        主模型腿拿它重放吃 KV 缓存，任何改写都会作废缓存。
        """
        captured: dict = {}

        def summarizer(transcript: str, prefix: list) -> str:
            captured["prefix"] = prefix
            return "摘要"

        compactor = Compactor(
            context_limit=1000, compact_at=0.7, keep_recent=4, summarizer=summarizer
        )
        messages = single_turn_conversation(rounds=8)
        compactor.compact(messages)
        prefix = captured["prefix"]
        self.assertEqual(prefix[0]["role"], "system")
        self.assertEqual(prefix, messages[: len(prefix)], "前缀必须逐字等于原消息")
        #  不劈开配对：前缀里每个 tool_call 都有对应的 tool 结果
        call_ids = {
            call["id"]
            for message in prefix
            for call in message.get("tool_calls") or []
        }
        answered = {
            message.get("tool_call_id") for message in prefix if message.get("role") == "tool"
        }
        self.assertEqual(call_ids, answered - {None})

    def test_happy_path_keeps_sequence_valid(self) -> None:
        compactor = Compactor(
            context_limit=1000,
            compact_at=0.7,
            keep_recent=5,
            #  摘要必须比被压掉的原文短，否则会触发"反而更大"的回退保护
            summarizer=lambda _t, _p: "读过 calc.py",
        )
        original = conversation()
        #  把被压区间充实到真实尺度：压缩的固定文案开销（交接前缀等）只有在
        #  被压内容足够大时才有净节省——生产里触发压缩时上下文都是十几万 token
        original[3]["content"] = "def add(a, b): ...\n" * 40
        messages, note = compactor.compact(original)

        assert_valid_sequence(self, messages)
        #  切点落在 user 消息上时，头部与它相邻 → 必须被并成一条
        self.assertEqual(sum(1 for m in messages if m["role"] == "user"), 1)
        #  原始任务原文保留 + 摘要追加在后面
        self.assertEqual(messages[1]["role"], "user")
        self.assertTrue(messages[1]["content"].startswith("看一下 calc.py"))
        self.assertIn(CONTEXT_PREFIX, messages[1]["content"])
        self.assertIn("读过 calc.py", messages[1]["content"])
        #  被并进来的那条近期指令不能丢
        self.assertIn("把 div 的除零补上", messages[1]["content"])
        #  被压掉的早期内容不该再出现
        self.assertNotIn("def add(a, b): ...", str(messages))
        #  最近的内容必须原样保留
        self.assertIn("改完了，测试通过", str(messages))
        self.assertIn("已压缩", note)
        self.assertEqual(compactor.state.count, 1)

    def test_user_voice_preserved_in_head(self) -> None:
        """压缩后被压区间的用户原话要备份进头部（原始 user 消息不丢）。"""
        compactor = Compactor(
            context_limit=1000,
            compact_at=0.7,
            keep_recent=4,
            summarizer=lambda _t, _p: "做了一些事",
            synthetic_user_texts=frozenset({"收尾指令"}),
        )
        messages = single_turn_conversation(rounds=8)
        #  充实被压区间（压缩要有净节省，交接前缀的固定开销才摊得开）
        messages[3]["content"] = "VERSION = 'v0'\n" * 40
        #  在被压区间插入两条用户消息：一条真实、一条 harness 注入的伪消息
        messages.insert(4, {"role": "user", "content": "重要约束：不要动 config.py"})
        messages.insert(5, {"role": "user", "content": "收尾指令"})
        compacted, note = compactor.compact(messages)
        self.assertIn("已压缩", note)
        head = compacted[1]["content"]
        self.assertIn("重要约束：不要动 config.py", head)  # 用户原话进了备份
        self.assertNotIn("收尾指令", head)  # 伪 user 消息不算原话

    def test_original_task_is_never_compacted(self) -> None:
        """任务定义是最不该丢的一条——早期版本把它压掉了。"""
        compactor = Compactor(
            context_limit=1000, compact_at=0.7, keep_recent=4, summarizer=lambda _t, _p: "摘要" * 50
        )
        messages, _ = compactor.compact(single_turn_conversation(rounds=8))
        self.assertIn("读完三个模块再写个汇总", messages[1]["content"])

    def test_reverts_when_summary_is_bigger(self) -> None:
        """摘要比原文还长时必须放弃——否则"压缩"会让上下文变大。"""
        original = single_turn_conversation(rounds=3)
        compactor = Compactor(
            context_limit=1000,
            compact_at=0.7,
            keep_recent=4,
            summarizer=lambda _t, _p: "非常长的摘要内容" * 500,
        )
        messages, note = compactor.compact(original)
        self.assertEqual(messages, original)
        self.assertIn("反而更大", note)

    def test_summary_does_not_accumulate_across_compactions(self) -> None:
        """多次压缩时旧摘要要被重新摘要，不能层层叠加。"""
        seen: list[str] = []

        def summarizer(transcript: str, _prefix: list) -> str:
            seen.append(transcript)
            return f"第 {len(seen)} 版摘要"

        compactor = Compactor(
            context_limit=1000, compact_at=0.7, keep_recent=4, summarizer=summarizer
        )
        messages, _ = compactor.compact(single_turn_conversation(rounds=8))
        #  再追加一轮新工作后二次压缩
        messages.extend(
            [
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {"id": "z1", "type": "function", "function": {"name": "bash", "arguments": "{}"}}
                    ],
                },
                {"role": "tool", "tool_call_id": "z1", "content": "exit_status: 0"},
                {"role": "assistant", "content": "完成"},
            ]
        )
        messages, note = compactor.compact(messages)

        content = messages[1]["content"]
        self.assertIn("第 2 版摘要", content)
        self.assertNotIn("第 1 版摘要", content, "旧摘要不该原样留在消息里")
        self.assertIn("此前的压缩摘要", seen[1], "旧摘要必须参与二次摘要，否则事实会丢")
        self.assertEqual(content.count(CONTEXT_PREFIX), 1)

    def test_summarizer_failure_keeps_history(self) -> None:
        def boom(_: str, _p: list) -> str:
            raise RuntimeError("网关超时")

        original = conversation()
        compactor = Compactor(
            context_limit=1000, compact_at=0.7, keep_recent=5, summarizer=boom
        )
        messages, note = compactor.compact(original)

        self.assertEqual(messages, original, "压缩失败绝不能丢历史")
        self.assertIn("压缩失败", note)
        self.assertEqual(compactor.state.failures, 1)

    def test_empty_summary_keeps_history(self) -> None:
        original = conversation()
        compactor = Compactor(
            context_limit=1000, compact_at=0.7, keep_recent=5, summarizer=lambda _t, _p: "   "
        )
        messages, note = compactor.compact(original)
        self.assertEqual(messages, original)
        self.assertIn("摘要为空", note)

    def test_failure_retries_with_smaller_transcript(self) -> None:
        """降级阶梯：首次失败减半 transcript 再试一次。"""
        seen: list[int] = []

        def flaky(text: str, _p: list) -> str:
            seen.append(len(text))
            if len(seen) == 1:
                raise RuntimeError("对摘要模型太大")
            return "摘要"

        big = conversation()
        big[3]["content"] = "x" * 5000  # 撑大 transcript，让 cap 真正起作用
        compactor = Compactor(
            context_limit=1000,
            compact_at=0.7,
            keep_recent=5,
            summarizer=flaky,
            transcript_cap=400,
        )
        messages, note = compactor.compact(big)
        self.assertEqual(len(seen), 2, "失败后应减半重试一次")
        self.assertLess(seen[1], seen[0], "重试的 transcript 必须更小")
        self.assertIn("已压缩", note)
        self.assertEqual(compactor.state.failures, 0, "阶梯内成功不算失败")

    def test_giving_up_twice_offers_self_help(self) -> None:
        """撞到断路器时给自救命令清单：自动路径断了，手动出口要指给用户。"""

        def boom(_: str, _p: list) -> str:
            raise RuntimeError("网关炸了")

        compactor = Compactor(
            context_limit=1000, compact_at=0.7, keep_recent=5, summarizer=boom
        )
        _, first_note = compactor.compact(conversation())
        self.assertNotIn("/compact", first_note, "第一次失败还轮不到自救清单")
        _, note = compactor.compact(conversation())
        self.assertEqual(compactor.state.failures, 2)
        self.assertIn("自动压缩已暂停", note)
        self.assertIn("/compact", note)
        self.assertFalse(compactor.should_compact(10**9), "断路器应停掉自动压缩")

    def test_unsafe_cut_is_skipped(self) -> None:
        #  历史太短，压掉之后没剩什么可摘要的 → 应该跳过
        original = [
            {"role": "system", "content": "s"},
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
        ]
        compactor = Compactor(
            context_limit=1000, compact_at=0.7, keep_recent=8, summarizer=lambda _t, _p: "摘要"
        )
        messages, note = compactor.compact(original)
        self.assertEqual(messages, original)
        self.assertIn("跳过", note)

    def test_single_turn_compaction_keeps_sequence_valid(self) -> None:
        """单 turn 里压缩后，序列必须仍然合法（这是最容易出 400 的地方）。"""
        compactor = Compactor(
            context_limit=1000,
            compact_at=0.7,
            keep_recent=4,
            summarizer=lambda _t, _p: "已读完 m0-m3，版本号 v0 v1 v2 v3",
        )
        messages, note = compactor.compact(single_turn_conversation(rounds=6))
        assert_valid_sequence(self, messages)
        self.assertIn("已压缩", note)
        self.assertIn("版本号 v0", messages[1]["content"])

    def test_threshold(self) -> None:
        compactor = Compactor(
            context_limit=1000, compact_at=0.7, keep_recent=5, summarizer=lambda _t, _p: "摘要"
        )
        self.assertEqual(compactor.budget(), 700)
        self.assertFalse(compactor.should_compact(699))
        self.assertTrue(compactor.should_compact(700))

    def test_backs_off_after_repeated_failures(self) -> None:
        compactor = Compactor(
            context_limit=1000, compact_at=0.7, keep_recent=5, summarizer=lambda _t, _p: "摘要"
        )
        compactor.state.failures = 2
        self.assertFalse(compactor.should_compact(999), "连续失败后不该每轮都白烧一次调用")

    def test_breaker_trips_after_ineffective_compactions(self) -> None:
        """断路器：连续两次收效甚微（<10%）就停止自动压缩。"""
        compactor = Compactor(
            context_limit=1000, compact_at=0.7, keep_recent=5, summarizer=lambda _t, _p: "摘要"
        )
        compactor.state.ineffective = 2
        self.assertFalse(compactor.should_compact(999), "无效压缩连续两次后应停手")
        #  失败计数和无效计数是两条独立线
        compactor.state.ineffective = 1
        self.assertTrue(compactor.should_compact(999))

    def test_ineffective_counter_tracks_saving_ratio(self) -> None:
        """省得少计数递增；省得多归零。"""
        #  摘要几乎和原文一样长 → 省不到 10% → ineffective +1
        long_summary = "x" * 3000
        compactor = Compactor(
            context_limit=1000,
            compact_at=0.7,
            keep_recent=2,
            summarizer=lambda _t, _p: long_summary,
        )
        messages = single_turn_conversation(rounds=8)
        _, note = compactor.compact(messages)
        if "已压缩" in note:
            self.assertEqual(compactor.state.ineffective, 1, note)

        #  摘要极短 → 大幅节省 → 归零
        effective = Compactor(
            context_limit=1000, compact_at=0.7, keep_recent=2, summarizer=lambda _t, _p: "很短的摘要"
        )
        effective.state.ineffective = 1
        _, note = effective.compact(single_turn_conversation(rounds=8))
        self.assertIn("已压缩", note)
        self.assertEqual(effective.state.ineffective, 0, "有效压缩应重置断路器计数")


class TestUsageAccounting(unittest.TestCase):
    def test_tracks_per_model(self) -> None:
        from xiaoyu.agent import Usage

        usage = Usage()
        usage.add("pricey-model", 1000, 200)
        usage.add("pricey-model", 1500, 300)
        usage.add("deepseek-v4-flash", 8000, 400)

        self.assertEqual(usage.turns, 3)
        self.assertEqual(usage.prompt_tokens, 10500)
        self.assertEqual(usage.completion_tokens, 900)
        self.assertEqual(usage.by_model["deepseek-v4-flash"].calls, 1)
        self.assertEqual(usage.by_model["pricey-model"].calls, 2)
        #  摘要跑在便宜模型上这件事必须能看出来，否则算不出省了多少
        self.assertIn("deepseek-v4-flash", str(usage))

    def test_empty(self) -> None:
        from xiaoyu.agent import Usage

        self.assertIn("还没有", str(Usage()))


class TestClamp(unittest.TestCase):
    def test_short_text_untouched(self) -> None:
        from xiaoyu.compaction import clamp

        self.assertEqual(clamp("短文本", cap=100), "短文本")

    def test_keeps_head_and_tail(self) -> None:
        from xiaoyu.compaction import clamp

        text = "开头是原始目标" + "填" * 5000 + "结尾是最近进展"
        clamped = clamp(text, cap=1000)
        self.assertLessEqual(len(clamped), 1000)
        #  摘要模型的窗口比主模型小，砍中段时目标和最近进展都不能丢
        self.assertTrue(clamped.startswith("开头是原始目标"))
        self.assertTrue(clamped.endswith("结尾是最近进展"))
        self.assertIn("中段省略", clamped)


class TestRender(unittest.TestCase):
    def test_covers_all_roles_and_truncates_tool_output(self) -> None:
        messages = conversation()[1:]
        messages.append({"role": "tool", "tool_call_id": "c9", "content": "x" * 2000})
        text = render(messages)
        self.assertIn("【用户】", text)
        self.assertIn("【小羽】", text)
        self.assertIn("【调用工具】read_file", text)
        self.assertIn("【工具结果】", text)
        self.assertIn("此处截断", text)
        self.assertLess(len(text), 4000)


class TestSummaryGuards(unittest.TestCase):
    """摘要退化检测与分界标记消毒。"""

    def test_degenerate_below_threshold(self) -> None:
        self.assertTrue(is_degenerate_summary(""))
        self.assertTrue(is_degenerate_summary("   "))
        self.assertTrue(is_degenerate_summary("摘要：修了个 bug。"))
        self.assertFalse(is_degenerate_summary("长" * MIN_SUMMARY_CHARS))

    def test_whitespace_padding_does_not_pass(self) -> None:
        """靠空白凑长度的输出照样算退化——阈值看的是清洗后的字符数。"""
        self.assertTrue(is_degenerate_summary("短。" + " " * MIN_SUMMARY_CHARS))

    def test_sanitize_defuses_echoed_prefix_without_deleting(self) -> None:
        echoed = f"正文开头\n{CONTEXT_PREFIX}被复读标记之后的内容"
        cleaned = sanitize_summary(echoed)
        self.assertNotIn(CONTEXT_PREFIX, cleaned)
        self.assertIn("正文开头", cleaned)
        self.assertIn("被复读标记之后的内容", cleaned)

    def test_split_head_ignores_defused_prefix(self) -> None:
        """经消毒的正文即使复读过标记，下次压缩 split_head 也只认真标记。"""
        body = sanitize_summary(f"摘要正文{CONTEXT_PREFIX}尾巴")
        head = f"原始任务\n\n{CONTEXT_PREFIX}{body}"
        original, previous = split_head(head)
        self.assertEqual(original, "原始任务")
        self.assertIn("摘要正文", previous)
        self.assertIn("尾巴", previous)

    def test_compactor_sanitizes_before_assembly(self) -> None:
        """Compactor.compact 拼装前消毒：摘要器复读标记也不会造出第二个活标记。"""
        summary = f"要点若干{CONTEXT_PREFIX}复读的假标记，其余照旧"
        compactor = Compactor(
            context_limit=1000,
            compact_at=0.5,
            keep_recent=2,
            summarizer=lambda _t, _p: summary,
            #  关闭用户原话备份：本用例的追加消息很大，全量备份会触发
            #  "摘要后反而更大"的回退，测不到拼装路径
            user_voice_tokens=0,
        )
        messages = conversation() + [
            {"role": "user", "content": f"追加要求 {i}：" + "字" * 500} for i in range(6)
        ]
        compacted, note = compactor.compact(messages)
        self.assertIsNot(compacted, messages, note)
        head_text = compacted[1]["content"]
        #  整条头部消息里只有拼装时加的那一个活标记
        self.assertEqual(head_text.count(CONTEXT_PREFIX), 1)


class TestAnchorIndex(unittest.TestCase):
    """机械锚点索引：摘要可以释义，逐字标识符不许丢——正则收割、零模型调用。"""

    def test_harvests_each_kind_verbatim(self) -> None:
        messages = [
            {
                "role": "assistant",
                "content": "看 src/app/main.py 和 https://example.com/x?a=1，提交 deadbee1234，工单 #4567",
                "tool_calls": [
                    {"id": "c1", "function": {"name": "read_file",
                                              "arguments": '{"path": "docs/guide/setup.md"}'}}
                ],
            },
            {"role": "tool", "tool_call_id": "c1", "content": "TypeError: bad thing"},
        ]
        block = anchor_index(messages)
        for needle in (
            "src/app/main.py", "https://example.com/x?a=1", "deadbee1234",
            "#4567", "TypeError", "docs/guide/setup.md",
        ):
            self.assertIn(needle, block)

    def test_dedup_and_per_kind_cap(self) -> None:
        content = "a/b.py " * 50 + " ".join(f"pkg{i}/mod{i}.py" for i in range(40))
        block = anchor_index([{"role": "user", "content": content}])
        self.assertEqual(block.count("a/b.py"), 1)  # 去重
        (paths_line,) = [line for line in block.splitlines() if line.startswith("路径")]
        self.assertLessEqual(len(paths_line.split("  ")), 15)  # 每类封顶
        self.assertLessEqual(len(block), 1201)  # 总量封顶

    def test_empty_when_nothing_found(self) -> None:
        self.assertEqual(anchor_index([{"role": "user", "content": "你好，改一下逻辑"}]), "")

    def test_pure_digit_runs_are_not_commits(self) -> None:
        block = anchor_index([{"role": "user", "content": "数字 12345678 不是提交号"}])
        self.assertNotIn("提交", block)

    def test_compact_appends_index_after_summary(self) -> None:
        compactor = Compactor(
            context_limit=1000, compact_at=0.7, keep_recent=5,
            summarizer=lambda _t, _p: "摘要",
        )
        original = conversation()
        original[3]["content"] = "读了 src/calc/impl.py\n" + "填充 " * 400
        messages, _ = compactor.compact(original)
        content = messages[1]["content"]
        self.assertIn("【索引】", content)
        self.assertIn("src/calc/impl.py", content)
        #  索引排在分界标记之后（属于摘要块，而不是原始任务的一部分）
        self.assertGreater(content.index("【索引】"), content.index(CONTEXT_PREFIX))


class TestCompactSkipsTinyRegion(unittest.TestCase):
    def test_small_region_skips_before_summary_call(self) -> None:
        """可压区间比摘要固定开销还小：直接跳过，摘要调用一次都不花。"""
        calls: list[int] = []

        def summarizer(_t: str, _p: list) -> str:
            calls.append(1)
            return "摘要"

        compactor = Compactor(
            context_limit=100_000, compact_at=0.7, keep_recent=2, summarizer=summarizer
        )
        messages = conversation()
        out, note = compactor.compact(messages)
        self.assertEqual(out, messages)
        self.assertIn("跳过", note)
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
