"""Source provenance and native child executable discovery without external services."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from labs.desktop.macos.release import manifest, source_provenance

ROOT = Path(__file__).resolve().parents[3]


class SourceProvenanceTests(unittest.TestCase):
    def test_commit_dirty_and_ignored_build_outputs(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            def git(*args):
                return subprocess.run(['git', '-C', str(root), *args], check=True,
                    capture_output=True, text=True, errors='replace').stdout.strip()
            git('init', '-q')
            (root/'.gitignore').write_text('output/\n', encoding='utf-8')
            (root/'source.txt').write_text('original\n', encoding='utf-8')
            git('add', '.')
            git('-c','user.name=Fixture','-c','user.email=fixture@example.test',
                '-c','core.hooksPath=/dev/null','-c','commit.gpgsign=false','commit','-qm','fixture')
            source = source_provenance(root)
            self.assertEqual(source, {'commit':git('rev-parse','HEAD'), 'dirty':False})
            app = root/'output/app'; app.mkdir(parents=True)
            (app/'file').write_text('artifact', encoding='utf-8')
            self.assertEqual(source_provenance(root), source)
            result = manifest(app, {'dependency':'1'}, source)
            self.assertEqual(result['source'], source)
            self.assertIn('file', result['files'])
            (root/'source.txt').write_text('changed\n', encoding='utf-8')
            self.assertTrue(source_provenance(root)['dirty'])
            git('checkout', '--', 'source.txt')
            (root/'new.txt').write_text('new\n', encoding='utf-8')
            self.assertTrue(source_provenance(root)['dirty'])


@unittest.skipUnless(sys.platform == 'darwin', 'native runtime discovery')
class ExecutablePathTests(unittest.TestCase):
    def test_finder_and_inherited_paths_launch_cli_with_node_shebang(self):
        from xiaoyu.mcp import find_command, _safe_env
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            main = root/'Probe.swift'
            main.write_text('''import Foundation
@main struct Probe {
    static func main() {
        print(desktopExecutablePath(python: CommandLine.arguments[1],
              inherited: CommandLine.arguments[2], home: CommandLine.arguments[3]))
    }
}
''', encoding='utf-8')
            binary = root/'probe'
            subprocess.run(['xcrun','swiftc',str(ROOT/'labs/desktop/macos/RuntimeEnvironment.swift'),
                str(main),'-o',str(binary),'-module-cache-path','/private/tmp/xiaoyu-swift-cache'],
                check=True, capture_output=True, text=True, errors='replace', timeout=60)
            # The fake npx has the same env-based node lookup as a real installation.
            # An absolute command alone would still fail without the child PATH fix.
            for inherited in ('/usr/bin:/bin', str(root/'custom tools')+':relative::/usr/bin'):
                tools = root/'custom tools' if 'custom tools' in inherited else root/'.local/bin'
                tools.mkdir(parents=True, exist_ok=True)
                (tools/'fixture-npx').write_text('#!/usr/bin/env fixture-node\n', encoding='utf-8')
                (tools/'fixture-node').write_text('#!/bin/sh\nprintf cli-ok\n', encoding='utf-8')
                for path in tools.iterdir():
                    path.chmod(0o700)
                resolved = subprocess.run([str(binary),str(root/'python/bin/python3'),inherited,str(root)],
                    check=True, capture_output=True, text=True, errors='replace').stdout.strip()
                paths = resolved.split(':')
                self.assertEqual(paths[0], str(root/'python/bin'))
                self.assertEqual(len(paths), len(set(paths)))
                self.assertNotIn('', paths); self.assertNotIn('relative', paths)
                self.assertIn('/opt/homebrew/bin', paths); self.assertIn('/usr/local/bin', paths)
                with patch.dict(os.environ, {'PATH':resolved}, clear=True):
                    command = find_command('fixture-npx')
                    self.assertEqual(command, str(tools/'fixture-npx'))
                    result = subprocess.run([command], env=_safe_env(), check=True,
                        capture_output=True, text=True, errors='replace')
                    self.assertEqual(result.stdout, 'cli-ok')
