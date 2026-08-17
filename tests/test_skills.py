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
        #  索引只用于发现，冗长描述常驻浪费上下文——超过 250 字符截断
        write_skill(self.primary, "long", "name: long\ndescription: " + "细" * 400)
        block = skills.index_block(skills.scan_skills())
        line = next(row for row in block.splitlines() if row.startswith("- long"))
        self.assertLess(len(line), 300)
        self.assertTrue(line.endswith("…"))

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
