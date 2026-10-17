"""Prompts of the sessions: the rules of each role and the prompt the loop builds.

The agent does not read the plan or the progress itself. The loop gives it the card of its task,
what the previous tasks of the run have added, and on a task resumed in a new session the handoff
and the work in progress. The fixer gets the card, the work and the findings of the gate.

A card (`Task.card`) holds `goal` (a string) and `criteria`, `files` (where to start), `tests`
(that must pass) and `decisions` (of the plan for this task), each a list of strings, possibly
empty. The planner gets the request of the operator and proposes the tasks of a run, see
`plan`. Prompts are in English whatever the language of the docs.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path

from agent_loop import git
from agent_loop.config import Settings
from agent_loop.gates import Verdict
from agent_loop.store import Task

ROLES = Path(__file__).parent
ANSWER_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {"enum": ["done", "too_big", "needs_human", "blocked"]},
        "summary": {"type": "string"},
        "commit_message": {"type": "string", "maxLength": 72},
        "notes_for_next": {"type": "string"},
        "question": {"type": "string"},
        "split": {"type": "array", "items": {"type": "string"}},
        "operator_actions": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["status", "summary", "commit_message", "notes_for_next"],
    "additionalProperties": False,
}
# The prompt of a paused session resumed with --resume: its context holds the task. The loop
# has removed the handoff of the pause before the session goes on.
RESUMED = (
    "agent-loop has resumed this session after a pause. Go on with the task from where you"
    " stopped and answer as before. The handoff you wrote at the pause is removed: if you are"
    " stopped again, write the handoff anew in full."
)


@dataclass(frozen=True)
class Answer:
    """The structured answer of an implementer or fixer session, see `session.md`."""

    status: str
    summary: str
    commit_message: str
    notes_for_next: str
    question: str | None = None
    split: tuple[str, ...] = ()
    # What the operator has to do by hand after the task: changes of the files closed to the
    # session that the task needs.
    operator_actions: tuple[str, ...] = ()


def answer(structured_output: dict) -> Answer:
    # The CLI has validated the output against `ANSWER_SCHEMA`.
    lists = {key: tuple(structured_output.get(key, ())) for key in ("split", "operator_actions")}
    return Answer(**{**structured_output, **lists})


def system_files(role: str, settings: Settings) -> tuple[Path, ...]:
    """Rules of a session of `role` (planner, implementer or fixer) for
    `agent.Session.system_files`.

    How the loop runs the sessions of a task, which the planner has not, the role, the code
    requirements of agent-loop and the context file of the project, which wins where they
    disagree.
    """
    files = (ROLES / f"{role}.md", ROLES / "AGENTS.md")
    if role != "planner":
        files = (ROLES / "session.md", *files)
    if settings.context_file is not None:
        files += (settings.context_file,)
    return files


def planner(request: str, settings: Settings) -> str:
    """The prompt of the planner of `request`, the task of the operator in their own words."""
    return "\n\n".join(
        [
            "# Request of the operator",
            _fenced(request, "markdown"),
            "## Size",
            f"The work of one task, tests included, fits in {settings.diff_soft_lines} changed"
            f" lines. A task over {settings.diff_hard_lines} lines comes back to the planner.",
            "## Language",
            f"Write the plan in {_language(settings)}: the operator reads and approves it.",
        ]
    )


def implementer(
    task: Task,
    total: int,
    settings: Settings,
    previous: str,
    handoff: str | None = None,
    work: str | None = None,
) -> str:
    """The prompt of the implementer of `task`, one of `total` tasks of the run.

    `previous` is what `previous_tasks` returns. A task resumed in a new session passes its work
    in progress as a diff against the checkpoint and the handoff of the stopped session, None
    if that session ended without writing one.
    """
    sections = _card(task, total) + _rules(settings)
    if previous:
        sections += ["## Previous tasks of the run", previous]
    if work is not None:
        sections += [
            "## Resuming the task",
            "A previous session of this task stopped before it finished. Its work is in the"
            " working tree: continue it.",
            "### Handoff of the previous session",
            handoff or "The session stopped without a handoff: go by the work in progress.",
            "### Work in progress against the start of the task",
            _fenced(work, "diff"),
        ]
    return "\n\n".join(sections)


def fixer(
    task: Task,
    total: int,
    settings: Settings,
    verdict: Verdict,
    work: str,
    handoff: str | None = None,
) -> str:
    """The prompt of the fixer of `task` after the gate has returned `verdict` on `work`, the
    diff of the task against its checkpoint.

    A fix resumed in a new session passes the handoff of the stopped fixer, if it wrote one; its
    work is already in `work`.
    """
    sections = _card(task, total) + _rules(settings)
    sections += [
        "## Work of the task against its start",
        _fenced(work, "diff"),
        f"## Findings of the checks: {verdict.reason}",
        *(_fenced(finding) for finding in verdict.findings),
    ]
    if handoff is not None:
        sections += [
            "## Resuming the fix",
            "A previous fixer session stopped before it finished. Its work is in the working"
            " tree and in the diff above: continue it.",
            "### Handoff of the previous session",
            handoff,
        ]
    return "\n\n".join(sections)


def commit_message(proposed: str) -> str:
    """The prompt that asks a session which has answered done for another commit message."""
    return "\n\n".join(
        [
            "agent-loop cannot commit your work with the commit message of your answer:",
            _fenced(proposed),
            "The message is one line of printable ASCII in English, `type(module): gist of"
            " changes`, at most 72 characters, with no co-author lines and no words Claude,"
            ' Anthropic or "generated with". The work has passed the checks: do not change the'
            " files, answer as before with such a message.",
        ]
    )


def previous_tasks(repo: Path, base: str, notes: list[tuple[str, str]]) -> str:
    """What the previous tasks of the run have done, for the prompt of the next one.

    The files changed from `base`, HEAD at the start of the run, to HEAD, with the public names
    added to Python modules, and the notes of the tasks as (title, notes) pairs. Without it the
    next task writes its own helper instead of the one the previous task has added. Empty when
    there is nothing to tell.
    """
    sections = []
    status = git.git(repo, "diff", "--name-status", "--no-renames", "-z", base, "HEAD")
    fields = status.split("\0")[:-1]
    files = []
    for change, path in zip(fields[::2], fields[1::2], strict=True):
        names = []
        if path.endswith(".py") and change != "D":
            old = [] if change == "A" else _public_names(git.git(repo, "show", f"{base}:{path}"))
            new = _public_names(git.git(repo, "show", f"HEAD:{path}"))
            names = [name for name in new if name not in old]
        files.append(f"{change} {path}" + (f": {', '.join(names)}" if names else ""))
    if files:
        sections += [
            "Files changed since the start of the run (A added, M modified, D deleted),"
            " with the public names added to Python modules:",
            "\n".join(files),
        ]
    notes = [(title, text) for title, text in notes if text.strip()]
    if notes:
        sections += [
            "Notes of the previous tasks:",
            "\n".join(f"- {title}: {text.strip()}" for title, text in notes),
        ]
    return "\n\n".join(sections)


def _card(task: Task, total: int) -> list[str]:
    card = task.card
    sections = [f"# Task {task.seq} of {total}: {task.title}", "## Goal", card["goal"]]
    for heading, key in (
        ("Acceptance criteria", "criteria"),
        ("Files to start from", "files"),
        ("Tests that must pass", "tests"),
        ("Decisions of the plan for this task", "decisions"),
    ):
        if card[key]:
            sections += [f"## {heading}", "\n".join(f"- {item}" for item in card[key])]
    return sections


def _rules(settings: Settings) -> list[str]:
    return [
        "## Size",
        f"The work of the task, tests included, fits in {settings.diff_soft_lines} changed"
        f" lines. A diff over {settings.diff_hard_lines} lines goes back to the planner.",
        "## Language",
        f"Write code comments, docstrings and documentation in {_language(settings)}."
        " The commit message is in English.",
    ]


def _language(settings: Settings) -> str:
    return {"ru": "Russian", "en": "English"}[settings.doc_lang]


def _fenced(text: str, info: str = "") -> str:
    # A diff of a markdown file has fences of its own; a longer fence keeps them inside.
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{fence}{info}\n{text.rstrip()}\n{fence}"


def _public_names(source: str) -> list[str]:
    """Top-level functions, classes with their methods, and variables, without private ones."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        # Not Python after all, such as a template: the file is listed without names.
        return []
    names = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            names.append(node.name)
            if isinstance(node, ast.ClassDef):
                names += [
                    f"{node.name}.{item.name}"
                    for item in node.body
                    if isinstance(item, ast.FunctionDef | ast.AsyncFunctionDef)
                ]
        elif isinstance(node, ast.Assign):
            names += [target.id for target in node.targets if isinstance(target, ast.Name)]
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.append(node.target.id)
        elif isinstance(node, ast.TypeAlias):
            names.append(node.name.id)
    return [name for name in names if not any(part.startswith("_") for part in name.split("."))]
