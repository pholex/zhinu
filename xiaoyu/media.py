"""图片进内核的唯一入口：内容部件（parts）+ sha256 落盘缓存。

**为什么内核历史里存的是引用而不是 base64**。多模态一进来，`content` 就从
`str` 变成 `str | list[part]`，而这条链路上每一个环节都会碰它：压缩要算长度、
会话日志要落盘、resume 要重放、hook 要看正文。一张 500KB 的截图转成 data URL
是 ~680KB 的字符串，直接塞进 `messages`：

- 会话 JSONL 每轮都把它再写一遍（append-only，压缩过的历史还会整份重写）；
- `estimate_text` 按字符估 token，一张图会被当成 ~18 万 token，压缩当场发疯；
- `xiaoyu sessions` 的预览、hook 的 `clip`、trace 全都要先搬运这坨字符串。

所以内核里的图片部件只带一个 `xiaoyu-media://<sha256>.<ext>` 引用，真实字节
落在用户配置目录下的内容寻址缓存里（同一张图在多轮/多会话里只存一份）。
展开成 data URL 发生在**出网的那一刻**（`responses.Transport`，与 `_reasoning`
的净化同一个位置，理由也相同：出网口只有一个，就不存在"哪天新加一条路径忘了展开"）。

于是 resume 天然可用（引用指向的文件还在）、压缩看到的是个小对象、
会话日志不膨胀，而模型收到的仍是标准的 OpenAI `image_url` 部件。

**不做缓存淘汰**：图片留在 `<配置目录>/media/`，与 `sessions/` 同寿命——
会话文件本身也从不自动清理，多一套 GC 反而是新的失败面。要清理就整个删目录。
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from urllib.request import url2pathname

from .config import user_config_dir

#  引用 scheme。刻意不用 `file://`：万一某条路径忘了展开，带自定义 scheme 的
#  URL 会被上游明确拒绝，而 `file://` 可能被某些端点当成"去读你本机文件"。
SCHEME = "xiaoyu-media://"

#  chat completions 的部件类型（Responses 侧的 input_text / input_image
#  由 responses.to_input 翻译，协议差异不进内核）
TEXT_PART = "text"
IMAGE_PART = "image_url"

#  一张图的固定 token 估算。各家算法不同（OpenAI 按 512px 分块、Anthropic 按
#  宽高/750），这里取一个"宁可高估"的常数：估低会让压缩迟迟不触发、最终撞上游
#  400，估高只是早压一点。真实 usage 一到就落锚覆盖（见 agent._anchor）。
IMAGE_TOKENS = 1500

_MIME_EXT = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/gif": "gif",
    "image/webp": "webp",
}
_EXT_MIME = {"png": "image/png", "jpg": "image/jpeg", "gif": "image/gif", "webp": "image/webp"}
#  引用名形状固定为 <64 位 hex>.<ext>：拼路径前必须校验，否则一个带 ../ 的
#  引用就能把读取指到缓存目录外面（引用可能来自会话文件、宿主、将来的 MCP）
_REF_RE = re.compile(r"^[0-9a-f]{64}\.[a-z0-9]{1,5}$")


#  能当图片收下的扩展名（与 _MIME_EXT 同一批格式，各家视觉端点的公约数）
IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp"})

#  单张图上限。各家限的是 base64 之后的体积（普遍 5~20 MB），base64 膨胀 4/3，
#  所以原始字节卡在 7 MB 左右两边都安全。超限当场拒绝，好过发出去被 400——
#  用户手里那张 20 MB 的截图，早说一句比一轮请求之后再说有用得多。
MAX_IMAGE_BYTES = 7 * 1024 * 1024

#  文件头魔数：**不信扩展名**。用户把 jpg 存成 .png 是常事，而 mime 报错了
#  上游只会回一句语焉不详的 invalid image，查起来很远
_MAGIC = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
)


#  JPEG 的 SOF（Start Of Frame）标记：宽高就藏在它后面。跳过 SOF4/SOF8/SOFC——
#  那三个是 DHT/JPG/DAC，不是帧头，按帧头解会读出垃圾尺寸
_JPEG_SOF = frozenset(
    {*range(0xC0, 0xC4), *range(0xC5, 0xC8), *range(0xC9, 0xCC), *range(0xCD, 0xD0)}
)


def cache_dir() -> Path:
    return user_config_dir() / "media"


def dimensions(data: bytes) -> tuple[int, int] | None:
    """从文件头读宽高。读不出返回 None（chip 退回只显示体积，不猜）。

    自己解四种格式的头，是为了不引 PIL——为一个标签引一整个成像库不划算。
    这些头的偏移都是格式规范里的定值，几十行就够，且解错的代价只是标签难看。
    """
    try:
        if data[:8] == b"\x89PNG\r\n\x1a\n" and data[12:16] == b"IHDR":
            return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
        if data[:3] == b"GIF":
            #  GIF 的逻辑屏幕宽高是小端 uint16
            return int.from_bytes(data[6:8], "little"), int.from_bytes(data[8:10], "little")
        if data[:2] == b"\xff\xd8":
            return _jpeg_dimensions(data)
        if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            return _webp_dimensions(data)
    except (IndexError, ValueError):
        return None
    return None


def _jpeg_dimensions(data: bytes) -> tuple[int, int] | None:
    """JPEG 要顺着段链走到 SOF 才有宽高，没法像 PNG 那样定偏移取。"""
    index = 2
    while index + 9 <= len(data):  #  <= 而非 <：SOF 恰好收尾的图（宽高就是最后 4 字节）不能漏
        if data[index] != 0xFF:
            index += 1
            continue
        marker = data[index + 1]
        #  填充字节、SOI、RSTn 都没有长度字段，直接跳过
        if marker in (0xFF, 0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            index += 2
            continue
        if marker in _JPEG_SOF:
            height = int.from_bytes(data[index + 5 : index + 7], "big")
            width = int.from_bytes(data[index + 7 : index + 9], "big")
            return (width, height) if width and height else None
        index += 2 + int.from_bytes(data[index + 2 : index + 4], "big")
    return None


def _webp_dimensions(data: bytes) -> tuple[int, int] | None:
    """WEBP 三种子格式，宽高的存法各不相同。"""
    chunk = data[12:16]
    if chunk == b"VP8X":  # 扩展格式：24 位小端，存的是"宽高减一"
        return (
            1 + int.from_bytes(data[24:27], "little"),
            1 + int.from_bytes(data[27:30], "little"),
        )
    if chunk == b"VP8 ":  # 有损：14 位有效
        return (
            int.from_bytes(data[26:28], "little") & 0x3FFF,
            int.from_bytes(data[28:30], "little") & 0x3FFF,
        )
    if chunk == b"VP8L":  # 无损：宽高各 14 位打包在 4 字节里
        bits = int.from_bytes(data[21:25], "little")
        return 1 + (bits & 0x3FFF), 1 + ((bits >> 14) & 0x3FFF)
    return None


def label(ref: str) -> str:
    """引用 → chip 上给人看的一行，如 "1920×1080 · 42 KB"。

    读不到或认不出尺寸就只报体积——标签是辅助信息，缺了不影响功能。
    """
    path = path_of(ref)
    try:
        data = b"" if path is None else path.read_bytes()
    except OSError:
        return "图片"
    size = f"{len(data) // 1024 or 1} KB"
    wh = dimensions(data)
    return f"{wh[0]}×{wh[1]} · {size}" if wh else size


def sniff_mime(data: bytes) -> str:
    """按文件头认格式。认不出返回 ""（调用方据此拒绝，而不是猜一个）。"""
    for magic, mime in _MAGIC:
        if data.startswith(magic):
            return mime
    #  WEBP 是 RIFF 容器，格式标记在第 8 字节
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return ""


# ---------- 部件 ----------


def text_part(text: str) -> dict[str, Any]:
    return {"type": TEXT_PART, "text": text}


def image_part(ref: str) -> dict[str, Any]:
    return {"type": IMAGE_PART, "image_url": {"url": ref}}


def is_parts(content: Any) -> bool:
    return isinstance(content, list)


def text_of(content: Any) -> str:
    """任意 content → 纯文本。**全仓收窄 content 的唯一入口**。

    在此之前满仓都是 `str(message.get("content") or "")`——content 变成部件列表
    之后，这种写法不报错，只是悄悄把 `[{'type': 'image_url', ...}]` 当成用户
    原话喂给 hook、写进会话预览、拿去和 SYNTHETIC_USER_TEXTS 比对。集中成一个
    函数（而不是逐处判断类型）是因为漏掉一处的症状全是"静默错值"。
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return str(content)
    pieces: list[str] = []
    for part in content:
        if not isinstance(part, dict):
            continue
        if part.get("type") == TEXT_PART:
            pieces.append(str(part.get("text") or ""))
        elif part.get("type") == IMAGE_PART:
            pieces.append("[图片]")
    return "".join(pieces)


