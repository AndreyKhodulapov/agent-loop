"""Checks of a step's work that the loop makes in code before the work is committed.

The permission rules and the sandbox keep the agent within bounds; the gate catches what got
past them and what the agent is free to do but must not: weaken the tests, grow the diff past
the size of one task, add a file with secrets. The work is the changes of tracked files against
the checkpoint of the step and the files the step has added. Once the work keeps these
invariants, the commands of the repository profile check that it installs, is formatted, passes
the linter and the tests, and the added lines of Python modules are checked against the rules of
the review template that code can tell.

A new file that looks like a secret stops the work for the operator before the commands: it stays
out of the commit, while the commands would pass with it in the working tree.

The commands read `.env*` of the repository, the agent does not: the values of its secret keys
are masked in what goes to the fixer, and a value in the diff stops the work for the operator.
A value the repository already has, such as the default password of `.env.example`, is no secret.
"""

from __future__ import annotations

import ast
import fnmatch
import io
import logging
import re
import shlex
import tokenize
from collections import Counter
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath

from agent_loop import commands, git, protected
from agent_loop.commands import CommandError
from agent_loop.config import ConfigError, Profile, Settings
from agent_loop.step import PROJECT_SETTINGS
from agent_loop.store import Step

logger = logging.getLogger(__name__)

BRANCH_CHANGED = "branch_changed"
HOOK_CONFIG = "hook_config"
WEAKENED_TESTS = "weakened_tests"
FLAKY_TESTS = "flaky_tests"
SECRET_FILE = "secret_file"
SECRET_IN_DIFF = "secret_in_diff"
STYLE = "style"


@dataclass(frozen=True)
class Verdict:
    # pass, or the status the task moves to: fixing, paused, needs_human or replan.
    status: str
    # Why; on pass, a warning for the report: flaky tests the task has not touched.
    reason: str | None = None
    # For the executor or the operator, one finding per item.
    findings: tuple[str, ...] = ()
    # New files kept out of the commit: they look like secrets.
    secrets: tuple[str, ...] = ()
    # Lines added and removed against the checkpoint, secrets left out.
    lines: int = 0
    # New files the commands have left, reports and caches the ignore rules miss: not the work.
    artifacts: tuple[str, ...] = ()


def check(
    repo: Path,
    run_dir: Path,
    branch: str,
    step: Step,
    profile: Profile,
    settings: Settings,
    tests_before: int | None,
) -> Verdict:
    """Check the work of `step` on the branch of the run.

    A moved branch pauses the run before anything else is touched: it may be the operator's
    work. The protected files are put back, as before each session. `tests_before` is what
    `count_tests` found before the first step of the task; None skips the count.
    """
    moved = branch_moved(repo, branch, step.checkpoint)
    if moved is not None:
        logger.error("%s, pausing", moved)
        return Verdict("paused", BRANCH_CHANGED, (moved,))
    # Put back before the diff is taken: the restored files are not the work of the step.
    restored = protected.restore(repo, run_dir)
    added = git.new_files(repo, step.checkpoint, step.untracked)
    secrets = secret_files(added, profile)
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
    if secrets:
        reason = reason or SECRET_FILE
        findings.append(
            "new files look like secrets, the loop does not commit them: " + ", ".join(secrets)
        )
    if findings:
        logger.error("a human has to look: %s", "; ".join(f.splitlines()[0] for f in findings))
        return Verdict("needs_human", reason, tuple(findings), secrets, lines)

    if lines > settings.diff_hard_lines:
        finding = f"the diff has {lines} lines, more than {settings.diff_hard_lines}"
        logger.error("%s, the task goes back to the planner", finding)
        return Verdict("replan", "diff_too_big", (finding,), secrets, lines)
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
    status, reason, findings = _run_commands(repo, profile, list(numstat), tests_before)
    after = git.new_files(repo, step.checkpoint, step.untracked)
    artifacts = tuple(path for path in after if path not in added)
    if artifacts:
        logger.info("left by the commands, not the work: %s", ", ".join(artifacts))
    # After the commands: `fmt` may have changed the work.
    style = _style(repo, step.checkpoint, work, list(numstat))
    env_secrets = _env_secrets(repo, step.checkpoint)
    if env_secrets:
        # Taken again, the artifacts too: the commands could write what they read into the work.
        after_secrets = secret_files(after, profile)
        work = [path for path in after if path not in after_secrets]
        patch = git.diff(repo, step.checkpoint, work, "-U0")
        added_lines = [line for line in patch.splitlines() if line.startswith("+")]
        leaked = sorted(
            {key for value, key in env_secrets.items() if any(value in x for x in added_lines)}
        )
        if leaked:
            finding = "values from .env* are in the diff: " + ", ".join(leaked)
            logger.error("a human has to look: %s", finding)
            return Verdict("needs_human", SECRET_IN_DIFF, (finding,), secrets, lines, artifacts)
    if style:
        logger.warning("breaks the style: %s", "; ".join(style))
        if status == "pass":
            # A flaky test the task has not touched is not for the fixer.
            status, reason, findings = "fixing", STYLE, ()
        findings = (*findings, *style)
    findings = tuple(_mask(finding, env_secrets) for finding in findings)
    return Verdict(status, reason, findings, secrets, lines, artifacts)


