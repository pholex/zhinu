"""Desktop-owned scheduling over the existing authenticated execution service."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
from pathlib import Path
import secrets


def install_automation_routes(app, path: Path, token: str, root: Path, origin: str, *, interval=1.0):
    from fastapi import HTTPException, Request
    from starlette.responses import JSONResponse
    from labs.desktop.automation import AutomationConflict, AutomationRejected, AutomationRunner, AutomationStore
    from labs.desktop.app_ownership import require_owner
    store = AutomationStore(path, write_guard=lambda: require_owner(app))
    runtime = {'runner': None, 'warning': '', 'active': False}
    original = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(application):
        import httpx
        async with original(application):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=origin,
                    headers={'Authorization': 'Bearer ' + token}, timeout=20) as client:
                instance = (await client.get('/health')).json()['instance_id']
                async def request(method, route, body=None, *, key=''):
                    headers = {'X-Xiaoyu-Instance': instance}
                    if key:
                        headers['Idempotency-Key'] = key
                    response = await client.request(method, route, json=body, headers=headers)
                    # Only definitive creation rejections are known not to have submitted a turn.
                    if method == 'POST' and route == '/session' and response.status_code in {400, 401, 403, 404, 422}:
                        raise AutomationRejected()
                    response.raise_for_status()
                    return response.json()
                runner = AutomationRunner(store, request)
                runtime['runner'] = runner
                try:
                    store.recover()
                except (OSError, ValueError):
                    runtime['warning'] = '自动化记录无法恢复；未启动调度，原文件已保留'
                else:
                    runtime['active'] = True
                async def loop():
                    while True:
                        await runner.tick()
                        await asyncio.sleep(interval)
                task = asyncio.create_task(loop()) if runtime['active'] else None
                try:
                    yield
                finally:
                    runtime['active'] = False
                    if task is not None:
                        task.cancel()
                        try:
                            await task
                        except asyncio.CancelledError:
                            pass
    app.router.lifespan_context = lifespan

    def authorize(request):
        supplied = request.headers.get('authorization', '').encode('utf-8')
        if not secrets.compare_digest(supplied, ('Bearer ' + token).encode('utf-8')):
            raise HTTPException(401, '需要本机应用令牌')

    def response(state, **extra):
        from labs.desktop.automation import calendar_view
        notifications = [{'id': rid + ':' + run['state'], 'kind': 'automation', 'run_id': rid,
                          'session_id': run['session_id'], 'state': run['state'], 'message': run['message']}
                         for rid, run in state['runs'].items() if run['state'] in {'completed', 'failed', 'interrupted', 'unknown'}]
        runner = runtime['runner']
        return JSONResponse({'state': state, 'runner_active': runtime['active'],
            'warning': runtime['warning'] or (runner.warning if runner else ''),
            'calendar': calendar_view(state), 'notifications': notifications[-100:],
            'schedule_policy': '仅服务运行时调度；停机不补跑；夏令时不存在的时间跳过，重复时间仅取第一次。', **extra},
            headers={'Cache-Control': 'no-store'})

    async def body(request, fields):
        authorize(request)
        raw = bytearray()
        async for part in request.stream():
            raw.extend(part)
            if len(raw) > 450000:
                raise HTTPException(413, '自动化配置超出大小限制')
        try:
            data = json.loads(raw.decode('utf-8', errors='replace'))
            if not isinstance(data, dict) or set(data) != fields:
                raise ValueError()
            if type(data.get('revision')) is not int or data['revision'] < 0:
                raise ValueError()
            return data
        except ValueError:
            raise HTTPException(422, '自动化请求格式不正确') from None

    def operation(callback):
        try:
            return callback()
        except AutomationConflict:
            raise HTTPException(409, '自动化状态已变化；请读取后核对原请求') from None
        except KeyError:
            raise HTTPException(404, '任务或运行记录不存在') from None
        except ValueError as error:
            raise HTTPException(422, str(error)) from None
        except OSError:
            raise HTTPException(503, '自动化记录无法可靠保存；请保留原请求并核对') from None

    def valid_id(value):
        if (not isinstance(value, str) or not 1 <= len(value) <= 80
                or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_' for c in value)):
            raise HTTPException(422, '自动化记录标识不正确')
        return value

    async def list_automations(request):
        authorize(request)
        return response(operation(store.read))

    async def configure(request):
        data = await body(request, {'revision', 'task_id', 'definition', 'create'})
        tid = valid_id(data['task_id'])
        if type(data['create']) is not bool:
            raise HTTPException(422, '任务创建标志不正确')
        def save():
            from labs.desktop.automation import definition
            item = definition(data['definition'])
            if item.get('project_id'):
                resolver = getattr(app.state, 'project_resolver', None)
                if resolver is None:
                    raise ValueError('当前服务不支持项目自动化')
                item['workspace'] = resolver(item['project_id'], '', None)['workspace']
            workspace = Path(item['workspace']).expanduser().resolve(strict=True)
            if not workspace.is_dir() or (not item.get('project_id') and not workspace.is_relative_to(root.resolve())):
                raise HTTPException(403, '自动化工作目录须在本工作台范围内')
            item['workspace'] = str(workspace)
            return store.configure(data['revision'], tid, item, create=data['create'])
        tid, state = operation(save)
        return response(state, task_id=tid)

    async def enabled(request, task_id):
        data = await body(request, {'revision', 'enabled'})
        return response(operation(lambda: store.enable(data['revision'], valid_id(task_id), data['enabled'])))

    async def acknowledge(request, run_id):
        data = await body(request, {'revision', 'note'})
        record = operation(lambda: store.read()['runs'][valid_id(run_id)])
        if record['state'] == 'unknown' and record.get('session_id'):
            runner = runtime['runner']
            if runner is None:
                raise HTTPException(409, '请先连接服务并核对原会话已经停止')
            try:
                sid = record['session_id']
                status = await runner.request('GET', f'/session/{sid}/status')
                fleets, persisted = await runner._pages(f'/session/{sid}/fleets', 'fleets')
                latest = await runner.request('GET', f'/session/{sid}/status')
                active = (latest.get('busy') or latest.get('pending_approvals')
                    or latest.get('writable') is False or latest.get('turns') != status.get('turns')
                    or status.get('busy') or status.get('pending_approvals')
                    or status.get('writable') is False or not persisted
                    or any(f.get('limits', {}).get('members', 0) for f in fleets)
                    or any(m.get('execution_state') != 'settled' or m.get('thread_alive') is True
                           for f in fleets for m in f.get('members', [])))
            except Exception:
                raise HTTPException(409, '无法确认原会话已停止；保留未知记录') from None
            if active:
                raise HTTPException(409, '原会话仍在执行或状态未确认；请先停止并核对')
        return response(operation(lambda: store.acknowledge(data['revision'], valid_id(run_id), data['note'])))

    for endpoint, route, method in [
        (list_automations, '/desktop/automations', 'GET'),
        (configure, '/desktop/automations/configure', 'POST'),
        (enabled, '/desktop/automations/{task_id}/enabled', 'POST'),
        (acknowledge, '/desktop/automation-runs/{run_id}/acknowledge', 'POST')]:
        endpoint.__annotations__['request'] = Request
        app.add_api_route(route, endpoint, methods=[method], include_in_schema=False)
    return store, runtime
