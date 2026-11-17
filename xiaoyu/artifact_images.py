"""Bounded passive raster previews; never infer media type from a filename."""
from __future__ import annotations

import base64
import hashlib
import struct
import zlib
from typing import Any

IMAGE_BYTES = 2 * 1024 * 1024
MAX_SIDE = 8192
MAX_PIXELS = 16 * 1024 * 1024
DECODED_BYTES = 32 * 1024 * 1024
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _dimensions(width: int, height: int) -> tuple[int, int]:
    if not (0 < width <= MAX_SIDE and 0 < height <= MAX_SIDE) or width * height > MAX_PIXELS:
        raise ValueError("图片尺寸超过预览上限（单边 8192、总像素 16 Mi）。")
    return width, height


def _png(data: bytes) -> tuple[int, int]:
    position, width, height = 8, 0, 0
    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}
    depth = color = 0
    compressed = bytearray()
    seen = set()
    idat_closed = False
    palette_entries = 0
    chunks = 0
    while position < len(data):
        chunks += 1
        if chunks > 1024:
            raise ValueError("PNG 数据块数量超过预览上限。")
        if len(data) - position < 12:
            raise ValueError("PNG 数据不完整。")
        length = int.from_bytes(data[position:position + 4], "big")
        kind = data[position + 4:position + 8]
        if (length > len(data) - position - 12 or len(kind) != 4
                or any(not (65 <= byte <= 90 or 97 <= byte <= 122) for byte in kind)):
            raise ValueError("PNG 数据块无效。")
        body = data[position + 8:position + 8 + length]
        checksum = int.from_bytes(data[position + 8 + length:position + 12 + length], "big")
        if zlib.crc32(kind + body) & 0xffffffff != checksum:
            raise ValueError("PNG 数据块校验失败。")
        position += 12 + length
        if not seen and kind != b"IHDR":
            raise ValueError("PNG 缺少首个尺寸头。")
        if kind == b"IHDR":
            if seen or length != 13:
                raise ValueError("PNG 尺寸头无效。")
            width, height, depth, color, compression, filtering, interlace = struct.unpack(">IIBBBBB", body)
            _dimensions(width, height)
            allowed = {0: {1, 2, 4, 8, 16}, 2: {8, 16}, 3: {1, 2, 4, 8}, 4: {8, 16}, 6: {8, 16}}
            if depth not in allowed.get(color, set()) or compression or filtering:
                raise ValueError("PNG 编码格式无效。")
            if interlace:
                raise ValueError("暂不预览交错 PNG，请使用普通静态 PNG。")
        elif kind == b"PLTE":
            if kind in seen or b"IDAT" in seen or length == 0 or length % 3 or length > 768:
                raise ValueError("PNG 调色板无效。")
            palette_entries = length // 3
        elif kind == b"IDAT":
            if idat_closed or (color == 3 and not palette_entries):
                raise ValueError("PNG 图像数据顺序无效。")
            compressed.extend(body)
        elif kind == b"IEND":
            if length or position != len(data) or b"IDAT" not in seen:
                raise ValueError("PNG 结束标记无效或含尾随数据。")
            row_bytes = (width * channels[color] * depth + 7) // 8
            expected = height * (row_bytes + 1)
            if expected > DECODED_BYTES:
                raise ValueError("PNG 展开大小超过 32 MiB 预览上限。")
            decoder = zlib.decompressobj()
            try:
                pixels = decoder.decompress(bytes(compressed), expected + 1)
            except zlib.error:
                raise ValueError("PNG 压缩数据无效。") from None
            if (len(pixels) != expected or not decoder.eof or decoder.unused_data or decoder.unconsumed_tail
                    or any(pixels[offset] > 4 for offset in range(0, len(pixels), row_bytes + 1))):
                raise ValueError("PNG 像素数据不完整或超出声明尺寸。")
            return width, height
        elif kind in {b"iCCP", b"zTXt", b"iTXt"}:
            raise ValueError("暂不预览含压缩配置或扩展文本的 PNG。")
        elif kind in {b"acTL", b"fcTL", b"fdAT"}:
            raise ValueError("暂不预览动画 PNG。")
        elif not kind[0] & 32:
            raise ValueError("PNG 包含不支持的关键数据块。")
        if kind != b"IDAT" and b"IDAT" in seen:
            idat_closed = True
        seen.add(kind)
    raise ValueError("PNG 缺少结束标记。")


