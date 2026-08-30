"""小羽 (Xiaoyu) — a harness coding agent.

Weaving code, connecting dots, and showing you the best harness architecture.

顶层导出即公开 API（嵌入面契约）
--------------------------------
`xiaoyu.__all__` 里的名字是对宿主承诺稳定的公开面：按 semver 维护，破坏性
变更先经一个小版本的弃用期（见 docs/embedding.md「稳定性承诺」）。其余模块
（`xiaoyu.agent` / `xiaoyu.tools` / … 的内部路径）随时可能重构，宿主别吃。

导出是**懒**的（PEP 562 `__getattr__`）：`import xiaoyu` 只定义 `__version__`
与名字表，不拖起 openai/anthropic SDK 与整个内核——包内模块用
`from . import __version__` 拿版本号，若这里急切 import agent 会形成循环；
CLI 启动与 `--version` 也不该为一个版本号付整包 import 的代价。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

__version__ = "0.46.0"

#  公开名 → 所在模块。改这张表 = 改公开契约：加名字要同步 docs/embedding.md
#  的清单（tests/test_public_api.py 锁双向一致），删名字要走弃用期。
_EXPORTS: dict[str, str] = {
    #  内核对象
    "Agent": "xiaoyu.agent",
    "AsyncAgent": "xiaoyu.embedding",
    "Config": "xiaoyu.config",
    "Toolbox": "xiaoyu.tools",
    #  审批 / 提问契约
    "Allow": "xiaoyu.agent",
    "Deny": "xiaoyu.agent",
    "Approver": "xiaoyu.agent",
    "Asker": "xiaoyu.agent",
    "AsyncApprover": "xiaoyu.embedding",
    "normalize_verdict": "xiaoyu.agent",
    #  单轮结算
    "RunResult": "xiaoyu.embedding",
    "RunCompleted": "xiaoyu.embedding",
    "measured_send": "xiaoyu.embedding",
    "Interrupted": "xiaoyu.agent",
    #  事件流（与 --output-format stream-json / --wire 同一套词汇）
    "UIEvent": "xiaoyu.events",
    "UISink": "xiaoyu.events",
    "RequestStarted": "xiaoyu.events",
    "RequestEnded": "xiaoyu.events",
    "TextDelta": "xiaoyu.events",
    "TextEnd": "xiaoyu.events",
    "ToolPending": "xiaoyu.events",
    "ToolPurpose": "xiaoyu.events",
    "ToolRunning": "xiaoyu.events",
    "ToolCompleted": "xiaoyu.events",
    "ToolDenied": "xiaoyu.events",
    "SteerAccepted": "xiaoyu.events",
    "PlanUpdated": "xiaoyu.events",
    "Notice": "xiaoyu.events",
    #  会话落盘 / resume
    "SessionLog": "xiaoyu.session_log",
    "load_messages": "xiaoyu.session_log",
    "list_sessions": "xiaoyu.session_log",
    #  权限规则
    "Permissions": "xiaoyu.permissions",
    "Rule": "xiaoyu.permissions",
    "parse_rule": "xiaoyu.permissions",
    "suggest_allow_rule": "xiaoyu.permissions",
    "banned_allow_reason": "xiaoyu.permissions",
    "user_rules_path": "xiaoyu.permissions",
    "workspace_rules_path": "xiaoyu.permissions",
    #  配置：环境加载与错误
    "load_dotenv": "xiaoyu.config",
    "user_env_path": "xiaoyu.config",
    "MissingConfig": "xiaoyu.config",
    "MissingApiKey": "xiaoyu.config",
}

__all__ = ["__version__", *_EXPORTS]


def __getattr__(name: str):
    module_name = _EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module 'xiaoyu' has no attribute {name!r}")
    import importlib

    value = getattr(importlib.import_module(module_name), name)
    #  缓存进模块字典：下次直接命中，且 `xiaoyu.Agent is xiaoyu.Agent`
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_EXPORTS))


if TYPE_CHECKING:  # 给类型检查器 / IDE 看的静态视图，运行期不执行
    from .agent import (  # noqa: F401
        Agent,
        Allow,
        Approver,
        Asker,
        Deny,
        Interrupted,
        normalize_verdict,
    )
    from .config import (  # noqa: F401
        Config,
        MissingApiKey,
        MissingConfig,
        load_dotenv,
        user_env_path,
    )
    from .embedding import (  # noqa: F401
        AsyncAgent,
        AsyncApprover,
        RunCompleted,
        RunResult,
        measured_send,
    )
    from .events import (  # noqa: F401
        Notice,
        PlanUpdated,
        RequestEnded,
        RequestStarted,
        SteerAccepted,
        TextDelta,
        TextEnd,
        ToolCompleted,
        ToolDenied,
        ToolPending,
        ToolPurpose,
        ToolRunning,
        UIEvent,
        UISink,
    )
    from .permissions import (  # noqa: F401
        Permissions,
        Rule,
        banned_allow_reason,
        parse_rule,
        suggest_allow_rule,
        user_rules_path,
        workspace_rules_path,
    )
    from .session_log import SessionLog, list_sessions, load_messages  # noqa: F401
    from .tools import Toolbox  # noqa: F401
