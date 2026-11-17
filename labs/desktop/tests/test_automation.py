from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from labs.desktop.automation import AutomationConflict, AutomationStore, AutomationRunner, AutomationRejected


class AutomationStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "automation.json"
        self.store = AutomationStore(self.path)
        self.definition = {"name": "日报", "workspace": "/project", "model": "scripted", "prompt": "整理变更",
                           "schedule": {"kind": "interval", "seconds": 60}}

    def configured(self):
        key, state = self.store.configure(0, "", self.definition, now=100)
        self.assertFalse(state["tasks"][key]["enabled"])
        self.assertIsNone(self.store.reserve_due(now=1000))
        self.store.enable(1, key, True, now=100)
        return key

    def test_explicit_enable_reservation_and_no_overlap(self):
        key = self.configured()
        self.assertIsNone(self.store.reserve_due(now=159))
        run, definition = self.store.reserve_due(now=160)
        self.assertEqual(definition, self.definition)
        self.assertEqual(self.store.read()["runs"][run]["state"], "reserved")
        self.assertIsNone(self.store.reserve_due(now=1000))
        self.store.record(run, state="completed", session_id="session-one", execution_id="turn-one")
        second, _ = self.store.reserve_due(now=1000)
        self.assertNotEqual(run, second)
        self.assertEqual(self.store.read()["tasks"][key]["next_due"], 1060)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_restart_uncertain_submission_never_replays_and_requires_reconciliation(self):
        key = self.configured()
        run, _ = self.store.reserve_due(now=160)
        self.store.record(run, state="submitting", session_id="maybe-created")
        state = AutomationStore(self.path).recover(now=500)
        self.assertEqual(state["runs"][run]["state"], "unknown")
        self.assertFalse(state["tasks"][key]["enabled"])
        self.assertIsNone(self.store.reserve_due(now=1000))
        with self.assertRaises(ValueError):
            self.store.enable(state["revision"], key, True, now=500)
        state = self.store.acknowledge(state["revision"], run, "已检查关联会话和成果，允许未来的新周期")
        self.assertEqual(state["runs"][run]["state"], "acknowledged")
        state = self.store.enable(state["revision"], key, True, now=500)
        self.assertEqual(state["tasks"][key]["next_due"], 560)

    def test_restart_skips_missed_interval_and_single_execution(self):
        key = self.configured()
        state = self.store.recover(now=1000)
        self.assertEqual(state["tasks"][key]["next_due"], 1060)
        self.assertIsNone(self.store.reserve_due(now=1000))
        once = {**self.definition, "schedule": {"kind": "once", "at": 1100}}
        other, state = self.store.configure(state["revision"], "", once, now=1000)
        state = self.store.enable(state["revision"], other, True, now=1000)
        state = self.store.recover(now=1200)
        self.assertFalse(state["tasks"][other]["enabled"])
        self.assertEqual(state["runs"], {})

    def test_disk_failure_has_no_reservation_and_corruption_is_not_reset(self):
        self.configured()
        with patch("os.replace", side_effect=OSError("disk failure")):
            with self.assertRaises(OSError):
                self.store.reserve_due(now=160)
        self.assertEqual(self.store.read()["runs"], {})
        original = b'{"bad":\xff}'
        self.path.write_bytes(original)
        with self.assertRaises(ValueError):
            self.store.recover(now=1000)
        self.assertEqual(self.path.read_bytes(), original)

    def test_revision_protects_concurrent_edits_and_active_config_is_frozen(self):
        key = self.configured()
        with self.assertRaises(AutomationConflict):
            self.store.enable(1, key, False, now=100)
        self.store.reserve_due(now=160)
        with self.assertRaises(ValueError):
            self.store.configure(3, key, {**self.definition, "prompt": "改任务"}, now=170)

    def test_create_enable_and_ack_retries_preserve_current_state(self):
        key, state = self.store.configure(0, "client-stable", self.definition, now=100, create=True)
        enabled = self.store.enable(state["revision"], key, True, now=100)
        self.assertEqual(self.store.configure(0, key, self.definition, now=999, create=True)[1], enabled)
        self.assertEqual(self.store.enable(1, key, True, now=999), enabled)
        self.assertEqual(self.store.read()["tasks"][key]["next_due"],160)
        with self.assertRaises(AutomationConflict):
            self.store.configure(0, key, {**self.definition, "prompt":"different"}, create=True)
        run, _ = self.store.reserve_due(now=160)
        state = self.store.record(run,state="unknown")
        acknowledged = self.store.acknowledge(state["revision"],run,"已核对")
        self.assertEqual(self.store.acknowledge(state["revision"],run,"已核对"),acknowledged)

    def test_non_regular_or_linked_store_is_rejected_without_reading(self):
        import os
        other = self.path.parent/'other.json'
        other.write_text('{"revision":0,"tasks":{},"runs":{}}')
        self.path.symlink_to(other)
        with self.assertRaises((OSError,ValueError)):
            self.store.read()
        self.path.unlink()
        os.link(other,self.path)
        with self.assertRaises(ValueError):
            self.store.read()


