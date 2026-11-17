# 桌面应用图标

`AppIcon.png` 为应用图标源图，保留透明外边距。源设计由用户提供，为蓝色渐变羽毛；
使用内置 imagegen 编辑为浅色圆角底板版本，提高小尺寸对比度。没有使用 CLI 或额外图像依赖。
构建尺寸转换不改图形设计，输出在被忽略的 `local-results/`。
工作台侧栏和空白页共用 `web/logo.png`，由本源图用 `sips -z 256 256` 缩小，
更新源图时同步生成这份 Web 资源。

## 第一轮提示词

Use case: precise-object-edit. Edit the supplied blue feather logo into one production-ready macOS desktop app icon, square 1024 x 1024. Preserve its recognizable silhouette: a graceful quill sweeping from bottom-left to top-right, the same long pointed upper vane, three separated tapered lobes, diagonal white negative-space shaft, and light-blue-to-royal-blue gradient. Keep the feather recognizable, not a new logo. Remove all paper grain. Increase blue contrast slightly for legibility at 32px. Optically center the feather, occupying about 66% of canvas width and 72% height. Place it on a clean near-white rounded-square tile, 88% of canvas width and height with consistent transparent exterior margin, smooth generous rounded corners, faint cool-white bevel and very subtle shadow. Crisp smooth vector-like edges, restrained flat gradient, no glossy plastic. Actual alpha transparency outside the tile including outer corners, no checkerboard. Only a single icon, no text, no additional objects, no presentation board, no perspective, no watermark.

## 最终编辑提示词

Precise cleanup of this existing app icon. Preserve the blue feather exactly, same silhouette and gradient and position. Preserve the near-white rounded-square tile but make its edge a mathematically smooth crisp clean rounded-square outline. Remove ALL drop shadow, bevel, glow, grain, speckles, smudges, noise, and stray pixels outside the tile. Tile exterior is uniformly 100% transparent including corners, clean alpha zero, with only normal 1px antialiasing along the actual contour. Flat white tile, no raised edge or 3D extrusion. Keep the tile contained within the square canvas with equal 7% transparent margins all around. A single polished production macOS app icon PNG with alpha. No text, no mockup.
