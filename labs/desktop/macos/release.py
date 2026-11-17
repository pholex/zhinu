"""Local, staged desktop distribution. Never pushes source or uploads to a download host."""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
from pathlib import Path
import platform
import plistlib
import shutil
import subprocess
import sys
import tempfile
import tomllib

if __package__:
    from .build import APP_VERSION
else:
    from build import APP_VERSION

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
BUNDLE_ID = "click.openapi.xiaoyu"


def run(*args, **kwargs):
    return subprocess.run([str(a) for a in args], check=True, **kwargs)


def dependency_set():
    """Export only the declared desktop dependency closure from the build environment."""
    from packaging.requirements import Requirement
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(errors="replace"))["project"]
    pending = list(project["dependencies"]) + ["httpx"]
    for extra in ("serve", "sdk", "tui"):
        pending.extend(project["optional-dependencies"][extra])
    selected = {}
    while pending:
        req = Requirement(pending.pop())
        if req.marker and not req.marker.evaluate({"extra": ""}):
            continue
        dist = importlib.metadata.distribution(req.name)
        if req.specifier and not req.specifier.contains(dist.version):
            raise ValueError(f"Installed {req.name} {dist.version} does not satisfy {req.specifier}")
        name = dist.metadata["Name"]
        if name not in selected:
            selected[name] = dist
            pending.extend(dist.requires or [])
    return selected


