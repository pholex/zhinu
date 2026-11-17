"""办公读取：真实 DOCX/XLSX、格式边界、只读性与权限接线。"""
from __future__ import annotations

from datetime import datetime
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock
import warnings
from xml.etree import ElementTree as ET
from zipfile import ZipFile, ZIP_DEFLATED

from xiaoyu import office
from xiaoyu.config import Config
from xiaoyu.permissions import parse_rule
from xiaoyu.spreadsheets import NS, read_xlsx
from xiaoyu.tools import Toolbox
from xiaoyu.word_documents import read_docx
from .test_agent_paths import AgentTestCase

HAS_DOCX = importlib.util.find_spec("docx") is not None
HAS_XLSX = importlib.util.find_spec("openpyxl") is not None


def rewrite_member(path, member, change):
    with ZipFile(path) as source:
        entries = [(name, source.read(name)) for name in source.namelist()]
    with ZipFile(path, "w", compression=ZIP_DEFLATED) as target:
        for name, raw in entries:
            target.writestr(name, change(raw) if name == member else raw)


class TempTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()

    def box(self):
        box = Toolbox(Config(base_url="http://unused", model="m", workspace=self.root,
                             enable_plugins=False, enable_mcp=False))
        self.addCleanup(box.tasks.shutdown)
        return box


class PackageTest(TempTest):
    def package(self, xml=b"<document/>"):
        path = self.root / "a.docx"
        with ZipFile(path, "w", compression=ZIP_DEFLATED) as archive:
            archive.writestr("[Content_Types].xml", b"<Types/>")
            archive.writestr("word/document.xml", xml)
        return path

    def test_snapshot_and_invalid_archive(self):
        path = self.package()
        self.assertEqual(office.package_bytes(path, "word/document.xml"), path.read_bytes())
        path.write_bytes(b"not a zip")
        with self.assertRaisesRegex(ValueError, "损坏"):
            office.package_bytes(path, "word/document.xml")

    def test_dtd_including_utf16_is_rejected(self):
        xml = '<!DOCTYPE document [<!ENTITY x SYSTEM "file:///nonexistent">]><document>&x;</document>'
        for encoding in ("utf-8", "utf-16"):
            with self.subTest(encoding=encoding):
                path = self.package(xml.encode(encoding))
                with self.assertRaisesRegex(ValueError, "DTD"):
                    office.package_bytes(path, "word/document.xml")

    def test_expansion_member_and_file_caps(self):
        path = self.package(b"<document>" + b"x" * 1024 + b"</document>")
        for setting in ("MAX_FILE", "MAX_MEMBER", "MAX_UNPACKED", "MAX_ENTRIES"):
            with self.subTest(setting=setting), mock.patch.object(office, setting, 1):
                with self.assertRaises(ValueError):
                    office.package_bytes(path, "word/document.xml")

    def test_xml_with_custom_extension_still_rejects_dtd(self):
        path = self.package()
        with ZipFile(path, "a") as archive:
            archive.writestr("custom/part.dat", (" " * 4096 + '<!DOCTYPE a [<!ENTITY x "x">]><a>&x;</a>').encode("utf-16"))
        with self.assertRaisesRegex(ValueError, "DTD"):
            office.package_bytes(path, "word/document.xml")

    def test_duplicate_and_wrong_format_rejected(self):
        path = self.package()
        with self.assertRaisesRegex(ValueError, "格式"):
            office.package_bytes(path, "xl/workbook.xml")
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            with ZipFile(path, "a") as archive:
                archive.writestr("word/document.xml", b"<document/>")
        with self.assertRaisesRegex(ValueError, "重名"):
            office.package_bytes(path, "word/document.xml")

    @unittest.skipIf(os.name == "nt", "POSIX FIFO")
    def test_special_file_refused_before_open(self):
        path = self.root / "pipe.docx"
        os.mkfifo(path)
        with self.assertRaises(OSError):
            office.package_bytes(path, "word/document.xml")

    def test_missing_extras_are_actionable_and_tools_discoverable(self):
        box = self.box()
        with mock.patch.dict(sys.modules, {"docx": None, "openpyxl": None}):
            self.assertIn("xiaoyu-agent[docx]", box.run("read_document", {"path": "a.docx"}))
            self.assertIn("xiaoyu-agent[spreadsheet]", box.run("read_spreadsheet", {"path": "a.xlsx"}))
        for name in ("read_document", "read_spreadsheet"):
            self.assertTrue(box.get(name).available())
            self.assertFalse(box.needs_approval(name, {"path": "a.docx"}))
            self.assertTrue(box.needs_approval(name, {"path": "../a.docx"}))


