"""严格的逐文件补丁解析；不执行文件操作，不猜测上下文。"""

from __future__ import annotations

import re


def changes_of(args: dict) -> list[dict]:
    value = args.get("changes")
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def validate(changes: list[dict]) -> None:
    if not isinstance(changes, list) or not 1 <= len(changes) <= 50:
        raise ValueError("changes 必须包含 1–50 个文件操作")
    for item in changes:
        if not isinstance(item, dict) or set(item) - {"path", "operation", "diff", "edits"}:
            raise ValueError("每项只接受 path、operation、diff、edits")
        if not isinstance(item.get("path"), str) or not item["path"].strip():
            raise ValueError("每项必须有非空 path")
        if item.get("operation") not in {"create", "update", "delete"}:
            raise ValueError("operation 必须是 create / update / delete")
        if "edits" in item:
            if item["operation"] != "update" or "diff" in item:
                raise ValueError("edits 仅用于 update，不能与 diff 同传")
            validate_edits(item["edits"])
            continue
        if item["operation"] != "delete" and not isinstance(item.get("diff"), str):
            raise ValueError("create / update 必须提供 diff")
        if item["operation"] == "delete" and item.get("diff"):
            raise ValueError("delete 不接受 diff")


_HUNK = re.compile(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(?: .*)?")


def apply_diff(text: str, diff: str) -> str:
    """应用不带文件头的 unified diff；行号、数量、上下文全部精确匹配。"""
    source = text.splitlines(keepends=True)
    lines = diff.splitlines(keepends=True)
    result: list[str] = []
    cursor = i = 0
    if not lines:
        raise ValueError("diff 不能为空")
    while i < len(lines):
        match = _HUNK.fullmatch(lines[i].rstrip("\r\n"))
        if match is None:
            raise ValueError("补丁必须以 @@ -旧行,数量 +新行,数量 @@ 开始，不含 ---/+++ 文件头")
        old_start, old_count, new_start, new_count = (
            int(value) if value is not None else 1 for value in match.groups()
        )
        start = old_start - 1 if old_count else old_start
        if start < cursor or start > len(source):
            raise ValueError("补丁行号越界、重叠或顺序错误")
        result.extend(source[cursor:start])
        expected_new = new_start - 1 if new_count else new_start
        if expected_new != len(result):
            raise ValueError("补丁新行号与前面的修改不一致")
        i += 1
        old: list[str] = []
        new: list[str] = []
        while i < len(lines) and not lines[i].startswith("@@ "):
            line = lines[i]
            if not line or line[0] not in " +-":
                raise ValueError("补丁正文每行必须以空格、+ 或 - 开始")
            prefix, value = line[0], line[1:]
            i += 1
            if i < len(lines) and lines[i].rstrip("\r\n") == "\\ No newline at end of file":
                value = value.rstrip("\r\n")
                i += 1
            if prefix != "+":
                old.append(value)
            if prefix != "-":
                new.append(value)
        if len(old) != old_count or len(new) != new_count:
            raise ValueError("补丁行数与 hunk 声明不一致")
        if source[start:start + old_count] != old:
            raise ValueError(f"第 {old_start} 行上下文不匹配，请重新 read_file")
        result.extend(new)
        cursor = start + old_count
    result.extend(source[cursor:])
    return "".join(result)


def validate_edits(edits: list[dict]) -> None:
    if not isinstance(edits, list) or not 1 <= len(edits) <= 100:
        raise ValueError("edits 必须包含 1–100 项替换")
    for edit in edits:
        if (not isinstance(edit, dict) or set(edit) != {"old_str", "new_str"}
                or not isinstance(edit["old_str"], str)
                or not isinstance(edit["new_str"], str) or not edit["old_str"]):
            raise ValueError("每项替换须有非空 old_str 与字符串 new_str")
        if edit["old_str"] == edit["new_str"]:
            raise ValueError("old_str 和 new_str 完全相同")


def apply_edits(text: str, edits: list[dict]) -> str:
    """各项匹配同一份原文，禁止重叠，不让前一项生成的文本影响后一项。"""
    validate_edits(edits)
    spans = []
    for edit in edits:
        old, new = edit["old_str"], edit["new_str"]
        start = text.find(old)
        if start < 0 or text.find(old, start + 1) >= 0:
            raise ValueError("old_str 必须在原文件中唯一精确匹配，请扩大上下文或重新 read_file")
        if "\n" in new and start > 0 and text[start - 1] != "\n":
            raise ValueError("多行替换必须从行首开始（含缩进）")
        spans.append((start, start + len(old), new))
    spans.sort()
    if any(right[0] < left[1] for left, right in zip(spans, spans[1:])):
        raise ValueError("edits 的原文范围重叠，未修改文件")
    for start, end, new in reversed(spans):
        text = text[:start] + new + text[end:]
    return text


def apply_change(text: str, item: dict) -> str:
    return apply_edits(text, item["edits"]) if "edits" in item else apply_diff(text, item["diff"])


def preview(args: dict) -> str:
    """审批预览展示全部补丁或替换对。"""
    blocks = []
    for item in changes_of(args):
        body = str(item.get("diff", ""))
        if isinstance(item.get("edits"), list):
            body = "\n\n".join(
                f"原文：\n{edit.get('old_str', '')}\n替换为：\n{edit.get('new_str', '')}"
                for edit in item["edits"] if isinstance(edit, dict))
        blocks.append(f"{item.get('operation', '?')} {item.get('path', '?')}\n{body}")
    return "\n\n".join(blocks)
