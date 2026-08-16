"""编辑器 Shift+Enter 配置的测试。

这个模块会写**工作区之外的用户配置**，所以重点全在"什么时候不该动手"：
解析不了不动、用户自己绑过不动、已经配好不重复加。写之前留备份。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from xiaoyu import editor_setup


class TestSequence(unittest.TestCase):
    def test_sequence_is_the_existing_alt_enter_bytes(self) -> None:
        """发的必须正好是 ESC+CR——那是 prompt_toolkit 解析成 (escape, enter)
        的字节形态，命中小羽已有的换行绑定。发反斜杠+CRLF 的方案会多打
        一个反斜杠，因为小羽没有"行尾反斜杠续行"这个约定。"""
        self.assertEqual(editor_setup.SHIFT_ENTER_SEQUENCE, "\x1b\r")
        payload = json.dumps(editor_setup._BINDING)
        self.assertIn("\\u001b\\r", payload)


class TestParsing(unittest.TestCase):
    def test_reads_jsonc_with_comments_and_trailing_commas(self) -> None:
        """keybindings.json 是 JSONC，VS Code 自己生成的模板就带注释。"""
        text = """
        // 这是 VS Code 默认生成的注释
        [
            { "key": "ctrl+a", "command": "foo" },
        ]
        """
        self.assertEqual(editor_setup.parse_keybindings(text), [{"key": "ctrl+a", "command": "foo"}])

    def test_empty_file_is_an_empty_list(self) -> None:
        self.assertEqual(editor_setup.parse_keybindings("   \n"), [])

    def test_unparseable_returns_none_rather_than_guessing(self) -> None:
        """看不懂就说看不懂——绝不能猜着覆盖用户手写的配置。"""
        self.assertIsNone(editor_setup.parse_keybindings("{ 这不是 json"))
        self.assertIsNone(editor_setup.parse_keybindings('{"不是": "数组"}'))

    def test_detects_shift_enter_regardless_of_spacing(self) -> None:
        self.assertTrue(editor_setup.has_shift_enter([{"key": "shift+enter"}]))
        self.assertTrue(editor_setup.has_shift_enter([{"key": "Shift + Enter"}]))
        self.assertFalse(editor_setup.has_shift_enter([{"key": "ctrl+enter"}]))


class TestPlanning(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())
        self.path = self.root / "keybindings.json"
        self.editor = editor_setup.Editor("测试编辑器", "Test")

    def plan(self):
        return editor_setup.plan_for(self.editor, self.path)

    def test_missing_file_means_create(self) -> None:
        self.assertEqual(self.plan().action, "install")

    def test_existing_user_binding_is_left_alone(self) -> None:
        """用户已经把 shift+enter 绑给别的命令了——那是他的选择，无权替他改。"""
        self.path.write_text(json.dumps([{"key": "shift+enter", "command": "别的"}]), encoding="utf-8")
        plan = self.plan()
        self.assertEqual(plan.action, "conflict")

    def test_already_installed_is_not_added_twice(self) -> None:
        self.path.write_text(json.dumps([editor_setup._BINDING]), encoding="utf-8")
        self.assertEqual(self.plan().action, "already")

    def test_unparseable_file_is_refused(self) -> None:
        self.path.write_text("{ 坏掉的内容", encoding="utf-8")
        self.assertEqual(self.plan().action, "unreadable")

    def test_apply_refuses_non_install_plans(self) -> None:
        """只有 install 能执行——防止调用方绕过判定直接写。"""
        self.path.write_text(json.dumps([editor_setup._BINDING]), encoding="utf-8")
        with self.assertRaises(ValueError):
            editor_setup.apply(self.plan())


class TestApply(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())
        self.path = self.root / "keybindings.json"
        self.editor = editor_setup.Editor("测试编辑器", "Test")

    def test_appends_and_keeps_existing_bindings(self) -> None:
        existing = [{"key": "ctrl+k", "command": "保留我"}]
        self.path.write_text(json.dumps(existing), encoding="utf-8")
        editor_setup.apply(editor_setup.plan_for(self.editor, self.path))
        written = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(written[0], existing[0])
        self.assertEqual(written[-1], editor_setup._BINDING)

    def test_backs_up_before_writing(self) -> None:
        """改用户配置前必须留后路。"""
        self.path.write_text(json.dumps([{"key": "ctrl+k"}]), encoding="utf-8")
        editor_setup.apply(editor_setup.plan_for(self.editor, self.path))
        backup = self.path.with_suffix(".json.bak")
        self.assertTrue(backup.exists())
        self.assertEqual(json.loads(backup.read_text(encoding="utf-8")), [{"key": "ctrl+k"}])

    def test_creates_the_file_when_absent(self) -> None:
        editor_setup.apply(editor_setup.plan_for(self.editor, self.path))
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")), [editor_setup._BINDING])
        #  新建的没有原文，也就不该留备份
        self.assertFalse(self.path.with_suffix(".json.bak").exists())

    def test_applying_twice_is_a_no_op_by_planning(self) -> None:
        """跑两遍不该出现两条一样的绑定。"""
        editor_setup.apply(editor_setup.plan_for(self.editor, self.path))
        self.assertEqual(editor_setup.plan_for(self.editor, self.path).action, "already")


class TestRemoval(unittest.TestCase):
    """uninstall 侧：只删严格等于 _BINDING 的项，其余一概不动。"""

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())
        self.path = self.root / "keybindings.json"
        self.editor = editor_setup.Editor("测试编辑器", "Test")
        patcher = mock.patch.object(
            editor_setup, "installed_editors", return_value=[(self.editor, self.path)]
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_plans_removal_when_our_binding_present(self) -> None:
        self.path.write_text(json.dumps([editor_setup._BINDING]), encoding="utf-8")
        plans = editor_setup.removal_plans()
        self.assertEqual([plan.action for plan in plans], ["remove"])

    def test_no_plan_without_our_binding(self) -> None:
        self.path.write_text(json.dumps([{"key": "ctrl+k", "command": "别的"}]), encoding="utf-8")
        self.assertEqual(editor_setup.removal_plans(), [])

    def test_user_modified_binding_is_not_ours(self) -> None:
        """用户改过的绑定（哪怕只动了 when）已是他自己的配置，不认领。"""
        modified = dict(editor_setup._BINDING, when="terminalFocus && 别的条件")
        self.path.write_text(json.dumps([modified]), encoding="utf-8")
        self.assertEqual(editor_setup.removal_plans(), [])

    def test_unparseable_file_is_skipped(self) -> None:
        self.path.write_text("{ 坏掉的内容", encoding="utf-8")
        self.assertEqual(editor_setup.removal_plans(), [])

    def test_missing_file_is_skipped(self) -> None:
        self.assertEqual(editor_setup.removal_plans(), [])

    def test_apply_removal_keeps_everything_else_and_backs_up(self) -> None:
        others = [{"key": "ctrl+k", "command": "保留我"}]
        self.path.write_text(json.dumps([*others, editor_setup._BINDING]), encoding="utf-8")
        (plan,) = editor_setup.removal_plans()
        editor_setup.apply_removal(plan)
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")), others)
        backup = self.path.with_suffix(".json.bak")
        self.assertTrue(backup.exists())
        self.assertIn(editor_setup._BINDING, json.loads(backup.read_text(encoding="utf-8")))

    def test_apply_removal_refuses_non_remove_plans(self) -> None:
        with self.assertRaises(ValueError):
            editor_setup.apply_removal(
                editor_setup.Plan(self.editor, self.path, "install")
            )


if __name__ == "__main__":
    unittest.main()