def prepare_python(source: Path, destination: Path):
    """Copy an explicit standalone distribution, then the validated dependency closure."""
    if (source / "pyvenv.cfg").exists():
        raise ValueError("Expected a standalone Python distribution, not a virtual environment")
    probe = run(source / "bin/python3", "-I", "-c",
                "import sys,platform,json;print(json.dumps([list(sys.version_info[:2]),platform.machine()]))",
                capture_output=True, text=True, errors="replace")
    version, arch = json.loads(probe.stdout)
    if version != list(sys.version_info[:2]) or arch != platform.machine():
        raise ValueError("Standalone Python must match the build environment's Python ABI and architecture")
    selected = dependency_set()
    shutil.copytree(source, destination, symlinks=True,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store"))
    site = destination / f"lib/python{version[0]}.{version[1]}/site-packages"
    shutil.rmtree(site, ignore_errors=True)
    site.mkdir(parents=True)
    for name, dist in sorted(selected.items()):
        if not dist.files:
            raise ValueError(f"Missing file manifest: {name}")
        for entry in dist.files:
            relative = Path(str(entry))
            # Console scripts point into the development venv; the app uses modules directly.
            if relative.is_absolute() or ".." in relative.parts or relative.suffix == ".pyc":
                continue
            if relative.suffix == ".pth":
                raise ValueError(f"Review site initialization before bundling: {name}/{relative}")
            original = Path(dist.locate_file(entry))
            if not original.is_file():
                raise ValueError(f"Missing dependency file: {name}/{relative}")
            target = site / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(original, target)
    for link in destination.rglob("*"):
        if link.is_symlink() and not link.resolve().is_relative_to(destination.resolve()):
            raise ValueError(f"External runtime symlink: {link}")
    # Do not ship development-machine paths embedded in pip launcher scripts.
    for entry in (destination / "bin").iterdir():
        if entry.name not in {"python", "python3", f"python{version[0]}.{version[1]}"}:
            if entry.is_dir():
                shutil.rmtree(entry)
            else:
                entry.unlink()
    # Runtime installers may rewrite the library's ID to their installation prefix.
    # Restore its relocatable ID on our copy before signing the application.
    for library in (destination / "lib").glob("libpython*.dylib"):
        if not library.is_symlink():
            run("install_name_tool", "-id", f"@rpath/{library.name}", library)
    return {name: dist.version for name, dist in sorted(selected.items())}


MACH_MAGICS = {bytes.fromhex(h) for h in ("feedface", "feedfacf", "cefaedfe", "cffaedfe", "cafebabe", "bebafeca", "cafebabf")}


def native_files(app: Path):
    for path in sorted(app.rglob("*")):
        if path.is_file() and not path.is_symlink():
            with path.open("rb") as stream:
                if stream.read(4) in MACH_MAGICS:
                    yield path


def audit_native(app: Path):
    """Reject dependencies on a developer's Homebrew/framework installation."""
    for path in native_files(app):
        output = run("otool", "-L", path, capture_output=True, text=True, errors="replace").stdout
        for line in output.splitlines()[1:]:
            dependency = line.strip().split(" (", 1)[0]
            if dependency.startswith("/") and not dependency.startswith(("/usr/lib/", "/System/Library/")):
                raise ValueError(f"Nonportable library in {path.name}: {dependency}")


def sign(app: Path, identity: str):
    if not identity.startswith("Developer ID Application:"):
        raise ValueError("Distribution requires a Developer ID Application identity")
    audit_native(app)
    for path in native_files(app):
        run("codesign", "--force", "--options", "runtime", "--timestamp", "--sign", identity, path,
            stdout=subprocess.DEVNULL)
    run("codesign", "--force", "--options", "runtime", "--timestamp", "--sign", identity, app)
    run("codesign", "--verify", "--deep", "--strict", app)


def source_provenance(root=ROOT):
    """Capture source identity at build time; later signing keeps this record intact."""
    def git(*args):
        return run("git", "-C", root, *args, capture_output=True, text=True, errors="replace").stdout.strip()
    return {"commit": git("rev-parse", "HEAD"),
            "dirty": bool(git("status", "--porcelain", "--untracked-files=all"))}


def manifest(app: Path, dependencies, source):
    files = {}
    for path in sorted(app.rglob("*")):
        if path.is_file() and not path.is_symlink():
            files[str(path.relative_to(app))] = hashlib.sha256(path.read_bytes()).hexdigest()
    return {"version": APP_VERSION, "bundle_id": BUNDLE_ID, "architecture": platform.machine(),
            "dependencies": dependencies, "source": source, "files": files}


def notarize(path: Path, profile: str, result_file: Path):
    result = run("xcrun", "notarytool", "submit", path, "--keychain-profile", profile,
                 "--wait", "--output-format", "json", capture_output=True, text=True, errors="replace")
    result_file.write_text(result.stdout, encoding="utf-8")
    if json.loads(result.stdout).get("status") != "Accepted":
        raise RuntimeError(f"Notarization not accepted; see {result_file}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=("build", "sign", "dmg", "notarize", "notarize-dmg"))
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--standalone-python", type=Path)
    parser.add_argument("--identity")
    parser.add_argument("--keychain-profile")
    args = parser.parse_args()
    directory = args.directory.resolve()
    directory.mkdir(parents=True, exist_ok=True)
    app = directory / "羽案.app"
    dmg = directory / f"Yuan-{APP_VERSION}-macOS-{platform.machine()}.dmg"
    if args.stage == "build":
        if app.exists():
            parser.error("Output already exists; use a new build directory to preserve rollback")
        if not args.standalone_python:
            parser.error("build requires --standalone-python")
        source = source_provenance()
        with tempfile.TemporaryDirectory(prefix="yuan-runtime-", dir=directory) as temp:
            runtime = Path(temp) / "python"
            deps = prepare_python(args.standalone_python.resolve(), runtime)
            run(sys.executable, HERE / "build.py", "--output", app,
                "--bundled-python", runtime, "--bundle-id", BUNDLE_ID)
        audit_native(app)
        (directory / "build-manifest.json").write_text(json.dumps(manifest(app, deps, source), indent=2), encoding="utf-8")
    elif args.stage == "sign":
        if not args.identity:
            parser.error("sign requires --identity")
        sign(app, args.identity)
    elif args.stage == "dmg":
        if not args.identity or not args.identity.startswith("Developer ID Application:"):
            parser.error("dmg requires a Developer ID Application --identity")
        if dmg.exists():
            parser.error("DMG already exists; keep the previous artifact")
        run("codesign", "--verify", "--deep", "--strict", app)
        run("xcrun", "stapler", "validate", app)
        with tempfile.TemporaryDirectory(prefix="yuan-dmg-", dir=directory) as temp:
            staging = Path(temp)
            run("ditto", app, staging / app.name)
            (staging / "Applications").symlink_to("/Applications")
            run("hdiutil", "create", "-volname", f"羽案 {APP_VERSION}", "-srcfolder", staging,
                "-format", "UDZO", "-ov", dmg)
        run("codesign", "--force", "--timestamp", "--sign", args.identity, dmg)
    elif args.stage == "notarize":
        if not args.keychain_profile:
            parser.error("notarize requires --keychain-profile")
        # Staple the app before making the DMG, so copying the app retains its ticket.
        if dmg.exists():
            parser.error("Notarize the app before creating its final DMG; choose a fresh build directory")
        archive = directory / "notarization.zip"
        run("ditto", "-c", "-k", "--keepParent", app, archive)
        notarize(archive, args.keychain_profile, directory / "notarization-app.json")
        run("xcrun", "stapler", "staple", app)
        run("xcrun", "stapler", "validate", app)
        run("spctl", "--assess", "--type", "execute", "--verbose", app)
    else:
        if not args.keychain_profile:
            parser.error("notarize-dmg requires --keychain-profile")
        notarize(dmg, args.keychain_profile, directory / "notarization-dmg.json")
        run("xcrun", "stapler", "staple", dmg)
        run("xcrun", "stapler", "validate", dmg)
        run("spctl", "--assess", "--type", "open", "--context", "context:primary-signature", dmg)
        checksum = hashlib.sha256(dmg.read_bytes()).hexdigest()
        (directory / "SHA256SUMS.txt").write_text(f"{checksum}  {dmg.name}\n", encoding="utf-8")
    print(dmg if args.stage in {"dmg", "notarize-dmg"} else app)


if __name__ == "__main__":
    main()
