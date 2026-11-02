"""DOCX 正文的段落/表格读取；块号不冒充排版页码。"""
from __future__ import annotations

import io
import json
from pathlib import Path
from zipfile import ZipFile

from .office import clipped, package_bytes

MAX_BLOCKS = 10000
MAX_BLOCK_TEXT = 20000
MAX_OUTPUT = 200000
W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def _block_text(element, document) -> tuple[str, str]:
    from docx.table import Table, _Cell
    from docx.text.paragraph import Paragraph

    if element.tag == W + "p":
        paragraph = Paragraph(element, document)
        style = paragraph.style.name if paragraph.style is not None else ""
        return f"段落（{style}）", clipped(paragraph.text, MAX_BLOCK_TEXT)
    if element.tag == W + "tbl":
        table = Table(element, document)
        rows, budget = [], MAX_BLOCK_TEXT
        for index, row in enumerate(element.findall(W + "tr"), 1):
            if index > 100 or budget <= 0:
                rows.append("[表格截断；请拆分大表格后读取]")
                break
            # 按实际 XML 单元格枚举，不把巨大的 gridSpan 展开成重复格。
            cells = row.findall(W + "tc")
            values = []
            for cell in cells[:50]:
                item = {"text": clipped(_Cell(cell, table).text, 2000)}
                span = cell.find(f"{W}tcPr/{W}gridSpan")
                merge = cell.find(f"{W}tcPr/{W}vMerge")
                if span is not None:
                    item["column_span"] = span.get(W + "val", "1")
                if merge is not None:
                    item["vertical_merge"] = merge.get(W + "val", "continue")
                values.append(item)
            line = f"R{index}: " + json.dumps(values, ensure_ascii=False)
            if len(cells) > 50:
                line += " [列数超过 50，已截断]"
            rows.append(clipped(line, budget))
            budget -= len(line)
        return "表格（物理单元格顺序，合并信息另列；嵌套表格未展开）", "\n".join(rows)
    return "未展开结构", f"[正文元素 {element.tag.rsplit('}', 1)[-1]} 未读取，可能含内容控件或修订正文]"


def read_docx(path: Path, mode: str = "text", *, offset: int = 1, limit: int = 50,
              query: str | None = None) -> str:
    if mode not in {"text", "info", "search"}:
        raise ValueError("DOCX 支持 text/info/search；页面图请先转换为 PDF 后读取")
    if type(offset) is not int or offset < 1 or type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("DOCX offset 从 1 开始，limit 为 1–100 的整数")
    if mode == "search":
        if not isinstance(query, str) or not query.strip() or len(query) > 200:
            raise ValueError("search 需要非空 query，最多 200 字符，区分大小写的字面匹配")
    elif query is not None:
        raise ValueError("query 仅用于 mode=search")
    try:
        from docx import Document
    except ImportError:
        return "ERROR: DOCX 支持未安装，请安装：pip install 'xiaoyu-agent[docx]'"
    raw = package_bytes(path, "word/document.xml")
    document = Document(io.BytesIO(raw))
    elements = [element for element in document.element.body if element.tag != W + "sectPr"]
    if len(elements) > MAX_BLOCKS:
        raise ValueError("DOCX 正文超过 10000 块，请先拆分")
    if offset > len(elements) and elements:
        raise ValueError(f"offset 越界：正文共 {len(elements)} 块")
    lines = [f"{path.name}：正文共 {len(elements)} 块；块号按正文顺序，不是页码。"]
    with ZipFile(io.BytesIO(raw)) as archive:
        names = archive.namelist()
    tags = {element.tag for element in document.element.iter()}
    detected = []
    for label, present in (
        ("图片", any(name.startswith("word/media/") for name in names)),
        ("批注", any(name.startswith("word/comments") for name in names)),
        ("修订", bool(tags & {W + n for n in ("ins", "del", "moveFrom", "moveTo")})),
        ("文本框", W + "txbxContent" in tags),
        ("页眉页脚/脚注尾注", any(name.startswith(("word/header", "word/footer", "word/footnotes", "word/endnotes")) for name in names)),
    ):
        if present:
            detected.append(label)
    lines.append("检测到的附加内容：" + ("、".join(detected) or "未检测到上述类型"))
    lines.append("[读取范围：普通正文段落、超链接文字和顶层表格；图片像素、批注正文、修订内容、文本框、页眉页脚、脚注尾注未读取，嵌套表格未展开；字段不重算，不保证版面或自动编号。]")
    if mode == "info":
        from docx.text.paragraph import Paragraph

        count = 0
        for number, element in enumerate(elements, 1):
            if element.tag != W + "p":
                continue
            paragraph = Paragraph(element, document)
            style = paragraph.style.name if paragraph.style is not None else ""
            if style.startswith("Heading") or style in {"Title", "Subtitle"}:
                if count == 200:
                    lines.append("[标题列表已截断]")
                    break
                lines.append(f"[块 {number}] {style}: {clipped(paragraph.text, 500)}")
                count += 1
        if not count:
            lines.append("[未找到标准标题样式；可分段读取正文或搜索关键词。]")
        return "\n".join(lines)
    stop = min(len(elements), offset + limit - 1)
    lines.append(f"本次范围：块 {offset}–{stop}。")
    budget = MAX_OUTPUT
    hits = 0
    for number in range(offset, stop + 1):
        kind, text = _block_text(elements[number - 1], document)
        if mode == "search":
            position = text.find(query)
            if position < 0:
                continue
            hits += 1
            text = "…" + text[max(0, position - 80):position + len(query) + 80] + "…"
        section = f"[块 {number} · {kind}]\n{text}"
        if len(section) > budget:
            lines.append(f"[输出达到上限；从 offset={number} 继续，缩小 limit。]")
            return "\n".join(lines)
        lines.append(section)
        budget -= len(section)
    if mode == "search":
        lines.append(f"当前范围命中 {hits} 块；每块仅搜索提取后的前 {MAX_BLOCK_TEXT} 字符，大表格另有限制；未命中不代表全文不存在。")
    if stop < len(elements):
        lines.append(f"[还有后续正文，可从 offset={stop + 1} 继续。]")
    return "\n".join(lines)
