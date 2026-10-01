"""Run with python -I from an environment containing only the installed SDK."""
import importlib.metadata
import importlib.util
import tempfile
from pathlib import Path
from types import SimpleNamespace

import xiaoyu
import xiaoyu_agent_sdk as sdk

for package, namespace in (("xiaoyu-agent", "xiaoyu"), ("xiaoyu-agent-sdk", "xiaoyu_agent_sdk")):
    for member in importlib.metadata.files(package):
        parts = member.parts
        if not parts or parts[0] != namespace:
            continue
        assert not {"tests", "examples"}.intersection(parts), f"Development file installed: {member}"
        assert member.name not in {"testing.py", "tests.py", "conftest.py"}, f"Test helper installed: {member}"
        assert not member.name.startswith("test_") and not member.name.endswith("_test.py"), f"Test file installed: {member}"
assert importlib.util.find_spec("xiaoyu_agent_sdk.testing") is None, "Storage test helper installed"

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
    store = sdk.SQLiteSessionStore(Path(directory) / "sessions.sqlite")
    options = sdk.SessionOptions(model=sdk.ModelOptions("offline", client=Client()),
                                workspace=Path(directory), builtin_tools=(), session_store=store)
    with sdk.Session(options) as session:
        session.run("Persist 42", output=sdk.OutputSpec({"type": "integer"}))
        key = session.session_id
    with sdk.Session(options, resume_id=key) as restored:
        assert restored.session_id == key
        assert restored.run("Return 42 again", output=sdk.OutputSpec({"type": "integer"})).output_status == "valid"
    assert len(store.list_sessions()) == 1
    class PlainClient:
        def __init__(self):
            self.chat = SimpleNamespace(completions=self)

        def create(self, **kwargs):
            return iter([SimpleNamespace(choices=[SimpleNamespace(
                delta=SimpleNamespace(content="42", tool_calls=None), finish_reason="stop")],
                usage=SimpleNamespace(prompt_tokens=10, completion_tokens=1))])

    traces = []
    platform_options = sdk.SessionOptions(
        model=sdk.ModelOptions("offline", client=PlainClient()), workspace=Path(directory), builtin_tools=(),
        session_store=store, subagents=(sdk.Subagent("worker", "worker", "work", ()),),
        budget=sdk.BudgetOptions(max_requests=3), telemetry=sdk.TelemetryOptions(traces.append),
    )
    with sdk.McpPool() as pool:
        from dataclasses import replace
        with sdk.Session(replace(platform_options, mcp_pool=pool)) as session:
            handles = session.tasks.submit((sdk.TaskSpec("a", "worker", "Return 42"),
                sdk.TaskSpec("b", "worker", "Confirm the answer", depends_on=("a",))))
            assert all(h.wait(10).state == "succeeded" for h in handles)
            assert session.cost.requests == 2
            key = session.session_id
        assert pool.server_states() == {}
    with sdk.Session(platform_options, resume_id=key) as session:
        assert len(session.tasks.list()) == 2
        assert all(t.state == "succeeded" for t in session.tasks.list())
        assert session.cost.requests == 2
    assert len(traces) == 2
    assert sdk.MemoryTokenStore().load() is None
print("Installed SDK smoke passed", sdk.__version__)
