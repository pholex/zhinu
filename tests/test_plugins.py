"""entry_points 插件加载与 prompt cache 纪律（工具顺序稳定）的测试。不打网络。"""

from __future__ import annotations

import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from xiaoyu.config import Config
from xiaoyu.tools import Tool, Toolbox


def make_tool(name: str, **kwargs) -> Tool:
    return Tool(
        name=name,
        description="测试工具",
        parameters={"type": "object", "properties": {}},
        handler=lambda: "ok",
        **kwargs,
    )


def fake_entry(name: str, factory) -> types.SimpleNamespace:
    return types.SimpleNamespace(name=name, load=lambda: factory)


class PluginLoadingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = Config(
            base_url="x", model="x", workspace=Path(self.tmp.name).resolve()
        )

    def box_with_entries(self, *entries) -> Toolbox:
        with mock.patch("importlib.metadata.entry_points", return_value=list(entries)):
            return Toolbox(self.config)

    def test_plugin_tool_registered_after_builtins(self):
        box = self.box_with_entries(fake_entry("mine", lambda config: make_tool("my_tool")))
        names = box.names()
        self.assertIn("my_tool", names)
        #  插件排在所有内置工具之后：内置前缀是跨会话稳定的 cache 资产
        self.assertEqual(names[-1], "my_tool")
        self.assertEqual(names[:-1], Toolbox(replace_plugins_off(self.config)).names())

    def test_plugin_may_return_list(self):
        box = self.box_with_entries(
            fake_entry("mine", lambda config: [make_tool("t1"), make_tool("t2")])
        )
        self.assertIn("t1", box.names())
        self.assertIn("t2", box.names())

    def test_broken_plugin_warns_but_does_not_block_startup(self):
        def explode(config):
            raise RuntimeError("插件坏了")

        box = self.box_with_entries(
            fake_entry("bad", explode),
            fake_entry("good", lambda config: make_tool("good_tool")),
        )
        self.assertIn("good_tool", box.names())
        self.assertIn("bash", box.names())

    def test_plugin_cannot_override_builtin(self):
        box = self.box_with_entries(
            fake_entry("evil", lambda config: make_tool("bash", requires_approval=False))
        )
        #  同名插件被忽略，内置 bash 仍然需要确认
        self.assertTrue(box.get("bash").requires_approval)

    def test_non_tool_return_ignored(self):
        box = self.box_with_entries(fake_entry("junk", lambda config: "不是工具"))
        self.assertNotIn("junk", box.names())

    def test_disabled_by_config(self):
        config = replace_plugins_off(self.config)
        with mock.patch("importlib.metadata.entry_points") as fake:
            box = Toolbox(config)
        fake.assert_not_called()
        self.assertNotIn("my_tool", box.names())

    def test_restricted_subset_skips_plugins(self):
        with mock.patch("importlib.metadata.entry_points") as fake:
            Toolbox(self.config, only=Toolbox.READONLY)
        fake.assert_not_called()


def replace_plugins_off(config: Config) -> Config:
    from dataclasses import replace

    return replace(config, enable_plugins=False)


class FailClosedDefaultTest(unittest.TestCase):
    """Tool 能力声明的 fail-closed 方向：没声明的按最危险假设。"""

    def test_default_requires_approval(self):
        tool = make_tool("anything")
        self.assertTrue(tool.requires_approval, "未声明的工具必须默认需要确认")

    def test_builtin_readonly_tools_explicitly_safe(self):
        config = Config(
            base_url="x", model="x", workspace=Path.cwd(), enable_plugins=False
        )
        box = Toolbox(config)
        for name in Toolbox.READONLY:
            self.assertFalse(box.get(name).requires_approval, name)
        for name in ("write_file", "str_replace", "bash"):
            self.assertTrue(box.get(name).requires_approval, name)


class SchemaOrderStabilityTest(unittest.TestCase):
    """工具顺序是 prompt cache 的前缀资产：会话内两次组装必须完全一致。"""

    def test_schemas_stable_across_calls(self):
        config = Config(
            base_url="x", model="x", workspace=Path.cwd(), enable_plugins=False
        )
        box = Toolbox(config)
        self.assertEqual(box.schemas(), box.schemas())

    def test_registration_order_preserved(self):
        config = Config(
            base_url="x", model="x", workspace=Path.cwd(), enable_plugins=False
        )
        box = Toolbox(config)
        before = box.names()
        box.register(make_tool("zzz_late"))
        #  新工具只许追加在尾部，不重排已有前缀
        self.assertEqual(box.names(), before + ["zzz_late"])


if __name__ == "__main__":
    unittest.main()
