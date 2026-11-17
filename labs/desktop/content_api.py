"""Authenticated desktop content aggregation and bounded, explicit search snapshots."""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
from pathlib import Path
import secrets
import time

from .app_ownership import OwnershipUnavailable, require_owner
from .content import ContentConflict, ContentStore, artifact_id, artifact_type, export_page
from .workspace import WorkspaceState, project_id, project_catalogue, session_project_id

SEARCH_BYTES = 2 * 1024 * 1024
SEARCH_TOTAL_BYTES = 16 * 1024 * 1024
SEARCH_DOCUMENTS = 10000


def catalogue(sessions, root=None, state=None):
    return project_catalogue(sessions, root, state)


def library(sessions, favorites):
    """Snapshot only registered owners. A path or an artefact id alone is not identity."""
    rows, warnings = [], []
    def append(sid, pid, owner, source, artifacts, created):
        previews = {p.get('id'): p for p in owner.get('artifact_previews', []) if isinstance(p, dict)}
        for artifact in artifacts or []:
            if not isinstance(artifact, dict) or not isinstance(artifact.get('id'), str):
                continue
            row = {'session_id': sid, 'project_id': pid, 'source': source, 'artifact': copy.deepcopy(artifact),
                   'created_at': created if type(created) in {int, float} else 0, 'type': artifact_type(artifact)}
            row['content_id'] = artifact_id(row)
            row['favorite'] = favorites.get(row['content_id']) is True
            # Search consumes only the original, digest-verified delivery snapshot.
            row['_preview'] = copy.deepcopy(previews.get(artifact['id'], {}))
            rows.append(row)
    for sid, session in list(sessions.items()):
        pid = session_project_id(session)
        if getattr(session, 'history_warning', ''):
            warnings.append('部分会话历史不完整，作品目录可能缺项')
        for run in copy.deepcopy(getattr(session, 'runs', [])):
            append(sid, pid, run, {'run_id': run['id']}, (run.get('result') or {}).get('artifacts'), run.get('started_at', 0))
        for batch in copy.deepcopy(getattr(getattr(session, 'weaves', None), 'batches', {})).values():
            for item in batch.get('items', []):
                append(sid, pid, item, {'batch_id': batch['batch_id'], 'item_id': item['id']}, item.get('artifacts'), batch.get('updated_at', 0))
        for contest in copy.deepcopy(getattr(getattr(session, 'contests', None), 'contests', {})).values():
            for item in contest.get('candidates', []):
                append(sid, pid, item, {'contest_id': contest['contest_id'], 'candidate_id': item['id']}, item.get('artifacts'), contest.get('updated_at', 0))
    rows.sort(key=lambda row: (-row['created_at'], row['content_id']))
    if len(rows) > 3000:
        warnings.append('作品超过 3000 条，仅列出最近登记作品；搜索与引用限于此范围')
    return rows[:3000], '；'.join(dict.fromkeys(warnings)), len(rows) > 3000


