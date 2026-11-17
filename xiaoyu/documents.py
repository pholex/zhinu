"""可选的 PDF 读取：按页提取文字或返回有界 RGB 页面图。"""

from __future__ import annotations

import math
from contextlib import closing
import re
import struct
import threading
import zlib
from pathlib import Path

from . import fsguard

# PDFium 不允许跨线程同时调用；嵌入宿主可以有多个会话。
_LOCK = threading.Lock()
MAX_BYTES = 50 * 1024 * 1024
MAX_EDGE = 2000
MAX_TEXT = 200_000
MAX_SEARCH_PAGES = 100
READING_HINT = "[阅读提示：文字提取可能遗漏图表或打乱双栏、表格顺序；涉及图表、排版或关键数值，请用 mode=image 核对原页。扫描页不自动 OCR。]"


def page_numbers(spec: str, total: int, limit: int) -> list[int]:
    if not isinstance(spec, str) or len(spec) > 200:
        raise ValueError("pages 必须是页码范围，例如 1-3,5")
    selected: list[int] = []
    for part in spec.split(","):
        match = re.fullmatch(r"\s*(\d+)(?:\s*-\s*(\d+))?\s*", part)
        if match is None:
            raise ValueError("pages 必须是页码范围，例如 1-3,5")
        start = int(match[1])
        end = int(match[2] or start)
        if not 1 <= start <= end <= total:
            raise ValueError(f"页码越界：{part}；文档共 {total} 页")
        if end - start + 1 > limit:
            raise ValueError(f"单次最多读取 {limit} 页，请分段读取")
        for number in range(start, end + 1):
            if number not in selected:
                selected.append(number)
        if len(selected) > limit:
            raise ValueError(f"单次最多读取 {limit} 页，请分段读取")
    return selected


def _png(bitmap) -> bytes:
    """编码已知的 RGB8 位图，避免仅为保存 PNG 引入另一套图像依赖。"""
    def chunk(kind: bytes, payload: bytes) -> bytes:
        return (struct.pack(">I", len(payload)) + kind + payload
                + struct.pack(">I", zlib.crc32(kind + payload)))

    raw = bytes(bitmap.buffer)
    rows = b"".join(b"\0" + raw[y * bitmap.stride:y * bitmap.stride + bitmap.width * 3]
                    for y in range(bitmap.height))
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", bitmap.width, bitmap.height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))


