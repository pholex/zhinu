"""推送钩子在 linked worktree 中也必须隔离测试子进程的 Git 环境。"""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


@unittest.skipUnless(os.name == 'posix', '钩子的 POSIX 可执行夹具')
class PrePushIsolationTest(unittest.TestCase):
    def test_linked_worktree_does_not_leak_git_environment(self):
        hook = Path(__file__).resolve().parents[1] / '.githooks' / 'pre-push'
        env = {key: value for key, value in os.environ.items() if not key.startswith('GIT_')}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, linked = root / 'repo', root / 'linked'

            def git(*args):
                return subprocess.check_output(
                    ['git', *args], env=env, text=True, encoding='utf-8', errors='replace',
                ).strip()

            git('init', '-q', str(repo))
            git('-C', str(repo), 'config', 'core.hooksPath', str(root / 'no-hooks'))
            git('-C', str(repo), '-c', 'user.name=test', '-c', 'user.email=test@example.com',
                'commit', '-q', '--allow-empty', '-m', 'fixture')
            git('-C', str(repo), 'worktree', 'add', '-q', '-b', 'topic', str(linked))
            real_head = git('-C', str(repo), 'rev-parse', 'HEAD')
            git_dir = git('-C', str(linked), 'rev-parse', '--absolute-git-dir')
            python = linked / '.venv' / 'bin' / 'python'
            python.parent.mkdir(parents=True)
            python.write_text(
                f'#!{sys.executable}\n'
                'import json, os\n'
                'print(json.dumps({"git": {k:v for k,v in os.environ.items() '
                'if k in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_PREFIX")}, '
                '"cwd": os.getcwd(), "path": os.environ.get("PYTHONPATH")}))\n',
                encoding='utf-8',
            )
            python.chmod(0o755)
            hook_env = {**env, 'GIT_DIR': git_dir, 'GIT_WORK_TREE': str(linked),
                        'GIT_INDEX_FILE': str(Path(git_dir) / 'index'), 'GIT_PREFIX': '',
                        'PYTHONPATH': '/old/editable'}
            result = subprocess.run(
                ['sh', str(hook)], cwd=linked, env=hook_env,
                input=f'refs/heads/topic {real_head} refs/heads/topic {"0" * 40}\n',
                capture_output=True, text=True, encoding='utf-8', errors='replace', check=True,
            )
            actual = json.loads(result.stdout.splitlines()[-1])
            self.assertEqual(actual['git'], {})
            self.assertEqual(Path(actual['cwd']).resolve(), linked.resolve())
            self.assertEqual(Path(actual['path'].split(os.pathsep)[0]).resolve(), linked.resolve())
            self.assertEqual(git('-C', str(repo), 'rev-parse', 'HEAD'), real_head)

    def test_tag_push_does_not_start_tests(self):
        hook = Path(__file__).resolve().parents[1] / '.githooks' / 'pre-push'
        result = subprocess.run(
            ['sh', str(hook)], cwd=hook.parents[1],
            input=f'refs/tags/v1.0.0 {"a" * 40} refs/tags/v1.0.0 {"0" * 40}\n',
            capture_output=True, text=True, encoding='utf-8', errors='replace', check=True,
        )
        self.assertIn('只推 tag', result.stdout)
        self.assertNotIn('运行单元测试', result.stdout)
