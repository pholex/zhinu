"""把图像模型生成的 logo 整理成 macOS 图标设计稿 AppIcon.png（1024×1024，真透明）。

生成图常见两个毛病：① "透明背景"是画出来的棋盘格，不是 alpha；② 圆角方块没按
macOS 规范留边（图形应占画布约 80%）。这里从四角向内泛洪，把浅灰 / 白的棋盘格抠成
透明（方块内的白色羽毛被彩色包围，泛洪到不了），再按不透明区域的外接框裁切、
按比例居中留边、缩到 1024。

用法：python3 prepare_icon.py <生成图.png> [输出，默认同目录 AppIcon.png]
依赖系统 python3 自带的 Pillow（本仓库 venv 刻意不装图像库）。
"""
from __future__ import annotations

import sys
from collections import deque
from pathlib import Path

from PIL import Image

CANVAS = 1024
TILE_RATIO = 0.80   # 圆角方块占画布边长的比例（macOS 图标规范约 824/1024）


def is_checker(rgb: tuple[int, int, int]) -> bool:
    r, g, b = rgb
    #  棋盘格是无彩色的灰白两色（含生成图里偏暗的灰），方块是饱和的蓝紫；按"无彩色且不太暗"判
    return max(r, g, b) - min(r, g, b) < 22 and min(r, g, b) > 80


def knock_out_background(img: Image.Image) -> Image.Image:
    img = img.convert("RGBA")
    w, h = img.size
    px = img.load()
    seen = bytearray(w * h)
    queue: deque[tuple[int, int]] = deque()
    for x, y in ((0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1)):
        queue.append((x, y))
    while queue:
        x, y = queue.popleft()
        if x < 0 or y < 0 or x >= w or y >= h or seen[y * w + x]:
            continue
        seen[y * w + x] = 1
        r, g, b, a = px[x, y]
        if a == 0 or is_checker((r, g, b)):
            px[x, y] = (0, 0, 0, 0)
            queue.extend(((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)))
    return img


def frame(img: Image.Image) -> Image.Image:
    box = img.getbbox()
    if box is None:
        raise SystemExit("图里没有不透明内容")
    tile = img.crop(box)
    side = max(tile.size)
    square = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    square.paste(tile, ((side - tile.width) // 2, (side - tile.height) // 2))
    target = int(CANVAS * TILE_RATIO)
    scaled = square.resize((target, target), Image.LANCZOS)
    canvas = Image.new("RGBA", (CANVAS, CANVAS), (0, 0, 0, 0))
    canvas.paste(scaled, ((CANVAS - target) // 2, (CANVAS - target) // 2))
    return canvas


def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    src = Path(sys.argv[1]).expanduser()
    dest = Path(sys.argv[2]).expanduser() if len(sys.argv) > 2 else Path(__file__).resolve().parent / "AppIcon.png"
    out = frame(knock_out_background(Image.open(src)))
    out.save(dest, "PNG")
    print(dest)


if __name__ == "__main__":
    main()
