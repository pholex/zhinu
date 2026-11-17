"""Load the SDK from this checkout or the same bundled application build."""
from __future__ import annotations

from pathlib import Path
import sys


def ensure_sdk() -> None:
    source = Path(__file__).resolve().parents[2] / 'packages/xiaoyu-agent-sdk/src'
    if (source / 'xiaoyu_agent_sdk').is_dir() and str(source) not in sys.path:
        sys.path.insert(0, str(source))
    import xiaoyu_agent_sdk  # noqa: F401
