"""Review current-to-target changes before applying a rewind; --demo is offline."""
import argparse
from pathlib import Path
import tempfile
from dataclasses import replace

from xiaoyu_agent_sdk import Session
from common import options, text_chunk, tool_chunk


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--demo', action='store_true')
    args = parser.parse_args()
    with tempfile.TemporaryDirectory() as temporary:
        workspace = Path(temporary).resolve()
        config = replace(options(args.demo, [
            [tool_chunk('write_file', '{"path":"report.txt","content":"Draft report.\\n"}')],
            [text_chunk('Report ready.')]], workspace),
            builtin_tools=('write_file',), approver=lambda *_: True)
        with Session(config) as session:
            session.run('Write a draft report to report.txt.')
            preview = session.preview_rewind(1, conversation=False)
            for item in preview.files:
                print(item.action, item.path, item.note)
                print(item.diff, end='')
            # A real host renders the preview and waits for user confirmation here.
            result = session.rewind(1, conversation=False, preview=preview)
            print(result.status)
            assert result.status == 'completed'
            assert not (workspace / 'report.txt').exists()


if __name__ == '__main__':
    main()