class SearchIndex:
    def __init__(self):
        self.documents = []
        self.updated_at = 0
        self.revision = -1
        self.warning = ''
        self.truncated = False
        self.scope = '当前工作台登记的会话正文、项目说明和资料、作品名称及保存的交付文本、空间和未删除页面；不扫描目录，不读取系统提示或凭据。'

    def build(self, state, projects, rows, logs, contexts, warning=''):
        from xiaoyu import serve_history
        from xiaoyu.serve_artifacts import decode_record
        from xiaoyu.artifact_views import is_text
        documents, warnings, used = [], [warning] if warning else [], 0
        truncated = False
        def add(kind, identifier, title, body, **source):
            nonlocal used, truncated
            if len(documents) >= SEARCH_DOCUMENTS or used >= SEARCH_TOTAL_BYTES:
                truncated = True
                return
            body = str(body)
            encoded = body.encode('utf-8', errors='replace')
            remaining = min(200000, SEARCH_TOTAL_BYTES - used)
            if len(encoded) > remaining:
                encoded = encoded[:remaining]
                truncated = True
            body = encoded.decode('utf-8', errors='replace')
            used += len(encoded)
            documents.append({'kind': kind, 'id': identifier, 'title': str(title), '_text': body, **source})
        for p in projects:
            pid = p['project_id']; data = state['projects'].get(pid, {})
            add('project', pid, p.get('name') or Path(p['workspace']).name or p['workspace'], '\n'.join([p['workspace'], contexts.get(pid, {}).get('context', ''), data.get('instructions', '')]), project_id=pid)
            for material in data.get('materials', []):
                add('material', material['id'], material['title'], material['text'] + '\n' + material['source'], project_id=pid)
        for p in state['pages'].values():
            if not p['deleted']:
                add('page', p['id'], p['title'], p['body'], project_id=p['project_id'], space_id=p['space_id'], version=p['version'])
        for p in state['spaces'].values():
            add('space', p['id'], p['title'], p['description'], project_ids=p['project_ids'])
        for row in rows:
            preview = row['_preview']; body = ''
            decoded = decode_record(preview.get('delivered'))
            if decoded is not None and preview.get('path') == row['artifact'].get('path') and hashlib.sha256(decoded).hexdigest() == row['artifact'].get('sha256') and is_text(decoded):
                body = decoded.decode('utf-8', errors='replace')
            add('artifact', row['content_id'], row['artifact'].get('path', row['artifact']['id']), body,
                project_id=row['project_id'], session_id=row['session_id'], source=row['source'], artifact_id=row['artifact']['id'])
        for log in logs:
            try:
                timeline = serve_history.read(log['path'], before=log['before'])
                if timeline['warning']:
                    warnings.append('部分会话正文不完整：' + timeline['warning'])
                add('session', log['session_id'], log['title'], log['title'], project_id=log['project_id'], session_id=log['session_id'])
                for message in timeline['messages']:
                    add('message', log['session_id'] + ':' + message['id'], log['title'], message['content'],
                        project_id=log['project_id'], session_id=log['session_id'], message_id=message['id'], role=message['role'])
            except (OSError, ValueError):
                warnings.append('有登记会话日志无法读取，正文搜索不完整')
            if used >= SEARCH_TOTAL_BYTES or len(documents) >= SEARCH_DOCUMENTS:
                truncated = True
                break
        if truncated:
            warnings.append('索引达到容量限制，结果不是全部历史')
        self.documents, self.warning, self.truncated = documents, '；'.join(dict.fromkeys(warnings)), truncated
        self.updated_at, self.revision = time.time(), state['revision']
        return self.status()

    def clear(self):
        self.documents = []
        self.updated_at = 0
        self.revision = -1
        self.warning = ''
        self.truncated = False

    def status(self):
        return {'indexed_at': self.updated_at, 'indexed_revision': self.revision, 'document_count': len(self.documents),
                'scope': self.scope, 'warning': self.warning, 'truncated': self.truncated}

    def search(self, query, kind='', pid='', cursor=0, limit=50):
        query = query.strip().casefold()
        if not query or len(query) > 200:
            raise ValueError('搜索词须为 1–200 字符')
        terms = query.split()
        matches = []
        for doc in self.documents:
            if kind and doc['kind'] != kind or pid and doc.get('project_id') != pid and pid not in doc.get('project_ids', []):
                continue
            haystack = doc['title'] + '\n' + doc['_text']
            if not all(term in haystack.casefold() for term in terms):
                continue
            at = max(0, haystack.casefold().find(terms[0]) - 70)
            matches.append({**{k: v for k, v in doc.items() if k != '_text'}, 'excerpt': haystack[at:at + 240]})
        end = cursor + limit
        return {**self.status(), 'results': matches[cursor:end], 'total': len(matches), 'next_cursor': end if end < len(matches) else None}