def commit_message_ok(message: str) -> bool:
    """Whether a task can be committed with `message`: it keeps the rules of `session.md`."""
    # `type(module): gist`, printable ASCII: English, one line.
    form = re.compile(r"[a-z]+\([\w./-]+\): [ -~]+", re.ASCII)
    attribution = re.compile(r"co-authored-by|claude|anthropic|generated with", re.IGNORECASE)
    return (
        len(message) <= 72
        and form.fullmatch(message) is not None
        and not attribution.search(message)
    )


def secret_files(paths: list[str], profile: Profile) -> tuple[str, ...]:
    """The new files of `paths` that look like secrets: they stay out of commits and prompts."""
    # Any name with credentials in it would take `test_credentials.py` for a secret.
    patterns = (
        ".env*",
        "*.pem",
        "*.key",
        "*credentials",
        "*credentials*.json",
        "*credentials*.yaml",
        "*credentials*.yml",
        *profile.secret_patterns,
    )
    return tuple(path for path in paths if _matches(path, patterns))


def count_tests(profile: Profile, repo: Path) -> int | None:
    """Tests the `collect` command of the profile finds; None if the profile has no such command.

    The count is the first integer in the last non-empty line of the output: pytest ends with
    `228 tests collected in 0.53s`, `grep -c` prints the bare number.
    """
    if "collect" not in profile.commands:
        return None
    output = commands.run(profile, "collect", repo)
    last = next((line for line in reversed(output.splitlines()) if line.strip()), "")
    match = re.search(r"\d+", last)
    if match is None:
        raise ConfigError(f"collect: no number in the last line of the output: {last!r}")
    return int(match.group())


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


def _run_commands(
    repo: Path, profile: Profile, changed: list[str], tests_before: int | None
) -> tuple[str, str | None, tuple[str, ...]]:
    """Status, reason and findings of the profile commands on the work.

    A failed `install` stops the gate: nothing else is meaningful without the dependencies.
    The other commands all run, so the fixer gets every failure at once.
    """
    deps = [path for path in changed if _matches(path, profile.deps_files)]
    if deps and "install" in profile.commands:
        logger.info("dependencies have changed (%s), installing", ", ".join(deps))
        try:
            commands.run(profile, "install", repo)
        except CommandError as exc:
            logger.warning("%s", str(exc).splitlines()[0])
            return "fixing", "install_failed", (str(exc),)
    # Test modules only, by name: a fixture file or a removed test passed to the runner is an
    # error of the runner, not of the work.
    tests = [
        path
        for path in changed
        if _is_test_module(PurePosixPath(path).name) and (repo / path).exists()
    ]
    value = " ".join(shlex.quote(path) for path in tests)
    profile = replace(
        profile,
        commands={
            name: command.replace("{changed_tests}", value)
            for name, command in profile.commands.items()
        },
    )

    failures = []
    for name in ("fmt", "lint"):
        if name in profile.commands:
            try:
                commands.run(profile, name, repo)
            except CommandError as exc:
                failures.append((f"{name}_failed", str(exc)))
    if tests_before is not None:
        try:
            count = count_tests(profile, repo)
        except CommandError as exc:
            failures.append(("collect_failed", str(exc)))
        else:
            if count is not None and count < tests_before:
                failures.append(
                    (WEAKENED_TESTS, f"{count} tests collected, {tests_before} before the task")
                )
    flaky = None
    if "test" in profile.commands:
        try:
            commands.run(profile, "test", repo)
        except CommandError as exc:
            logger.warning("%s, running the tests again", str(exc).splitlines()[0])
            try:
                commands.run(profile, "test", repo)
            except CommandError as again:
                failures.append(("test_failed", str(again)))
            else:
                flaky = f"the tests failed once and passed when run again:\n{exc}"
                # The report of the failed run names the failed test files.
                if any(PurePosixPath(path).name in str(exc) for path in tests):
                    failures.append((FLAKY_TESTS, flaky))
                    flaky = None
    if failures:
        logger.warning("failed: %s", ", ".join(reason for reason, _ in failures))
        return "fixing", failures[0][0], tuple(finding for _, finding in failures)
    if flaky is not None:
        logger.warning("flaky tests the task has not touched, they need a task of their own")
        return "pass", FLAKY_TESTS, (flaky,)
    return "pass", None, ()


