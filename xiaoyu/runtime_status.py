"""当前会话的元数据快照；不探测工具、不读取配置、不刷新连接。"""
from __future__ import annotations

import json
from typing import Any

SECTIONS = ('all', 'mcp', 'tools', 'skills', 'models', 'workspace')


def snapshot(agent: Any, section: str = 'all', offset: int = 0,
             limit: int = 50, **extra: Any) -> str:
    if extra or not isinstance(section, str) or section not in SECTIONS:
        raise ValueError('section 必须是 all / mcp / tools / skills / models / workspace，不接受其他参数')
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError('offset 必须是非负整数，limit 必须是 1–100 的整数')

    def page(items: list[Any]) -> dict[str, Any]:
        end = offset + limit
        return {'items': items[offset:end], 'total': len(items),
                'next_offset': end if end < len(items) else None}

    result: dict[str, Any] = {
        'scope': 'current_session',
        'source': 'in_memory_snapshot',
    }
    if section in ('all', 'mcp', 'tools'):
        inventory = agent.toolbox.runtime_inventory()
        if section in ('all', 'mcp'):
            result['mcp'] = page(inventory['servers'])
            result['mcp_status_note'] = (
                '状态为最近已知状态，未做实时探测；ready 表示已完成连接，'
                'cached 表示仅有缓存声明、尚未启动连接；注册不代表调用免审批。'
            )
        if section in ('all', 'tools'):
            result['tools'] = page(inventory['tools'])
            # 本次请求真正发出的 schema；不调用 schemas()/available()，避免隐式探测。
            step = agent._step
            visible = sorted({s['function']['name'] for s in step.schemas}) if step else None
            result['model_visible_tools'] = page(visible) if visible is not None else None
            result['tool_exposure'] = inventory['exposure']
            result['tools_note'] = '注册清单未重新检查可用性；model_visible_tools 是本次模型请求实际收到的工具名。'
    if section in ('all', 'skills'):
        result['skills'] = page([
            {'name': s.name, 'plugin': s.plugin, 'body_loaded': s.name in agent._loaded_skills}
            for s in agent.skills
        ])
        result['skills_note'] = '清单来自当前会话技能索引；body_loaded 仅表示正文已载入，不表示执行过。'
    if section in ('all', 'models'):
        result['current_model'] = agent.config.model
        # listing()/resolve() 会懒构造网络客户端；这里只需声明的名称，不解析路由。
        names = dict.fromkeys(model for provider in agent.registry.providers for model in provider.models)
        result['models'] = page(list(names))
        result['models_note'] = '仅枚举配置中显式声明的模型，不代表联网验证通过；通配路由的模型无法完整枚举。'
    if section in ('all', 'workspace'):
        result['workspace'] = str(agent.config.workspace)
        result['permission_mode'] = agent.mode
        result['auto_approve'] = agent.config.auto_approve
    return json.dumps(result, ensure_ascii=False)
