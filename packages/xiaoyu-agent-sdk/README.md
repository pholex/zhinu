# Xiaoyu Agent SDK

Python 3.11+ host API for the Xiaoyu workspace execution engine. The SDK runs in
your process and depends on the exact same version of `xiaoyu-agent`.

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
