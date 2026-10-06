# Xiaoyu Agent SDK

Python 3.11+ host API for the Xiaoyu workspace execution engine. The SDK runs in
your process and depends on the exact same version of `xiaoyu-agent`.

## Installation

```sh
pip install xiaoyu-agent-sdk
```

You do not need to install `xiaoyu-agent` yourself: pip pulls in
`xiaoyu-agent[sdk]` pinned to the same version. Only the SDK extra comes along;
the TUI, serve and browser extras are not installed. Nothing else needs to be
running either — no `xiaoyu` CLI process or `xiaoyu serve` daemon.

Because the pin is exact, installing the SDK into an environment that already
has a different `xiaoyu-agent` version will change that version, and with it
the `xiaoyu` command from that environment. Use a separate virtual environment
if you want to keep the two apart.

The SDK does not read Xiaoyu's `.env` or user configuration. Pass the model,
credentials and endpoint through `ModelOptions`.

```python
from pathlib import Path
from xiaoyu_agent_sdk import ModelOptions, SessionOptions, run

result = run("Explain this project", SessionOptions(
    model=ModelOptions(model="your-model", api_key="your-key"),
    workspace=Path.cwd(),
))
print(result.text)
```

Model credentials are explicit. Mutating tools require host approval by default.
Use `Session` for multiple turns, `AsyncSession` for async applications, and
`OutputSpec` for validated JSON results with bounded repair attempts.

See [SDK guide](https://github.com/pholex/zhinu/blob/main/docs/sdk.md) for ownership,
approval, streaming, structured output, extension and persistence contracts.
