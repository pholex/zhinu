"""Run with python -I from an environment containing only the installed SDK."""
import importlib.metadata
import importlib.util
import tempfile
from pathlib import Path
from types import SimpleNamespace

import xiaoyu
import xiaoyu_agent_sdk as sdk

assert sdk.__version__ == xiaoyu.__version__
assert importlib.metadata.version("xiaoyu-agent-sdk") == sdk.__version__
for package in ("fastapi", "playwright", "prompt_toolkit", "rich"):
    assert importlib.util.find_spec(package) is None, f"Unselected extra installed: {package}"


class Client:
    def __init__(self):
        self.chat = SimpleNamespace(completions=self)

    def create(self, **kwargs):
        call = SimpleNamespace(index=0, id="result", function=SimpleNamespace(
            name="structured_output", arguments='{"value":42}'))
        return iter([SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=None, tool_calls=[call]))], usage=None)])


with tempfile.TemporaryDirectory() as directory:
    result = sdk.run("Return 42", sdk.SessionOptions(
        model=sdk.ModelOptions("offline", client=Client()), workspace=Path(directory), builtin_tools=(),
    ), output=sdk.OutputSpec({"type": "integer"}))
    assert (result.output, result.output_status) == (42, "valid")
    with sdk.Session(sdk.SessionOptions(
        model=sdk.ModelOptions("offline", client=Client()), workspace=Path(directory), builtin_tools=(),
    )) as session:
        assert session.mcp_status() == ()
        rewind = session.rewind(999)
        assert isinstance(rewind, sdk.RewindResult) and rewind.status == "unavailable"
    assert session.closed
    assert sdk.Subagent("worker", "worker", "work", (), isolation="worktree").isolation == "worktree"
    assert sdk.Plugin("entry", "package").distribution == "package"
print("Installed SDK smoke passed", sdk.__version__)
