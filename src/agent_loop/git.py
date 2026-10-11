"""Git commands the loop runs in the working repository."""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

WIP_REFS = "refs/loop/wip"


class GitError(Exception):
    pass


def git(repo: Path, *args: str, env: dict[str, str] | None = None) -> str:
    proc = subprocess.run(["git", *args], cwd=repo, env=env, capture_output=True, text=True)
    if proc.returncode != 0:
        raise GitError(f"git {' '.join(args)}: {proc.stderr.strip()}")
    return proc.stdout


def head(repo: Path) -> str:
    return git(repo, "rev-parse", "HEAD").strip()


def untracked(repo: Path) -> list[str]:
    """Untracked files that are not ignored, relative to the repository root."""
    return git(repo, "ls-files", "--others", "--exclude-standard", "-z").split("\0")[:-1]


def wip_ref(run_id: str, task_seq: int) -> str:
    return f"{WIP_REFS}/{run_id}/{task_seq}"


def save_wip(repo: Path, ref: str, untracked_before: list[str]) -> str:
    """Keep the uncommitted work of a step in `ref`; returns the commit.

    The commit on top of HEAD holds the changes of tracked files and the untracked files that
    appeared after `untracked_before` was taken. Files that were untracked before the step are
    the operator's environment and stay out. The index and the working tree are not touched.
    """
    new_files = sorted(set(untracked(repo)) - set(untracked_before))
    with tempfile.TemporaryDirectory() as tmp:
        # A separate index: the real one stays as the agent or the operator left it.
        env = {**os.environ, "GIT_INDEX_FILE": str(Path(tmp) / "index")}
        git(repo, "read-tree", "HEAD", env=env)
        git(repo, "add", "--update", env=env)
        if new_files:
            git(repo, "--literal-pathspecs", "add", "--", *new_files, env=env)
        tree = git(repo, "write-tree", env=env).strip()
    # A signing prompt would hang the loop, and the commit is never published.
    commit = git(
        repo, "commit-tree", "--no-gpg-sign", tree, "-p", "HEAD", "-m", f"agent-loop wip {ref}"
    ).strip()
    git(repo, "update-ref", ref, commit)
    return commit