def install_content_routes(app, path: Path, token: str, root: Path | None = None, *, workspace_path: Path | None = None):
    from fastapi import HTTPException, Request
    from starlette.responses import JSONResponse
    store = ContentStore(path, write_guard=lambda: require_owner(app))
    index = SearchIndex()
    build_lock = asyncio.Lock()
    app.state.desktop_content_index = index

    async def clear_index():
        async with build_lock:
            index.clear()
            return index.status()

    app.state.clear_desktop_content_index = clear_index

    def authorize(request):
        if not secrets.compare_digest(request.headers.get('authorization', '').encode('utf-8'), ('Bearer ' + token).encode('utf-8')):
            raise HTTPException(401, '需要本机应用令牌')

    def response(value):
        return JSONResponse(value, headers={'Cache-Control': 'no-store'})

    def snapshot():
        try:
            state = store.public()
        except (OSError, ValueError):
            raise HTTPException(503, '内容库无法读取，原数据保留') from None
        return state, catalogue(app.state.sessions, root, WorkspaceState(workspace_path).read() if workspace_path else None)

    async def read(request):
        authorize(request)
        state, known = snapshot()
        return response({'state': state, 'catalogue': known, 'warning': '', 'limits': {'page_characters': 40000, 'versions': 100, 'materials': 30}})

    async def update(request):
        authorize(request)
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > 900000:
                raise HTTPException(413, '内容请求过大')
        try:
            body = json.loads(raw.decode('utf-8', errors='replace'))
            state, known = snapshot()
            rows, _, _ = library(app.state.sessions, state['favorites'])
            saved = store.update(body, project_ids={p['project_id'] for p in known['projects']}, artifact_ids={r['content_id'] for r in rows})
        except ContentConflict as exc:
            raise HTTPException(409, str(exc)) from None
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        except OwnershipUnavailable as exc:
            raise HTTPException(503, str(exc)) from None
        except OSError:
            raise HTTPException(503, '保存失败；原输入及原请求应保留核对') from None
        return response({**saved, 'catalogue': known, 'warning': ''})

    async def read_library(request):
        authorize(request)
        state, known = snapshot()
        rows, warning, truncated = library(app.state.sessions, state['favorites'])
        p = request.query_params
        selected = [r for r in rows if (not p.get('project_id') or r['project_id'] == p['project_id']) and (not p.get('type') or r['type'] == p['type']) and (p.get('favorite') != 'true' or r['favorite'])]
        return response({'artifacts': [{k: v for k, v in r.items() if not k.startswith('_')} for r in selected], 'truncated': truncated, 'warning': warning, 'catalogue': known})

    async def rebuild_index():
        async with build_lock:
            state, known = snapshot()
            rows, warning, _ = library(app.state.sessions, state['favorites'])
            logs = []
            total_log_bytes = 0
            for sid, session in list(app.state.sessions.items())[:200]:
                log = getattr(session.agent, 'session_log', None)
                path = getattr(log, 'path', None)
                if path is None:
                    warning += ' 部分会话没有持久日志，不能检索正文。'
                    continue
                try:
                    size = path.stat().st_size
                except OSError:
                    warning += ' 部分会话日志无法读取。'
                    continue
                current = session.in_flight if getattr(session, 'busy', False) else None
                size = min(size, current.get('log_offset', size)) if current else size
                if size > SEARCH_BYTES or total_log_bytes + size > SEARCH_TOTAL_BYTES:
                    warning += ' 部分会话超过单日志 2 MiB / 总日志 16 MiB 的索引范围，未索引该会话。'
                    continue
                total_log_bytes += size
                logs.append({'path': path, 'before': size, 'session_id': sid, 'project_id': session_project_id(session), 'title': getattr(session, 'title', '') or sid})
            if len(app.state.sessions) > 200:
                warning += ' 仅索引前 200 个已登记会话。'
            contexts = {}
            if workspace_path:
                try:
                    contexts = WorkspaceState(workspace_path).read()['projects']
                except (OSError, ValueError):
                    warning += ' 项目上下文无法读取，未覆盖原文件。'
            # Build off to the side. Cancelling a request does not stop its worker
            # thread; that thread must not repopulate the shared index after clear.
            replacement = SearchIndex()
            await asyncio.to_thread(replacement.build, state, known['projects'], rows, logs, contexts, warning)
            index.__dict__.update(replacement.__dict__)
            return index.status()

    async def rebuild(request):
        authorize(request)
        # Ephemeral derived index; this does not mutate any persisted source.
        return response(await rebuild_index())

    async def search(request):
        authorize(request)
        if not index.updated_at:
            await rebuild_index()
        try:
            p = request.query_params
            cursor, limit = int(p.get('cursor', '0')), int(p.get('limit', '50'))
            if cursor < 0 or not 1 <= limit <= 200:
                raise ValueError()
            state, _ = snapshot()
            value = index.search(p.get('q', ''), p.get('kind', ''), p.get('project_id', ''), cursor, limit)
            return response({**value, 'stale': index.revision != state['revision'], 'refresh_note': '索引是上次更新时的快照；新会话消息和成果需点击更新索引。'})
        except ValueError:
            raise HTTPException(422, '搜索词或分页参数不正确') from None

    async def export(request, page_id):
        authorize(request)
        state, _ = snapshot()
        page = state['pages'].get(page_id)
        if not page:
            raise HTTPException(404, '页面不存在')
        try:
            number = request.query_params.get('version')
            return response(export_page(page, int(number) if number else None))
        except ValueError:
            raise HTTPException(422, '页面版本不存在') from None

    async def files(request):
        authorize(request)
        from .content import project_files, project_file
        known = catalogue(app.state.sessions, root, WorkspaceState(workspace_path).read() if workspace_path else None)['projects']
        pid = request.query_params.get('project_id', '')
        project = next((p for p in known if p['project_id'] == pid), None)
        if project is None:
            raise HTTPException(404, '项目不属于当前工作台')
        listing = request.url.path.endswith('/files')
        folders = project.get('folders', [project['workspace']])
        folder = request.query_params.get('folder', project['workspace'])
        if not folders:
            if listing:
                return response({'project_id': pid, 'folder': '', 'directory': '', 'entries': [],
                                 'truncated': False, 'warning': '此项目尚未关联文件夹'})
            raise HTTPException(404, '此项目尚未关联文件夹')
        if folder not in folders:
            raise HTTPException(403, '文件夹不属于此项目')
        relative = request.query_params.get('directory' if listing else 'path', '')
        try:
            value = await asyncio.to_thread(project_files if listing else project_file, Path(folder), relative)
            return response({'project_id': pid, 'folder': folder, **value})
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        except OSError:
            raise HTTPException(404, '此文件或目录不存在，或无法安全读取') from None

    for route, handler, methods in [('/desktop/content', read, ['GET']), ('/desktop/content', update, ['POST']),
            ('/desktop/content/library', read_library, ['GET']), ('/desktop/content/search', search, ['GET']),
            ('/desktop/content/search/rebuild', rebuild, ['POST']), ('/desktop/content/pages/{page_id}/export', export, ['GET']), ('/desktop/content/files', files, ['GET']), ('/desktop/content/file', files, ['GET'])]:
        handler.__annotations__['request'] = Request
        if handler is export:
            handler.__annotations__['page_id'] = str
        app.add_api_route(route, handler, methods=methods, include_in_schema=False)
    return store
