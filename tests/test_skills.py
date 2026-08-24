"""SKILL.md 技能与工具可用性探测的测试。不碰真实技能库。"""

from __future__ import annotations

import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from xiaoyu import skills, tokens
from xiaoyu.tools import Tool


class SkillDirsTest(unittest.TestCase):
    def test_undeterminable_home_skips_agents_dir_instead_of_crashing(self) -> None:
        """服务账户/容器随机 UID 推不出 home（见 config.home_dir）：跳过
        ~/.agents/skills 来源即可，scan_skills 乃至构造 Agent 不许炸。"""
        with mock.patch.object(skills, "home_dir", return_value=None):
            dirs = skills.skill_dirs()
            self.assertEqual(len(dirs), 1)
            self.assertTrue(str(dirs[0]).endswith(os.path.join("xiaoyu", "skills")))
            skills.scan_skills()  # 不炸即通过（目录不存在时本来就返回空）

    def test_with_home_agents_dir_comes_first(self) -> None:
        with mock.patch.object(skills, "home_dir", return_value=Path("/home/u")):
            dirs = skills.skill_dirs()
        self.assertEqual(dirs[0], Path("/home/u") / ".agents" / "skills")
        self.assertEqual(len(dirs), 2)


def write_skill(root: Path, dirname: str, frontmatter: str, body: str = "正文") -> Path:
    skill_dir = root / dirname
    skill_dir.mkdir(parents=True)
    path = skill_dir / "SKILL.md"
    path.write_text(f"---\n{frontmatter}\n---\n\n{body}\n", encoding="utf-8")
    return path


class FrontmatterTest(unittest.TestCase):
    def test_parses_flat_keys_and_quotes(self):
        text = '---\nname: my-skill\ndescription: "做某件事"\nversion: 1.0\n---\n\n正文'
        meta = skills.parse_frontmatter(text)
        self.assertEqual(meta["name"], "my-skill")
        self.assertEqual(meta["description"], "做某件事")

    def test_skips_nested_keys(self):
        text = "---\nname: x\nmetadata:\n  otherns:\n    tags: a\n---\n正文"
        meta = skills.parse_frontmatter(text)
        self.assertEqual(meta["name"], "x")
        self.assertNotIn("tags", meta)

    def test_block_scalar_description(self):
        """description: >- 的多行折叠写法（真实技能库里大量存在）。"""
        text = (
            "---\nname: x\ndescription: >-\n  第一行说明，\n  第二行说明。\nversion: 1\n---\n正文"
        )
        meta = skills.parse_frontmatter(text)
        self.assertEqual(meta["description"], "第一行说明， 第二行说明。")
        self.assertEqual(meta["version"], "1")

    def test_block_scalar_at_end_of_frontmatter(self):
        text = "---\nname: x\ndescription: |\n  说明内容\n---\n正文"
        meta = skills.parse_frontmatter(text)
        self.assertEqual(meta["description"], "说明内容")

    def test_no_frontmatter_returns_empty(self):
        self.assertEqual(skills.parse_frontmatter("# 只是文档"), {})

    def test_unclosed_frontmatter_returns_empty(self):
        self.assertEqual(skills.parse_frontmatter("---\nname: x\n没有闭合"), {})

    def test_typo_keys_reported_with_the_intended_spelling(self):
        problems = skills.frontmatter_problems({"name": "x", "descripton": "拼错了"})
        self.assertEqual(len(problems), 2)
        self.assertIn("descripton", problems[0])
        self.assertIn("description", problems[0])  # 指出正确拼法
        self.assertIn("缺少 description", problems[1])
        self.assertEqual(skills.frontmatter_problems({"name": "x", "description": "ok", "version": "1"}), [])

    def test_foreign_keys_are_silent(self):
        """回归钉子：别家生态的合法键不是问题。

        第一版把"不在允许集"直接当问题报，于是 kdocs 官方技能的 homepage: 与
        agent-skills 的 dependencies: 每次启动各刷一行——而那些是 npx skills
        装的第三方件，用户改了下次 update 就被覆盖，噪音无从消除。
        """
        for foreign in ("homepage", "dependencies", "author", "category", "date", "tags", "icon"):
            with self.subTest(key=foreign):
                self.assertEqual(
                    skills.frontmatter_problems({"name": "x", "description": "ok", foreign: "v"}),
                    [],
                )

    def test_typos_still_caught_including_transpositions(self):
        for typo, intended in (
            ("descripton", "description"),
            ("descrpition", "description"),  # 换位：按普通编辑距离算两步就漏了
            ("Description", "description"),  # YAML 区分大小写，等效于拼错
            ("nmae", "name"),
            ("verison", "version"),
            ("allowed-tool", "allowed-tools"),
        ):
            with self.subTest(typo=typo):
                self.assertEqual(skills._typo_of(typo), intended)

    def test_short_foreign_keys_are_not_forced_onto_known_ones(self):
        """短键容易撞脸（date 与 name 只差 2）——误报比漏报贵：用户对第三方
        技能里的键无能为力，只能每次启动看着它。"""
        for foreign in ("date", "tags", "path", "kind"):
            with self.subTest(key=foreign):
                self.assertEqual(skills._typo_of(foreign), "")

    def test_strip_frontmatter(self):
        text = "---\nname: x\n---\n\n# 标题\n内容"
        self.assertEqual(skills.strip_frontmatter(text), "# 标题\n内容")
        self.assertEqual(skills.strip_frontmatter("没有 frontmatter"), "没有 frontmatter")


class ScanTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.primary = Path(self.tmp.name) / "agents-skills"
        self.secondary = Path(self.tmp.name) / "config-skills"
        #  patch 的是 skill_sources 而不是 skill_dirs：插件包也是扫描来源之一，
        #  只挡住散装目录的话，开发机上真装了插件包就会把测试污染成随机结果
        patcher = mock.patch.object(
            skills,
            "skill_sources",
            return_value=[skills.SkillSource(self.primary), skills.SkillSource(self.secondary)],
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_scans_and_reads_metadata(self):
        write_skill(self.primary, "deploy", "name: deploy\ndescription: 部署流程")
        found = skills.scan_skills()
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].name, "deploy")
        self.assertEqual(found[0].description, "部署流程")

    def test_name_falls_back_to_dirname(self):
        write_skill(self.primary, "no-name", "description: 无名技能")
        found = skills.scan_skills()
        self.assertEqual(found[0].name, "no-name")

    def test_first_source_wins_on_conflict_and_says_so(self):
        """撞名以前是静默丢弃：只看得到一条，却不知道另一份被吃了。"""
        write_skill(self.primary, "dup", "name: dup\ndescription: 规范库版本")
        write_skill(self.secondary, "dup", "name: dup\ndescription: 配置目录版本")
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            found = skills.scan_skills()
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].description, "规范库版本")
        self.assertIn("撞名", stderr.getvalue())
        self.assertIn("config-skills", stderr.getvalue())

    def test_plugin_skills_are_namespaced(self):
        """插件技能带包名前缀：两家插件各带一个同名技能也不会互相顶掉。"""
        plugin_a = Path(self.tmp.name) / "pa"
        plugin_b = Path(self.tmp.name) / "pb"
        write_skill(plugin_a, "deploy", "name: deploy\ndescription: A 家的部署")
        write_skill(plugin_b, "deploy", "name: deploy\ndescription: B 家的部署")
        write_skill(self.primary, "deploy", "name: deploy\ndescription: 我自己写的")
        with mock.patch.object(
            skills,
            "skill_sources",
            return_value=[
                skills.SkillSource(self.primary),
                skills.SkillSource(plugin_a, plugin="pkg-a"),
                skills.SkillSource(plugin_b, plugin="pkg-b"),
            ],
        ):
            found = {skill.name: skill for skill in skills.scan_skills()}
        self.assertEqual(set(found), {"deploy", "pkg-a:deploy", "pkg-b:deploy"})
        self.assertIsNone(found["deploy"].plugin)
        self.assertEqual(found["pkg-a:deploy"].plugin, "pkg-a")
        self.assertEqual(found["pkg-b:deploy"].description, "B 家的部署")

    def test_typo_description_skipped_with_warning(self):
        """descripton: 拼错 → 以前静默进索引、永远选不中；现在跳过并在 stderr 指出。"""
        write_skill(self.primary, "typo", "name: typo\ndescripton: 做事")
        write_skill(self.primary, "rare", "name: rare\ndescription: 有描述\nfoo: 生僻键")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            names = {skill.name for skill in skills.scan_skills()}
        self.assertNotIn("typo", names)
        self.assertIn("rare", names)  # 只是多了个生僻键，照常加载且不出声
        self.assertIn("descripton", err.getvalue())
        self.assertIn("description", err.getvalue())  # 指出正确拼法
        #  生僻键既不影响加载，也不该刷一行——第三方技能里的私有键是常态
        self.assertNotIn("foo", err.getvalue())

    def test_index_report_and_user_warning(self):
        found = self._write_bulk(20, description_chars=300)
        skills.index_block(found, max_tokens=100_000)
        self.assertIsNone(skills.budget_warning())
        self.assertEqual(skills.last_index_report.truncated, 0)
        skills.index_block(found, max_tokens=500)
        report = skills.last_index_report
        self.assertEqual(report.total, 20)
        self.assertGreater(report.truncated, 0)
        self.assertEqual(report.omitted, 0)
        self.assertIn("描述被截短", skills.budget_warning())
        skills.index_block(found, max_tokens=150)
        self.assertGreater(skills.last_index_report.omitted, 0)
        self.assertIn("未列出", skills.budget_warning())

    def test_small_truncation_does_not_nag(self):
        """平均只截掉几十个字不值得警告。"""
        found = self._write_bulk(20, description_chars=60)
        skills.index_block(found, max_tokens=len(found) * 20 + 60)
        report = skills.last_index_report
        if report.truncated and not report.omitted:
            self.assertLessEqual(report.truncated_chars / report.truncated, 60)
            self.assertIsNone(skills.budget_warning())

    def test_missing_dirs_are_fine(self):
        self.assertEqual(skills.scan_skills(), [])

    def test_load_body_strips_frontmatter(self):
        write_skill(self.primary, "s", "name: s", body="# 步骤\n1. 做事")
        skill = skills.scan_skills()[0]
        body = skills.load_skill_body(skill)
        self.assertIn("# 步骤", body)
        self.assertNotIn("---", body)

    def test_index_block(self):
        write_skill(self.primary, "a", "name: a\ndescription: 干活")
        block = skills.index_block(skills.scan_skills())
        self.assertIn("- a: 干活", block)
        self.assertIn("skill 工具", block)
        self.assertEqual(skills.index_block([]), "")

    def test_index_description_capped(self):
        """单条描述的硬上限是挡失控极端值的兜底，不设预算时也生效。"""
        overlong = "细" * (skills.DESCRIPTION_CAP + 200)
        write_skill(self.primary, "long", "name: long\ndescription: " + overlong)
        block = skills.index_block(skills.scan_skills())
        line = next(row for row in block.splitlines() if row.startswith("- long"))
        self.assertLessEqual(len(line), len("- long: ") + skills.DESCRIPTION_CAP + 1)
        self.assertTrue(line.endswith("…"))

    def test_index_keeps_long_descriptions_when_budget_allows(self):
        """预算宽裕时不该再砍描述：控总量归预算，硬上限只挡极端值。

        砍在描述末尾丢掉的往往正是路由信息（"什么时候别用这个技能"这类反向
        边界常写在最后），而那恰恰是索引最该保住的东西。
        """
        description = "描述" * 200  # 400 字，远超旧的 250 上限、仍在硬上限内
        write_skill(self.primary, "verbose", f"name: verbose\ndescription: {description}")
        block = skills.index_block(skills.scan_skills(), max_tokens=100_000)
        self.assertIn(description, block)

    def _write_bulk(self, count: int, description_chars: int = 100) -> list[skills.Skill]:
        for index in range(count):
            write_skill(
                self.primary,
                f"skill{index:02d}",
                f"name: skill{index:02d}\ndescription: " + "活" * description_chars,
            )
        return skills.scan_skills()

    def test_index_respects_token_budget(self):
        found = self._write_bulk(20)
        block = skills.index_block(found, max_tokens=500)
        self.assertLessEqual(tokens.estimate_text(block), 500)
        #  不设预算则全部列出，且明显更长
        self.assertLess(len(block), len(skills.index_block(found)))
        self.assertIn("skill19", skills.index_block(found))

    def test_index_shortens_descriptions_before_dropping_skills(self):
        """第 2 级降级：预算不够时截短描述，技能名一个都不能少。

        整条丢掉 = 该技能对模型静默失效（装了却永远不被选中），
        比多花 token 糟得多——这是这个预算机制存在的意义所在。
        """
        found = self._write_bulk(20)
        block = skills.index_block(found, max_tokens=500)
        for index in range(20):
            self.assertIn(f"skill{index:02d}", block)
        self.assertIn("已截短", block)
        self.assertNotIn("未列出", block)
        #  描述确实被截了：没有哪一条还留着完整的 100 个字
        self.assertNotIn("活" * 100, block)

    def test_index_waterfill_is_fair(self):
        """剩余额度轮流分：不能让排在前面的技能吃光预算、后面全成光名字。"""
        found = self._write_bulk(20)
        block = skills.index_block(found, max_tokens=500)
        widths = [
            len(row.split(": ", 1)[1]) if ": " in row else 0
            for row in block.splitlines()
            if row.startswith("- skill")
        ]
        self.assertEqual(len(widths), 20)
        #  轮流分配下每条描述长度最多差一个字符
        self.assertLessEqual(max(widths) - min(widths), 1)
        self.assertGreater(min(widths), 0)

    def test_index_drops_only_when_names_alone_overflow(self):
        """第 3 级降级：光名字也塞不下时才丢，并折叠成一行提示。"""
        found = self._write_bulk(60)
        block = skills.index_block(found, max_tokens=150)
        self.assertLessEqual(tokens.estimate_text(block), 150)
        self.assertIn("skill00", block)
        self.assertIn("未列出", block)
        #  丢弃路径上留下来的是光名字，不带描述
        self.assertNotIn("- skill00: ", block)
        #  提示必须点明"描述被略去"：不说，模型会把这些当成本来就没写描述的
        #  技能，从而认定它们不相关——那等于列了名字也白列
        self.assertIn("描述", block)

    def test_index_budget_covers_the_trailing_note(self):
        """尾部提示行自己也要计入预算，不能靠它超支。

        换行符同理：行是 "\\n".join 起来的，每行漏记 1 个字符、几十行就超支。
        """
        found = self._write_bulk(30)
        for budget in (100, 200, 600, 1500):
            with self.subTest(budget=budget):
                block = skills.index_block(found, max_tokens=budget)
                self.assertLessEqual(tokens.estimate_text(block), budget)

    def test_index_never_vanishes_below_its_floor(self):
        """预算小于索引块的物理下限（表头+提示+一个名字）时超支也要出块。

        技能表整块消失 = 所有技能对模型静默失效，比略微超支糟得多。
        """
        found = self._write_bulk(60)
        block = skills.index_block(found, max_tokens=1)
        self.assertIn("skill 工具", block)
        self.assertIn("skill00", block)


