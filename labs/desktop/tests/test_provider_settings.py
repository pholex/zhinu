import multiprocessing
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from labs.desktop.provider_settings import ProviderSettings
from labs.desktop.preferences import PreferenceConflict


def competing_provider_writer(path, ready, release):
    calls = 0
    def guard():
        nonlocal calls
        calls += 1
        if calls == 2:
            ready.set()
            if not release.wait(10):
                raise TimeoutError('test writer was not released')
    store = ProviderSettings(path=path, write_guard=guard)
    store.save(store.read()['revision'], {'id':'first', 'base_url':'https://example.test/v1',
        'models':['m'], 'protocol':'chat', 'api_key_action':'keep'})


class ProviderSettingsTests(unittest.TestCase):
    def test_other_process_cannot_overwrite_and_can_retry_after_refresh(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'.env'
            ctx = multiprocessing.get_context('spawn')
            ready, release = ctx.Event(), ctx.Event()
            child = ctx.Process(target=competing_provider_writer, args=(path, ready, release))
            store = ProviderSettings(path=path)
            revision = store.read()['revision']
            value = {'id':'second','base_url':'https://example.test/v1','models':['m'],
                     'protocol':'chat','api_key_action':'keep'}
            child.start()
            try:
                self.assertTrue(ready.wait(10))
                with self.assertRaises(PreferenceConflict):
                    store.save(revision, value)
            finally:
                release.set()
                child.join(10)
                if child.is_alive():
                    child.terminate(); child.join(5)
            self.assertEqual(child.exitcode, 0)
            with self.assertRaises(PreferenceConflict):
                store.save(revision, value)
            result = store.save(store.read()['revision'], value)
            self.assertTrue({'first','second'} <= {p['id'] for p in result['providers']})


    def test_write_only_key_roundtrip_keep_clear_cas_and_no_process_mutation(self):
        from xiaoyu.config import _parse_dotenv
        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ,{},clear=True):
            path=Path(d)/'.env';path.write_text('UNRELATED="keep me"\n',encoding='utf-8')
            store=ProviderSettings(path=path)
            value={'id':'local_test','base_url':'http://127.0.0.1:8080/v1','models':['model-a'],'protocol':'chat','api_key_action':'set','api_key':'supersecret'}
            before=store.read();saved=store.save(before['revision'],value)
            self.assertNotIn('supersecret',str(saved))
            self.assertTrue(saved['providers'][0]['has_key'])
            self.assertEqual(_parse_dotenv(path)['UNRELATED'],'keep me')
            self.assertEqual(_parse_dotenv(path)['XIAOYU_PROVIDER_LOCAL_TEST_API_KEY'],'supersecret')
            self.assertNotIn('XIAOYU_PROVIDER_LOCAL_TEST_API_KEY',os.environ)
            self.assertEqual(path.stat().st_mode & 0o777,0o600)
            with self.assertRaises(PreferenceConflict):store.save(before['revision'],value)
            value.pop('api_key');value['api_key_action']='keep';value['models']=['model-b']
            saved=store.save(saved['revision'],value)
            self.assertEqual(_parse_dotenv(path)['XIAOYU_PROVIDER_LOCAL_TEST_API_KEY'],'supersecret')
            value['api_key_action']='clear';saved=store.save(saved['revision'],value)
            self.assertFalse(saved['providers'][0]['has_key'])

    def test_owner_loss_before_replace_preserves_file_and_endpoint_secrets_rejected(self):
        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ,{},clear=True):
            path=Path(d)/'.env';path.write_text('A=1\n',encoding='utf-8')
            held=True
            def guard():
                if not held:raise OSError('owner lost')
            store=ProviderSettings(path=path,write_guard=guard)
            value={'id':'custom','base_url':'http://localhost:8080','models':['m'],'protocol':'chat','api_key_action':'keep'}
            revision=store.read()['revision']
            original=os.fsync
            def lose(fd):
                nonlocal held
                original(fd);held=False
            with patch('os.fsync',lose),self.assertRaises(OSError):store.save(revision,value)
            self.assertEqual(path.read_text(),'A=1\n')
            for url in ['https://user:secret@example.test','https://example.test?key=secret','file:///tmp/x']:
                with self.assertRaises(ValueError):store.save(revision,{**value,'base_url':url})

    def test_non_discovering_registry_never_contacts_auto_endpoint(self):
        from xiaoyu.config import Config
        from xiaoyu.providers import build
        env={'XIAOYU_PROVIDERS':'localtest','XIAOYU_PROVIDER_LOCALTEST_BASE_URL':'http://127.0.0.1:9999','XIAOYU_PROVIDER_LOCALTEST_MODELS':'auto'}
        with patch.dict(os.environ,env,clear=True),patch('xiaoyu.providers._discover_models',side_effect=AssertionError('network')):
            from xiaoyu.config import MissingConfig
            with self.assertRaises(MissingConfig):
                build(Config(base_url='',model='m',workspace=Path('/tmp')),discover=False)
