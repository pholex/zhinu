"""Host-owned configuration. Sessions take defensive copies of mutable fields."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Literal, TYPE_CHECKING

if TYPE_CHECKING:
    from .questions import QuestionOptions
    from .storage import SessionStore
    from .mcp import McpPool
    from .oauth import OAuthClient
    from .budget import BudgetOptions
    from .telemetry import TelemetryOptions

from xiaoyu.agent import Allow, Deny
from xiaoyu.hooks import Decision as HookDecision

Approval = bool | str | tuple[bool, str] | Allow | Deny
Approver = Callable[[str, dict[str, Any]], Approval | Awaitable[Approval]]
Asker = Callable[[list[dict[str, Any]]], dict[str, str] | Awaitable[dict[str, str]]]
ToolHandler = Callable[..., Any]
HookHandler = Callable[[dict[str, Any]], HookDecision | Awaitable[HookDecision]]
SessionMode = Literal["default", "auto", "plan"]


@dataclass(frozen=True)
class ModelOptions:
    model: str
    base_url: str = "https://api.openai.com/v1"
    api_key: str = field(default="", repr=False)
    protocol: Literal["chat", "responses", "anthropic"] = "chat"
    request_timeout: float = 120.0
    # Borrowed OpenAI-compatible synchronous client; never closed by the SDK.
    client: Any = field(default=None, repr=False, compare=False)


@dataclass(frozen=True)
class OutputSpec:
    schema: dict[str, Any]
    max_retries: int = 2


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: ToolHandler = field(repr=False)
    requires_approval: bool = True


@dataclass(frozen=True)
class ToolResult:
    content: Any
    is_error: bool = False


@dataclass(frozen=True)
class ToolOutput:
    """Text after execution, before output clipping and persistence."""

    tool_name: str
    text: str
    is_error: bool
    session_id: str
    run_id: str
    task_id: str
    tool_call_id: str


ResultTransformHandler = Callable[[ToolOutput], str | Awaitable[str]]


@dataclass(frozen=True)
class ResultTransform:
    name: str
    callback: ResultTransformHandler = field(repr=False)
    tool_name: str = ""


@dataclass(frozen=True)
class Hook:
    event: Literal["PreToolUse", "PostToolUse", "UserPromptSubmit", "Stop", "SessionStart", "SessionEnd",
                   "SubagentStart", "SubagentEnd", "ToolFailed", "BeforeCompact", "AfterCompact"]
    callback: HookHandler = field(repr=False)
    tool_name: str = ""


@dataclass(frozen=True)
class McpServer:
    name: str
    command: str = ""
    args: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict, repr=False)
    url: str = ""
    headers: dict[str, str] = field(default_factory=dict, repr=False)
    timeout: float = 60.0
    oauth: OAuthClient | None = field(default=None, repr=False, compare=False)


@dataclass(frozen=True)
class Subagent:
    name: str
    description: str
    system_prompt: str
    tools: tuple[str, ...]
    model: str = ""
    max_iterations: int = 12
    isolation: Literal["none", "worktree"] = "none"
    mcp_servers: tuple[str, ...] = ()


@dataclass(frozen=True)
class McpServerStatus:
    name: str
    state: str


@dataclass(frozen=True)
class Plugin:
    """An explicitly selected installed xiaoyu.tools entry point."""

    name: str
    distribution: str


@dataclass(frozen=True)
class SessionOptions:
    model: ModelOptions
    workspace: Path
    system_prompt: str | None = None
    builtin_tools: tuple[str, ...] | None = None
    tools: tuple[Tool, ...] = ()
    hooks: tuple[Hook, ...] = ()
    mcp_servers: tuple[McpServer, ...] = ()
    subagents: tuple[Subagent, ...] = ()
    approver: Approver | None = field(default=None, repr=False)
    approval_timeout: float = 120.0
    close_timeout: float = 10.0
    max_iterations: int = 50
    budget_tokens: int | None = None
    load_project_instructions: bool = False
    skill_directories: tuple[Path, ...] = ()
    session_dir: Path | None = None
    # Environment additions apply to child tools, never to os.environ.
    tool_env: dict[str, str] = field(default_factory=dict, repr=False)
    deny_rules: tuple[str, ...] = ()
    event_buffer_size: int = 128
    plugins: tuple[Plugin, ...] = ()
    session_store: SessionStore | None = field(default=None, repr=False, compare=False)
    mcp_pool: McpPool | None = field(default=None, repr=False, compare=False)
    max_parallel_tasks: int = 4
    budget: BudgetOptions | None = None
    telemetry: TelemetryOptions | None = None
    asker: Asker | None = field(default=None, repr=False)
    question_timeout: float = 120.0
    mode: SessionMode = "default"
    enable_plan: bool = False
    result_transforms: tuple[ResultTransform, ...] = ()
    result_transform_timeout: float = 30.0
    questions: QuestionOptions | None = None


class SDKError(Exception):
    """Base class for host-facing SDK errors."""


class ConfigurationError(SDKError, ValueError):
    pass


class SessionClosedError(SDKError):
    pass


class SessionBusyError(SDKError):
    pass


class CloseTimeoutError(SDKError, TimeoutError):
    """Work is still settling; call close again before reusing its resources."""


class ExecutionError(SDKError):
    """Model/engine failure. Original exception is available as __cause__."""


class ResultTransformError(ExecutionError):
    """Tool executed, but its text was withheld; never retry the action blindly."""


class SessionStorageError(SDKError):
    pass


class McpManagementError(SDKError):
    pass


class OAuthError(SDKError):
    pass
