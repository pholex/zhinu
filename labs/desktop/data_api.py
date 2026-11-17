"""Actual usage and explicit, metadata-only desktop data management."""
from __future__ import annotations
import asyncio
import copy
import json
import os
from pathlib import Path
import secrets
import stat
import tempfile
import threading

from .app_ownership import require_owner

BACKUP_SCHEMA = 'yuan-metadata-v1'
MAX_BYTES = 3 * 1024 * 1024


def usage_snapshot(sessions):
    rows, by_model = [], {}
    for sid, session in list(sessions.items()):
        usage = session.agent.usage.to_dict()
        rows.append({'session_id': sid, 'title': session.title, 'usage': usage,
                     'history_complete': not bool(getattr(session, 'history_warning', ''))})
        for model, entry in usage.get('by_model', {}).items():
            total = by_model.setdefault(model, {'calls': 0, 'prompt_tokens': 0, 'completion_tokens': 0})
            for field in total:
                number = entry.get(field, 0)
                if type(number) is int and number >= 0:
                    total[field] += number
    return {'sessions': rows, 'by_model': by_model,
            'totals': {field: sum(row[field] for row in by_model.values()) for field in ('calls','prompt_tokens','completion_tokens')},
            'scope': 'registered_session_counters', 'cost_available': False,
            'warning': '仅汇总当前服务已登记会话的实际计数；不包括未登记历史，不推算账单。模型未返回的用量无法补计。'}


def occupancy(paths):
    rows = []
    for name, root in paths.items():
        total = count = 0; truncated = False; warnings = []
        pending = [Path(root)]; visited = 0
        while pending:
            path = pending.pop(); visited += 1
            if visited > 10000:
                truncated = True; break
            try:
                info = path.lstat()
                if stat.S_ISLNK(info.st_mode):
                    warnings.append('符号链接未跟随'); continue
                if stat.S_ISREG(info.st_mode):
                    total += info.st_size; count += 1
                elif stat.S_ISDIR(info.st_mode):
                    with os.scandir(path) as iterator:
                        for entry in iterator:
                            pending.append(Path(entry.path))
                            if len(pending) + visited > 10000:
                                truncated = True; break
            except FileNotFoundError:
                pass
            except OSError:
                warnings.append('部分路径无法读取')
        rows.append({'id': name, 'bytes': total, 'files': count, 'truncated': truncated,
                     'warning': '；'.join(dict.fromkeys(warnings))})
    return rows


class WorkspaceBackup:
    def __init__(self, path, *, write_guard):
        from .workspace import WorkspaceState
        self.path = Path(path)
        self.store = WorkspaceState(self.path, write_guard=write_guard)
        self.write_guard = write_guard
        self.lock = threading.Lock()

    def read(self):
        return self.store.read()

    def restore(self, revision, value):
        from .workspace import validate, Conflict
        if not isinstance(value, dict) or set(value) != {'projects', 'sessions'}:
            raise ValueError('项目备份字段不正确')
        candidate = validate({'revision': 0, **copy.deepcopy(value)})
        with self.lock, self.store.lock:
            old = self.store._read()
            if type(revision) is not int or revision != old['revision']:
                raise Conflict()
            saved = {**candidate, 'revision': revision + 1}
            raw = json.dumps(saved, ensure_ascii=False).encode('utf-8')
            from .workspace import MAX_BYTES as WORKSPACE_LIMIT
            if len(raw) > WORKSPACE_LIMIT:
                raise ValueError('项目元数据超过容量限制')
            self.write_guard()
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd, temporary = tempfile.mkstemp(prefix='.workspace-restore-', dir=self.path.parent)
            try:
                with os.fdopen(fd, 'wb') as stream:
                    self.write_guard(); stream.write(raw); stream.flush(); os.fsync(stream.fileno())
                # Ordinary project routes may use another Store instance.
                if self.store._read()['revision'] != revision:
                    raise Conflict()
                self.write_guard(); os.replace(temporary, self.path)
            finally:
                if os.path.exists(temporary): os.unlink(temporary)
            return saved


