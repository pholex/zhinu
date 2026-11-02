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


def read_pdf(path: Path, pages: str = "1", mode: str = "text") -> tuple[str, list[bytes]]:
    if mode not in {"text", "image"}:
        raise ValueError("mode 必须是 text 或 image")
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
            selected = page_numbers(pages, total, 4 if mode == "image" else 20)
            sections.append(f"{path.name}：共 {total} 页；读取 {', '.join(map(str, selected))}")
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
                        scale = min(2.0, MAX_EDGE / max(width, height))
                        with closing(page.render(scale=scale, rev_byteorder=True,
                                                 force_bitmap_format=pdfium.raw.FPDFBitmap_BGR)) as bitmap:
                            images.append(_png(bitmap))
                            sections.append(f"页面图 {bitmap.width}×{bitmap.height}，按上述页序附在工具结果后。")
    return "\n".join(sections), images
