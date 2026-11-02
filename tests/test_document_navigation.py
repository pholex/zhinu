"""PDF 定位、局部渲染及工具接线的真实文档回归。"""
import importlib.util
import struct
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest import mock

from xiaoyu import documents, media
from xiaoyu.config import Config
from xiaoyu.tools import Toolbox
from .test_builtin_extensions import make_pdf


class ArgumentsTest(unittest.TestCase):
    def test_invalid_options_fail_before_open(self):
        for kwargs in (
            {"mode": "search"}, {"mode": "search", "query": " "},
            {"mode": "search", "query": "x" * 201}, {"query": "x"},
            {"dpi": 300}, {"mode": "image", "dpi": True},
            {"mode": "image", "dpi": 601},
            *({"mode": "image", "crop": crop} for crop in
              ([0, 0, 2, 1], [0, 0, 0, 1], [0, 0, float("nan"), 1], [0, True, 1, 1], [0, 1])),
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                documents.read_pdf(Path("absent.pdf"), **kwargs)


@unittest.skipUnless(importlib.util.find_spec("pypdfium2"), "需 PDF extra")
class NavigationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.path = self.root / "sample.pdf"
        make_pdf(self.path)

    def test_info_bookmarks_and_missing_outline(self):
        text, images = documents.read_pdf(self.path, mode="info")
        self.assertIn("共 2 页", text)
        self.assertIn("无内嵌目录", text)
        self.assertFalse(images)
        make_pdf(self.path, bookmarks=True)
        text, _ = documents.read_pdf(self.path, mode="info")
        self.assertIn("Introduction → 第 1 页", text)

    def test_search_page_scope_literal_matching_and_scan_caveat(self):
        text, images = documents.read_pdf(self.path, mode="search", query="Hello")
        self.assertIn("命中 1 页", text)
        self.assertIn("[第 1 页] …Hello PDF…", text)
        self.assertIn("扫描页", text)
        self.assertFalse(images)
        text, _ = documents.read_pdf(self.path, pages="2", mode="search", query="Hello")
        self.assertIn("命中 0 页", text)
        self.assertIn("未搜索全文", text)
        for query in ("hello", "Hello.*"):
            text, _ = documents.read_pdf(self.path, mode="search", query=query)
            self.assertIn("命中 0 页", text)

    def test_search_truncated_page_is_not_claimed_complete(self):
        with mock.patch.object(documents, "MAX_TEXT", 3):
            text, _ = documents.read_pdf(self.path, mode="search", query="PDF")
        self.assertIn("命中 0 页", text)
        self.assertIn("不能判定整页无匹配", text)

    def test_search_default_window_and_explicit_continuation(self):
        import pypdfium2 as pdfium

        long_path = self.root / "long.pdf"
        with pdfium.PdfDocument(self.path) as source, pdfium.PdfDocument.new() as target:
            target.import_pages(source, pages=[1] * 100 + [0])
            target.save(long_path)
        text, _ = documents.read_pdf(long_path, mode="search", query="Hello")
        self.assertIn("命中 0 页", text)
        self.assertIn("未搜索全文", text)
        text, _ = documents.read_pdf(long_path, pages="101", mode="search", query="Hello")
        self.assertIn("[第 101 页] …Hello PDF…", text)
        with self.assertRaises(ValueError):
            documents.read_pdf(long_path, pages="1-101", mode="search", query="Hello")

    def test_large_page_respects_pixel_budget(self):
        import pypdfium2 as pdfium

        large_path = self.root / "large.pdf"
        with pdfium.PdfDocument.new() as target:
            page = target.new_page(2000, 1000)
            page.close()
            target.save(large_path)
        _, images = documents.read_pdf(large_path, mode="image", dpi=600)
        self.assertEqual(media.dimensions(images[0]), (4096, 2048))

    def test_text_always_explains_visual_limitations(self):
        text, _ = documents.read_pdf(self.path)
        self.assertIn("Hello PDF", text)
        self.assertIn("mode=image", text)
        self.assertIn("图表", text)

    @staticmethod
    def pixels(png):
        offset, compressed = 8, b""
        while offset < len(png):
            length = struct.unpack(">I", png[offset:offset + 4])[0]
            if png[offset + 4:offset + 8] == b"IDAT":
                compressed += png[offset + 8:offset + 8 + length]
            offset += length + 12
        width, height = media.dimensions(png)
        raw = zlib.decompress(compressed)
        return b"".join(raw[y * (width * 3 + 1) + 1:(y + 1) * (width * 3 + 1)] for y in range(height))

    def test_crop_uses_top_origin_and_renders_at_requested_resolution(self):
        _, top = documents.read_pdf(self.path, mode="image", crop=[0, 0, 1, .5], dpi=288)
        _, bottom = documents.read_pdf(self.path, mode="image", crop=[0, .6, 1, 1], dpi=288)
        self.assertEqual(media.dimensions(top[0]), (800, 200))
        self.assertEqual(media.dimensions(bottom[0]), (800, 160))
        self.assertLess(min(self.pixels(top[0])), 100)  # 文字在页面上半部
        self.assertEqual(set(self.pixels(bottom[0])), {255})
        _, whole = documents.read_pdf(self.path, mode="image")
        self.assertEqual(media.dimensions(whole[0]), (400, 200))

    def test_tool_forwards_new_options_and_returns_media(self):
        box = Toolbox(Config(base_url="http://unused", model="m", workspace=self.root,
                             enable_plugins=False, enable_mcp=False))
        self.addCleanup(box.tasks.shutdown)
        result = box.run("read_document", {"path": self.path.name, "mode": "search", "query": "Hello"})
        self.assertIn("命中 1 页", result)
        with mock.patch.object(media, "cache_dir", return_value=self.root / "media"):
            result = box.run("read_document", {"path": self.path.name, "mode": "image",
                                               "crop": [0, 0, 1, .5], "dpi": 288})
        self.assertIn("800×200", result)
        self.assertEqual(len(box.take_media()), 1)
