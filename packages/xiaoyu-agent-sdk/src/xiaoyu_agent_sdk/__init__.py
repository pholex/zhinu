"""Stable host API for Xiaoyu's in-process workspace execution engine."""
from ._version import __version__ as __version__
from .session import AsyncSession as AsyncSession, Session as Session, run as run, run_async as run_async
from .session import list_sessions as list_sessions
from .types import Plugin as Plugin, McpServerStatus as McpServerStatus
from xiaoyu.rewind import RewindResult as RewindResult
from .types import (
    Allow as Allow, Approval as Approval, Approver as Approver, Deny as Deny,
    CloseTimeoutError as CloseTimeoutError, ConfigurationError as ConfigurationError,
    ExecutionError as ExecutionError, Hook as Hook, HookDecision as HookDecision,
    HookHandler as HookHandler, McpServer as McpServer, ModelOptions as ModelOptions,
    OutputSpec as OutputSpec, SDKError as SDKError, SessionBusyError as SessionBusyError,
    SessionClosedError as SessionClosedError, SessionOptions as SessionOptions,
    SessionStorageError as SessionStorageError, Subagent as Subagent, Tool as Tool,
    ToolHandler as ToolHandler, ToolResult as ToolResult,
)
from xiaoyu.embedding import RunCompleted as RunCompleted, RunResult as RunResult
from xiaoyu.events import (
    Notice as Notice, RequestEnded as RequestEnded, RequestStarted as RequestStarted,
    TextDelta as TextDelta, TextEnd as TextEnd, ToolCompleted as ToolCompleted,
    ToolDenied as ToolDenied, ToolPending as ToolPending, ToolRunning as ToolRunning,
    UIEvent as UIEvent,
)
from xiaoyu.output import OutputSchemaError as OutputSchemaError
from xiaoyu.session_log import SessionLockedError as SessionLockedError
from xiaoyu.session_log import SessionInfo as SessionInfo

__all__ = [
    "Plugin", "McpServerStatus", "RewindResult",
    "__version__", "Session", "AsyncSession", "run", "run_async", "ModelOptions",
    "SessionOptions", "OutputSpec", "Tool", "ToolResult", "ToolHandler", "Hook",
    "HookHandler", "HookDecision", "McpServer", "Subagent", "Approval", "Approver",
    "Allow", "Deny", "RunResult", "RunCompleted", "UIEvent", "Notice", "TextDelta",
    "TextEnd", "RequestStarted", "RequestEnded", "ToolPending", "ToolRunning",
    "ToolCompleted", "ToolDenied", "SDKError", "ConfigurationError", "ExecutionError",
    "CloseTimeoutError", "SessionBusyError", "SessionClosedError", "SessionStorageError",
    "SessionLockedError", "SessionInfo", "list_sessions", "OutputSchemaError",
]