class AgentSkillIntegrationTest(unittest.TestCase):
    """技能挂上 Agent：索引进 system prompt、skill 工具按需加载正文。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name) / "skills"
        write_skill(root, "greet", "name: greet\ndescription: 打招呼", body="说你好")
        patcher = mock.patch.object(
            skills, "skill_sources", return_value=[skills.SkillSource(root)]
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def build_agent(self):
        from xiaoyu.agent import Agent
        from xiaoyu.config import Config
        from xiaoyu.providers import Registry
        from xiaoyu.tools import Toolbox

        config = Config(
            base_url="http://unused",
            model="m",
            workspace=Path(self.tmp.name),
            enable_explore=False,
        )
        return Agent(config, Toolbox(config), registry=Registry.for_client(object()))

    def test_index_in_system_prompt_and_tool_registered(self):
        agent = self.build_agent()
        self.assertIn("- greet: 打招呼", agent.messages[0]["content"])
        self.assertIsNotNone(agent.toolbox.get("skill"))
        self.assertIn("skill", [s["function"]["name"] for s in agent.toolbox.schemas()])

    def test_load_skill_returns_body(self):
        agent = self.build_agent()
        result = agent.toolbox.run("skill", {"name": "greet"})
        #  正文前必须带基准目录：技能里的相对路径模型无从知道相对谁
        self.assertIn("技能目录", result)
        self.assertTrue(result.endswith("说你好"))

    def test_unknown_skill_lists_available(self):
        agent = self.build_agent()
        result = agent.toolbox.run("skill", {"name": "nope"})
        self.assertIn("ERROR", result)
        self.assertIn("greet", result)

    def test_disabled_skills_config(self):
        from xiaoyu.agent import Agent
        from xiaoyu.config import Config
        from xiaoyu.providers import Registry
        from xiaoyu.tools import Toolbox

        config = Config(
            base_url="http://unused",
            model="m",
            workspace=Path(self.tmp.name),
            enable_explore=False,
            enable_skills=False,
            enable_agents=False,
            enable_hooks=False,
            )
        agent = Agent(config, Toolbox(config), registry=Registry.for_client(object()))
        self.assertEqual(agent.skills, [])
        self.assertIsNone(agent.toolbox.get("skill"))
        self.assertNotIn("greet", agent.messages[0]["content"])


class DynamicSkillsTest(unittest.TestCase):
    """会话中途的技能动态性：未命中重扫、轮首差量注入、显式 reload、reset 转正。

    三条通道各管一段（改语义前先对齐这张分工表）：
    - skill 工具未命中重扫 → 覆盖**本轮**刚落盘的技能（模型自己写的）
    - _refresh_skills（轮首）→ 覆盖**轮间**外部装入/删除，注入 [系统提示] 不动 system prompt
    - reload_skills（显式）→ 重建 system prompt 索引，prompt cache 成本由要它的人付
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "skills"
        self.root.mkdir(parents=True)
        patcher = mock.patch.object(
            skills, "skill_sources", return_value=[skills.SkillSource(self.root)]
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def build_agent(self):
        from xiaoyu.agent import Agent
        from xiaoyu.config import Config
        from xiaoyu.providers import Registry
        from xiaoyu.tools import Toolbox

        config = Config(
            base_url="http://unused",
            model="m",
            workspace=Path(self.tmp.name),
            enable_explore=False,
        )
        return Agent(config, Toolbox(config), registry=Registry.for_client(object()))

    def test_skill_tool_rescans_on_miss(self):
        """本轮刚落盘的技能必须能按名加载——索引只是快照，磁盘才是真相。

        起步给一个技能：零技能时 check_fn 会把 skill 工具整个挡在 schemas 与
        执行之外（那是另一条已接受的边界——轮内写**第一个**技能要等下一轮
        转正，模型刚写完的内容本来就在它上下文里，无需加载）。
        """
        write_skill(self.root, "seed", "name: seed\ndescription: 起步就有")
        agent = self.build_agent()
        write_skill(self.root, "fresh", "name: fresh\ndescription: 新落盘", body="现学现用")
        result = agent.toolbox.run("skill", {"name": "fresh"})
        self.assertNotIn("ERROR", result)
        self.assertTrue(result.endswith("现学现用"))
        #  重扫顺带更新了快照：后续未命中报错里能列出它
        self.assertIn("fresh", [item.name for item in agent.skills])

    def test_skill_tool_registered_even_with_zero_skills(self):
        """注册看开关不看当下有无技能：启动时零技能 ≠ 永远零技能。
        没有技能的时刻由 check_fn 挡在 schemas 外。"""
        agent = self.build_agent()
        self.assertIsNotNone(agent.toolbox.get("skill"))
        self.assertNotIn("skill", [s["function"]["name"] for s in agent.toolbox.schemas()])
        write_skill(self.root, "one", "name: one\ndescription: d")
        agent._refresh_skills()
        self.assertIn("skill", [s["function"]["name"] for s in agent.toolbox.schemas()])

    def test_refresh_injects_note_without_touching_system_prompt(self):
        agent = self.build_agent()
        system_before = agent.messages[0]["content"]
        write_skill(self.root, "newbie", "name: newbie\ndescription: 会话中途装入")
        agent._refresh_skills()
        #  system prompt（cache 前缀）纹丝不动
        self.assertEqual(agent.messages[0]["content"], system_before)
        note = agent.messages[-1]
        self.assertEqual(note["role"], "user")
        self.assertTrue(note["content"].startswith("[系统提示]"), note["content"])
        self.assertIn("newbie", note["content"])
        self.assertIn("会话中途装入", note["content"])
        #  压缩侧不能把注入当用户原话
        self.assertIn(note["content"], agent.compactor.synthetic_user_texts)

    def test_refresh_reports_removals_too(self):
        write_skill(self.root, "gone", "name: gone\ndescription: d")
        agent = self.build_agent()
        import shutil

        shutil.rmtree(self.root / "gone")
        agent._refresh_skills()
        self.assertIn("移除 gone", agent.messages[-1]["content"])
        self.assertEqual(agent.skills, [])

    def test_refresh_is_quiet_when_nothing_changed(self):
        """指纹相同的轮次零重扫零注入——这是每轮都跑的路径，必须近乎免费。"""
        write_skill(self.root, "still", "name: still\ndescription: d")
        agent = self.build_agent()
        before = len(agent.messages)
        with mock.patch.object(skills, "scan_skills") as scan:
            agent._refresh_skills()
        scan.assert_not_called()
        self.assertEqual(len(agent.messages), before)

    def test_reload_rebuilds_index_and_reports_diff(self):
        write_skill(self.root, "old", "name: old\ndescription: 旧的")
        agent = self.build_agent()
        self.assertIn("- old:", agent.messages[0]["content"])
        import shutil

        shutil.rmtree(self.root / "old")
        write_skill(self.root, "new", "name: new\ndescription: 新的")
        added, removed = agent.reload_skills()
        self.assertEqual((added, removed), (["new"], ["old"]))
        self.assertIn("- new: 新的", agent.messages[0]["content"])
        self.assertNotIn("- old:", agent.messages[0]["content"])

    def test_reset_promotes_session_skills_into_index(self):
        """reset 后 cache 反正从头计：中途出现的技能应转正进重建的索引。"""
        agent = self.build_agent()
        write_skill(self.root, "late", "name: late\ndescription: 中途来的")
        agent._refresh_skills()
        self.assertNotIn("- late:", agent.messages[0]["content"])  # 会话内不动前缀
        agent.reset()
        self.assertIn("- late: 中途来的", agent.messages[0]["content"])


class CheckFnTest(unittest.TestCase):
    """工具可用性探测：check_fn 为 False 时不进 schemas、拒绝执行。"""

    def make_tool(self, ok: bool) -> Tool:
        return Tool(
            name="probe",
            description="d",
            parameters={"type": "object", "properties": {}},
            handler=lambda: "ran",
            check_fn=lambda: ok,
        )

    def test_available_tool_behaves_normally(self):
        tool = self.make_tool(True)
        self.assertTrue(tool.available())

    def test_unavailable_tool_filtered_from_schemas_and_run(self):
        from xiaoyu.config import Config
        from xiaoyu.providers import Registry
        from xiaoyu.tools import Toolbox

        config = Config(base_url="x", model="m", workspace=Path.cwd())
        box = Toolbox(config, only=["read_file"])
        box.register(self.make_tool(False))
        self.assertNotIn("probe", [s["function"]["name"] for s in box.schemas()])
        self.assertIn("不可用", box.run("probe", {}))

    def test_check_fn_exception_means_unavailable(self):
        tool = Tool(
            name="boom",
            description="d",
            parameters={},
            handler=lambda: "x",
            check_fn=lambda: 1 / 0,
        )
        self.assertFalse(tool.available())


if __name__ == "__main__":
    unittest.main()