def _style(repo: Path, checkpoint: str, work: list[str], changed: list[str]) -> list[str]:
    """Findings in the added lines of the changed Python modules: decorative separators in
    comments, markdown in comments and docstrings, references to plans, tasks and decisions, new
    module-level names the module uses fewer than 3 times.

    A name is not new if one of the changed modules had it at the module level before: a moved
    global is not the work of the task. A name other files of the repository refer to is the API
    of its module and needs no uses in it.
    """
    sources = [path for path in changed if path.endswith(".py")]
    if not sources:
        return []
    # `diff.noprefix` or `diff.mnemonicPrefix` in the config would change the prefixes.
    prefixes = ("--src-prefix=a/", "--dst-prefix=b/")
    patch = git.diff(repo, checkpoint, work, "-U0", *prefixes, paths=sources)
    listed = git.git(
        repo,
        "--literal-pathspecs",
        "ls-tree",
        "-r",
        "-z",
        "--name-only",
        checkpoint,
        "--",
        *sources,
    )
    before = set()
    for path in listed.split("\0")[:-1]:
        try:
            tree = ast.parse(git.git(repo, "cat-file", "blob", f"{checkpoint}:{path}"))
        except SyntaxError:
            continue
        before.update(_module_names(tree))
    findings = []
    added = _added_lines(patch)
    # A path git quotes in the patch is not found and not checked.
    for path in sources:
        if added.get(path):
            findings += _module_style(repo, path, added[path], before)
    return findings


def _module_style(repo: Path, path: str, added: set[int], before: set[str]) -> list[str]:
    source = (repo / path).read_text(encoding="utf-8")
    try:
        tree = ast.parse(source)
    except SyntaxError:
        # The linter and the tests report it.
        return []
    lines = source.splitlines()
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ) and ast.get_docstring(node, clean=False):
            docstrings.update(range(node.body[0].lineno, node.body[0].end_lineno + 1))
    tokens = tokenize.generate_tokens(io.StringIO(source).readline)
    comments = {t.start[0]: t.string for t in tokens if t.type == tokenize.COMMENT}

    findings = []
    for n in sorted(added):
        texts = []
        if n in comments:
            texts.append(comments[n])
            if re.search(r"[-=*#~_]{3,}", comments[n][1:]):
                findings.append((n, f"a decorative separator: {comments[n]}"))
        if n in docstrings:
            texts.append(lines[n - 1].strip())
            if re.match(r"\s*#{1,6} ", lines[n - 1]):
                findings.append((n, f"a markdown heading in a docstring: {texts[-1]}"))
        for text in texts:
            # `*args` and `**kwargs` are not emphasis: the star is not closed before a space. Nor
            # is a star in code.
            prose = re.sub(r"`[^`\n]*`", "", text)
            if re.search(r"(?<![\w*])\*{1,2}(?=[^\s*])[^*\n]*[^\s*]\*{1,2}(?![\w*])", prose):
                findings.append((n, f"markdown emphasis: {text}"))
            # Numbers alone are too common: `step 2`, `retry the task 3 times`.
            if re.search(
                r"\b(plan|progress)\.md\b|§\s*\d|\badr-?\s?\d"
                r"|\b(task|step|stage|phase|decision|ticket|задач\w*|шаг\w*|этап\w*|решени\w*"
                r"|раздел\w*|пункт\w*)\s+([#№]\d|\d+\.\d|\d+[a-zа-я]\b)",
                text,
                re.IGNORECASE,
            ):
                findings.append((n, f"a reference to a plan, task or decision: {text}"))

    uses = Counter(
        node.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
    )
    rare = {
        name: line
        for name, line in _module_names(tree).items()
        if line in added
        and name not in before
        and uses[name] < 3
        and not (name.startswith("__") and name.endswith("__"))
        and name not in ("logger", "pytestmark", "pytest_plugins")
    }
    elsewhere = git.words_in(repo, rare, path) if rare else set()
    for name, line in rare.items():
        if name not in elsewhere:
            findings.append(
                (
                    line,
                    f"the module-level `{name}` has {uses[name]} uses in the module, fewer"
                    " than 3: make it a local variable or a class attribute",
                )
            )
    return [f"{path}:{n}: {finding}" for n, finding in sorted(findings)]


