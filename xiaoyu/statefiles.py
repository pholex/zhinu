"""更新用户状态前区分缺失、损坏和读取失败，损坏原件保留后才允许重建。"""

from __future__ import annotations

import json
import logging
import uuid
from pathlib import Path
from typing import Any, Callable

from . import fsguard

logger = logging.getLogger(__name__)


def read_for_update(path: Path, parse: Callable[[str], dict]) -> dict:
    """不存在返回空表；内容损坏备份原字节；IO 或备份失败原样抛出。

    仅供写入路径使用。读者不得借恢复默认值隐式改动用户文件。
    这不是跨进程读改写事务；调用者仍负责自己的写入串行化。
    """
    try:
        fsguard.require_regular(path)
        raw = path.read_bytes()
    except FileNotFoundError:
        if path.is_symlink():
            raise OSError(f"状态文件是失效的符号链接：{path}")
        return {}
    try:
        text = raw.decode("utf-8-sig", errors="replace")
        if text.encode("utf-8") != raw.removeprefix(b"\xef\xbb\xbf"):
            raise ValueError("文件不是有效 UTF-8")
        return parse(text)
    except (ValueError, RecursionError):
        backup = path.with_name(f"{path.name}.corrupt-{uuid.uuid4().hex}")
        fsguard.write_atomic(backup, raw, private=True)
        logger.warning("%s 内容损坏；原始字节已保留到 %s", path, backup)
        return {}


def json_object(text: str) -> dict[str, Any]:
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("状态文件顶层必须是对象")
    return data


def read_json_for_update(path: Path, field: str = "") -> dict[str, Any]:
    def parse(text: str) -> dict:
        data = json_object(text)
        if field and field in data and not isinstance(data[field], dict):
            raise ValueError(f"{field} 必须是对象")
        return data
    return read_for_update(path, parse)
