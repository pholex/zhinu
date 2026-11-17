"""Build a macOS .app using system frameworks and an external or bundled Python."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys


APP_VERSION = "0.13.3"


def model_setup_providers() -> list[dict]:
    """UI ordering is local; credential aliases remain owned by core presets."""
    root = str(Path(__file__).resolve().parents[3])
    if root not in sys.path:
        sys.path.insert(0, root)
    from xiaoyu.providers import PRESETS
    labels = (("deepseek", "深度求索"), ("moonshot", "月之暗面"), ("qwen", "千问"),
              ("zhipu", "智谱"), ("anthropic", "Anthropic"), ("gemini", "Gemini"),
              ("openai", "OpenAI"), ("xai", "xAI"))
    return [{"id": name, "label": label, "keyEnvs": list(PRESETS[name].key_envs), "models": list(PRESETS[name].models)}
            for name, label in labels]


def build_icon(lab: Path, resources: Path) -> None:
    """Package the source artwork at every standard and Retina icon size."""
    source = lab / "macos/assets/AppIcon.png"
    iconset = lab / "local-results/AppIcon.iconset"
    iconset.mkdir(parents=True, exist_ok=True)
    for size in (16, 32, 128, 256, 512):
        for scale in (1, 2):
            pixels = size * scale
            suffix = "@2x" if scale == 2 else ""
            target = iconset / f"icon_{size}x{size}{suffix}.png"
            subprocess.run(["sips", "-z", str(pixels), str(pixels), str(source),
                            "--out", str(target)], check=True, stdout=subprocess.DEVNULL)
    subprocess.run(["iconutil", "-c", "icns", str(iconset),
                    "-o", str(resources / "AppIcon.icns")], check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python", default=sys.executable, help="Existing Python with [serve] and httpx")
    parser.add_argument("--output", type=Path, help="Optional output .app path")
    parser.add_argument("--bundled-python", type=Path, help="Prepared relocatable Python directory, including dependencies")
    parser.add_argument("--bundle-id", default="local.xiaoyu.desktop-lab")
    args = parser.parse_args()
    if sys.platform != "darwin":
        parser.error("此构建仅支持 macOS")
    # Do not resolve a venv's Python symlink: that would lose its site-packages.
    python = str(Path(args.python).expanduser().absolute())
    subprocess.run([python, "-c", "import fastapi, uvicorn, httpx"], check=True)
    lab = Path(__file__).resolve().parents[1]
    root = lab.parents[1]
    app = args.output or lab / "local-results/羽案.app"
    if app.exists():
        shutil.rmtree(app)
    contents = app / "Contents"
    macos = contents / "MacOS"
    resources = contents / "Resources"
    runtime = resources / "runtime"
    macos.mkdir(parents=True)
    runtime.mkdir(parents=True)
    build_icon(lab, resources)
    (resources / "model-providers.json").write_text(json.dumps(model_setup_providers(), ensure_ascii=False), encoding="utf-8")
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store", ".env", "*.local.json")
    shutil.copytree(root / "xiaoyu", runtime / "xiaoyu", ignore=ignore)
    shutil.copytree(root / "packages/xiaoyu-agent-sdk/src/xiaoyu_agent_sdk",
                    runtime / "xiaoyu_agent_sdk", ignore=ignore)
    target = runtime / "labs/desktop"
    target.mkdir(parents=True)
    for name in ("demo.py", "server.py", "state.py", "workspace.py", "pairing.py",
                 "app_ownership.py", "automation.py", "automation_api.py",
                 "connections.py", "connections_api.py", "sdk_runtime.py",
                 "preferences.py", "preferences_api.py", "skill_settings.py", "provider_settings.py", "data_api.py", "content.py", "content_api.py", "terminal.py"):
        shutil.copy2(lab / name, target / name)
    shutil.copytree(lab / "web", target / "web", ignore=ignore)
    (target / "macos").mkdir()
    shutil.copy2(lab / "macos/backend.py", target / "macos/backend.py")
    runtime_config = {"python": python}
    if args.bundled_python:
        shutil.copytree(args.bundled_python, resources / "python", symlinks=True, ignore=ignore)
        runtime_config = {"bundledPython": "python/bin/python3"}
    (resources / "runtime.json").write_text(json.dumps(runtime_config), encoding="utf-8")
    shutil.copy2(root / "LICENSE", resources / "LICENSE.txt")
    info = {"CFBundleExecutable": "XiaoyuDesktopLab", "CFBundleIdentifier": args.bundle_id,
        "CFBundleName": "羽案", "CFBundleDisplayName": "羽案",
        "CFBundleIconFile": "AppIcon.icns",
        "CFBundlePackageType": "APPL", "CFBundleShortVersionString": APP_VERSION,
        "CFBundleVersion": APP_VERSION, "LSMinimumSystemVersion": "13.0", "NSHighResolutionCapable": True,
        "NSAppTransportSecurity": {"NSAllowsLocalNetworking": True}}
    (contents / "Info.plist").write_bytes(plistlib.dumps(info))
    cache = lab / "local-results/swift-module-cache"
    import platform
    subprocess.run(["xcrun", "swiftc", "-swift-version", "5", "-parse-as-library", "-O",
        "-target", f"{platform.machine()}-apple-macosx13.0", "-module-cache-path", str(cache),
        "-framework", "AppKit", "-framework", "WebKit", "-framework", "Security",
        str(lab / "macos/RuntimeEnvironment.swift"), str(lab / "macos/ModelSetup.swift"), str(lab / "macos/DesktopCoordinator.swift"), str(lab / "macos/App.swift"),
        "-o", str(macos / "XiaoyuDesktopLab")], check=True)
    subprocess.run(["codesign", "--force", "--sign", "-", str(app)], check=True)
    print(app)


if __name__ == "__main__":
    main()
