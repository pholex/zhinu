"""办公文件的有界快照与 ZIP/XML 预检；不解压到磁盘、不解析外部实体。"""
from __future__ import annotations

import io
from pathlib import Path
from xml.parsers import expat
from zipfile import BadZipFile, ZipFile

from . import fsguard

MAX_FILE = 50 * 1024 * 1024
MAX_MEMBER = 20 * 1024 * 1024
MAX_UNPACKED = 100 * 1024 * 1024
MAX_ENTRIES = 10000


def package_bytes(path: Path, required: str) -> bytes:
    """所有解析器消费同一份已校验字节，避免预检与读取之间文件变化。"""
    if fsguard.require_regular(path).st_size > MAX_FILE:
        raise ValueError("办公文件超过 50 MiB，请先拆分")
    with path.open("rb") as stream:
        raw = stream.read(MAX_FILE + 1)
    if len(raw) > MAX_FILE:
        raise ValueError("办公文件超过 50 MiB，请先拆分")

    def reject_doctype(*_args):
        raise ValueError("办公 XML 含 DTD，拒绝解析实体声明")

    try:
        with ZipFile(io.BytesIO(raw)) as archive:
            entries = archive.infolist()
            names = [entry.filename for entry in entries]
            if required not in names or "[Content_Types].xml" not in names:
                raise ValueError("不是支持的办公文件格式；旧版或加密文件请先另存为 DOCX/XLSX")
            if len(entries) > MAX_ENTRIES or len(names) != len(set(names)):
                raise ValueError("办公压缩包条目过多或存在重名条目")
            if sum(entry.file_size for entry in entries) > MAX_UNPACKED:
                raise ValueError("办公文件展开后超过 100 MiB，请先拆分")
            for entry in entries:
                if entry.flag_bits & 1 or entry.file_size > MAX_MEMBER:
                    raise ValueError("办公文件含加密条目或单个条目超过 20 MiB")
                # 关系目标可能使用自定义扩展名；不能只凭 .xml 后缀决定是否预检。
                data = archive.read(entry)
                xml_prefix = data.lstrip(b"\xef\xbb\xbf\xff\xfe\x00 \t\r\n")
                if entry.filename.lower().endswith((".xml", ".rels")) or xml_prefix.startswith(b"<"):
                    parser = expat.ParserCreate()
                    parser.StartDoctypeDeclHandler = reject_doctype
                    # 交给解析器识别 UTF-16 XML 的 DTD，不靠搜索 DOCTYPE 字节串。
                    parser.Parse(data, True)
    except (BadZipFile, expat.ExpatError) as exc:
        raise ValueError(f"办公文件 ZIP/XML 损坏或格式不受支持：{exc}") from exc
    return raw


def clipped(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + "…[内容截断]"
