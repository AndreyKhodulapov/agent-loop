"""Files of the working repository that configure the CLI itself; they belong to the operator.

With `--setting-sources project` the CLI reads the settings of the repository: their hooks are
merged with the loop's and run outside the sandbox, their permissions are merged too. An agent
that writes such a file gets its command run outside the sandbox in the next session. The
permission rules keep the file tools away from these paths, but Bash in the sandbox writes
anywhere in the working directory, so before each session the loop compares the files with the
copy taken when the run started and puts the copy back.

Files are compared by content, not by `git diff`: `settings.local.json` and `.mcp.json` are
usually ignored by git.

The loop does not run in a repository whose settings have hooks: a hook of the operator that
runs a formatter, tests or `make` would run the files the agent writes outside the sandbox.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
from pathlib import Path

# `hooks` is where the scripts of project hooks usually live: they run outside the sandbox too.
PATHS = (
    ".claude/settings.json",
    ".claude/settings.local.json",
    ".claude/rules",
    ".claude/hooks",
    ".mcp.json",
)
SETTINGS_FILES = (".claude/settings.json", ".claude/settings.local.json")
COPY_DIR = "protected"


class ProjectHooksError(Exception):
    pass


def save(repo: Path, run_dir: Path) -> None:
    """Copy the protected files as they are at the start of the run; a repository whose settings
    have hooks raises `ProjectHooksError`."""
    for name in SETTINGS_FILES:
        path = repo / name
        if path.is_file() and json.loads(path.read_text(encoding="utf-8")).get("hooks"):
            raise ProjectHooksError(
                f"{path} has hooks: they run outside the sandbox and may run the files the agent"
                " writes; agent-loop does not run in such a repository"
            )
    copy = run_dir / COPY_DIR
    # Missing files are missing from the copy, so the directory itself is the record of them.
    copy.mkdir(parents=True)
    for name in PATHS:
        source = repo / name
        target = copy / name
        if source.is_dir() and not source.is_symlink():
            shutil.copytree(source, target, symlinks=True)
        elif source.is_symlink() or source.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target, follow_symlinks=False)


def restore(repo: Path, run_dir: Path) -> list[str]:
    """Put back the protected files that differ from the copy of `save`; returns their paths.

    A file that is not in the copy is removed. What a restore leaves wrong is found by the next
    one.
    """
    copy = run_dir / COPY_DIR
    if not copy.is_dir():
        # Without the copy every protected file of the operator would look new and be removed.
        raise FileNotFoundError(f"no copy of the protected files in {copy}")
    saved = _signatures(copy)
    current = _signatures(repo)
    changed = sorted(p for p in saved.keys() | current.keys() if saved.get(p) != current.get(p))
    for path in changed:
        _remove(repo / path)
    for path in changed:
        if path in saved:
            target = repo / path
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(copy / path, target, follow_symlinks=False)
    return changed


def _signatures(root: Path) -> dict[str, str]:
    """Signature of every file under the protected paths of `root`, by its relative path."""
    found = {}
    for name in PATHS:
        top = root / name
        if top.is_dir() and not top.is_symlink():
            for dirpath, dirnames, filenames in os.walk(top):
                # A link to a directory is listed with the directories, but not walked into.
                links = [d for d in dirnames if Path(dirpath, d).is_symlink()]
                for entry in [*filenames, *links]:
                    path = Path(dirpath, entry)
                    found[path.relative_to(root).as_posix()] = _signature(path)
        elif top.is_symlink() or top.exists():
            found[name] = _signature(top)
    return found


def _signature(path: Path) -> str:
    mode = path.lstat().st_mode
    if stat.S_ISLNK(mode):
        return f"link {os.readlink(path)}"
    if stat.S_ISREG(mode):
        return hashlib.sha256(path.read_bytes()).hexdigest()
    # Reading a fifo would hang the loop; a file of any other kind is a change by itself.
    return f"mode {stat.S_IFMT(mode):o}"


def _remove(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    elif path.is_symlink() or path.exists():
        path.unlink()
