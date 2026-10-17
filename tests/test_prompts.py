import dataclasses
import subprocess
from pathlib import Path

import pytest

from agent_loop import git, prompts
from agent_loop.config import Settings
from agent_loop.gates import Verdict
from agent_loop.store import Task

SETTINGS = Settings(doc_lang="ru", diff_soft_lines=800, diff_hard_lines=1200)
CARD = {
    "goal": "Add the prompt module.",
    "criteria": ["The prompt has the card", "Tests pass"],
    "files": ["src/agent_loop/prompts/__init__.py"],
    "tests": [],
    "decisions": ["The fixer runs in a new session"],
}
TASK = Task(
    id=7,
    run_id="r1",
    seq=2,
    title="Prompt of a task",
    card=CARD,
    status="implementing",
    reason=None,
    created_at="2026-10-11T10:00:00+00:00",
    updated_at="2026-10-11T10:00:00+00:00",
    tests=None,
)


def commit(repo, message):
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", message], cwd=repo, check=True)


@pytest.mark.parametrize(
    ("role", "rules"),
    [
        ("implementer", ["session.md", "implementer.md", "AGENTS.md"]),
        ("fixer", ["session.md", "fixer.md", "AGENTS.md"]),
        # The planner has no task to work on and commit.
        ("planner", ["planner.md", "AGENTS.md"]),
    ],
)
def test_system_files_of_a_role(role, rules, tmp_path):
    context = tmp_path / "CLAUDE.md"
    context.write_text("Project context.")

    files = prompts.system_files(role, Settings(context_file=context))

    assert [path.name for path in files] == [*rules, "CLAUDE.md"]
    assert all(path.is_file() for path in files)
    root = Path(__file__).parents[1]
    assert files[-2].read_text() == (root / "AGENTS.md").read_text()
    assert prompts.system_files(role, Settings()) == files[:-1]


def test_answer_matches_the_schema():
    fields = {field.name for field in dataclasses.fields(prompts.Answer)}
    assert fields == set(prompts.ANSWER_SCHEMA["properties"])
    assert set(prompts.ANSWER_SCHEMA["required"]) == {
        field.name
        for field in dataclasses.fields(prompts.Answer)
        if field.default is dataclasses.MISSING
    }

    done = prompts.answer(
        {"status": "done", "summary": "s", "commit_message": "feat(x): y", "notes_for_next": ""}
    )
    too_big = prompts.answer(
        {
            "status": "too_big",
            "summary": "s",
            "commit_message": "feat(x): y",
            "notes_for_next": "",
            "split": ["first", "second"],
        }
    )
    with_actions = prompts.answer(
        {
            "status": "done",
            "summary": "s",
            "commit_message": "feat(x): y",
            "notes_for_next": "",
            "operator_actions": ["add JWT_SECRET= to .env.example"],
        }
    )

    assert done == prompts.Answer("done", "s", "feat(x): y", "")
    assert too_big.split == ("first", "second")
    assert with_actions.operator_actions == ("add JWT_SECRET= to .env.example",)


def test_implementer_prompt_has_the_card_and_the_rules():
    prompt = prompts.implementer(TASK, 5, SETTINGS, "M app.py: run")

    assert prompt == (
        "# Task 2 of 5: Prompt of a task\n\n"
        "## Goal\n\nAdd the prompt module.\n\n"
        "## Acceptance criteria\n\n- The prompt has the card\n- Tests pass\n\n"
        "## Files to start from\n\n- src/agent_loop/prompts/__init__.py\n\n"
        "## Decisions of the plan for this task\n\n- The fixer runs in a new session\n\n"
        "## Size\n\nThe work of the task, tests included, fits in 800 changed lines."
        " A diff over 1200 lines goes back to the planner.\n\n"
        "## Language\n\nWrite code comments, docstrings and documentation in Russian."
        " The commit message is in English.\n\n"
        "## Previous tasks of the run\n\nM app.py: run"
    )


