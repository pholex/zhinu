"""Explicit host configuration and a tiny offline model for runnable examples."""
from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from xiaoyu_agent_sdk import ModelOptions, SessionOptions


def text_chunk(text: str) -> Any:
    return SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=text, tool_calls=None))], usage=None)


def tool_chunk(name: str, arguments: str) -> Any:
    call = SimpleNamespace(index=0, id="demo-call", function=SimpleNamespace(name=name, arguments=arguments))
    return SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=None, tool_calls=[call]))], usage=None)


class DemoClient:
    def __init__(self, script: list[list[Any]]) -> None:
        self.script = iter(script)
        self.chat = SimpleNamespace(completions=self)

    def create(self, **kwargs: Any) -> Any:
        return iter(next(self.script))


def options(demo: bool, script: list[list[Any]], workspace: Path) -> SessionOptions:
    model = ModelOptions("demo", client=DemoClient(script)) if demo else ModelOptions(
        model=os.environ["SDK_MODEL"], api_key=os.environ["SDK_API_KEY"],
        base_url=os.environ.get("SDK_BASE_URL", "https://api.openai.com/v1"),
    )
    return SessionOptions(model=model, workspace=workspace, builtin_tools=())