def read_pdf(path: Path, pages: str | None = None, mode: str = "text", *,
             query: str | None = None, crop: list[float] | None = None,
             dpi: int | None = None) -> tuple[str, list[bytes]]:
    if mode not in {"text", "image", "info", "search"}:
        raise ValueError("mode 必须是 text、image、info 或 search")
    if mode == "search":
        if not isinstance(query, str) or not query.strip() or len(query) > 200:
            raise ValueError("search 需要非空 query，最多 200 字符（字面匹配，不支持正则）")
    elif query is not None:
        raise ValueError("query 仅用于 mode=search")
    if mode != "image" and (crop is not None or dpi is not None):
        raise ValueError("crop/dpi 仅用于 mode=image")
    if dpi is not None and (type(dpi) is not int or not 36 <= dpi <= 600):
        raise ValueError("dpi 必须是 36–600 的整数")
    if crop is not None and (
        not isinstance(crop, list) or len(crop) != 4
        or any(type(v) not in (int, float) or not math.isfinite(v) for v in crop)
        or not (0 <= crop[0] < crop[2] <= 1 and 0 <= crop[1] < crop[3] <= 1)
    ):
        raise ValueError("crop 必须是左上角为原点的归一化 [左,上,右,下]，范围 0–1 且有面积")
    if fsguard.require_regular(path).st_size > MAX_BYTES:
        raise ValueError("PDF 超过 50 MiB，请先拆分文档")
    with path.open("rb") as stream:
        raw = stream.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError("PDF 超过 50 MiB，请先拆分文档")
    if not raw.startswith(b"%PDF-"):
        raise ValueError("read_document 当前只支持 PDF 文件")
    try:
        import pypdfium2 as pdfium
    except ImportError:
        return "ERROR: PDF 支持未安装，请安装：pip install 'xiaoyu-agent[pdf]'", []
    images: list[bytes] = []
    sections: list[str] = []
    with _LOCK:
        with pdfium.PdfDocument(raw) as document:
            total = len(document)
            if mode == "info":
                sections.append(f"{path.name}：共 {total} 页；页码为从 1 开始的物理页码。")
                sections.append("目录（PDF 书签，最多 200 项、15 层）：")
                found = False
                for index, bookmark in enumerate(document.get_toc(max_depth=15)):
                    found = True
                    if index == 200:
                        sections.append("[目录已截断]")
                        break
                    dest = bookmark.get_dest()
                    page_index = dest.get_index() if dest else None
                    try:
                        title = bookmark.get_title()[:500]
                    except UnicodeDecodeError as exc:
                        title = exc.object.decode(exc.encoding, errors="replace")[:500]
                    location = f"第 {page_index + 1} 页" if page_index is not None else "无页码目标"
                    sections.append(f"{'  ' * bookmark.level}{title} → {location}")
                if not found:
                    sections.append("[无内嵌目录；可用 mode=search 定位关键词。]")
                sections.append(READING_HINT)
                return "\n".join(sections), []
            limit = MAX_SEARCH_PAGES if mode == "search" else (4 if mode == "image" else 20)
            if pages is None:
                pages = f"1-{min(total, limit)}" if mode == "search" and total else "1"
            selected = page_numbers(pages, total, limit)
            sections.append(f"{path.name}：共 {total} 页；读取 {', '.join(map(str, selected))}")
            if mode == "search":
                hits = 0
                incomplete = []
                for number in selected:
                    with closing(document[number - 1]) as page, closing(page.get_textpage()) as textpage:
                        count = textpage.count_chars()
                        text = textpage.get_text_range(0, min(count, MAX_TEXT), errors="replace") if count else ""
                    if count > MAX_TEXT:
                        incomplete.append(number)
                    # 保留原字符偏移；不忽略大小写，避免折叠字符改变片段位置。
                    position = text.find(query)
                    if position >= 0:
                        hits += 1
                        excerpt = " ".join(text[max(0, position - 80):position + len(query) + 80].split())
                        sections.append(f"[第 {number} 页] …{excerpt}…")
                sections.append(f"当前范围命中 {hits} 页（区分大小写的字面匹配，每页展示首处）。")
                if incomplete:
                    sections.append(f"[以下页仅搜索前 {MAX_TEXT} 字符：{incomplete}；不能判定整页无匹配。]")
                if len(selected) < total:
                    sections.append("[未搜索全文；请用 pages 指定其它范围，每次最多 100 页。]")
                sections.append("[仅搜索可提取文字；扫描页与图内文字可能漏检，不代表文档中不存在。]")
                return "\n".join(sections), []
            if mode == "text":
                sections.append(READING_HINT)
            remaining = MAX_TEXT
            for number in selected:
                with closing(document[number - 1]) as page:
                    sections.append(f"\n[第 {number} 页]")
                    if mode == "text":
                        with closing(page.get_textpage()) as textpage:
                            count = textpage.count_chars()
                            text = textpage.get_text_range(0, min(count, remaining), errors="replace") if count else ""
                        sections.append(text or "[本页无可提取文字，请用 mode=image 查看；未执行 OCR。]")
                        remaining -= min(count, remaining)
                        if not remaining:
                            sections.append("[文字达到单次上限，后续内容未读取；请缩小 pages 范围。]")
                            break
                    else:
                        width, height = page.get_size()
                        if not all(math.isfinite(n) and n > 0 for n in (width, height)):
                            raise ValueError(f"第 {number} 页尺寸无效")
                        left, top, right, bottom = crop or [0, 0, 1, 1]
                        region_width, region_height = width * (right - left), height * (bottom - top)
                        edge = 4096 if dpi is not None or crop is not None else MAX_EDGE
                        scale = min((dpi or 144) / 72, edge / max(region_width, region_height))
                        if max(width, height) * scale > 2 ** 30:
                            raise ValueError("页面坐标过大，请先规范化 PDF 页面尺寸")
                        margins = (width * left, height * (1 - bottom), width * (1 - right), height * top)
                        with closing(page.render(scale=scale, crop=margins, rev_byteorder=True,
                                                 force_bitmap_format=pdfium.raw.FPDFBitmap_BGR)) as bitmap:
                            images.append(_png(bitmap))
                            sections.append(f"页面图 {bitmap.width}×{bitmap.height}，按上述页序附在工具结果后。")
    return "\n".join(sections), images
