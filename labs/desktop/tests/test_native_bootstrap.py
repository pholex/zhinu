"""Load the complete production import graph and UI in WKWebView with fake keys."""
import json
import os
from pathlib import Path
import plistlib
import selectors
import signal
import subprocess
import sys
import tempfile
import unittest
import uuid

ROOT = Path(__file__).resolve().parents[3]


@unittest.skipUnless(sys.platform == 'darwin', 'requires WKWebView')
class NativeBootstrapTests(unittest.TestCase):
    def test_real_page_connects_selects_and_remembers_model(self):
        with tempfile.TemporaryDirectory(prefix='desktop-bootstrap-') as folder:
            root = Path(folder)
            app = root / 'Bootstrap.app'
            binary = app / 'Contents/MacOS/Bootstrap'
            binary.parent.mkdir(parents=True)
            (app / 'Contents/Info.plist').write_bytes(plistlib.dumps({
                'CFBundleIdentifier': 'test.xiaoyu.bootstrap.' + uuid.uuid4().hex,
                'CFBundleExecutable': 'Bootstrap', 'CFBundlePackageType': 'APPL',
                'NSAppTransportSecurity': {'NSAllowsLocalNetworking': True, 'NSAllowsArbitraryLoads': True},
            }))
            compiled = subprocess.run(['xcrun', 'swiftc', str(ROOT / 'labs/desktop/tests/fixtures/bootstrap/Bootstrap.swift'),
                '-o', str(binary), '-module-cache-path', '/private/tmp/xiaoyu-swift-cache'],
                capture_output=True, text=True, errors='replace', timeout=60)
            self.assertEqual(compiled.returncode, 0, compiled.stderr)
            env = {k: os.environ[k] for k in ('PATH', 'TMPDIR') if k in os.environ}
            env.update(HOME=str(root), XDG_CONFIG_HOME=str(root / 'config'), PYTHONPATH=str(ROOT),
                       PYTHONNOUSERSITE='1', XIAOYU_SUGGESTIONS='0', XIAOYU_PROVIDERS='deepseek,qwen',
                       XIAOYU_MODEL='deepseek-chat', DEEPSEEK_API_KEY='fake-bootstrap-deepseek',
                       QWEN_API_KEY='fake-bootstrap-qwen')
            for feature in ('MCP', 'PLUGINS', 'HOOKS', 'AGENTS', 'EXPLORE', 'WEB_SEARCH',
                            'X_SEARCH', 'DEEP_RESEARCH', 'BROWSER', 'ASKPASS'):
                env[f'XIAOYU_ENABLE_{feature}'] = '0'
            env['XIAOYU_ENABLE_SKILLS'] = '1'
            skill = root / '.agents/skills/desktop-fixture/SKILL.md'
            skill.parent.mkdir(parents=True)
            skill.write_text('---\nname: desktop-fixture\ndescription: Offline desktop selection fixture\n---\nUse only in tests.\n', encoding='utf-8')
            for name, description in [('aws-auth', 'User authentication and identity'), ('aws-compute', 'Virtual machine workloads'), ('lark-approval', '查询和处理审批任务')]:
                sample = root / '.agents/skills' / name / 'SKILL.md'
                sample.parent.mkdir(parents=True)
                sample.write_text(f'---\nname: {name}\ndescription: {description}\n---\nOffline fixture only.\n', encoding='utf-8')
            workspace = root / 'Documents/羽案/Default'
            workspace.mkdir(parents=True)
            (workspace.parent / "Reference").mkdir()
            from labs.desktop.preferences import DEFAULTS
            import copy
            import hashlib
            legacy = copy.deepcopy(DEFAULTS)
            legacy['ui']['send_key'] = 'mod-enter'
            key = hashlib.sha256(os.fsencode(workspace.resolve())).hexdigest()[:24]
            prefs = root / 'Library/Application Support/Xiaoyu Desktop Lab/state' / key / 'preferences.json'
            prefs.parent.mkdir(parents=True)
            prefs.write_text(json.dumps({'revision': 1, 'values': legacy}), encoding='utf-8')
            with tempfile.TemporaryFile() as errors:
                backend = subprocess.Popen([sys.executable, '-P', '-u', '-m', 'labs.desktop.macos.backend',
                    '--workspace', str(workspace)], cwd=ROOT, env=env,
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errors)
                try:
                    with selectors.DefaultSelector() as selector:
                        selector.register(backend.stdout, selectors.EVENT_READ)
                        self.assertTrue(selector.select(20), 'backend readiness')
                    info = json.loads(backend.stdout.readline())
                    native_env = {k: env[k] for k in ('PATH', 'TMPDIR', 'HOME') if k in env}
                    native = subprocess.Popen([str(binary)], env=native_env, start_new_session=True,
                        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                    try:
                        try:
                            output, err = native.communicate(json.dumps({'origin': info['url'].split('/workbench/')[0],
                                'token': info['token'], 'output': str(root / 'bootstrap.png')}).encode(), timeout=60)
                        except subprocess.TimeoutExpired as exc:
                            self.fail((exc.stdout or b'').decode(errors='replace') + (exc.stderr or b'').decode(errors='replace'))
                        self.assertEqual(native.returncode, 0, (output+err).decode(errors='replace'))
                        self.assertIn(b'BOOTSTRAP_PASS', output)
                        self.assertTrue((root / 'bootstrap.png').is_file())
                        if os.environ.get('DESKTOP_TEST_CAPTURE'):
                            capture = Path(os.environ['DESKTOP_TEST_CAPTURE'])
                            capture.write_bytes((root / 'bootstrap.png').read_bytes())
                            for mode in ('chat', 'process', 'weave', 'contest', 'fleet', 'split', 'project', 'plugins', 'skills-expanded', 'skills-switches', 'skills-search', 'plugin-discovery', 'connector-discovery', 'amap-setup', 'supabase-setup', 'vercel-setup', 'github-setup', 'plugin-connectors', 'mcp-http', 'mcp-stdio', 'mcp-narrow', 'archive-page', 'archive-wide', 'archive-narrow', 'archive-confirm', 'more-menu', 'archive-empty'):
                                capture.with_stem(capture.stem + '-' + mode).write_bytes(
                                    (root / f'bootstrap-{mode}.png').read_bytes())
                    finally:
                        if native.poll() is None:
                            os.killpg(native.pid, signal.SIGKILL)
                            native.wait(timeout=5)
                finally:
                    backend.stdin.close()
                    try:
                        backend.wait(timeout=12)
                    except subprocess.TimeoutExpired:
                        backend.kill(); backend.wait(timeout=5)
                    backend.stdout.close()
