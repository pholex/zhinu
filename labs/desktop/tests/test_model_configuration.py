"""Inherited model routing must survive native onboarding, without network discovery."""
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from labs.desktop.macos.backend import load_model_configuration
from xiaoyu.providers import PRESETS


class ModelConfigurationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.workspace = self.root / 'workspace'
        self.workspace.mkdir()
        self.user = self.root / 'config/xiaoyu/.env'
        self.user.parent.mkdir(parents=True)
        self.addCleanup(os.chdir, Path.cwd())
        os.chdir(self.workspace)
        self.enterContext(patch.dict(os.environ, {'HOME': str(self.root),
            'XDG_CONFIG_HOME': str(self.root / 'config')}, clear=True))
        self.enterContext(patch('xiaoyu.config.PROJECT_ROOT', self.root / 'package'))
        self.trust = self.enterContext(patch('xiaoyu.folder_trust.evaluate',
            return_value=SimpleNamespace(verdict='untrusted')))

    def test_cli_gateway_and_default_survive_native_fallback(self):
        self.user.write_text('XIAOYU_MODEL=my-model\nXIAOYU_BASE_URL=http://localhost:9999/v1\nXIAOYU_API_KEY=fake-cli-key\nXIAOYU_PROVIDERS=gateway\n')
        os.environ['DEEPSEEK_API_KEY'] = 'fake-native-key'
        before = self.user.read_bytes()
        load_model_configuration(self.workspace, fallback_model=PRESETS['deepseek'].models[0])
        self.assertEqual(os.environ['XIAOYU_MODEL'], 'my-model')
        self.assertEqual(os.environ['XIAOYU_PROVIDERS'], 'gateway')
        self.assertEqual(os.environ['XIAOYU_API_KEY'], 'fake-cli-key')
        self.assertEqual(self.user.read_bytes(), before)

    def test_environment_overrides_cli_file_and_untrusted_workspace_is_skipped(self):
        self.user.write_text('XIAOYU_MODEL=file-model\n')
        (self.workspace / '.env').write_text('XIAOYU_MODEL=project-model\nXIAOYU_BASE_URL=http://untrusted.invalid\n')
        os.environ['XIAOYU_MODEL'] = 'environment-model'
        load_model_configuration(self.workspace)
        self.assertEqual(os.environ['XIAOYU_MODEL'], 'environment-model')
        self.assertNotIn('XIAOYU_BASE_URL', os.environ)
        del os.environ['XIAOYU_MODEL']
        load_model_configuration(self.workspace)
        self.assertEqual(os.environ['XIAOYU_MODEL'], 'file-model')

    def test_trusted_workspace_and_explicit_file_follow_cli_precedence(self):
        self.user.write_text('XIAOYU_MODEL=user-model\n')
        (self.workspace / '.env').write_text('XIAOYU_MODEL=project-model\n')
        self.trust.return_value.verdict = 'trusted'
        load_model_configuration(self.workspace)
        self.assertEqual(os.environ['XIAOYU_MODEL'], 'project-model')
        del os.environ['XIAOYU_MODEL']
        explicit = self.root / 'chosen.env'
        explicit.write_text('XIAOYU_MODEL=chosen-model\n')
        os.environ['XIAOYU_ENV_FILE'] = str(explicit)
        load_model_configuration(self.workspace)
        self.assertEqual(os.environ['XIAOYU_MODEL'], 'chosen-model')

    def test_first_enabled_fallback_and_disabled_key_not_reintroduced(self):
        self.user.write_text('DEEPSEEK_API_KEY=fake-disabled-file-key\n')
        os.environ['QWEN_API_KEY'] = 'fake-selected-key'
        model = PRESETS['qwen'].models[0]
        load_model_configuration(self.workspace, fallback_model=model, disabled_providers=['deepseek'])
        self.assertEqual(os.environ['XIAOYU_MODEL'], model)
        self.assertNotIn('DEEPSEEK_API_KEY', os.environ)

    def test_cli_file_only_credentials_choose_available_model(self):
        self.user.write_text('QWEN_API_KEY=fake-cli-only-key\n')
        load_model_configuration(self.workspace)
        self.assertIn(os.environ['XIAOYU_MODEL'], PRESETS['qwen'].models)
