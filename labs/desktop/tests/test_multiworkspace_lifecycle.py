"""Run real AppKit controllers against credential-free local HTTP fixtures."""
from __future__ import annotations

import json
import os
from pathlib import Path
import plistlib
import shutil
import signal
import subprocess
import sys
import tempfile
import unittest
import uuid

LAB = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).with_name("fixtures") / "multiworkspace"


@unittest.skipUnless(sys.platform == "darwin", "requires native macOS windows")
class MultiworkspaceLifecycleTests(unittest.TestCase):
    def test_native_workspace_and_view_lifecycle(self):
        if shutil.which("xcrun") is None:
            self.skipTest("requires the Swift compiler")
        with tempfile.TemporaryDirectory(prefix="xiaoyu-workspace-lifecycle-") as temporary:
            root = Path(temporary)
            app = root / "WorkspaceLifecycle.app"
            resources = app / "Contents/Resources"
            binary = app / "Contents/MacOS/WorkspaceLifecycle"
            binary.parent.mkdir(parents=True)
            resources.mkdir(parents=True)
            domain = "test.xiaoyu.multiworkspace." + uuid.uuid4().hex
            (app / "Contents/Info.plist").write_bytes(plistlib.dumps({
                "CFBundleIdentifier": domain,
                "CFBundleExecutable": binary.name,
                "CFBundleName": "WorkspaceLifecycle",
                "CFBundlePackageType": "APPL",
                "NSHighResolutionCapable": True,
                "NSAppTransportSecurity": {
                    "NSAllowsLocalNetworking": True,
                    "NSAllowsArbitraryLoads": True,
                },
            }))
            (resources / "runtime.json").write_text(
                json.dumps({"python": sys.executable}), encoding="utf-8"
            )
            (resources / "model-providers.json").write_text(json.dumps([
                {"id": f"fixture-{i}", "label": f"Fixture {i}", "keyEnvs": [f"DESKTOP_FIXTURE_KEY_{i}"],
                 "models": [f"fixture-model-{i}"]} for i in range(8)
            ]), encoding="utf-8")
            module = resources / "runtime/labs/desktop/macos"
            module.mkdir(parents=True)
            for directory in (module, module.parent, module.parent.parent):
                (directory / "__init__.py").touch()
            shutil.copyfile(FIXTURES / "backend.py", module / "backend.py")
            source = root / "App.swift"
            production = (LAB / "macos/App.swift").read_text(encoding="utf-8", errors="replace")
            self.assertEqual(production.count("@main\n"), 1)
            source.write_text(production.split("@main\n")[0], encoding="utf-8")
            compiled = subprocess.run([
                "xcrun", "swiftc", "-swift-version", "5", "-parse-as-library",
                str(source), str(LAB / "macos/RuntimeEnvironment.swift"), str(LAB / "macos/ModelSetup.swift"),
                str(LAB / "macos/DesktopCoordinator.swift"),
                str(FIXTURES / "Lifecycle.swift"), "-o", str(binary),
                "-module-cache-path", str(root / "swift-cache"),
            ], capture_output=True, text=True, errors="replace", timeout=90)
            self.assertEqual(compiled.returncode, 0, compiled.stderr)
            signed = subprocess.run(
                ["/usr/bin/codesign", "--force", "--sign", "-", str(app)],
                capture_output=True, text=True, errors="replace", timeout=15,
            )
            self.assertEqual(signed.returncode, 0, signed.stderr)
            data = root / "data"
            home = root / "home"
            data.mkdir()
            home.mkdir()
            # Do not inherit API keys, provider configuration, or real workspace paths.
            environment = {
                "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
                "HOME": str(home),
                "TMPDIR": os.environ.get("TMPDIR", "/tmp"),
                "WORKSPACE_TEST_ROOT": str(data),
            }
            environment.update({f"DESKTOP_FIXTURE_KEY_{i}": "fake-fixture-value" for i in range(8)})
            process = subprocess.Popen(
                [str(binary)], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, errors="replace", env=environment, start_new_session=True,
            )
            try:
                try:
                    stdout, stderr = process.communicate(timeout=55)
                except subprocess.TimeoutExpired:
                    self.fail("native lifecycle timed out; the owned process group is reclaimed")
                self.assertEqual(process.returncode, 0, stdout + "\n" + stderr)
                self.assertIn("MULTIWORKSPACE_NATIVE_PASS 6", stdout)
                events = [json.loads(line) for line in (data / "backend-events.jsonl").read_text(
                    encoding="utf-8", errors="replace"
                ).splitlines()]
                starts = [event for event in events if event["event"] == "start"]
                stops = [event for event in events if event["event"] == "stop"]
                self.assertEqual(len(starts), 6, "A, B, automatically restored A, reopened A, setup C, recovered setup D")
                self.assertEqual({event["pid"] for event in starts}, {event["pid"] for event in stops})
                self.assertEqual(len(stops), 6, "every owned backend exits normally")
            finally:
                # The new session contains only this app and the children it created.
                # Reclaim it even if Swift assertions or a modal window interrupt exit.
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    process.communicate(timeout=2)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    process.communicate(timeout=5)
                # The UUID domain belongs solely to this test, including on failure.
                subprocess.run(
                    ["/usr/bin/defaults", "delete", domain], env=environment,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5,
                )
