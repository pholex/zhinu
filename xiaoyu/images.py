"""本地图片读取；可选的裁剪缩放只生成媒体缓存，不修改源文件。"""

from __future__ import annotations

import io
from pathlib import Path

from . import fsguard, media


def load_image(path: Path, crop: list[int] | None = None,
               max_size: int | None = None) -> tuple[str, str]:
    if crop is not None and (
        not isinstance(crop, list) or len(crop) != 4
        or any(type(value) is not int for value in crop)
        or not (0 <= crop[0] < crop[2] and 0 <= crop[1] < crop[3])
    ):
        return "", "crop 必须是非负且有面积的 [左,上,右,下] 整数坐标"
    if max_size is not None and (type(max_size) is not int or not 1 <= max_size <= 4096):
        return "", "max_size 必须是 1–4096 的整数"
    if crop is None and max_size is None:
        return media.accept_file(path)
    try:
        from PIL import Image
    except ImportError:
        return "", "裁剪缩放需要图片扩展：pip install 'xiaoyu-agent[image]'；省略 crop/max_size 可读取原图"
    try:
        info = fsguard.require_regular(path)
        if info.st_size > media.MAX_IMAGE_BYTES:
            return "", "源图片超过 7 MiB 上限，请先压缩文件"
        with path.open("rb") as stream:
            data = stream.read(media.MAX_IMAGE_BYTES + 1)
        if len(data) > media.MAX_IMAGE_BYTES or not media.sniff_mime(data):
            return "", "源图片超限或不是 PNG/JPEG/GIF/WEBP"
        with Image.open(io.BytesIO(data)) as source:
            # 在解码像素前设上限，避免小文件解压为巨大的位图。
            if source.width * source.height > 40_000_000:
                return "", "图片超过 4000 万像素，请先缩小"
            if crop is not None and (crop[2] > source.width or crop[3] > source.height):
                return "", "crop 超出原图边界"
            # 动画只读第一帧；坐标按原始像素，不隐式旋转 EXIF。
            result = source.crop(tuple(crop)) if crop is not None else source.copy()
            try:
                if max_size is not None:
                    result.thumbnail((max_size, max_size), Image.Resampling.LANCZOS)
                converted = result.convert("RGBA")
                try:
                    output = io.BytesIO()
                    converted.save(output, format="PNG")
                    return media.accept(output.getvalue(), path.name)
                finally:
                    converted.close()
            finally:
                result.close()
    except (OSError, ValueError, Image.DecompressionBombError) as exc:
        return "", f"无法读取图片：{exc}"