def as_parts(content: Any) -> list[dict[str, Any]]:
    """任意 content → 部件列表。空内容给空列表（拼接时天然不留空洞）。"""
    if isinstance(content, list):
        return list(content)
    text = "" if content is None else str(content)
    return [text_part(text)] if text else []


def images_of(content: Any) -> list[dict[str, Any]]:
    if not isinstance(content, list):
        return []
    return [
        part
        for part in content
        if isinstance(part, dict) and part.get("type") == IMAGE_PART
    ]


def has_image(messages: list[dict[str, Any]]) -> bool:
    return any(images_of(message.get("content")) for message in messages)


# ---------- 落盘缓存 ----------


def store(data: bytes, mime: str) -> str:
    """字节 → `xiaoyu-media://<sha256>.<ext>` 引用。内容寻址，天然去重。

    写不进去（只读盘、配额满）不抛异常：调用方拿到 "" 就知道这张图存不下，
    退回文字占位即可——为了一张图让整轮对话失败不划算。
    """
    ext = _MIME_EXT.get(mime.lower().split(";")[0].strip(), "png")
    ref = f"{hashlib.sha256(data).hexdigest()}.{ext}"
    path = cache_dir() / ref
    if path.exists():
        return SCHEME + ref
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        #  同 sha256 的并发写：先写临时文件再原子改名，读者永远看不到半张图
        tmp = path.with_name(f".{ref}.{os.getpid()}")
        tmp.write_bytes(data)
        os.replace(tmp, path)
    except OSError:
        return ""
    return SCHEME + ref


