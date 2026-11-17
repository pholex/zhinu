"""Move a distribution app and verify its backend without development environment paths."""
from __future__ import annotations

import argparse
from contextlib import closing
import http.client
import json
from pathlib import Path
import selectors
import subprocess
import tempfile
from urllib.parse import urlsplit


def verify(app: Path):
    with tempfile.TemporaryDirectory(prefix="yuan-relocation-") as folder:
        root = Path(folder)
        relocated = root / "移动后的羽案.app"
        subprocess.run(["ditto", str(app), str(relocated)], check=True)
        resources = relocated / "Contents/Resources"
        config = json.loads((resources / "runtime.json").read_text(errors="replace"))
        python = resources / config["bundledPython"]
        env = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "HOME": str(root),
               "TMPDIR": str(root), "XDG_CONFIG_HOME": str(root / "config"),
               "PYTHONPATH": str(resources / "runtime"), "PYTHONNOUSERSITE": "1"}
        probe = subprocess.run([str(python), "-B", "-P", "-c",
            "import sys,ssl,sqlite3,ctypes,fastapi,uvicorn,httpx,openai,anthropic,jsonschema,"
            "tree_sitter,tree_sitter_bash,websockets,xiaoyu,xiaoyu_agent_sdk;"
            "from xiaoyu import providers; from labs.desktop.macos import backend;"
            "assert all(not p or p.startswith(sys.prefix) or p.startswith(sys.argv[1]) for p in sys.path),sys.path;"
            "print('isolated imports passed')", str(resources)], env=env, cwd=root,
            capture_output=True, text=True, errors="replace", timeout=30)
        if probe.returncode:
            raise RuntimeError(probe.stderr)
        for feature in ("MCP", "PLUGINS", "SKILLS", "HOOKS", "AGENTS", "EXPLORE", "WEB_SEARCH", "BROWSER"):
            env[f"XIAOYU_ENABLE_{feature}"] = "0"
        with tempfile.TemporaryFile() as errors:
            child = subprocess.Popen([str(python), "-B", "-P", "-u", "-m", "labs.desktop.macos.backend", "--fixture"],
                cwd=root, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errors)
            try:
                with selectors.DefaultSelector() as selector:
                    selector.register(child.stdout, selectors.EVENT_READ)
                    if not selector.select(30):
                        raise RuntimeError("Bundled backend readiness timeout")
                line = child.stdout.readline()
                if not line:
                    errors.seek(0)
                    raise RuntimeError(errors.read().decode(errors="replace"))
                ready = json.loads(line)
                url = urlsplit(ready["url"])
                with closing(http.client.HTTPConnection(url.hostname, url.port, timeout=10)) as conn:
                    conn.request("GET", "/session", headers={"Authorization": "Bearer " + ready["token"]})
                    response = conn.getresponse()
                    if response.status != 200:
                        raise RuntimeError(f"Session API: {response.status}")
                    response.read()
                    conn.request("GET", url.path)
                    response = conn.getresponse()
                    if response.status != 200 or b"<html" not in response.read():
                        raise RuntimeError("Bundled workbench page failed")
                child.stdin.close()
                if child.wait(timeout=15) != 0:
                    raise RuntimeError("Bundled backend shutdown failed")
            finally:
                if child.poll() is None:
                    child.kill(); child.wait(timeout=5)
                child.stdout.close()
                if not child.stdin.closed:
                    child.stdin.close()
        subprocess.run(["codesign", "--verify", "--deep", "--strict", str(relocated)], check=True)
        print("PASS: relocated app, isolated dependencies, authenticated API, page, EOF shutdown, signature")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("app", type=Path)
    verify(parser.parse_args().app.resolve())
