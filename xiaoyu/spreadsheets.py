"""XLSX 的只读结构与区域读取；保留公式和缓存结果，不计算或执行。"""
from __future__ import annotations

from contextlib import ExitStack, closing
from datetime import date, datetime, time, timedelta
import io
import json
from pathlib import Path
import re
from xml.etree import ElementTree as ET
from zipfile import ZipFile

from .office import clipped, package_bytes

NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
MAX_CELLS = 2000
MAX_OUTPUT = 200000


def _value(cell) -> dict:
    value = cell.value
    if isinstance(value, (datetime, date, time)):
        return {"type": "date", "value": value.isoformat()}
    if isinstance(value, timedelta):
        return {"type": "duration", "seconds": value.total_seconds()}
    if value is None:
        return {"type": "blank", "value": None}
    if isinstance(value, bool):
        return {"type": "boolean", "value": value}
    if isinstance(value, (float, int)):
        return {"type": "number", "value": value}
    if isinstance(value, str):
        return {"type": "error" if cell.data_type == "e" else "text", "value": clipped(value, 2000)}
    # 数组公式/数据表公式等对象不冒充普通值，也不输出含内存地址的 repr。
    return {"type": "unsupported", "value": type(value).__name__}


def _sheet_details(archive, sheet) -> dict:
    # 只读模式不暴露合并/隐藏元数据，读取同一快照里的工作表 XML 补足。
    # _worksheet_path 来自固定版本解析器解析的关系，不拼接用户输入路径。
    root = ET.fromstring(archive.read(sheet._worksheet_path))
    merges = [item.get("ref") for item in root.iter(NS + "mergeCell")]
    rows = [item.get("r") for item in root.iter(NS + "row") if item.get("hidden") in {"1", "true"}]
    cols = [f"{item.get('min')}–{item.get('max')}" for item in root.iter(NS + "col") if item.get("hidden") in {"1", "true"}]
    return {
        "sheet": sheet.title, "state": sheet.sheet_state,
        "declared_rows": sheet.max_row, "declared_columns": sheet.max_column,
        "merged_ranges": merges[:100], "merged_ranges_total": len(merges),
        "hidden_rows": rows[:200], "hidden_rows_total": len(rows),
        "hidden_column_spans": cols[:200], "hidden_column_spans_total": len(cols),
    }