class AutomationRunnerTests(unittest.IsolatedAsyncioTestCase):
    async def test_project_task_uses_current_primary_without_stale_directory(self):
        from labs.desktop.workspace import ProjectResolver, WorkspaceState
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve(); old = root/'old'; new = root/'new'
            old.mkdir(); new.mkdir()
            projects = WorkspaceState(root/'projects.json'); pid = 'a'*24
            projects.update(0, 'projects', pid, {'name':'project','context':'','folders':[str(old)],'primary':str(old)})
            store = AutomationStore(root/'automation.json')
            task, state = store.configure(0, '', {'name':'task','workspace':str(old),'project_id':pid,
                'model':'scripted','prompt':'work','schedule':{'kind':'interval','seconds':60}}, now=100)
            store.enable(state['revision'], task, True, now=100)
            projects.update(1, 'projects', pid, {'name':'project','context':'','folders':[str(new)],'primary':str(new)})
            resolver = ProjectResolver(projects.path, root)
            calls = []
            async def request(method, path, body=None, **kwargs):
                calls.append(path)
                if path == '/session':
                    self.assertNotIn('workspace', body)
                    self.assertEqual(resolver(body['project_id'], body.get('workspace',''), None)['workspace'], str(new))
                    return {'session_id':'s'}
                return {'in_flight':{'id':'r'}}
            await AutomationRunner(store, request).tick(now=160)
            state = store.read()
            self.assertTrue(state['tasks'][task]['enabled'])
            self.assertEqual(next(iter(state['runs'].values()))['state'], 'running')
            self.assertEqual(calls, ['/session','/session/s/prompt_async'])

    async def test_creation_rejected_is_failed_and_can_be_reenabled_without_unknown_ack(self):
        with tempfile.TemporaryDirectory() as folder:
            store = AutomationStore(Path(folder)/'automation.json')
            task, state = store.configure(0, '', {'name':'task','workspace':'/work','model':'scripted',
                'prompt':'work','schedule':{'kind':'interval','seconds':60}}, now=100)
            store.enable(state['revision'], task, True, now=100)
            calls = []
            async def request(method, path, body=None, **kwargs):
                calls.append(path)
                raise AutomationRejected()
            await AutomationRunner(store, request).tick(now=160)
            state = store.read()
            self.assertFalse(state['tasks'][task]['enabled'])
            self.assertEqual(next(iter(state['runs'].values()))['state'], 'failed')
            self.assertEqual(calls, ['/session'])
            enabled = store.enable(state['revision'], task, True, now=200)
            self.assertTrue(enabled['tasks'][task]['enabled'])


    async def test_paginated_followup_and_retired_live_member_block_false_success(self):
        with tempfile.TemporaryDirectory() as temporary:
            store=AutomationStore(Path(temporary)/'automation.json')
            task,state=store.configure(0,'',{'name':'task','workspace':'/project','model':'scripted','prompt':'work',
                'schedule':{'kind':'once','at':120}},now=100)
            store.enable(state['revision'],task,True,now=100)
            run,_=store.reserve_due(now=120)
            store.record(run,state='running',session_id='s',execution_id='r')
            alive=True
            calls=[]
            async def request(method,path,body=None,**kwargs):
                calls.append((method,path))
                if '/runs?cursor=0' in path:
                    return {'runs':[{'id':'r','turn':1,'status':'completed'}],'persisted':True,'next_cursor':1}
                if '/runs?cursor=1' in path:
                    return {'runs':[{'id':'later','turn':2,'status':'failed'}],'persisted':True,'next_cursor':None}
                if path.endswith('/status'):
                    return {'busy':False,'turns':2,'pending_approvals':[]}
                if path.endswith('/questions'):
                    return {'questions':[]}
                if '/fleets?' in path:
                    return {'persisted':True,'fleets':[{'board_id':'b','members':[{'status':'retired','execution_state':'settled',
                        'execution_outcome':'completed','thread_alive':alive}]}]}
                raise AssertionError(path)
            runner=AutomationRunner(store,request)
            await runner.refresh()
            self.assertEqual(store.read()['runs'][run]['state'],'running')
            alive=False
            await runner.refresh()
            self.assertEqual(store.read()['runs'][run]['state'],'failed')
            self.assertTrue(all(method=='GET' for method,_ in calls))

    async def test_approval_and_result_polling_never_resubmits_after_restart(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = AutomationStore(Path(temporary) / "automation.json")
            task, state = store.configure(0, "", {"name": "任务", "workspace": "/project", "model": "scripted",
                "prompt": "工作", "schedule": {"kind": "once", "at": 120}}, now=100)
            store.enable(state["revision"], task, True, now=100)
            calls = []
            pending = True
            async def request(method, path, body=None, **kwargs):
                calls.append((method, path, body, kwargs))
                if path == "/session":
                    self.assertEqual(next(iter(store.read()["runs"].values()))["state"], "creating")
                    return {"session_id": "s"}
                if path.endswith("/prompt_async"):
                    self.assertEqual(next(iter(store.read()["runs"].values()))["state"], "submitting")
                    return {"in_flight": {"id": "r"}}
                if "/runs?" in path:
                    return {"persisted": True, "runs": [{"id": "r", "turn": 1, "status": "completed"}]}
                if path.endswith("/status"):
                    return {"busy": False, "turns": 1, "pending_approvals": ["approval"] if pending else []}
                if path.endswith("/questions"):
                    return {"questions": []}
                if "/fleets?" in path:
                    return {"fleets": [], "persisted": True}
                raise AssertionError(path)
            runner = AutomationRunner(store, request)
            await runner.tick(now=120)
            self.assertEqual(len([c for c in calls if c[0] == "POST"]), 2)
            store.recover(now=200)
            runner = AutomationRunner(store, request)
            await runner.tick(now=200)
            self.assertEqual(next(iter(store.read()["runs"].values()))["state"], "running")
            pending = False
            await runner.tick(now=201)
            self.assertEqual(next(iter(store.read()["runs"].values()))["state"], "completed")
            self.assertEqual(len([c for c in calls if c[0] == "POST"]), 2)

    async def test_lost_prompt_reply_pauses_future_triggers(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = AutomationStore(Path(temporary) / "automation.json")
            task, state = store.configure(0, "", {"name": "任务", "workspace": "/project", "model": "scripted",
                "prompt": "工作", "schedule": {"kind": "interval", "seconds": 60}}, now=100)
            store.enable(state["revision"], task, True, now=100)
            calls = []
            async def request(method, path, body=None, **kwargs):
                calls.append(path)
                if path == "/session":
                    return {"session_id": "s"}
                raise ConnectionError("reply dropped after acceptance")
            runner = AutomationRunner(store, request)
            await runner.tick(now=160)
            state = store.read()
            self.assertEqual(next(iter(state["runs"].values()))["state"], "unknown")
            self.assertFalse(state["tasks"][task]["enabled"])
            await runner.tick(now=1000)
            self.assertEqual(len(calls), 2)

    async def test_owner_loss_before_tick_or_during_refresh_never_reserves_or_posts(self):
        import asyncio
        for boundary in ('before','during-read'):
            with self.subTest(boundary=boundary), tempfile.TemporaryDirectory() as temporary:
                held=True
                def guard():
                    if not held:
                        raise OSError('fixture owner lost')
                store=AutomationStore(Path(temporary)/'automation.json',write_guard=guard)
                value={'name':'任务','workspace':'/project','model':'scripted','prompt':'工作','schedule':{'kind':'interval','seconds':60}}
                first,state=store.configure(0,'active',value,now=100,create=True)
                state=store.enable(state['revision'],first,True,now=100)
                run,_=store.reserve_due(now=160)
                state=store.record(run,state='running',session_id='s',execution_id='r')
                second,state=store.configure(state['revision'],'due',value,now=100,create=True)
                saved=store.enable(state['revision'],second,True,now=100)
                calls=[]
                async def request(method,path,body=None,**kwargs):
                    nonlocal held
                    calls.append((method,path))
                    self.assertEqual(method,'GET')
                    await asyncio.sleep(0)
                    held=False
                    return {'runs':[{'id':'r','turn':1,'status':'running'}],'persisted':True}
                if boundary=='before':
                    held=False
                runner=AutomationRunner(store,request)
                await runner.tick(now=200)
                self.assertEqual(store.read(),saved)
                self.assertTrue(runner.warning)
                self.assertEqual(len(calls),0 if boundary=='before' else 1)
                self.assertFalse(any(method=='POST' for method,_ in calls))


class CalendarScheduleTests(unittest.TestCase):
    def test_timezone_weekdays_and_dst_nonexistent_and_repeated_times(self):
        from datetime import datetime, timezone
        from labs.desktop.automation import next_due, definition
        def epoch(text): return datetime.fromisoformat(text).replace(tzinfo=timezone.utc).timestamp()
        calendar={'kind':'calendar','timezone':'America/New_York','time':'02:30','weekdays':[6]}
        # Mar 8 2026 02:30 does not exist. It is skipped, not shifted to 03:30.
        self.assertEqual(next_due(calendar,epoch('2026-03-08T00:00')),epoch('2026-03-15T06:30'))
        calendar['time']='01:30'
        first=next_due(calendar,epoch('2026-11-01T00:00'))
        self.assertEqual(first,epoch('2026-11-01T05:30'))
        self.assertEqual(next_due(calendar,first),epoch('2026-11-08T06:30'))
        value={'name':'task','workspace':'/work','model':'scripted','prompt':'work','schedule':calendar}
        self.assertEqual(definition(value),value)
        with self.assertRaises(ValueError):definition({**value,'schedule':{**calendar,'weekdays':[True]}})

    def test_calendar_restart_skip_reservation_and_readonly_calendar(self):
        from datetime import datetime, timezone
        from labs.desktop.automation import calendar_view
        now=datetime(2026,10,12,tzinfo=timezone.utc).timestamp()
        with tempfile.TemporaryDirectory() as d:
            store=AutomationStore(Path(d)/'schedule.json')
            value={'name':'daily','workspace':'/work','model':'scripted','prompt':'work','schedule':{'kind':'calendar','timezone':'Asia/Tokyo','time':'10:00','weekdays':[0,1,2,3,4]}}
            tid,state=store.configure(0,'daily',value,create=True,now=now)
            state=store.enable(state['revision'],tid,True,now=now)
            view=calendar_view(state)
            self.assertEqual(len(view[0]['occurrences']),8)
            self.assertEqual(store.read(),state)
            state=store.recover(now=now+2*86400)
            self.assertGreater(state['tasks'][tid]['next_due'],now+2*86400)
            self.assertEqual(state['runs'],{})
            due=state['tasks'][tid]['next_due']
            rid,_=store.reserve_due(now=due)
            recovered=AutomationStore(store.path).recover(now=due+60)
            self.assertEqual(recovered['runs'][rid]['state'],'unknown')
            self.assertFalse(recovered['tasks'][tid]['enabled'])
            self.assertIsNone(store.reserve_due(now=due+86400))
