"""Build both distributions from one checkout and check their boundaries."""
from __future__ import annotations

import argparse
import email
import hashlib
import json
import re
import subprocess
import sys
import zipfile
from pathlib import Path


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
    for name, source, namespace in (
        ("kernel", root, "xiaoyu"),
        ("sdk", root / "packages/xiaoyu-agent-sdk", "xiaoyu_agent_sdk"),
    ):
        destination = out / name
        command = [sys.executable, "-m", "build", "--outdir", str(destination)]
        if args.no_isolation:
            command.append("--no-isolation")
        subprocess.run([*command, str(source)], check=True)
        distribution = "xiaoyu_agent" if name == "kernel" else "xiaoyu_agent_sdk"
        wheel = destination / f"{distribution}-{version}-py3-none-any.whl"
        with zipfile.ZipFile(wheel) as archive:
            paths = archive.namelist()
            metadata = email.message_from_bytes(archive.read(next(p for p in paths if p.endswith("/METADATA"))))
            assert metadata["Version"] == version
            assert all(p.startswith(namespace + "/") or ".dist-info/" in p for p in paths)
            assert namespace + "/py.typed" in paths
            if name == "sdk":
                assert f"xiaoyu-agent[sdk]=={version}" in metadata.get_all("Requires-Dist", [])
        print(f"Verified {wheel}")
    manifest = {
        "version": version, "commit": commit, "dirty": dirty,
        "files": {
            path.relative_to(out).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for folder in ("kernel", "sdk") for path in sorted((out / folder).iterdir())
            if path.suffix in (".whl", ".gz")
        },
    }
    (out / "release-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
