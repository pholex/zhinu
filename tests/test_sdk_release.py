"""Artifact corruption and partial publication must fail or resume explicitly."""
import hashlib
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

from scripts.sdk_release import index_files, missing_files, verify
from scripts.build_sdk import verify_wheel


def wheel_files(group):
    namespace = "xiaoyu" if group == "kernel" else "xiaoyu_agent_sdk"
    info = ("xiaoyu_agent" if group == "kernel" else namespace) + "-0.58.0.dist-info/"
    metadata = f"Name: {namespace.replace('_', '-') if group == 'sdk' else 'xiaoyu-agent'}\nVersion: 0.58.0\n"
    if group == "sdk":
        metadata += "Requires-Dist: xiaoyu-agent[sdk]==0.58.0\n"
    files = {namespace + "/__init__.py": "", namespace + "/py.typed": "",
             info + "METADATA": metadata, info + "WHEEL": "Wheel-Version: 1.0\n",
             info + "RECORD": "", info + "licenses/LICENSE": "MIT"}
    if group == "kernel":
        files.update({"xiaoyu/docs/extending.md": "runtime template",
                      "xiaoyu/evals/models.example.json": "{}", "xiaoyu/cli.py": "",
                      "xiaoyu/evals/runner.py": "", info + "entry_points.txt": ""})
    return files


def write_wheel(path, files):
    with zipfile.ZipFile(path, "w") as archive:
        for name, content in files.items():
            archive.writestr(name, content)


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.manifest = {"version": "0.58.0", "commit": "abc", "dirty": False, "files": {}}
        for group, name in (("kernel", "xiaoyu_agent"), ("sdk", "xiaoyu_agent_sdk")):
            (self.root / group).mkdir()
            for suffix in ("-py3-none-any.whl",):
                path = self.root / group / (name + "-0.58.0" + suffix)
                write_wheel(path, wheel_files(group))
                self.manifest["files"][path.relative_to(self.root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
        (self.root / "release-manifest.json").write_text(json.dumps(self.manifest))

    def test_integrity_and_provenance(self):
        self.assertEqual(verify(self.root, commit="abc", version="0.58.0"), self.manifest)
        with self.assertRaises(ValueError):
            verify(self.root, commit="other")
        with self.assertRaises(ValueError):
            verify(self.root, version="0.59.0")
        (self.root / next(iter(self.manifest["files"]))).write_bytes(b"corrupted")
        with self.assertRaisesRegex(ValueError, "digest mismatch"):
            verify(self.root)

    def test_partial_publication_only_retries_missing_files(self):
        names = missing_files(self.manifest, "kernel", {})
        self.assertEqual(len(names), 1)
        remote = {names[0]: self.manifest["files"]["kernel/" + names[0]]}
        self.assertEqual(missing_files(self.manifest, "kernel", remote), [])
        self.assertEqual(len(missing_files(self.manifest, "sdk", {})), 1)
        remote[names[0]] = "different"
        with self.assertRaisesRegex(ValueError, "do not overwrite"):
            missing_files(self.manifest, "kernel", remote)

    def test_source_archives_cannot_enter_release(self):
        path = self.root / "sdk/xiaoyu_agent_sdk-0.58.0.tar.gz"
        path.write_bytes(b"source archive")
        with self.assertRaisesRegex(ValueError, "only the two wheels"):
            verify(self.root)
        self.manifest["files"][path.relative_to(self.root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
        (self.root / "release-manifest.json").write_text(json.dumps(self.manifest))
        with self.assertRaisesRegex(ValueError, "exactly both wheels"):
            verify(self.root)

    def test_release_rejects_development_files_even_with_matching_hashes(self):
        name = "sdk/xiaoyu_agent_sdk-0.58.0-py3-none-any.whl"
        files = wheel_files("sdk")
        files["xiaoyu_agent_sdk/testing.py"] = "development helper"
        write_wheel(self.root / name, files)
        self.manifest["files"][name] = hashlib.sha256((self.root / name).read_bytes()).hexdigest()
        (self.root / "release-manifest.json").write_text(json.dumps(self.manifest))
        with self.assertRaisesRegex(ValueError, "non-runtime files"):
            verify(self.root)

    def test_only_404_means_not_published(self):
        with patch("urllib.request.urlopen", side_effect=HTTPError("url", 404, "missing", {}, None)):
            self.assertEqual(index_files("xiaoyu-agent", "0.58.0"), {})
        with patch("urllib.request.urlopen", side_effect=HTTPError("url", 403, "forbidden", {}, None)):
            with self.assertRaises(HTTPError):
                index_files("xiaoyu-agent", "0.58.0")


class WheelContentsTests(unittest.TestCase):
    def test_development_files_and_unreviewed_assets_are_rejected(self):
        for group in ("kernel", "sdk"):
            namespace = "xiaoyu" if group == "kernel" else "xiaoyu_agent_sdk"
            for name in ("tests/test_sdk.py", "examples/demo.py", "scripts/build_sdk.py",
                         namespace + "/testing.py", namespace + "/test_fixture.py",
                         namespace + "/tests/helper.py", namespace + "/conftest.py",
                         namespace + "/docs/extra.md", namespace + "/models.local.json",
                         namespace + "/__pycache__/module.pyc", namespace + "//fixture.py"):
                with self.subTest(group=group, member=name), tempfile.TemporaryDirectory() as tmp:
                    path = Path(tmp) / "fixture.whl"
                    files = wheel_files(group)
                    files[name] = "development file"
                    write_wheel(path, files)
                    with self.assertRaisesRegex(ValueError, "non-runtime files"):
                        verify_wheel(path, package=group, version="0.58.0")

    def test_runtime_assets_cannot_be_removed(self):
        for group, member in (("sdk", "xiaoyu_agent_sdk/py.typed"),
                              ("kernel", "xiaoyu/docs/extending.md"),
                              ("kernel", "xiaoyu/evals/models.example.json")):
            with self.subTest(group=group, member=member), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "fixture.whl"
                files = wheel_files(group)
                del files[member]
                write_wheel(path, files)
                with self.assertRaisesRegex(ValueError, "required runtime files"):
                    verify_wheel(path, package=group, version="0.58.0")