def store_base64(payload: str, mime: str) -> str:
    """MCP 回来的 base64 字符串 → 引用。解不开就返回 ""（当没有这张图）。"""
    try:
        return store(base64.b64decode(payload, validate=True), mime)
    except (ValueError, TypeError):
        return ""


def accept(data: bytes, source: str = "") -> tuple[str, str]:
    """字节 → (引用, 出错原因)。二者恰有一个非空。

    **进内核的每一张图都必须过这里**（剪贴板、文件、将来的宿主注入都一样）：
    格式与体积的判断只写一遍，新入口只管取字节。
    """
    if not data:
        return "", f"{source or '这张图'}是空的"
    mime = sniff_mime(data)
    if not mime:
        return "", f"{source or '这个文件'}不是能识别的图片格式（PNG/JPEG/GIF/WEBP）"
    if len(data) > MAX_IMAGE_BYTES:
        mb = len(data) / 1024 / 1024
        return "", f"图片 {mb:.1f} MB，超过 {MAX_IMAGE_BYTES // 1024 // 1024} MB 上限"
    ref = store(data, mime)
    return (ref, "") if ref else ("", "写图片缓存失败（磁盘满或只读？）")


def accept_file(path: Path) -> tuple[str, str]:
    """文件路径 → (引用, 出错原因)。"""
    try:
        data = path.read_bytes()
    except OSError as exc:
        return "", f"读不了 {path.name}：{exc.strerror or exc}"
    return accept(data, path.name)


def _shell_split(line: str) -> list[str]:
    """按终端拖入的形态切成一个个路径 token。

    ⚠️ 分平台，别图省事只写 `shlex.split(line)`：POSIX 模式下反斜杠是**转义符**，
    而 Windows 路径的反斜杠是**分隔符**——`C:\\Users\\me\\a.png` 会被吃成
    `C:Usersmea.png`，路径不存在于是整体判否，Windows 上"拖文件进终端"因此
    整个功能是死的（v0.29.0/0.29.1 都带着这个）。posix=False 保住反斜杠，
    代价是引号会留在 token 里，得自己剥。

    两种平台各自只支持自己终端真正会产出的形态：POSIX 的 `\\ ` 转义空格，
    Windows 的 `"整条路径"` 加引号。Windows 上的 `\\ ` 无法支持也不必支持——
    反斜杠已经是分隔符，`my\\ shot.png` 本身就有歧义。
    """
    if os.name == "nt":
        return [token.strip('"') for token in shlex.split(line, posix=False)]
    return shlex.split(line)