@unittest.skipUnless(HAS_DOCX, "需 docx extra")
class WordTest(TempTest):
    def setUp(self):
        super().setUp()
        from docx import Document

        self.document = Document()
        self.document.add_heading("Overview", 1)
        self.document.add_paragraph("Before the table")
        table = self.document.add_table(rows=2, cols=2)
        table.cell(0, 0).text = "Metric"
        table.cell(0, 1).text = "Value"
        table.cell(1, 0).text = "Revenue"
        table.cell(1, 1).text = "120"
        self.document.add_paragraph("After the table")
        self.path = self.root / "a.docx"
        self.document.save(self.path)

    def test_order_blocks_info_and_readonly(self):
        original = self.path.read_bytes()
        text = read_docx(self.path)
        self.assertLess(text.index("Before the table"), text.index("Revenue"))
        self.assertLess(text.index("Revenue"), text.index("After the table"))
        self.assertIn("[块 3 · 表格", text)
        info = read_docx(self.path, "info")
        self.assertIn("[块 1] Heading 1: Overview", info)
        self.assertNotIn("Before the table", info)
        self.assertEqual(self.path.read_bytes(), original)

    def test_slicing_and_literal_search_scope(self):
        text = read_docx(self.path, offset=3, limit=1)
        self.assertIn("Revenue", text)
        self.assertNotIn("Before the table", text)
        self.assertNotIn("After the table", text)
        self.assertIn("offset=4", text)
        text = read_docx(self.path, "search", query="Revenue", offset=1, limit=2)
        self.assertIn("命中 0 块", text)
        text = read_docx(self.path, "search", query="Revenue", offset=3, limit=1)
        self.assertIn("命中 1 块", text)
        self.assertIn("块 3", text)
        self.assertIn("命中 0 块", read_docx(self.path, "search", query="Rev.*"))

    def test_extra_content_and_revision_are_not_silently_claimed_read(self):
        from docx.oxml import OxmlElement
        self.document.sections[0].header.paragraphs[0].text = "HEADER_PRIVATE"
        self.document.add_comment(self.document.paragraphs[1].runs, text="COMMENT_PRIVATE")
        paragraph = self.document.add_paragraph("Current ")
        revision = OxmlElement("w:ins")
        run = OxmlElement("w:r")
        text = OxmlElement("w:t")
        text.text = "REVISION_PRIVATE"
        run.append(text)
        revision.append(run)
        paragraph._p.append(revision)
        hyperlink = OxmlElement("w:hyperlink")
        run = OxmlElement("w:r")
        text = OxmlElement("w:t")
        text.text = "Visible link"
        run.append(text)
        hyperlink.append(run)
        paragraph._p.append(hyperlink)
        self.document.save(self.path)
        result = read_docx(self.path)
        self.assertIn("批注、修订", result)
        self.assertIn("页眉页脚", result)
        self.assertIn("Visible link", result)
        for omitted in ("HEADER_PRIVATE", "COMMENT_PRIVATE", "REVISION_PRIVATE"):
            self.assertNotIn(omitted, result)

    def test_merged_cells_and_nested_table_notice(self):
        table = self.document.tables[0]
        table.cell(0, 0).merge(table.cell(0, 1))
        table.cell(1, 0).add_table(rows=1, cols=1).cell(0, 0).text = "NESTED_PRIVATE"
        self.document.save(self.path)
        result = read_docx(self.path)
        self.assertIn('"column_span": "2"', result)
        self.assertIn("嵌套表格未展开", result)
        self.assertNotIn("NESTED_PRIVATE", result)

    def test_limits_and_invalid_pdf_options(self):
        for kwargs in ({"offset": 0}, {"limit": 101}, {"offset": 99}, {"mode": "image"},
                       {"mode": "search"}, {"query": "x"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                read_docx(self.path, **kwargs)
        box = self.box()
        self.assertIn("ERROR", box.run("read_document", {"path": "a.docx", "pages": "1"}))
        result = box.run("read_document", {"path": "a.docx", "offset": 3, "limit": 1})
        self.assertIn("Revenue", result)
        self.assertNotIn("After the table", result)

    def test_long_block_reports_truncation(self):
        self.document.add_paragraph("x" * 22000)
        self.document.save(self.path)
        self.assertIn("内容截断", read_docx(self.path))


@unittest.skipUnless(HAS_XLSX, "需 spreadsheet extra")
class SpreadsheetTest(TempTest):
    def setUp(self):
        super().setUp()
        from openpyxl import Workbook

        self.book = Workbook()
        self.sheet = self.book.active
        self.sheet.title = "Data"
        self.sheet.append([12.5, "00123", datetime(2026, 1, 2), True, "=SUM(A2:A3)", "=1+1", "#DIV/0!"])
        self.sheet["A2"] = 5
        self.sheet["A3"] = 6
        self.sheet["A1"].number_format = "0.00%"
        self.sheet.merge_cells("B2:C2")
        self.sheet.row_dimensions[3].hidden = True
        self.sheet.column_dimensions["G"].hidden = True
        self.book.create_sheet("Hidden").sheet_state = "hidden"
        self.path = self.root / "a.xlsx"
        self.book.save(self.path)
        def cache(raw):
            root = ET.fromstring(raw)
            root.find(f".//{NS}c[@r='E1']/{NS}v").text = "11"
            return ET.tostring(root, encoding="utf-8")
        rewrite_member(self.path, "xl/worksheets/sheet1.xml", cache)

    def cells(self, cell_range):
        result = read_xlsx(self.path, "Data", cell_range)
        return {item["cell"]: item for line in result.splitlines() if line.startswith('{"cell":')
                for item in [json.loads(line)]}

    def test_info_hidden_and_merged_structure(self):
        info = read_xlsx(self.path)
        self.assertIn('"sheet": "Hidden", "state": "hidden"', info)
        info = read_xlsx(self.path, "Data")
        self.assertIn('"merged_ranges": ["B2:C2"]', info)
        self.assertIn('"hidden_rows": ["3"]', info)
        self.assertIn('"hidden_column_spans": ["7–7"]', info)

    def test_types_formula_cache_and_source_unchanged(self):
        original = self.path.read_bytes()
        cells = self.cells("A1:H1")
        self.assertEqual(cells["A1"]["value"], 12.5)
        self.assertEqual(cells["A1"]["number_format"], "0.00%")
        self.assertEqual(cells["B1"]["value"], "00123")
        self.assertEqual(cells["C1"]["value"], "2026-01-02T00:00:00")
        self.assertEqual(cells["D1"]["type"], "boolean")
        self.assertEqual(cells["E1"]["formula"], "=SUM(A2:A3)")
        self.assertEqual(cells["E1"]["cached"]["value"], 11)
        self.assertEqual(cells["F1"]["cache_status"], "missing_or_blank")
        self.assertEqual(cells["G1"]["type"], "error")
        self.assertEqual(cells["H1"]["type"], "blank")
        self.assertEqual(self.path.read_bytes(), original)

    def test_requested_blank_tail_and_empty_region_are_preserved(self):
        self.assertEqual(set(self.cells("A3:B4")), {"A3", "B3", "A4", "B4"})
        cells = self.cells("A8:B9")
        self.assertEqual(len(cells), 4)
        self.assertTrue(all(cell["type"] == "blank" for cell in cells.values()))

    def test_incorrect_dimension_does_not_hide_requested_cells(self):
        def dimension(raw):
            root = ET.fromstring(raw)
            root.find(NS + "dimension").set("ref", "A1:A1")
            return ET.tostring(root, encoding="utf-8")
        rewrite_member(self.path, "xl/worksheets/sheet1.xml", dimension)
        self.assertEqual(self.cells("A3")["A3"]["value"], 6)

    def test_cached_zero_and_false_are_not_missing(self):
        for kind, expected in ((None, 0), ("b", False)):
            def cache(raw):
                root = ET.fromstring(raw)
                cell = root.find(f".//{NS}c[@r='E1']")
                if kind:
                    cell.set("t", kind)
                cell.find(NS + "v").text = "0"
                return ET.tostring(root, encoding="utf-8")
            rewrite_member(self.path, "xl/worksheets/sheet1.xml", cache)
            cell = self.cells("E1")["E1"]
            self.assertEqual(cell["cached"]["value"], expected)
            self.assertEqual(cell["cache_status"], "saved_unverified")

    def test_bad_ranges_unknown_sheet_and_legacy_format(self):
        for area in ("A:A", "1:100", "Data!A1", "A0", "B2:A1", "XFE1", "A1048577", "A1:Z100"):
            with self.subTest(area=area), self.assertRaises(ValueError):
                read_xlsx(self.path, "Data", area)
        with self.assertRaisesRegex(ValueError, "同时指定"):
            read_xlsx(self.path, cell_range="A1")
        with self.assertRaisesRegex(ValueError, "找不到"):
            read_xlsx(self.path, "unknown")
        with self.assertRaisesRegex(ValueError, "另存为"):
            read_xlsx(self.root / "old.xls")

    def test_tool_route_and_cell_text_cap(self):
        self.sheet["B1"] = "x" * 5000
        self.book.save(self.path)
        result = self.box().run("read_spreadsheet", {"path": "a.xlsx", "sheet": "Data", "cell_range": "B1"})
        self.assertIn("内容截断", result)
        self.assertNotIn("x" * 3000, result)


class OfficePermissionTest(AgentTestCase):
    def execute(self, agent, name, args):
        return agent._execute({"id": "office", "function": {"name": name, "arguments": json.dumps(args)}})["content"]

    def test_plan_read_and_untrusted_result(self):
        agent = self.build([])
        agent.enter_plan_mode()
        tool = agent.toolbox.get("read_spreadsheet")
        tool.handler = mock.Mock(return_value="external cells")
        result = self.execute(agent, "read_spreadsheet", {"path": "a.xlsx"})
        tool.handler.assert_called_once()
        self.assertIn("untrusted_content", result)
        self.assertIn("external cells", result)

    def test_deny_and_outside_confirmation_precede_parsing(self):
        self.config.auto_approve = False
        asked = mock.Mock(return_value=False)
        agent = self.build([], approver=asked)
        tool = agent.toolbox.get("read_spreadsheet")
        tool.handler = mock.Mock(return_value="must not run")
        self.execute(agent, "read_spreadsheet", {"path": "../a.xlsx"})
        asked.assert_called_once()
        tool.handler.assert_not_called()
        agent.permissions.rules.append(parse_rule("deny read_spreadsheet(a.xlsx)"))
        asked.reset_mock()
        self.execute(agent, "read_spreadsheet", {"path": "a.xlsx"})
        asked.assert_not_called()
        tool.handler.assert_not_called()
