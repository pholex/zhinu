"""Verify saved artifacts and reconcile PyPI before an ordered, retryable upload.

This script never uploads. Publication remains in the OIDC workflow.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import time
import urllib.error
import urllib.request
from pathlib import Path

PROJECTS = {"kernel": "xiaoyu-agent", "sdk": "xiaoyu-agent-sdk"}


def verify(root: Path, *, commit: str | None = None, version: str | None = None) -> dict:
    manifest = json.loads((root / "release-manifest.json").read_text(encoding="utf-8"))
    if commit and (manifest["commit"] != commit or manifest["dirty"]):
        raise ValueError("Release must come from the exact clean tested commit")
    if version and manifest["version"] != version:
        raise ValueError("Release version differs from the tag")
    expected = {
        f"{group}/{project.replace('-', '_')}-{manifest['version']}{suffix}"
        for group, project in PROJECTS.items() for suffix in ("-py3-none-any.whl", ".tar.gz")
    }
    if set(manifest["files"]) != expected:
        raise ValueError("Manifest must describe exactly both wheels and sdists")
    for name, digest in manifest["files"].items():
        if hashlib.sha256((root / name).read_bytes()).hexdigest() != digest:
            raise ValueError(f"Artifact digest mismatch: {name}")
    return manifest


def index_files(project: str, version: str) -> dict[str, str]:
    request = urllib.request.Request(
        f"https://pypi.org/pypi/{project}/{version}/json",
        headers={"User-Agent": "xiaoyu-sdk-release", "Cache-Control": "no-cache"},
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            data = json.load(response)
    except urllib.error.HTTPError as exc:
        exc.close()
        if exc.code == 404:
            return {}
        raise
    return {item["filename"]: item["digests"]["sha256"] for item in data["urls"]}


def missing_files(manifest: dict, group: str, remote: dict[str, str]) -> list[str]:
    expected = {Path(name).name: digest for name, digest in manifest["files"].items()
                if name.startswith(group + "/")}
    for name, digest in remote.items():
        if expected.get(name) != digest:
            raise ValueError(f"Published artifact differs; do not overwrite or rebuild: {name}")
    return [name for name in expected if name not in remote]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("verify", "stage", "wait"))
    parser.add_argument("root", type=Path)
    parser.add_argument("--commit")
    parser.add_argument("--version")
    parser.add_argument("--package", choices=PROJECTS)
    parser.add_argument("--destination", type=Path)
    parser.add_argument("--timeout", type=float, default=180)
    args = parser.parse_args()
    manifest = verify(args.root, commit=args.commit, version=args.version)
    if args.action == "verify":
        print(f"Verified {manifest['version']} from {manifest['commit']}")
        return
    if not args.package:
        parser.error("--package is required for stage/wait")
    deadline = time.monotonic() + args.timeout
    while True:
        missing = missing_files(manifest, args.package, index_files(PROJECTS[args.package], manifest["version"]))
        if args.action != "wait" or not missing:
            break
        if time.monotonic() >= deadline:
            raise TimeoutError("Uploaded package is not yet available with the expected digests")
        time.sleep(min(5, max(0, deadline - time.monotonic())))
    if args.action == "stage":
        if not args.destination:
            parser.error("--destination is required for stage")
        args.destination.mkdir(parents=True, exist_ok=True)
        if any(args.destination.iterdir()):
            raise ValueError("Upload staging directory must be empty")
        for name in missing:
            shutil.copy2(args.root / args.package / name, args.destination / name)
        if output := os.environ.get("GITHUB_OUTPUT"):
            with open(output, "a") as stream:
                stream.write(f"pending={len(missing)}\n")
        print(f"{args.package}: {len(missing)} artifacts need upload; existing files match SHA-256")


if __name__ == "__main__":
    main()
