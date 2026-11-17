"""Authenticated desktop connection routes and one public OAuth return endpoint."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
import secrets
import threading


@asynccontextmanager
async def management_guard(session):
    from xiaoyu.serve_ownership import require_write
    from labs.desktop.connections import ConnectionConflict, idle_reason
    if getattr(session, 'management_busy', False):
        raise ConnectionConflict('另一连接管理操作正在执行')
    require_write(session)
    if problem := idle_reason(session):
        raise ConnectionConflict(problem)
    session.management_busy = True
    try:
        yield
    finally:
        done = getattr(session, 'management_worker_done', None)
        if done is None or done.is_set():
            session.management_busy = False


async def _managed(session, call):
    async with management_guard(session):
        done = threading.Event()
        session.management_worker_done = done
        def worker():
            try:
                return call()
            finally:
                done.set()
        task = asyncio.create_task(asyncio.to_thread(worker))
        session.management_task = task
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            # A cancelled HTTP request cannot release the prompt gate while its
            # worker still mutates the pool. Retain a drain task until it settles.
            async def drain():
                try:
                    await task
                except Exception:
                    pass
                finally:
                    if done.is_set():
                        session.management_busy = False
                        session.management_task = None
            session.management_drain = asyncio.create_task(drain())
            raise
        finally:
            if done.is_set():
                session.management_task = None



def install_connection_routes(app, path, token, origin, *, legacy_path=None):
    from fastapi import HTTPException, Request
    from starlette.responses import JSONResponse, PlainTextResponse
    from labs.desktop.connections import ConnectionStore, ConnectionRegistry, ConnectionError, ConnectionConflict
    registry = ConnectionRegistry(ConnectionStore(path, legacy_path), origin)
    original = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(application):
        async with original(application):
            try:
                yield
            finally:
                # Core shutdown owns connection_close and retains uncertain pools.
                # Do not re-close a pool after core deliberately declined to race it.
                registry.begin_shutdown()
    app.router.lifespan_context = lifespan
    app.state.connections = registry

    def authorize(request):
        if not secrets.compare_digest(request.headers.get('authorization', '').encode(), ('Bearer ' + token).encode()):
            raise HTTPException(401, '需要本机应用令牌')

    def require_owner():
        owner = getattr(app.state, 'execution_owner', None)
        if owner is None or not owner.held:
            raise HTTPException(503, '服务状态目录已失去执行所有权')

    def owned(callback):
        require_owner()
        return callback()

    def session_of(sid):
        if not isinstance(sid, str):
            raise HTTPException(422, '会话标识不正确')
        session = app.state.sessions.get(sid)
        if session is None:
            raise HTTPException(404, '会话不存在')
        session.connection_close = lambda: registry.close_session(sid)
        return session

    def response(data):
        return JSONResponse(data, headers={'Cache-Control': 'no-store'})

    def call(fn):
        try:
            return fn()
        except ConnectionConflict as error:
            raise HTTPException(409, str(error)) from None
        except (ConnectionError, ValueError):
            raise HTTPException(422, '连接请求或配置文件不正确') from None
        except KeyError:
            raise HTTPException(404, '连接或授权请求不存在') from None
        except OSError:
            raise HTTPException(503, '连接配置无法可靠保存或读取') from None

    async def body(request, fields, optional=()):
        authorize(request)
        raw = bytearray()
        async for part in request.stream():
            raw.extend(part)
            if len(raw) > 65536:
                raise HTTPException(413, '连接请求过长')
        try:
            value = json.loads(raw.decode('utf-8', errors='replace'))
            if not isinstance(value, dict) or set(value) - set(fields) - set(optional) or set(fields) - set(value):
                raise ValueError()
            return value
        except ValueError:
            raise HTTPException(422, '连接请求格式不正确') from None

    async def list_config(request):
        authorize(request)
        return response(await asyncio.to_thread(call, registry.store.read))

    async def configure(request):
        value = await body(request, {'revision', 'connection_id', 'definition'})
        return response(await asyncio.to_thread(call, lambda: owned(lambda: registry.configure(value['revision'], value['connection_id'], value['definition']))))

    async def delete(request, connection_id):
        value = await body(request, {'revision'})
        return response(await asyncio.to_thread(call, lambda: owned(lambda: registry.delete(value['revision'], connection_id))))

    async def session_connections(request, session_id):
        authorize(request)
        session = session_of(session_id)
        value = await asyncio.to_thread(call, lambda: registry.list(session))
        value['instance_id'] = getattr(app.state, 'instance_id', '')
        return response(value)

    async def manage(request, session_id, connection_id):
        value = await body(request, {'action', 'configuration_revision'}, {'expected_fingerprint'})
        session = session_of(session_id)
        key = request.headers.get('idempotency-key', '')
        instance = getattr(app.state, 'instance_id', '')
        if not instance or request.headers.get('x-xiaoyu-instance') != instance:
            raise HTTPException(409, '服务实例已变化，请重新读取连接状态；旧操作不会重放')
        earlier = call(lambda: registry.receipt(session_id, connection_id, value, key))
        if earlier is not None:
            return response(earlier)
        executed = False
        def operation():
            nonlocal executed
            executed = True
            return call(lambda: registry.manage(session, connection_id, value, key))
        try:
            result = await _managed(session, operation)
        except (ConnectionConflict, HTTPException) as error:
            earlier = call(lambda: registry.receipt(session_id, connection_id, value, key))
            if earlier is not None and not executed:
                return response(earlier)
            if isinstance(error, HTTPException):
                raise
            raise HTTPException(409, str(error)) from None
        return response(result)

    async def begin(request, connection_id):
        value = await body(request, {'session_id'})
        session = session_of(value['session_id'])
        return response(await asyncio.to_thread(call, lambda: owned(lambda: registry.begin_authorization(session, connection_id))))

    async def authorization(request, connection_id, authorization_id):
        authorize(request)
        return response(await asyncio.to_thread(call, lambda: registry.authorization(connection_id, authorization_id)))

    async def open_url(request, connection_id, authorization_id):
        authorize(request)
        return response(await asyncio.to_thread(call, lambda: registry.open_url(connection_id, authorization_id)))

    async def cancel(request, connection_id, authorization_id):
        await body(request, set())
        return response(await asyncio.to_thread(call, lambda: registry.cancel(connection_id, authorization_id)))

    async def revoke(request, connection_id):
        value = await body(request, {'session_id'})
        session = session_of(value['session_id'])
        try:
            result = await _managed(session, lambda: call(lambda: registry.revoke(session, connection_id)))
        except ConnectionConflict as error:
            raise HTTPException(409, str(error)) from None
        return response(result)

    async def callback(request, authorization_id):
        # Never trust the inbound Host to construct the redirect checked by SDK.
        query = request.scope.get('query_string', b'')
        if len(query) > 16384:
            return PlainTextResponse('授权响应过长', status_code=413, headers={'Cache-Control': 'no-store'})
        await asyncio.to_thread(call, lambda: registry.callback(authorization_id, query.decode('ascii', errors='replace')))
        return PlainTextResponse('已收到授权响应，请返回应用查看结果。', headers={
            'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer', 'Content-Security-Policy': "default-src 'none'"})

    registry.guard = require_owner
    registry.store.guard = require_owner

    routes = [
        (list_config, '/desktop/connections', 'GET'), (configure, '/desktop/connections', 'POST'),
        (delete, '/desktop/connections/{connection_id}/delete', 'POST'),
        (session_connections, '/session/{session_id}/connections', 'GET'),
        (manage, '/session/{session_id}/connections/{connection_id}', 'POST'),
        (begin, '/desktop/connections/{connection_id}/authorizations', 'POST'),
        (authorization, '/desktop/connections/{connection_id}/authorizations/{authorization_id}', 'GET'),
        (open_url, '/desktop/connections/{connection_id}/authorizations/{authorization_id}/open-url', 'GET'),
        (cancel, '/desktop/connections/{connection_id}/authorizations/{authorization_id}/cancel', 'POST'),
        (revoke, '/desktop/connections/{connection_id}/revoke', 'POST'),
        (callback, '/desktop/oauth/callback/{authorization_id}', 'GET'),
    ]
    for endpoint, path, method in routes:
        endpoint.__annotations__['request'] = Request
        app.add_api_route(path, endpoint, methods=[method], include_in_schema=False)
    return registry
