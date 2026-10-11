"""Checks of a step's work that the loop makes in code before the work is committed.

The permission rules and the sandbox keep the agent within bounds; the gate catches what got
past them and what the agent is free to do but must not: weaken the tests, grow the diff past
the size of one task, add a file with secrets. The work is the changes of tracked files against
the checkpoint of the step and the files the step has added.
"""

from __future__ import annotations

import fnmatch
import logging
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from agent_loop import git, protected
from agent_loop.config import Profile, Settings
from agent_loop.step import PROJECT_SETTINGS
from agent_loop.store import Step

logger = logging.getLogger(__name__)

BRANCH_CHANGED = "branch_changed"
HOOK_CONFIG = "hook_config"
DIFF_TOO_BIG = "diff_too_big"
WEAKENED_TESTS = "weakened_tests"

SECRET_PATTERNS = (".env*", "*.pem", "*.key", "*credentials*")
# Configs of git hook managers: their hooks run outside the sandbox on the operator's commit.
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
TEST_DIRS = {"test", "tests", "__tests__"}
TEST_NAME = re.compile(r"^test_|_test\.|\.(test|spec)\.")
# pytest, unittest, Go and JavaScript test frameworks.
SKIP = re.compile(
    r"\b(mark\.(skip|skipif|xfail)|pytest\.(skip|xfail)|unittest\.skip\w*|skipTest"
    r"|t\.Skip\w*|xit|xdescribe|xtest|(it|test|describe)\.skip)\b"
)
ASSERT = re.compile(r"\b(assert\w*|expect\(|require\.|t\.(Error|Fatal)\w*)")


@dataclass(frozen=True)
class Verdict:
    # pass, or the status the task moves to: fixing, paused, needs_human or replan.
    status: str
    reason: str | None = None
    # For the executor or the operator, one finding per item.
    findings: tuple[str, ...] = ()
    # New files kept out of the commit: they look like secrets.
    secrets: tuple[str, ...] = ()
    # Lines added and removed against the checkpoint, secrets left out.
    lines: int = 0


def check(
    repo: Path, run_dir: Path, branch: str, step: Step, profile: Profile, settings: Settings
) -> Verdict:
    """Check the work of `step` on the branch of the run.

    A moved branch pauses the run before anything else is touched: it may be the operator's
    work. The protected files are put back, as before each session.
    """
    moved = branch_moved(repo, branch, step.checkpoint)
    if moved is not None:
        logger.error("%s, pausing", moved)
        return Verdict("paused", BRANCH_CHANGED, (moved,))
    # Put back before the diff is taken: the restored files are not the work of the step.
    restored = protected.restore(repo, run_dir)
    added = git.new_files(repo, step.checkpoint, step.untracked)
    patterns = SECRET_PATTERNS + profile.secret_patterns
    secrets = tuple(path for path in added if _matches(path, patterns))
    if secrets:
        logger.warning("left out of the commit, may hold secrets: %s", ", ".join(secrets))
    work = [path for path in added if path not in secrets]
    numstat = _numstat(git.diff(repo, step.checkpoint, work, "--numstat", "-z"))
    lines = sum(numstat.values())

    findings = []
    reason = None
    if restored:
        reason = PROJECT_SETTINGS
        findings.append(
            "the CLI settings of the project have changed and are put back: " + ", ".join(restored)
        )
    hook_configs = [path for path in numstat if _is_hook_config(path)]
    if hook_configs:
        reason = reason or HOOK_CONFIG
        findings.append(
            "configs of git hook managers have changed, their hooks would run on the"
            " operator's commit outside the sandbox:\n"
            + git.diff(repo, step.checkpoint, work, paths=hook_configs)
        )
    if findings:
        logger.error("a human has to look: %s", "; ".join(f.splitlines()[0] for f in findings))
        return Verdict("needs_human", reason, tuple(findings), secrets, lines)

    if lines > settings.diff_hard_lines:
        finding = f"the diff has {lines} lines, more than {settings.diff_hard_lines}"
        logger.error("%s, the task goes back to the planner", finding)
        return Verdict("replan", DIFF_TOO_BIG, (finding,), secrets, lines)
    if lines > settings.diff_soft_lines:
        logger.warning("the diff has %d lines, more than %d", lines, settings.diff_soft_lines)

    tests = [path for path in numstat if _is_test(path)]
    findings = []
    if tests:
        # `diff.noprefix` or `diff.mnemonicPrefix` in the config would change the prefixes.
        prefixes = ("--src-prefix=a/", "--dst-prefix=b/")
        findings = _weakened(git.diff(repo, step.checkpoint, work, "-U0", *prefixes, paths=tests))
    if findings:
        logger.warning("weakened tests: %s", "; ".join(findings))
        return Verdict("fixing", WEAKENED_TESTS, tuple(findings), secrets, lines)
    return Verdict("pass", None, (), secrets, lines)


def branch_moved(repo: Path, branch: str, checkpoint: str) -> str | None:
    """What has moved since the checkpoint: the branch or HEAD; None if nothing has.

    The loop cannot tell a commit of the agent from one of the operator, so either is a pause.
    """
    current = git.branch(repo)
    if current != branch:
        return f"the branch is {current} instead of {branch}"
    head = git.head(repo)
    if head != checkpoint:
        return f"HEAD is {head[:12]} instead of {checkpoint[:12]}"
    return None


def _numstat(output: str) -> dict[str, int]:
    """Lines added and removed by path; a binary file counts as no lines."""
    lines = {}
    for record in output.split("\0")[:-1]:
        added, removed, path = record.split("\t", 2)
        lines[path] = int(added) + int(removed) if added != "-" else 0
    return lines


def _weakened(patch: str) -> list[str]:
    """Skips added and asserts removed in the patch of the test files.

    Lines are counted over all test files together: a changed line is a removal and an addition,
    and a moved test is a removal from one file and an addition to another.
    """
    skips: Counter[str] = Counter()
    asserts: Counter[str] = Counter()
    path = None
    in_hunks = False
    for line in patch.splitlines():
        if line.startswith("diff --git "):
            in_hunks = False
        elif not in_hunks and line.startswith(("--- a/", "+++ b/")):
            path = line[len("--- a/") :]
        elif line.startswith("@@"):
            in_hunks = True
        elif in_hunks and line[:1] in ("+", "-"):
            sign = 1 if line[0] == "+" else -1
            skips[path] += sign * bool(SKIP.search(line))
            asserts[path] -= sign * bool(ASSERT.search(line))
    findings = []
    if skips.total() > 0:
        findings.append(f"skip or xfail added: {_by_file(skips)}")
    if asserts.total() > 0:
        findings.append(f"assert lines removed: {_by_file(asserts)}")
    return findings


def _by_file(counts: Counter[str]) -> str:
    return ", ".join(f"{path} ({n})" for path, n in counts.items() if n > 0)


def _matches(path: str, patterns: tuple[str, ...]) -> bool:
    # A missed secret costs more than a file the operator commits by hand.
    name = PurePosixPath(path).name.lower()
    return any(
        fnmatch.fnmatchcase(name, p.lower()) or fnmatch.fnmatchcase(path.lower(), p.lower())
        for p in patterns
    )


def _is_hook_config(path: str) -> bool:
    return any(path == name or path.startswith(f"{name}/") for name in HOOK_CONFIGS)


def _is_test(path: str) -> bool:
    parts = PurePosixPath(path).parts
    return bool(TEST_DIRS.intersection(parts[:-1])) or TEST_NAME.search(parts[-1]) is not None
