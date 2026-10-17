"""Files of the working repository that configure the CLI itself; they belong to the operator.

Sessions of the loop run with `--restricted` and ignore these files, but the operator's own
sessions in the repository read them: a hook the agent puts there would run outside the sandbox
the next time the operator starts `claude`. The permission rules keep the file tools away from
these paths and the sandbox keeps Bash away from them, so this is the last line: before each
session the loop compares the files with the copy taken when the run started and puts the copy
back.

Files are compared by content, not by `git diff`: `settings.local.json` and `.mcp.json` are
usually ignored by git, so a change to them never shows in the diff.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
from pathlib import Path

# `hooks` is where the scripts of project hooks usually live: they run outside the sandbox too.
# Skills, subagents and commands may declare hooks in their frontmatter.
PATHS = (
    ".claude/settings.json",
    ".claude/settings.local.json",
    ".claude/rules",
    ".claude/hooks",
    ".claude/skills",
    ".claude/agents",
    ".claude/commands",
    ".mcp.json",
)
# Configs of git hook managers: their hooks run outside the sandbox on the operator's commit.
# They are not put back: the gate sends a change of them to the operator.
HOOK_CONFIGS = (
    ".pre-commit-config.yaml",
    ".pre-commit-config.yml",
    ".husky",
    "lefthook.yml",
    "lefthook.yaml",
    ".lefthook.yml",
    ".lefthook.yaml",
    "lefthook-local.yml",
    "lefthook-local.yaml",
)
# Files of the operator in the repository that run code outside the sandbox: git hooks and
# config, the CLI settings, git hook managers, IDE and devcontainer settings. Neither the
# sessions nor the gate commands write them.
OPERATOR_FILES = (".git", *PATHS, *HOOK_CONFIGS, ".vscode", ".idea", ".devcontainer")
# Names of secrets anywhere in the repository, closed to reading too.
SECRETS = ("*.pem", "*.key")
# The settings of the repository with its secrets: closed to the sessions, while the tests of the
# gate commands read them.
ENV_FILES = ".env*"
COPY_DIR = "protected"


def save(repo: Path, run_dir: Path) -> None:
    """Copy the protected files as they are at the start of the run.

    Called once per run: a second call raises `FileExistsError`. `loop resume` keeps the copy,
    since one taken later would take in what the agent of an interrupted session has written.
    """
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
