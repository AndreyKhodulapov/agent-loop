"""The plan of a run: the tasks the planner proposes and the operator approves.

A plan is the answer of a planner session, checked by the CLI against `PLAN_SCHEMA`: the `goal`
of the request, its acceptance `criteria`, which also decide whether a replan needs the
operator, the `decisions` the tasks share, and the `tasks`. A task is its `title` and a card as
in the cards file of `loop run`, so the tasks of `plan.json` can go to `loop run` as they are.

`validate` finds what the schema cannot tell: its errors go back to the planner session.
"""

from __future__ import annotations

import fnmatch
import json
from collections import Counter
from pathlib import Path, PurePosixPath

from agent_loop import protected
from agent_loop.files import write_atomic
from agent_loop.store import Store

PLAN_FILE = "plan.json"
STRINGS = {"type": "array", "items": {"type": "string"}}
PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "goal": {"type": "string"},
        "criteria": STRINGS,
        "decisions": STRINGS,
        "tasks": {
            "type": "array",
            "items": {
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
            },
        },
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
    titles = Counter(task["title"].strip() for task in plan["tasks"])
    errors += [
        f"{n} tasks have the title {title!r}" for title, n in titles.items() if title and n > 1
    ]
    for number, task in enumerate(plan["tasks"], start=1):
        if not task["title"].strip():
            errors.append(f"task {number} has no title")
        if not task["goal"].strip():
            errors.append(f"task {number} has no goal")
        if not task["criteria"]:
            errors.append(f"task {number} has no acceptance criteria")
        closed = [path for path in task["files"] if _closed(path)]
        if closed:
            errors.append(
                f"task {number} lists files closed to the sessions: {', '.join(closed)};"
                " the operator changes them by hand"
            )
    return errors


def save(run_dir: Path, plan: dict, name: str = PLAN_FILE) -> None:
    write_atomic(run_dir / name, json.dumps(plan, ensure_ascii=False, indent=2))


def load(run_dir: Path) -> dict:
    return json.loads((run_dir / PLAN_FILE).read_text(encoding="utf-8"))


def add_tasks(store: Store, run_id: str, plan: dict) -> None:
    """Make the tasks of `plan` the tasks of the run, in their order."""
    for task in plan["tasks"]:
        card = {key: value for key, value in task.items() if key != "title"}
        store.add_task(run_id, task["title"], card)


def _closed(path: str) -> bool:
    # In any case, as the sandbox matches them: the file system ignores it.
    path = PurePosixPath(path.strip().lower())
    top = path.as_posix()
    if any(top == name or top.startswith(f"{name}/") for name in protected.OPERATOR_FILES):
        return True
    secrets = (*protected.SECRETS, protected.ENV_FILES)
    return any(fnmatch.fnmatchcase(path.name, pattern) for pattern in secrets)