def read_xlsx(path: Path, sheet: str | None = None, cell_range: str | None = None) -> str:
    if path.suffix.lower() != ".xlsx":
        raise ValueError("当前仅支持 .xlsx；.xls/.xlsb/.xlsm 请先另存为 .xlsx，CSV/TSV 可用 read_file")
    if sheet is not None and (not isinstance(sheet, str) or not sheet):
        raise ValueError("sheet 必须是非空工作表名")
    if cell_range is not None and (not isinstance(cell_range, str) or not re.fullmatch(
        r"\$?[A-Za-z]{1,3}\$?[1-9][0-9]{0,6}(?::\$?[A-Za-z]{1,3}\$?[1-9][0-9]{0,6})?", cell_range
    )):
        raise ValueError("cell_range 必须是 A1 或 A1:F50；工作表名请用 sheet 指定，不支持整行整列")
    if cell_range is not None and sheet is None:
        raise ValueError("读取区域必须同时指定 sheet；先省略区域列出工作表")
    try:
        from openpyxl import load_workbook
        from openpyxl.utils.cell import range_boundaries, get_column_letter
        from openpyxl.cell.read_only import EMPTY_CELL
    except ImportError:
        return "ERROR: XLSX 支持未安装，请安装：pip install 'xiaoyu-agent[spreadsheet]'"
    bounds = None
    if cell_range is not None:
        bounds = range_boundaries(cell_range)
        c1, r1, c2, r2 = bounds
        if not (1 <= c1 <= c2 <= 16384 and 1 <= r1 <= r2 <= 1048576):
            raise ValueError("cell_range 越界或范围倒置")
        if (c2 - c1 + 1) * (r2 - r1 + 1) > MAX_CELLS:
            raise ValueError("单次最多读取 2000 个单元格，请缩小 cell_range")
    raw = package_bytes(path, "xl/workbook.xml")
    lines = [f"{path.name}：只读 XLSX；不执行宏、不更新外部链接、不重算公式。",
             "[formula 是公式原文；cached 是文件保存的结果，可能缺失或过期，不能视为本次计算。数字保留原值，number_format 仅作格式提示，不是渲染结果。图表、图片、批注内容未读取。]"]
    with ExitStack() as stack:
        workbook = stack.enter_context(closing(load_workbook(io.BytesIO(raw), read_only=True, data_only=False, keep_links=False)))
        archive = stack.enter_context(ZipFile(io.BytesIO(raw)))
        names = workbook.sheetnames
        lines.append(f"共 {len(names)} 个页签；工作表声明范围可能包含空白或样式区域。")
        if sheet is not None and sheet not in names:
            raise ValueError(f"找不到工作表 {sheet!r}，请先省略 sheet 查看列表")
        if bounds is None:
            selected = [workbook[sheet]] if sheet is not None else [workbook[name] for name in names[:100]]
            for current in selected:
                if not hasattr(current, "iter_rows"):
                    lines.append(json.dumps({"sheet": current.title, "type": "图表页（不读取）"}, ensure_ascii=False))
                    continue
                # 全工作簿概览不遍历各表全文；选表后才提取合并与隐藏信息。
                details = (_sheet_details(archive, current) if sheet is not None else
                           {"sheet": current.title, "state": current.sheet_state,
                            "declared_rows": current.max_row, "declared_columns": current.max_column})
                lines.append(json.dumps(details, ensure_ascii=False))
            if sheet is None:
                lines.append("[最多列出前 100 个页签；指定 sheet 查看合并/隐藏信息，指定 sheet + cell_range 读取区域。]")
            return "\n".join(lines)
        current = workbook[sheet]
        if not hasattr(current, "iter_rows"):
            raise ValueError("该页签是图表页，不能读取单元格区域")
        lines.append(json.dumps(_sheet_details(archive, current), ensure_ascii=False))
        cached_book = stack.enter_context(closing(load_workbook(io.BytesIO(raw), read_only=True, data_only=True, keep_links=False)))
        cached_sheet = cached_book[sheet]
        # 不相信导出器给的 dimension；显式区域按实际 XML 流读取。
        current.reset_dimensions()
        cached_sheet.reset_dimensions()
        c1, r1, c2, r2 = bounds
        options = dict(min_row=r1, max_row=r2, min_col=c1, max_col=c2)
        lines.append(f"区域 {sheet}!{cell_range}（包含隐藏单元格；合并格通常仅左上角有值）：")
        budget = MAX_OUTPUT
        formula_rows = current.iter_rows(**options)
        cached_rows = cached_sheet.iter_rows(**options)
        stack.callback(formula_rows.close)
        stack.callback(cached_rows.close)
        empty_row = (EMPTY_CELL,) * (c2 - c1 + 1)
        # 某些只读迭代器在 XML 最后一行就停止，显式补齐请求区域的尾部空行。
        for row_number in range(r1, r2 + 1):
            formula_row = next(formula_rows, empty_row)
            cached_row = next(cached_rows, empty_row)
            for column, (cell, saved) in enumerate(zip(formula_row, cached_row), c1):
                coordinate = f"{get_column_letter(column)}{row_number}"
                record = {"cell": coordinate, "number_format": clipped(cell.number_format or "General", 200)}
                if cell.data_type == "f":
                    formula = cell.value
                    record.update(type="formula", formula=clipped(formula, 2000) if isinstance(formula, str) else {
                        "kind": type(formula).__name__, "text": clipped(getattr(formula, "text", "") or "", 2000),
                        "ref": getattr(formula, "ref", None)}, cached=_value(saved))
                    record["cache_status"] = "missing_or_blank" if saved.value is None else "saved_unverified"
                else:
                    record.update(_value(cell))
                line = json.dumps(record, ensure_ascii=False)
                if len(line) > budget:
                    lines.append(f"[输出达到上限，从 {coordinate} 起尚未输出；请缩小 cell_range 重读。]")
                    return "\n".join(lines)
                lines.append(line)
                budget -= len(line)
    return "\n".join(lines)
