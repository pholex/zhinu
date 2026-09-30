"""Artifact corruption and partial publication must fail or resume explicitly."""
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

from scripts.sdk_release import index_files, missing_files, verify


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.manifest = {"version": "0.58.0", "commit": "abc", "dirty": False, "files": {}}
        for group, name in (("kernel", "xiaoyu_agent"), ("sdk", "xiaoyu_agent_sdk")):
            (self.root / group).mkdir()
            for suffix in ("-py3-none-any.whl", ".tar.gz"):
                path = self.root / group / (name + "-0.58.0" + suffix)
                path.write_bytes(path.name.encode())
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
        self.assertEqual(len(names), 2)
        remote = {names[0]: self.manifest["files"]["kernel/" + names[0]]}
        self.assertEqual(missing_files(self.manifest, "kernel", remote), [names[1]])
        remote[names[1]] = self.manifest["files"]["kernel/" + names[1]]
        self.assertEqual(missing_files(self.manifest, "kernel", remote), [])
        remote[names[0]] = "different"
        with self.assertRaisesRegex(ValueError, "do not overwrite"):
            missing_files(self.manifest, "kernel", remote)

    def test_only_404_means_not_published(self):
        with patch("urllib.request.urlopen", side_effect=HTTPError("url", 404, "missing", {}, None)):
            self.assertEqual(index_files("xiaoyu-agent", "0.58.0"), {})
        with patch("urllib.request.urlopen", side_effect=HTTPError("url", 403, "forbidden", {}, None)):
            with self.assertRaises(HTTPError):
                index_files("xiaoyu-agent", "0.58.0")
