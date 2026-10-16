"""Git commands the loop runs in the working repository.

Git of the loop runs outside the sandbox, so it never runs hooks or the fsmonitor named in the
repository config: they could be the agent's code. Today the CLI keeps the agent out of
`.git/hooks` and `.git/config`, these flags do not depend on that.
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import tempfile
from collections.abc import Iterable, Iterator, Sequence
from pathlib import Path

WIP_REFS = "refs/loop/wip"
SAFE_CONFIG = ("-c", f"core.hooksPath={os.devnull}", "-c", "core.fsmonitor=false")


class GitError(Exception):
    pass


def git(repo: Path, *args: str, env: dict[str, str] | None = None) -> str:
    try:
        proc = subprocess.run(
            ["git", *SAFE_CONFIG, *args], cwd=repo, env=env, capture_output=True, text=True
        )
    except OSError as exc:
        # The repository of a run may have been moved or deleted since the run started.
        raise GitError(f"git {' '.join(args)}: {exc}") from exc
    if proc.returncode != 0:
        raise GitError(f"git {' '.join(args)}: {proc.stderr.strip()}")
    return proc.stdout


def head(repo: Path) -> str:
    return git(repo, "rev-parse", "HEAD").strip()


def branch(repo: Path) -> str:
    """The current branch; `HEAD` when HEAD is detached."""
    return git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip()


def untracked(repo: Path) -> list[str]:
    """Untracked files that are not ignored, relative to the repository root."""
    return git(repo, "ls-files", "--others", "--exclude-standard", "-z").split("\0")[:-1]


def exclude(repo: Path, pattern: str) -> None:
    """Add `pattern` to `info/exclude` of the repository unless it is there already."""
    path = repo / git(repo, "rev-parse", "--git-path", "info/exclude").strip()
    lines = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
    if pattern in lines:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(f"{line}\n" for line in [*lines, pattern]), encoding="utf-8")


def wip_ref(run_id: str, task_seq: int) -> str:
    return f"{WIP_REFS}/{run_id}/{task_seq}"


def delete_ref(repo: Path, ref: str) -> None:
    # A ref that is not there is not an error to git.
    git(repo, "update-ref", "-d", ref)


def new_files(repo: Path, base: str, untracked_before: list[str]) -> list[str]:
    """Files a step has added on top of `base`, relative to the repository root.

    These are the untracked files that were not there before the step and the files it has
    added to the index with `git add`. Files that were untracked before the step are the
    operator's environment and stay out.
    """
    added = git(
        repo, "diff", "--cached", "--name-only", "--no-renames", "--diff-filter=A", "-z", base
    ).split("\0")[:-1]
    return sorted((set(untracked(repo)) | set(added)) - set(untracked_before))


def save_wip(repo: Path, ref: str, untracked_before: list[str], base: str) -> str:
    """Keep the uncommitted work of a step in `ref`; returns the commit.

    The commit on top of `base`, the checkpoint of the step, holds the changes of tracked files
    and the files of `new_files`. The index and the working tree are not touched.
    """
    with _work_index(repo, base, new_files(repo, base, untracked_before)) as env:
        tree = git(repo, "write-tree", env=env).strip()
    # A signing prompt would hang the loop, and the commit is never published.
    commit = git(
        repo, "commit-tree", "--no-gpg-sign", tree, "-p", base, "-m", f"agent-loop wip {ref}"
    ).strip()
    git(repo, "update-ref", ref, commit)
    return commit


def commit(repo: Path, base: str, added: list[str], message: str) -> str:
    """Commit the work on top of `base`, the HEAD of the current branch: the changes of tracked
    files and `added`; returns the commit.

    The branch moves only if it is still at `base`. The index is reset to the commit and the
    working tree is not touched: files left out of the commit stay as they are. The author and
    the signing are as the git config says.
    """
    with _work_index(repo, base, added) as env:
        tree = git(repo, "write-tree", env=env).strip()
    commit = git(repo, "commit-tree", tree, "-p", base, "-m", message).strip()
    git(repo, "update-ref", "-m", f"commit: {message}", "HEAD", commit, base)
    git(repo, "reset", "--quiet")
    return commit


def words_in(repo: Path, words: Iterable[str], outside: str) -> set[str]:
    """The `words` found as whole words in the files of the repository other than `outside`:
    tracked files and untracked ones that are not ignored, as they are in the working tree."""
    patterns = [arg for word in words for arg in ("-e", word)]
    return _grep(
        repo,
        "--untracked",
        "--word-regexp",
        "--fixed-strings",
        *patterns,
        "--",
        ".",
        f":(exclude,literal){outside}",
    )


def strings_in(repo: Path, strings: Iterable[str], rev: str) -> set[str]:
    """The `strings` found in the files of `rev`, inside words too."""
    patterns = [arg for string in strings for arg in ("-e", string)]
    return _grep(repo, "--fixed-strings", *patterns, rev)


def diff(repo: Path, base: str, added: list[str], *options: str, paths: Sequence[str] = ()) -> str:
    """`git diff` of the work on top of `base`: the changes of tracked files and `added`.

    Renames show as a removal and an addition. Diff drivers and textconv filters are named in the
    repository config and attributes, so they are never run.
    """
    with _work_index(repo, base, added) as env:
        return git(
            repo,
            "--literal-pathspecs",
            "diff",
            "--cached",
            "--no-ext-diff",
            "--no-textconv",
            "--no-renames",
            "--no-color",
            *options,
            base,
            "--",
            *paths,
            env=env,
        )


@contextlib.contextmanager
def _work_index(repo: Path, base: str, added: list[str]) -> Iterator[dict[str, str]]:
    """Environment of git with a separate index: `base` plus the work in the working tree.

    The real index stays as the agent or the operator left it.
    """
    with tempfile.TemporaryDirectory() as tmp:
        env = {**os.environ, "GIT_INDEX_FILE": str(Path(tmp) / "index")}
        git(repo, "read-tree", base, env=env)
        git(repo, "add", "--update", env=env)
        if added:
            # A file the step has added to the index with `git add -f` may be ignored.
            git(repo, "--literal-pathspecs", "add", "--force", "--", *added, env=env)
        yield env


def _grep(repo: Path, *args: str) -> set[str]:
    proc = subprocess.run(
        [
            "git",
            *SAFE_CONFIG,
            "grep",
            "-I",
            "--only-matching",
            "-h",
            "--no-line-number",
            "--no-column",
            *args,
        ],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    # 1 is no match.
    if proc.returncode > 1:
        raise GitError(f"git grep: {proc.stderr.strip()}")
    return set(proc.stdout.splitlines())
