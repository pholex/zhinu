"""Build both distributions from one checkout and check their boundaries."""
from __future__ import annotations

import argparse
import email
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path, PurePosixPath


RUNTIME_ASSETS = {
    "kernel": {"xiaoyu/py.typed", "xiaoyu/docs/extending.md", "xiaoyu/evals/models.example.json"},
    "sdk": {"xiaoyu_agent_sdk/py.typed"},
}


def verify_wheel(wheel: Path, *, package: str, version: str) -> None:
    """Reject development files while retaining executable modules and assets."""
    namespace, distribution = {
        "kernel": ("xiaoyu", "xiaoyu_agent"),
        "sdk": ("xiaoyu_agent_sdk", "xiaoyu_agent_sdk"),
    }[package]
    info = f"{distribution}-{version}.dist-info/"
    metadata_files = {info + name for name in (
        "METADATA", "WHEEL", "RECORD", "entry_points.txt", "top_level.txt", "licenses/LICENSE",
    )}
    module_packages = {(namespace,)}
    if package == "kernel":
        module_packages.add((namespace, "evals"))

    def allowed(name: str) -> bool:
        path = PurePosixPath(name)
        if name != path.as_posix():
            return False
        module = (path.suffix == ".py" and path.parts[:-1] in module_packages
                  and path.name not in {"testing.py", "tests.py", "conftest.py"}
                  and not path.name.startswith("test_") and not path.name.endswith("_test.py"))
        return module or name in RUNTIME_ASSETS[package] or name in metadata_files

    with zipfile.ZipFile(wheel) as archive:
        paths = archive.namelist()
        if len(paths) != len(set(paths)):
            raise ValueError("Wheel contains duplicate members")
        unexpected = [name for name in paths if not allowed(name)]
        if unexpected:
            raise ValueError("Wheel contains non-runtime files: " + ", ".join(unexpected))
        required = RUNTIME_ASSETS[package] | {
            f"{namespace}/__init__.py", info + "METADATA", info + "WHEEL", info + "RECORD",
            info + "licenses/LICENSE",
        }
        if package == "kernel":
            required |= {"xiaoyu/cli.py", "xiaoyu/evals/runner.py", info + "entry_points.txt"}
        missing = required - set(paths)
        if missing:
            raise ValueError("Wheel lacks required runtime files: " + ", ".join(sorted(missing)))
        metadata = email.message_from_bytes(archive.read(info + "METADATA"))
        if metadata["Version"] != version:
            raise ValueError("Wheel version differs from release version")
        if re.sub(r"[-_.]+", "-", metadata.get("Name", "")).lower() != distribution.replace("_", "-"):
            raise ValueError("Wheel project differs from release package")
        if package == "sdk" and not any(
            re.fullmatch(r"xiaoyu-agent\[sdk\]\s*==\s*" + re.escape(version), dependency)
            for dependency in metadata.get_all("Requires-Dist", [])
        ):
            raise ValueError("SDK must depend on the paired kernel version")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--outdir", type=Path, default=Path("dist/sdk-release"))
    parser.add_argument("--no-isolation", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    out = args.outdir.resolve()
    if any(out.glob("*/*")):
        parser.error("Output directory must be empty; preserve previous release artifacts for retries")
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
    dirty = bool(subprocess.check_output(["git", "status", "--porcelain"], cwd=root, text=True).strip())
    version = re.search(r'^__version__ = "([^"]+)"', (root / "xiaoyu/__init__.py").read_text(encoding="utf-8"), re.M).group(1)
    for name, source in (
        ("kernel", root),
        ("sdk", root / "packages/xiaoyu-agent-sdk"),
    ):
        destination = out / name
        distribution = "xiaoyu_agent" if name == "kernel" else "xiaoyu_agent_sdk"
        wheel = destination / f"{distribution}-{version}-py3-none-any.whl"
        # Validate rebuilding from sdist, but retain only wheels for publication.
        with tempfile.TemporaryDirectory(prefix="xiaoyu-build-") as temporary:
            command = [sys.executable, "-m", "build", "--outdir", temporary]
            if args.no_isolation:
                command.append("--no-isolation")
            subprocess.run([*command, str(source)], check=True)
            destination.mkdir(parents=True, exist_ok=True)
            shutil.copy2(Path(temporary) / wheel.name, wheel)
        verify_wheel(wheel, package=name, version=version)
        print(f"Verified {wheel}")
    manifest = {
        "version": version, "commit": commit, "dirty": dirty,
        "files": {
            path.relative_to(out).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for folder in ("kernel", "sdk") for path in sorted((out / folder).iterdir())
            if path.suffix == ".whl"
        },
    }
    (out / "release-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
