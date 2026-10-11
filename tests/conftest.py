import json
import os
import sys

import pytest
from fakes import FAKE_CLAUDE


@pytest.fixture
def fake_claude(tmp_path, monkeypatch):
    """Put a fake `claude` first in PATH; returns a function that sets what it prints."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "claude"
    script.write_text(FAKE_CLAUDE.replace("{python}", sys.executable))
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")

    def configure(new, resume=(), stderr="", exit_code=0):
        (bin_dir / "scenario.json").write_text(
            json.dumps({"new": new, "resume": list(resume), "stderr": stderr, "exit": exit_code})
        )
        return bin_dir / "calls.jsonl"

    return configure