def _path_from_file_uri(uri: str) -> str:
    """`file://` URI → 本平台的路径字符串。

    ⚠️ 别退回 `unquote(uri[7:])` 那种裸切字符串：Windows 的 `Path.as_uri()` 是
    `file:///C:/…`，切掉 7 个字符后剩下 `/C:/…`——一个在 Windows 上根本不存在
    的路径，于是 split_paths 整体判否、拖进来的文件被当普通文字。v0.29.0 的
    Windows CI 就红在这里（macOS/Linux 全绿，因为 POSIX 下多出来的那个 `/`
    恰好就是根路径的斜杠）。url2pathname 按平台还原，且自带 unquote。
    """
    return url2pathname(urlparse(uri).path)


def split_paths(text: str) -> list[Path]:
    """一行文本 → 它列出的文件路径。**只要有一个不是真实文件就整体判否**（返回空）。

    覆盖两个入口：拖文件进终端（终端按 shell 规则转义后当文本粘进来，拖多个
    就是空格分隔的多个路径）、`@` 补全出来的路径。判据刻意收紧到"每一项都是
    真实存在的文件"——宁可漏判（用户改按 Ctrl-V）也不能误判：把用户想讨论的
    一段文字吃成附件，比不识别烦人得多。
    """
    line = text.strip()
    if not line or "\n" in line:
        return []
    try:
        tokens = _shell_split(line)
    except ValueError:
        return []
    if not tokens:
        return []
    paths: list[Path] = []
    for token in tokens:
        if token.startswith("file://"):
            token = _path_from_file_uri(token)
        path = Path(token).expanduser()
        try:
            if not path.is_file():
                return []
        except OSError:
            return []
        paths.append(path)
    return paths


def is_image_path(path: Path) -> bool:
    return path.suffix.lower() in IMAGE_SUFFIXES


@dataclass(frozen=True)
class Clip:
    """剪贴板里拿得出的东西。

    分成两半是因为它们的去向不同：`images` 是位图数据（截图），只能当图片收；
    `files` 是文件路径，图片进 chip、其余原样插成路径文本——模型拿到路径可以
    自己 read_file，比我们替它决定"这个格式没用"强。
    """

    images: tuple[bytes, ...] = ()
    files: tuple[Path, ...] = ()
    problem: str = ""


def clipboard() -> Clip:
    """系统剪贴板 → Clip。

    不引 PIL（`ImageGrab` 省事，但那是一整个成像库的依赖，为一个
    粘贴功能不值），改用各平台自带的命令行取数据。取不到一律给**可行动的
    原因**而不是静默失败——"按了没反应"是最难自查的一类问题。
    """
    if sys.platform == "darwin":
        images = tuple(data for data in (_osascript_png(),) if data)
        files = _osascript_files()
        if images or files:
            return Clip(images, files)
        return Clip(problem="剪贴板里没有图片（截图用 Cmd-Shift-Ctrl-4 可直接进剪贴板）")

    if sys.platform == "win32":
        return _windows_clipboard()

    #  Linux/BSD：Wayland 与 X11 各一个标准工具，装了哪个用哪个
    for image_cmd, uri_cmd, tool in (
        (
            ["wl-paste", "--no-newline", "--type", "image/png"],
            ["wl-paste", "--no-newline", "--type", "text/uri-list"],
            "wl-paste",
        ),
        (
            ["xclip", "-selection", "clipboard", "-t", "image/png", "-o"],
            ["xclip", "-selection", "clipboard", "-t", "text/uri-list", "-o"],
            "xclip",
        ),
    ):
        if shutil.which(image_cmd[0]) is None:
            continue
        proc = _run(image_cmd)
        images = (proc.stdout,) if proc is not None and proc.returncode == 0 and proc.stdout else ()
        uri_proc = _run(uri_cmd, text=True)
        files = (
            _paths_from_uri_list(uri_proc.stdout)
            if uri_proc is not None and uri_proc.returncode == 0
            else ()
        )
        if images or files:
            return Clip(images, files)
        return Clip(problem=f"剪贴板里没有图片（{tool} 没取到 image/png）")
    return Clip(problem="取剪贴板图片需要 wl-paste（Wayland）或 xclip（X11），先装一个")


def _paths_from_uri_list(raw: str) -> tuple[Path, ...]:
    paths = []
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("file://"):
            line = _path_from_file_uri(line)
        path = Path(line)
        with contextlib.suppress(OSError):
            if path.is_file():
                paths.append(path)
    return tuple(paths)


