import json
import os
import subprocess
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


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A git repository with one commit, isolated from the global and system git config."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    path = tmp_path / "repo"
    path.mkdir()
    for args in (
        ["init", "-q", "-b", "main"],
        ["config", "user.name", "Test"],
        ["config", "user.email", "test@example.com"],
    ):
        subprocess.run(["git", *args], cwd=path, check=True)
    (path / "app.py").write_text("x = 1\n")
    (path / "old.py").write_text("y = 2\n")
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=path, check=True)
    return path