def _module_names(tree: ast.Module) -> dict[str, int]:
    """Names assigned at the module level, each with the line of its first assignment."""
    names = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets = [node.target]
        else:
            continue
        for target in targets:
            if isinstance(target, ast.Name):
                names.setdefault(target.id, node.lineno)
    return names


def _added_lines(patch: str) -> dict[str, set[int]]:
    """Numbers of the added lines by path in a patch made with `-U0`."""
    added: dict[str, set[int]] = {}
    lines: set[int] = set()
    in_hunks = False
    for line in patch.splitlines():
        if line.startswith("diff --git "):
            lines = set()
            in_hunks = False
        elif not in_hunks and line.startswith("+++ b/"):
            # A name with a space ends with a tab.
            lines = added.setdefault(line[len("+++ b/") :].removesuffix("\t"), set())
        elif line.startswith("@@"):
            in_hunks = True
            start, count = re.match(r"@@ -\S+ \+(\d+)(?:,(\d+))?", line).groups()
            lines.update(range(int(start), int(start) + int(count or 1)))
    return added


def _env_secrets(repo: Path, checkpoint: str) -> dict[str, str]:
    """Values of the secret keys in the `.env*` files of the repository, ignored ones included,
    each with its key and file.

    Values shorter than 8 characters are left out: `true` or `admin` would be found everywhere.
    So are the values the files of `checkpoint` have: they are published already. A value of a
    tracked `.env*` changed in the working tree is not.
    """
    listed = git.git(repo, "ls-files", "-z", "--cached", "--others", ":(glob,icase)**/.env*")
    secret_key = re.compile(r"KEY|TOKEN|SECRET|PASS|PWD|CREDENTIAL|AUTH|DSN|PRIVATE", re.IGNORECASE)
    env_secrets = {}
    for path in listed.split("\0")[:-1]:
        for line in (repo / path).read_text(encoding="utf-8", errors="replace").splitlines():
            key, sep, value = line.removeprefix("export ").partition("=")
            value = value.strip().strip("\"'")
            if sep and secret_key.search(key) and len(value) >= 8:
                env_secrets[value] = f"{key.strip()} of {path}"
    published = git.strings_in(repo, env_secrets, checkpoint) if env_secrets else set()
    return {value: key for value, key in env_secrets.items() if value not in published}


def _mask(text: str, env_secrets: dict[str, str]) -> str:
    # The longest first: a value may contain another one.
    for value in sorted(env_secrets, key=len, reverse=True):
        text = text.replace(value, f"<{env_secrets[value]}>")
    return text


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
    # pytest, unittest, Go and JavaScript test frameworks.
    skip = re.compile(
        r"\b(mark\.(skip|skipif|xfail)|pytest\.(skip|xfail)|unittest\.skip\w*|skipTest"
        r"|t\.Skip\w*|xit|xdescribe|xtest|(it|test|describe)\.skip)\b"
    )
    assertion = re.compile(r"\b(assert\w*|expect\(|require\.|t\.(Error|Fatal)\w*)")
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
            skips[path] += sign * bool(skip.search(line))
            asserts[path] -= sign * bool(assertion.search(line))
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
    # The file system ignores the case, so does the hook manager.
    path = path.lower()
    return any(path == name or path.startswith(f"{name}/") for name in protected.HOOK_CONFIGS)


def _is_test(path: str) -> bool:
    parts = PurePosixPath(path).parts
    in_test_dir = bool({"test", "tests", "__tests__"}.intersection(parts[:-1]))
    return in_test_dir or _is_test_module(parts[-1])


def _is_test_module(name: str) -> bool:
    return re.search(r"^test_|_test\.|\.(test|spec)\.", name) is not None
