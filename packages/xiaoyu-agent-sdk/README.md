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

## Quick start

```python
import os
from pathlib import Path

from xiaoyu_agent_sdk import ModelOptions, Session, SessionOptions, run

options = SessionOptions(
    model=ModelOptions(
        model=os.environ["SDK_MODEL"],
        api_key=os.environ["SDK_API_KEY"],
        # Any OpenAI-compatible endpoint; defaults to https://api.openai.com/v1
        base_url=os.environ.get("SDK_BASE_URL", "https://api.openai.com/v1"),
    ),
    workspace=Path.cwd(),
)

# One-shot call
print(run("Summarize this project in one sentence", options).text)

# Multi-turn session: later turns see earlier ones
with Session(options) as session:
    print(session.run("Remember the number 42").text)
    print(session.run("What number did I give you?").text)
```

Model credentials are explicit. Mutating tools require host approval by default.
Use `Session` for multiple turns, `AsyncSession` for async applications, and
`OutputSpec` for validated JSON results with bounded repair attempts.

See [SDK guide](https://github.com/pholex/zhinu/blob/main/docs/sdk.md) for ownership,
approval, streaming, structured output, extension and persistence contracts.
Runnable examples, most with an offline `--demo` mode, live in
[examples/sdk](https://github.com/pholex/zhinu/tree/main/examples/sdk).
