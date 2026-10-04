"""Host-owned token redaction before tool results are published; --demo is offline."""
import argparse
from dataclasses import replace
from pathlib import Path
import re
import tempfile

from xiaoyu_agent_sdk import ResultTransform, RunCompleted, Session, Tool, ToolCompleted, ToolOutput
from common import options, text_chunk, tool_chunk


def redact_token(output: ToolOutput) -> str:
    # A deliberately narrow business format, not a general secret scanner.
    return re.sub(r"token=[A-Za-z0-9_-]+", "token=[redacted]", output.text)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--demo", action="store_true")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory() as temporary:
        config = replace(options(args.demo, [[tool_chunk("order", "{}")],
            [text_chunk("Order is ready.")]], Path(temporary).resolve()),
            tools=(Tool("order", "Read sample order status", {"type": "object"},
                        lambda: "order=42 status=ready token=demo-private", requires_approval=False),),
            result_transforms=(ResultTransform("redact-order-token", redact_token, "order"),))
        with Session(config) as session:
            for event in session.stream("Read the order status."):
                if isinstance(event, ToolCompleted):
                    print(event.output)
                elif isinstance(event, RunCompleted):
                    print(event.result.text)


if __name__ == "__main__":
    main()
