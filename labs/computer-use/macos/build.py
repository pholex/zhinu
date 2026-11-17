"""把 Service.swift 编成可双击的「Xiaoyu Computer Use.app」。

只用系统自带的 Swift 编译器与框架，不下载依赖、不改核心包。产物在
labs/computer-use/local-results/ 下（忽略入库），装到 ~/Applications 后 Python 侧
的 computer_use 工具就能找到它（见 xiaoyu/computer_use.py 的 app_path）。

授权按签名的指定要求记：有开发者证书就用它签（重建不丢授权）；退回 ad-hoc 时每次
重建都要重新勾一次「辅助功能」与「屏幕录制」，这是系统的既有行为，不是故障。
"""
from __future__ import annotations

import argparse
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

BUNDLE_ID = "app.xiaoyu.computer-use"
APP_NAME = "Xiaoyu Computer Use"
EXECUTABLE = "XiaoyuComputerUse"
MIN_MACOS = "14.0"  # SCScreenshotManager 从 macOS 14 起可用


CLI_NAME = "xiaoyu-cua"  # --link 装出来的命令名：同一个可执行文件，带参数就是客户端


def build(out_dir: Path, install: bool, sign: str | None = None) -> Path:
    here = Path(__file__).resolve().parent
    app = out_dir / f"{APP_NAME}.app"
    if app.exists():
        shutil.rmtree(app)
    macos = app / "Contents" / "MacOS"
    macos.mkdir(parents=True)
    info = {
        "CFBundleExecutable": EXECUTABLE,
        "CFBundleIdentifier": BUNDLE_ID,
        "CFBundleName": APP_NAME,
        "CFBundleDisplayName": APP_NAME,
        "CFBundlePackageType": "APPL",
        "CFBundleShortVersionString": "0.1.0",
        "CFBundleVersion": "1",
        "LSMinimumSystemVersion": MIN_MACOS,
        "LSUIElement": True,
        "NSHighResolutionCapable": True,
        "NSAppleEventsUsageDescription": "小羽不使用 Apple 事件；此项仅为系统要求而声明。",
    }
    info["CFBundleIconFile"] = "AppIcon"
    (app / "Contents" / "Info.plist").write_bytes(plistlib.dumps(info))
    resources = app / "Contents" / "Resources"
    resources.mkdir()
    make_icon(here, out_dir, resources / "AppIcon.icns")
    (app / "Contents" / "PkgInfo").write_text("APPL????")
    cache = out_dir / "swift-module-cache"
    subprocess.run(
        ["xcrun", "swiftc", "-swift-version", "5", "-O", "-module-cache-path", str(cache),
         "-target", f"arm64-apple-macos{MIN_MACOS}" if _arm() else f"x86_64-apple-macos{MIN_MACOS}",
         "-framework", "AppKit", "-framework", "ApplicationServices", "-framework", "Carbon", "-framework", "ScreenCaptureKit",
         "-framework", "ImageIO", "-framework", "UniformTypeIdentifiers",
         str(here / "Service.swift"), "-o", str(macos / EXECUTABLE)],
        check=True,
    )
    identity = signing_identity(sign)
    subprocess.run(["codesign", "--force", "--sign", identity, "--identifier", BUNDLE_ID, str(app)], check=True)
    print(f"签名身份：{identity}")
    if install:
        dest = Path.home() / "Applications" / app.name
        dest.parent.mkdir(exist_ok=True)
        if dest.exists():
            subprocess.run(["pkill", "-x", EXECUTABLE], check=False)
            shutil.rmtree(dest)
        shutil.copytree(app, dest, symlinks=True)
        return dest
    return app


def link_cli(app: Path, bin_dir: Path) -> Path:
    """把 app 里的可执行文件软链成一条命令：`xiaoyu-cua state 计算器` 这样不经小羽直接用。
    链接指向 app 内的二进制，它启动时解析真身找到 .app，服务仍经 `open` 拉起、授权仍归 app。"""
    bin_dir.mkdir(parents=True, exist_ok=True)
    link = bin_dir / CLI_NAME
    if link.is_symlink() or link.exists():
        link.unlink()
    link.symlink_to(app / "Contents" / "MacOS" / EXECUTABLE)
    return link


def make_icon(here: Path, out_dir: Path, dest: Path) -> None:
    """合成 .icns：macos/AppIcon.png（1024×1024，设计稿）存在就按尺寸缩放它；
    没有就用 MakeIcon.swift 画一枚代码生成的。都经系统 iconutil 合成。"""
    iconset = out_dir / "AppIcon.iconset"
    if iconset.exists():
        shutil.rmtree(iconset)
    master = here / "AppIcon.png"
    if master.exists():
        iconset.mkdir()
        for points in (16, 32, 128, 256, 512):
            for scale in (1, 2):
                name = f"icon_{points}x{points}.png" if scale == 1 else f"icon_{points}x{points}@2x.png"
                subprocess.run(["sips", "-z", str(points * scale), str(points * scale), str(master),
                                "--out", str(iconset / name)], check=True, stdout=subprocess.DEVNULL)
    else:
        subprocess.run(["xcrun", "swift", str(here / "MakeIcon.swift"), str(iconset)], check=True,
                       stdout=subprocess.DEVNULL)
    subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(dest)], check=True)


def signing_identity(requested: str | None) -> str:
    """选签名身份。授权（辅助功能 / 屏幕录制）按签名的指定要求记：ad-hoc 的要求是 cdhash，
    每次重建都变、权限随之作废；用钥匙串里的开发者证书签，要求变成"bundle id + 证书"，
    重建不丢授权。优先 Developer ID，其次 Apple Development，都没有才退回 ad-hoc。"""
    if requested:
        return requested
    out = subprocess.run(["security", "find-identity", "-v", "-p", "codesigning"],
                         capture_output=True, text=True, check=False).stdout
    for prefix in ("Developer ID Application:", "Apple Development:", "Mac Developer:"):
        for line in out.splitlines():
            if prefix in line and '"' in line:
                return line.split('"')[1]
    return "-"


def _arm() -> bool:
    import platform

    return platform.machine() == "arm64"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--install", action="store_true", help="构建后复制到 ~/Applications（会先停掉在跑的旧实例）")
    parser.add_argument("--sign", default=None, help="codesign 身份（默认自动挑钥匙串里的开发者证书；`-` 为 ad-hoc）")
    parser.add_argument("--link", metavar="DIR", default=None,
                        help=f"再把命令行客户端软链到 DIR/{CLI_NAME}（如 ~/.local/bin；需 DIR 在 PATH 里）")
    args = parser.parse_args()
    if sys.platform != "darwin":
        parser.error("此构建仅支持 macOS")
    out = Path(__file__).resolve().parents[1] / "local-results"
    out.mkdir(exist_ok=True)
    app = build(out, args.install, args.sign)
    print(app)
    if args.link:
        print(link_cli(app, Path(args.link).expanduser()))


if __name__ == "__main__":
    main()
