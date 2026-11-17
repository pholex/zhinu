"""Observe reported prompt cache usage without changing execution or tool results."""
import argparse
from dataclasses import dataclass
from pathlib import Path
import tempfile
from types import SimpleNamespace

from xiaoyu_agent_sdk import RequestEnded, Session
from common import options, text_chunk


@dataclass
class CacheReport:
    requests: int = 0
    reported: int = 0
    prompt_tokens: int = 0
    cached_tokens: int = 0

    def observe(self, event: RequestEnded) -> None:
        self.requests += 1
        if "prompt_tokens" not in event.usage:
            return
        self.reported += 1
        self.prompt_tokens += event.usage["prompt_tokens"]
        self.cached_tokens += event.usage.get("cached_tokens", 0)

    def summary(self) -> str:
        ratio = f"{self.cached_tokens / self.prompt_tokens:.1%}" if self.prompt_tokens else "unknown"
        return (f"Reported requests: {self.reported}/{self.requests}; "
                f"cached prompt tokens: {self.cached_tokens}/{self.prompt_tokens} ({ratio})")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--demo", action="store_true")
    args = parser.parse_args()
    usage = SimpleNamespace(choices=[], usage=SimpleNamespace(prompt_tokens=100, completion_tokens=10,
                            prompt_tokens_details=SimpleNamespace(cached_tokens=60)))
    report = CacheReport()
    with tempfile.TemporaryDirectory() as temporary:
        config = options(args.demo, [[text_chunk("Hello."), usage]], Path(temporary).resolve())
        with Session(config) as session:
            for event in session.stream("Say hello."):
                if isinstance(event, RequestEnded):
                    report.observe(event)
    print(report.summary())


if __name__ == "__main__":
    main()
