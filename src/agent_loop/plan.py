"""The plan of a run: the tasks the planner proposes and the operator approves.

A plan is the answer of a planner session, checked by the CLI against `PLAN_SCHEMA`: the `goal`
of the request, its acceptance `criteria`, which also decide whether a replan needs the
operator, the `decisions` the tasks share, and the `tasks`. A task is its `title` and its card,
as `prompts` takes it: the `goal` and the lists `criteria`, `files`, `tests`, `decisions`.

`validate` finds what the schema cannot tell: its errors go back to the planner session.
`validate_replan` does the same for the answer of a planner that replans the tasks left in a run.
`loop approve` keeps the `digest` of `plan.json` with the run: a run starts only from the file
the operator has approved.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
from collections import Counter
from pathlib import Path, PurePosixPath

from agent_loop import protected
from agent_loop.files import write_atomic

PLAN_FILE = "plan.json"
STRINGS = {"type": "array", "items": {"type": "string"}}
TASK_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "goal": {"type": "string"},
        "criteria": STRINGS,
        "files": STRINGS,
        "tests": STRINGS,
        "decisions": STRINGS,
    },
    "required": ["title", "goal", "criteria", "files", "tests", "decisions"],
    "additionalProperties": False,
}
PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "goal": {"type": "string"},
        "criteria": STRINGS,
        "decisions": STRINGS,
        "tasks": {"type": "array", "items": TASK_SCHEMA},
    },
    "required": ["goal", "criteria", "decisions", "tasks"],
    "additionalProperties": False,
}


def validate(plan: dict) -> list[str]:
    """What is wrong with a plan that has passed `PLAN_SCHEMA`, one error per item."""
    errors = []
    if not plan["goal"].strip():
        errors.append("the plan has no goal")
    if not plan["criteria"]:
        errors.append("the plan has no acceptance criteria")
    if not plan["tasks"]:
        errors.append("the plan has no tasks")
    errors += _repeated([task["title"] for task in plan["tasks"]])
    for number, task in enumerate(plan["tasks"], start=1):
        errors += _task_errors(f"task {number}", task)
    return errors


def validate_replan(answer: dict, left: dict[int, str], through: list[str]) -> list[str]:
    """What is wrong with the answer of a replanning session that has passed its schema.

    `left` are the titles of the tasks left after the replanned one by their numbers, each of
    them needs one action; `through` are the titles of the tasks the run is through with.
    """
    errors = []
    actions = Counter(entry["task"] for entry in answer["remaining"])
    errors += [
        f"task {number} is not one of the tasks left" for number in actions if number not in left
    ]
    errors += [f"task {number} has {n} actions" for number, n in actions.items() if n > 1]
    missing = [str(number) for number in left if number not in actions]
    if missing:
        errors.append(f"tasks {', '.join(missing)} have no action")
    titles = [*through, *(task["title"] for task in answer["tasks"])]
    for number, task in enumerate(answer["tasks"], start=1):
        errors += _task_errors(f"new task {number}", task)
    for entry in answer["remaining"]:
        number, card = entry["task"], entry.get("card")
        if entry["action"] == "modify" and card is None:
            errors.append(f"task {number} is modified without its new card")
        elif entry["action"] != "modify" and card is not None:
            errors.append(f"task {number} has a new card but is not modified")
        elif card is not None:
            errors += _task_errors(f"the new card of task {number}", card)
            titles.append(card["title"])
        elif entry["action"] == "keep" and number in left:
            titles.append(left[number])
    return errors + _repeated(titles)


def save(run_dir: Path, plan: dict, name: str = PLAN_FILE) -> None:
    write_atomic(run_dir / name, json.dumps(plan, ensure_ascii=False, indent=2))


def load(run_dir: Path) -> dict:
    return json.loads((run_dir / PLAN_FILE).read_text(encoding="utf-8"))


def digest(run_dir: Path) -> str:
    return hashlib.sha256((run_dir / PLAN_FILE).read_bytes()).hexdigest()


def cards(plan: dict) -> list[tuple[str, dict]]:
    """The (title, card) pairs of the tasks of `plan`, in their order."""
    return [card(task) for task in plan["tasks"]]


def card(task: dict) -> tuple[str, dict]:
    """The title and the card of a task of a plan."""
    return task["title"], {key: value for key, value in task.items() if key != "title"}


def _repeated(titles: list[str]) -> list[str]:
    counts = Counter(title.strip() for title in titles)
    return [f"{n} tasks have the title {title!r}" for title, n in counts.items() if title and n > 1]


def _task_errors(name: str, task: dict) -> list[str]:
    errors = []
    if not task["title"].strip():
        errors.append(f"{name} has no title")
    if not task["goal"].strip():
        errors.append(f"{name} has no goal")
    if not task["criteria"]:
        errors.append(f"{name} has no acceptance criteria")
    closed = [path for path in task["files"] if _closed(path)]
    if closed:
        errors.append(
            f"{name} lists files closed to the sessions: {', '.join(closed)};"
            " the operator changes them by hand"
        )
    return errors


def _closed(path: str) -> bool:
    # In any case, as the sandbox matches them: the file system ignores it.
    path = PurePosixPath(path.strip().lower())
    top = path.as_posix()
    if any(top == name or top.startswith(f"{name}/") for name in protected.OPERATOR_FILES):
        return True
    secrets = (*protected.SECRETS, protected.ENV_FILES)
    return any(fnmatch.fnmatchcase(path.name, pattern) for pattern in secrets)