def _run(command: list[str], text: bool = False) -> Any:
    """跑一个取剪贴板的小命令。任何失败都返回 None——粘贴功能不该抛异常。"""
    try:
        return subprocess.run(command, capture_output=True, text=text, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None


def _osascript_png() -> bytes:
    #  osascript 把二进制回成 «data PNGf89504E47…» 的十六进制字面量
    proc = _run(["osascript", "-e", "the clipboard as «class PNGf»"], text=True)
    if proc is None or proc.returncode != 0:
        return b""
    match = re.search(r"«data PNGf([0-9A-Fa-f]+)»", proc.stdout)
    if not match:
        return b""
    try:
        return bytes.fromhex(match.group(1))
    except ValueError:
        return b""


#  ⚠️ 取文件路径必须走 `the clipboard as list`，**不能用 `as «class furl»`**：
#  后者只在剪贴板里恰好一个文件时成立，多选时直接报 -1700（"不能将某些数据
#  转换为预期的类型"），于是"复制两张图"反而比"复制一张"更用不了。
#  as list 三种情况实测都对：多文件逐行、单文件一行、纯文本返回空。
_OSASCRIPT_FILES = """set out to {}
try
  set items_ to (the clipboard as list)
on error
  return ""
end try
repeat with x in items_
  try
    set end of out to POSIX path of (x as alias)
  end try
end repeat
set AppleScript's text item delimiters to linefeed
return out as text"""


def _osascript_files() -> tuple[Path, ...]:
    proc = _run(["osascript", "-e", _OSASCRIPT_FILES], text=True)
    if proc is None or proc.returncode != 0:
        return ()
    paths = []
    for line in proc.stdout.splitlines():
        path = Path(line.strip())
        with contextlib.suppress(OSError):
            if line.strip() and path.is_file():
                paths.append(path)
    return tuple(paths)


def _windows_clipboard() -> Clip:
    #  一次 PowerShell 调用两件事：位图存成临时 PNG（Get-Clipboard 给的是 .NET
    #  Bitmap 对象，没有直接拿字节的路子），文件清单直接打到 stdout
    tmp = Path(tempfile.gettempdir()) / f"xiaoyu-clip-{os.getpid()}.png"
    script = (
        "Add-Type -AssemblyName System.Windows.Forms,System.Drawing;"
        "$img=[Windows.Forms.Clipboard]::GetImage();"
        f"if($img){{$img.Save('{tmp}',[Drawing.Imaging.ImageFormat]::Png)}};"
        "[Windows.Forms.Clipboard]::GetFileDropList()|ForEach-Object{$_}"
    )
    proc = _run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script], text=True)
    if proc is None:
        return Clip(problem="调不起 powershell，取不了剪贴板图片")
    images: tuple[bytes, ...] = ()
    with contextlib.suppress(OSError):
        images = (tmp.read_bytes(),)
        tmp.unlink()
    files = tuple(
        path
        for line in (proc.stdout or "").splitlines()
        if line.strip()
        for path in [Path(line.strip())]
        if path.is_file()
    )
    if images or files:
        return Clip(images, files)
    return Clip(problem="剪贴板里没有图片")


def path_of(url: str) -> Path | None:
    """引用 → 缓存文件路径。不是本地引用、或引用名不合法时返回 None。"""
    if not url.startswith(SCHEME):
        return None
    ref = url[len(SCHEME) :]
    if not _REF_RE.match(ref):
        return None
    return cache_dir() / ref


def data_url(url: str) -> str:
    """引用 → `data:<mime>;base64,...`。不是本地引用就原样返回
    （宿主直接塞 https:// 或现成 data URL 的情况照旧能用）。

    文件丢了（用户删了缓存目录、换了机器 resume 旧会话）同样原样返回——
    上游会拒掉这个 URL 并给出可读错误，好过内核在这里抛异常打断整轮。
    """
    path = path_of(url)
    if path is None:
        return url
    try:
        data = path.read_bytes()
    except OSError:
        return url
    mime = _EXT_MIME.get(path.suffix.lstrip("."), "image/png")
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


def inline(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """出网前把所有 `xiaoyu-media://` 引用展开成 data URL。

    没有图片的历史原样返回（不拷贝）——这条路径在每一次请求上，
    绝大多数会话一张图都没有，不该为它多付一遍列表重建。
    """
    if not has_image(messages):
        return messages
    expanded: list[dict[str, Any]] = []
    for message in messages:
        content = message.get("content")
        if not images_of(content):
            expanded.append(message)
            continue
        parts: list[dict[str, Any]] = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == IMAGE_PART:
                url = (part.get("image_url") or {}).get("url", "")
                part = {**part, "image_url": {**part["image_url"], "url": data_url(url)}}
            parts.append(part)
        expanded.append({**message, "content": parts})
    return expanded