def test_planner_prompt_has_the_request_and_the_rules():
    request = "# Dishes\n\nAdd the admin of dishes:\n```\nGET /dishes\n```\n"

    prompt = prompts.planner(request, SETTINGS)

    assert prompt == (
        "# Request of the operator\n\n"
        f"````markdown\n{request.rstrip()}\n````\n\n"
        "## Size\n\nThe work of one task, tests included, fits in 800 changed lines."
        " A task over 1200 lines comes back to the planner.\n\n"
        "## Language\n\nWrite the plan in Russian: the operator reads and approves it."
    )


def test_first_task_has_no_previous_tasks_section():
    prompt = prompts.implementer(TASK, 5, SETTINGS, "")

    assert "Previous tasks" not in prompt
    assert "Resuming" not in prompt


@pytest.mark.parametrize(
    ("handoff", "expected"),
    [
        ("Done: the module. Left: tests.", "Done: the module. Left: tests."),
        (None, "The session stopped without a handoff: go by the work in progress."),
    ],
)
def test_resumed_task_gets_the_handoff_and_the_work(handoff, expected):
    work = "+++ b/README.md\n+```python\n+x = 1\n+```"

    prompt = prompts.implementer(TASK, 5, SETTINGS, "", handoff, work)

    resumed = prompt.split("## Resuming the task\n\n")[1]
    assert resumed.split("\n\n")[1:] == [
        "### Handoff of the previous session",
        expected,
        "### Work in progress against the start of the task",
        f"````diff\n{work}\n````",
    ]


def test_fixer_prompt_has_the_work_and_the_findings():
    verdict = Verdict("fixing", "lint_failed", ("lint: exit 1\nE501", "test: exit 1\nFAILED"))

    prompt = prompts.fixer(TASK, 5, SETTINGS, verdict, "+x = 1\n")

    assert prompt.startswith("# Task 2 of 5: Prompt of a task\n\n## Goal")
    assert prompt.endswith(
        "## Work of the task against its start\n\n```diff\n+x = 1\n```\n\n"
        "## Findings of the checks: lint_failed\n\n"
        "```\nlint: exit 1\nE501\n```\n\n```\ntest: exit 1\nFAILED\n```"
    )


def test_resumed_fixer_gets_the_handoff():
    verdict = Verdict("fixing", "test_failed", ("test: exit 1",))

    prompt = prompts.fixer(TASK, 5, SETTINGS, verdict, "+x = 1\n", "Fixed x, y is left.")

    assert prompt.endswith(
        "```\ntest: exit 1\n```\n\n"
        "## Resuming the fix\n\nA previous fixer session stopped before it finished. Its work"
        " is in the working tree and in the diff above: continue it.\n\n"
        "### Handoff of the previous session\n\nFixed x, y is left."
    )


def test_previous_tasks_list_files_with_new_public_names(repo):
    base = git.head(repo)
    (repo / "app.py").write_text("x = 1\n\n\ndef run():\n    pass\n\n\ndef _helper():\n    pass\n")
    (repo / "old.py").unlink()
    (repo / "pkg").mkdir()
    (repo / "pkg" / "store.py").write_text(
        "LIMIT: int = 3\n_cache = {}\ntype Row = dict\n\n\n"
        "class Store:\n    def __init__(self):\n        pass\n\n"
        "    def save(self):\n        pass\n\n\n"
        "class _Hidden:\n    def run(self):\n        pass\n\n\n"
        "async def fetch():\n    pass\n"
    )
    (repo / "pkg" / "template.py").write_text("{{ name }} = 1\n")
    (repo / "notes.md").write_text("notes\n")
    commit(repo, "tasks 1 and 2")

    summary = prompts.previous_tasks(
        repo, base, [("Store", "Use Store.save for rows."), ("Notes", " ")]
    )

    assert summary == (
        "Files changed since the start of the run (A added, M modified, D deleted),"
        " with the public names added to Python modules:\n\n"
        "M app.py: run\n"
        "A notes.md\n"
        "D old.py\n"
        "A pkg/store.py: LIMIT, Row, Store, Store.save, fetch\n"
        "A pkg/template.py\n\n"
        "Notes of the previous tasks:\n\n"
        "- Store: Use Store.save for rows."
    )


def test_previous_tasks_are_empty_at_the_start_of_the_run(repo):
    assert prompts.previous_tasks(repo, git.head(repo), []) == ""