def install_data_routes(app, token, *, workspace_path, managed_paths=None):
    from fastapi import HTTPException, Request
    from starlette.responses import JSONResponse
    from .preferences import PreferenceConflict, validate_values
    from .workspace import Conflict, validate as validate_workspace
    workspace = WorkspaceBackup(workspace_path, write_guard=lambda: require_owner(app))

    def authorize(request):
        if not secrets.compare_digest(request.headers.get('authorization','').encode(), ('Bearer '+token).encode()):
            raise HTTPException(401,'需要本机应用令牌')

    def operation(callback):
        try: return callback()
        except (PreferenceConflict, Conflict): raise HTTPException(409,'元数据已变化，请重新读取并核对') from None
        except OSError: raise HTTPException(503,'无法可靠读取或保存元数据；原文件已保留') from None
        except ValueError as error: raise HTTPException(422,str(error)) from None

    def response(data): return JSONResponse(data,headers={'Cache-Control':'no-store'})

    def stores(kind):
        if kind == 'preferences':
            store = getattr(app.state,'preferences',None)
            if store is None: raise HTTPException(503,'偏好服务未装配')
            return store
        if kind == 'workspace': return workspace
        raise HTTPException(422,'仅支持 preferences 或 workspace 元数据')

    async def body(request):
        authorize(request); raw=bytearray()
        async for part in request.stream():
            raw.extend(part)
            if len(raw)>MAX_BYTES: raise HTTPException(413,'备份超过容量限制')
        try:
            data=json.loads(raw.decode('utf-8',errors='replace'))
            if not isinstance(data,dict): raise ValueError()
            return data
        except ValueError: raise HTTPException(422,'备份不是有效 JSON 对象') from None

    async def usage(request):
        authorize(request)
        return response(usage_snapshot(getattr(app.state,'sessions',{})))

    async def data_status(request):
        authorize(request)
        index=getattr(app.state,'desktop_content_index',None)
        return response({'storage':await asyncio.to_thread(occupancy,managed_paths or {}),
            'index':index.status() if index else None,'can_clear_index':callable(getattr(app.state,'clear_desktop_content_index',None)),
            'revisions':{k:operation(stores(k).read)['revision'] for k in ('preferences','workspace')},
            'backup_scope':'偏好与项目元数据分别备份；不包含会话正文、执行回执、页面正文、成果文件或凭据。'})

    async def backup(request):
        authorize(request); kind=request.query_params.get('kind','preferences'); state=operation(stores(kind).read)
        values=state['values'] if kind=='preferences' else {k:state[k] for k in ('projects','sessions')}
        value={'schema':BACKUP_SCHEMA,'kind':kind,'values':values}
        return response({'filename':'yuan-'+kind+'-metadata.json','mime_type':'application/json',
                         'content':json.dumps(value,ensure_ascii=False,indent=2),
                         'scope':'应用元数据；可能含个人分组和项目说明，请自行选择保存位置。'})

    async def restore(request):
        data=await body(request)
        if set(data)!={'kind','revision','backup'}:raise HTTPException(422,'导入请求字段不正确')
        value=data['backup']; kind=data['kind']; store=stores(kind)
        if (not isinstance(value,dict) or set(value)!={'schema','kind','values'}
                or value['schema']!=BACKUP_SCHEMA or value['kind']!=kind):
            raise HTTPException(422,'备份格式或组件不匹配')
        if kind=='preferences':
            values=operation(lambda:validate_values(value['values']))
            # Restoration goes through the same strict schema and current revision; never restores grants.
            saved=operation(lambda:store.save(data['revision'],values))
        else:
            values=value['values']
            if not isinstance(values,dict) or set(values)!={'projects','sessions'}:raise HTTPException(422,'项目备份字段不正确')
            operation(lambda:validate_workspace({'revision':0,**values}))
            saved=operation(lambda:store.restore(data['revision'],values))
        return response({'kind':kind,'revision':saved['revision'],'restored':True,
                         'note':'已恢复所选元数据；执行、授权和会话历史没有导入。'})

    async def clear_index(request):
        data=await body(request)
        if data:raise HTTPException(422,'清理索引不接受路径参数')
        operation(lambda:require_owner(app))
        callback=getattr(app.state,'clear_desktop_content_index',None)
        if callback is None:raise HTTPException(503,'当前服务未提供可重建搜索索引')
        result=await callback()
        return response({'cleared':True,'index':result,'note':'仅清理内存搜索索引，原始会话、页面与成果保持原样。'})

    for endpoint,route,method in ((usage,'/desktop/usage','GET'),(data_status,'/desktop/data','GET'),
        (backup,'/desktop/data/backup','GET'),(restore,'/desktop/data/import','POST'),(clear_index,'/desktop/data/clear-index','POST')):
        endpoint.__annotations__['request']=Request
        app.add_api_route(route,endpoint,methods=[method],include_in_schema=False)
    return workspace
