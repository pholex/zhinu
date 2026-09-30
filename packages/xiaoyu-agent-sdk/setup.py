"""Generate both metadata fields from the kernel version; sdist is standalone."""
import re
from pathlib import Path

from setuptools import setup

here = Path(__file__).resolve().parent
source = here.parents[1] / "xiaoyu" / "__init__.py"
generated = here / "src" / "xiaoyu_agent_sdk" / "_version.py"
if source.is_file():
    version = re.search(r'^__version__ = "([^"]+)"', source.read_text(encoding="utf-8"), re.M).group(1)
    generated.write_text(f'__version__ = "{version}"\n', encoding="utf-8")
else:
    version = re.search(r'^__version__ = "([^"]+)"', generated.read_text(encoding="utf-8"), re.M).group(1)
setup(version=version, install_requires=[f"xiaoyu-agent[sdk]=={version}"])