def _jpeg(data: bytes) -> tuple[int, int]:
    position, dimensions, scan, has_scan = 2, None, False, False
    segments = scans = 0
    while position < len(data):
        if data[position] != 0xff:
            if not scan:
                raise ValueError("JPEG 标记顺序无效。")
            position += 1
            continue
        position += 1
        while position < len(data) and data[position] == 0xff:
            position += 1
        if position >= len(data):
            break
        marker = data[position]
        position += 1
        if scan and (marker == 0 or 0xd0 <= marker <= 0xd7):
            continue
        scan = False
        segments += 1
        if segments > 1024:
            raise ValueError("JPEG 数据块数量超过预览上限。")
        if marker == 0xd9:
            if position != len(data) or dimensions is None or not has_scan:
                raise ValueError("JPEG 结束标记无效或含尾随数据。")
            return dimensions
        if marker in {0x00, 0xd8, 0x01} or 0xd0 <= marker <= 0xd7:
            raise ValueError("JPEG 包含非预期标记。")
        if position + 2 > len(data):
            break
        length = int.from_bytes(data[position:position + 2], "big")
        if length < 2 or position + length > len(data):
            raise ValueError("JPEG 数据块不完整。")
        body = data[position + 2:position + length]
        position += length
        if marker in {0xc0, 0xc2}:
            if dimensions is not None or len(body) < 6:
                raise ValueError("JPEG 尺寸头无效。")
            precision, height, width, components = struct.unpack(">BHHB", body[:6])
            if precision != 8 or components not in {1, 3, 4} or len(body) != 6 + components * 3:
                raise ValueError("JPEG 仅支持常规 8 位静态编码。")
            dimensions = _dimensions(width, height)
        elif marker in {0xc1, 0xc3, 0xc5, 0xc6, 0xc7, 0xc9, 0xca, 0xcb, 0xcd, 0xce, 0xcf, 0xcc, 0xdc}:
            raise ValueError("JPEG 包含不支持的扩展编码或动态尺寸。")
        elif marker == 0xda:
            scans += 1
            if scans > 16:
                raise ValueError("JPEG 扫描次数超过预览上限。")
            if dimensions is None or not body or not 1 <= body[0] <= 4 or len(body) != 4 + 2 * body[0]:
                raise ValueError("JPEG 扫描头无效。")
            scan, has_scan = True, True
    raise ValueError("JPEG 数据不完整，缺少结束标记。")


def image_preview(data: bytes | None, version: str) -> dict[str, Any]:
    result = {"available": False, "mime_type": "", "width": 0, "height": 0,
              "data_url": "", "sha256": "", "version": version, "note": ""}
    if data is None or version not in {"delivered", "current_changed"}:
        result["note"] = "当前文件不可读取；未保存历史图片快照。"
        return result
    if len(data) > IMAGE_BYTES:
        result["note"] = "图片超过 2 MiB 预览上限。"
        return result
    try:
        if data.startswith(PNG_SIGNATURE):
            width, height = _png(data)
            mime = "image/png"
        elif data.startswith(b"\xff\xd8\xff"):
            width, height = _jpeg(data)
            mime = "image/jpeg"
        else:
            result["note"] = "仅支持经校验的静态 PNG / JPEG；其他格式不作为图片执行或嵌入。"
            return result
    except ValueError as exc:
        result["note"] = str(exc)
        return result
    result.update(available=True, mime_type=mime, width=width, height=height,
                  data_url=f"data:{mime};base64," + base64.b64encode(data).decode("ascii"),
                  sha256=hashlib.sha256(data).hexdigest(),
                  note="交付版本（指纹一致）。" if version == "delivered" else "当前文件已变化；此图不代表原交付版本。")
    return result
