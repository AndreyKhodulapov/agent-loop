"""Files that one process writes while others read them: the loop, its hooks, other loops."""

from __future__ import annotations

import os
from pathlib import Path


def write_atomic(path: Path, text: str) -> None:
    """Replace the file in one step, so a reader never sees it half written."""
    # The pid keeps temporary files of concurrent writers apart.
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)
