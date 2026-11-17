"""Authenticated application settings and truthful capability inventory."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
from pathlib import Path
import secrets
import time
from zoneinfo import available_timezones

from .preferences import PreferenceStore, PreferenceConflict, MAX_BYTES


def install_preferences_routes(app, path, token, *, root, origin=None, skill_settings_path=None):
    from fastapi import HTTPException, Request
    from starlette.responses import JSONResponse
    from .app_ownership import require_owner
    store = PreferenceStore(Path(path), write_guard=lambda: require_owner(app))
    app.state.preferences = store
    from .skill_settings import SkillSettings
    skill_settings = SkillSettings(skill_settings_path or Path(path).with_name('skill-settings.json'), write_guard=lambda: require_owner(app))
    app.state.skill_settings = skill_settings
    app.state.serve_config.skill_policy = skill_settings.disabled_names

    def apply_skill_settings(session):
        disabled = skill_settings.disabled_names()
        if disabled != session.agent.config.disabled_skill_names:
            previous = session.agent.config.disabled_skill_names
            session.agent.config.disabled_skill_names = disabled
            try:
                return session.agent.reload_skills()
            except Exception:
                session.agent.config.disabled_skill_names = previous
                raise
        return [], []

    from .provider_settings import ProviderSettings
    providers = ProviderSettings(write_guard=lambda: require_owner(app))

    def authorize(request):
        if not secrets.compare_digest(request.headers.get('authorization', '').encode(), ('Bearer ' + token).encode()):
            raise HTTPException(401, '需要本机应用令牌')

    def session_of(sid):
        session = getattr(app.state, 'sessions', {}).get(sid)
        if session is None:
            raise HTTPException(404, '会话不存在')
        return session

    def config_registry(session=None):
        if session is not None:
            return session.agent.config, session.agent.registry
        from xiaoyu.config import Config, MissingConfig
        from xiaoyu.providers import build
        cfg = getattr(app.state, 'serve_config', None)
        config = Config.from_env(workspace=Path(root), model=getattr(cfg, 'model', None) or None,
                                 mode=getattr(cfg, 'mode', None) or None,
                                 base_url=getattr(cfg, 'base_url', None) or None)
        try:
            return config, build(config, discover=False)
        except MissingConfig:
            return config, None

    def decorated(state):
        from xiaoyu.modes import MODES
        options = {'models': [], 'modes': [{'id': m.name, 'label': m.label, 'description': m.desc} for m in MODES],
                   'timezones': sorted(available_timezones())}
        values, warning = state['values'], ''
        model, mode = '', 'default'
        try:
            config, registry = config_registry()
            model, mode = config.model, config.mode
            names = {row.model: row.owner_label for row in registry.listing()} if registry else {}
            names.setdefault(model, '当前服务配置')
            options['models'] = [{'id': name, 'source': source} for name, source in names.items()]
            if registry is None:
                warning = '没有可枚举的已配置模型路由；仅显示默认配置值，未执行网络发现。'
            elif values['default_model']:
                registry.resolve(values['default_model'])
        except Exception:
            warning = '部分模型配置无法解析；请检查核心配置，未触发模型发现或网络请求。'
        service = getattr(app.state, 'serve_config', None)
        return {**state, 'options': options, 'effective': {
            'model': values['default_model'] or model, 'mode': values['default_mode'] or mode,
            'model_source': 'desktop_preferences' if values['default_model'] else ('service_override' if getattr(service, 'model', None) else 'core_configuration'),
            'mode_source': 'desktop_preferences' if values['default_mode'] else ('service_override' if getattr(service, 'mode', None) else 'core_configuration')},
            'scope': 'new_sessions', 'write_target': 'desktop_preferences', 'warning': warning}

    def response(value):
        return JSONResponse(value, headers={'Cache-Control': 'no-store'})

    def operation(callback):
        try:
            return callback()
        except PreferenceConflict:
            raise HTTPException(409, '偏好已变化，请刷新后核对') from None
        except OSError:
            raise HTTPException(503, '偏好无法可靠保存或读取；原记录已保留') from None
        except ValueError as error:
            raise HTTPException(422, str(error)) from None

    async def body(request):
        authorize(request)
        data = bytearray()
        async for part in request.stream():
            data.extend(part)
            if len(data) > MAX_BYTES:
                raise HTTPException(413, '请求过大')
        try:
            result = json.loads(data.decode('utf-8', errors='replace'))
            if not isinstance(result, dict):
                raise ValueError()
            return result
        except ValueError:
            raise HTTPException(422, '请求格式不正确') from None

    async def preferences(request):
        authorize(request)
        return response(await asyncio.to_thread(lambda: decorated(operation(store.read))))

    async def save_preferences(request):
        data = await body(request)
        if set(data) != {'revision', 'values'}:
            raise HTTPException(422, '偏好请求字段不正确')
        from .preferences import validate_values
        values = operation(lambda: validate_values(data['values']))
        if values['default_model']:
            try:
                _, registry = config_registry()
                registry.resolve(values['default_model'])
            except Exception:
                raise HTTPException(422, '默认模型没有可用的已配置路由') from None
        saved = operation(lambda: store.save(data['revision'], values))
        return response(await asyncio.to_thread(decorated, saved))

    def discover_skills():
        from xiaoyu import skills
        from xiaoyu.config import Config
        # Read the core's already established environment; never load project .env here.
        enabled = Config.from_env(workspace=Path(root)).enable_skills
        sources = [{'path': str(source.directory), 'kind': 'plugin' if source.plugin else 'user',
                    'plugin': source.plugin} for source in skills.skill_sources()]
        found = skills.scan_skills(include_disabled=True)
        summary = {'status': 'ready' if enabled else 'disabled', 'enabled': enabled,
                   'count': len(found), 'sources': sources, 'observed_at': time.time(),
                   'disabled_patterns': list(skills.disabled_patterns()), 'warning': ''}
        return found, summary

    async def discover_at_startup():
        try:
            _, summary = await asyncio.to_thread(discover_skills)
        except Exception:
            summary = {'status': 'error', 'enabled': None, 'count': 0, 'sources': [],
                       'observed_at': time.time(), 'disabled_patterns': [],
                       'warning': '启动技能发现未完成；请在能力中心重新读取并核对核心配置。'}
        app.state.desktop_skill_discovery = summary

    original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(application):
        async with original_lifespan(application):
            await discover_at_startup()
            yield

    app.router.lifespan_context = lifespan

    def inventory(sid):
        from xiaoyu import skills, plugins
        session = session_of(sid) if sid else None
        warnings, rows, plugin_rows = [], [], []
        discovered, discovery = discover_skills()
        policy = skill_settings.read()
        if session is not None:
            loaded = skills.scan_skills(session.agent._skills_workspace(), directories=session.agent.config.skill_directories, include_disabled=True)
            installed = {item.name: item for item in loaded}
            for item in session.agent.skills:
                installed.setdefault(item.name, item)
            loaded = list(installed.values())
            enabled = session.agent.config.enable_skills
        else:
            # No session exists yet: list installed user sources without trusting a project.
            loaded = discovered
            enabled = discovery['enabled']
        live = {item.name for item in session.agent.skills} if session else set()
        for skill in loaded[:1000]:
            locked = not enabled or skills.is_disabled(skill.name, skill.path.parent.name)
            active = not locked and skill.name not in policy['disabled']
            rows.append({'id': skill.name, 'name': skill.name, 'description': skill.description[:2000],
                         'source': 'plugin' if skill.plugin else ('project' if skill.project else 'user'),
                         'plugin': skill.plugin, 'loaded': skill.name in live, 'directory': str(skill.path.parent),
                         'enabled': active, 'toggle_locked': locked,
                         'when_not': skill.when_not[:1000]})
        if len(loaded) > 1000:
            warnings.append('技能列表仅展示前 1000 项')
        for name, directory in plugins.installed_dirs()[:200]:
            try:
                bundle = plugins.inspect_bundle(directory, fallback_name=name)
                plugin_rows.append({'id': name, 'name': bundle.name, 'version': bundle.version,
                    'description': bundle.description[:2000], 'skills': list(bundle.skills)[:200],
                    'connections': list(bundle.mcp_servers)[:200], 'notes': list(bundle.notes)[:20]})
            except (OSError, ValueError, plugins.PluginError):
                warnings.append('一个已安装插件无法读取，未宣称其能力可用')
        return {'session_id': sid or None, 'skills': rows, 'skills_enabled': enabled, 'skill_settings': policy,
                'plugins': plugin_rows, 'warning': ' '.join(warnings),
                'discovery': discovery,
                'startup_discovery': getattr(app.state, 'desktop_skill_discovery', None),
                'scope': 'session_loaded' if session else 'installed_user_sources',
                'management': {'reload_skills': session is not None,
                    'install': {'method': 'core_cli', 'command': 'xiaoyu plugin add <来源>',
                                'note': '在终端审阅并安装插件后，回到空闲会话重新加载技能；连接配置在连接管理中单独确认。'}},
                'workflows': [{'id': key, 'label': label, 'kind': kind} for key, label, kind in (
                    ('chat', '对话', 'task'), ('create', '创作', 'task'), ('code', '工程', 'task'),
                    ('weave', '批量执行', 'qixiang'), ('contest', '方案比选', 'douqiao'),
                    ('fleet', '统筹协作', 'chenshu'))]}

    async def provider_list(request):
        authorize(request)
        return response(operation(providers.read))

    async def provider_save(request):
        data = await body(request)
        if set(data) != {'revision', 'provider'}:
            raise HTTPException(422, '服务商请求字段不正确')
        return response(operation(lambda: providers.save(data['revision'], data['provider'])))

    async def capabilities(request):
        authorize(request)
        return response(await asyncio.to_thread(lambda: operation(lambda: inventory(request.query_params.get('session_id', '')))))

    async def reload_skills(request, session_id):
        data = await body(request)
        if data:
            raise HTTPException(422, '重新加载不接受额外配置')
        if request.headers.get('X-Xiaoyu-Instance') != getattr(app.state, 'instance_id', None):
            raise HTTPException(409, '服务实例已变化，请刷新后重新确认')
        from .connections_api import _managed
        from .connections import ConnectionConflict
        session = session_of(session_id)
        try:
            def reload():
                session.agent.config.disabled_skill_names = skill_settings.disabled_names()
                return session.agent.reload_skills()
            added, removed = await _managed(session, reload)
        except ConnectionConflict as error:
            raise HTTPException(409, str(error)) from None
        return response({'added': added, 'removed': removed, 'scope': 'selected_session',
                         'note': '技能索引已重新加载；没有授予任何工具权限。'})

    async def save_skill_settings(request):
        data = await body(request)
        if set(data) != {'revision', 'ids', 'enabled', 'session_id'} or not isinstance(data['session_id'], str):
            raise HTTPException(422, '技能开关请求字段不正确')
        if request.headers.get('X-Xiaoyu-Instance') != getattr(app.state, 'instance_id', None):
            raise HTTPException(409, '服务实例已变化，请刷新后重试')
        from .skill_settings import names
        ids = operation(lambda: names(data['ids']))
        all_rows = inventory('')['skills']
        if data['session_id']:
            all_rows += inventory(data['session_id'])['skills']
        allowed = {s['id'] for s in all_rows if not s['toggle_locked']}
        if not ids or not set(ids) <= allowed:
            raise HTTPException(422, '技能不存在或已被核心配置停用，请刷新后核对')
        saved = operation(lambda: skill_settings.save(data['revision'], ids, data['enabled']))
        from .connections_api import _managed
        from .connections import ConnectionConflict
        updated, pending = 0, 0
        for session in list(app.state.sessions.values()):
            try:
                await _managed(session, lambda session=session: apply_skill_settings(session))
                updated += 1
            except (ConnectionConflict, OSError, ValueError, HTTPException):
                pending += 1
        return response({'skill_settings': saved, 'updated_sessions': updated, 'pending_sessions': pending,
                         'note': '设置已保存。' + ('执行中的对话将在下一轮应用。' if pending else '空闲对话已更新。')})

    async def select_model(request, session_id):
        data = await body(request)
        require_owner(app)
        if request.headers.get('X-Xiaoyu-Instance') != getattr(app.state, 'instance_id', None):
            raise HTTPException(409, '服务实例已变化，请刷新后重新确认')
        if set(data) != {'model', 'previous'} or any(not isinstance(data[k], str) or not data[k] or len(data[k]) > 256 for k in data):
            raise HTTPException(422, '模型选择格式不正确')
        return response(app.state.select_session_model(session_id, data['model'], data['previous']))

    async def rename(request, session_id):
        data = await body(request)
        title = data.get('title')
        if set(data) != {'title'} or not isinstance(title, str) or not title.strip() or len(title) > 160:
            raise HTTPException(422, '标题须为 1–160 字符')
        callback = getattr(app.state, 'rename_session', None)
        if callback is None:
            raise HTTPException(503, '当前服务不支持保存会话标题')
        return response(callback(session_id, title.strip()))

    async def export(request, session_id):
        authorize(request)
        from xiaoyu import serve_history
        session = session_of(session_id)
        if session.busy:
            raise HTTPException(409, '请在当前轮结束后导出完整记录')
        log = session.agent.session_log
        try:
            before = log.path.stat().st_size if log else None
        except OSError:
            raise HTTPException(503, '历史文件不可读取') from None
        if before is not None and before > 16 * 1024 * 1024:
            raise HTTPException(413, '历史超过 16 MiB，请使用核心会话导出命令')
        title = session.title
        data = await asyncio.to_thread(serve_history.read, log.path if log else None, before=before)
        fmt = request.query_params.get('format', 'markdown')
        if fmt == 'json':
            content = json.dumps({'title': title, 'messages': data['messages'], 'warning': data['warning']}, ensure_ascii=False, indent=2)
            mime, suffix = 'application/json', 'json'
        elif fmt == 'markdown':
            content = '# ' + title.replace('\n', ' ') + '\n\n' + '\n\n'.join(
                '## ' + ('用户' if m['role'] == 'user' else '助手') + '\n\n' + m['content'] for m in data['messages'])
            mime, suffix = 'text/markdown', 'md'
        else:
            raise HTTPException(422, '仅支持 markdown 或 json')
        return response({'filename': 'session-' + session.id + '.' + suffix, 'mime_type': mime,
                         'content': content, 'warning': data['warning']})

    for endpoint, route, method in (
        (preferences, '/desktop/preferences', 'GET'), (save_preferences, '/desktop/preferences', 'POST'),
        (capabilities, '/desktop/capabilities', 'GET'),
        (save_skill_settings, '/desktop/skills/settings', 'POST'),
        (provider_list, '/desktop/providers', 'GET'), (provider_save, '/desktop/providers', 'POST'),
        (reload_skills, '/desktop/sessions/{session_id}/skills/reload', 'POST'),
        (rename, '/desktop/sessions/{session_id}/title', 'POST'),
        (select_model, '/desktop/sessions/{session_id}/model', 'POST'),
        (export, '/desktop/sessions/{session_id}/export', 'GET')):
        endpoint.__annotations__['request'] = Request
        app.add_api_route(route, endpoint, methods=[method], include_in_schema=False)
    return store
