import pytest

from agent_loop import plan

TASK = {
    "title": "Schema of dishes",
    "goal": "Add the schema of a dish.",
    "criteria": ["A dish without a name is rejected"],
    "files": ["app/schemas.py"],
    "tests": ["tests/test_schemas.py"],
    "decisions": ["Prices are in cents"],
}
PLAN = {
    "goal": "The admin of dishes",
    "criteria": ["The admin lists, adds and removes dishes"],
    "decisions": ["The admin is a router of the API"],
    "tasks": [TASK, {**TASK, "title": "Router of dishes", "files": ["app/routers/dishes.py"]}],
}


def test_good_plan_has_no_errors():
    assert plan.validate(PLAN) == []


@pytest.mark.parametrize(
    ("change", "errors"),
    [
        ({"goal": " "}, ["the plan has no goal"]),
        ({"criteria": []}, ["the plan has no acceptance criteria"]),
        ({"tasks": []}, ["the plan has no tasks"]),
        ({"tasks": [TASK, TASK]}, ["2 tasks have the title 'Schema of dishes'"]),
        (
            {"tasks": [TASK, {**TASK, "title": " Schema of dishes"}]},
            ["2 tasks have the title 'Schema of dishes'"],
        ),
        (
            {"tasks": [{**TASK, "title": ""}, {**TASK, "title": " "}]},
            ["task 1 has no title", "task 2 has no title"],
        ),
        ({"tasks": [TASK, {**TASK, "title": "Router", "goal": ""}]}, ["task 2 has no goal"]),
        ({"tasks": [{**TASK, "criteria": []}]}, ["task 1 has no acceptance criteria"]),
    ],
    ids=[
        "no goal",
        "no criteria",
        "no tasks",
        "repeated title",
        "title repeated with spaces",
        "no title",
        "task without a goal",
        "task without criteria",
    ],
)
def test_plan_errors(change, errors):
    assert plan.validate({**PLAN, **change}) == errors


NEW = {**TASK, "title": "Prices of dishes"}
LEFT = {3: "Router of dishes", 4: "Docs of dishes"}


def replanned(tasks=(NEW,), *remaining):
    """A replan answer with `tasks` and the actions on the tasks left, keep for those not given."""
    given = {entry["task"]: entry for entry in remaining}
    return {
        "why": "Prices go first",
        "tasks": list(tasks),
        "remaining": [
            given.get(number, {"task": number, "action": "keep", "why": "Still needed"})
            for number in LEFT
        ]
        + [entry for entry in remaining if entry["task"] not in LEFT],
    }


def act(number, action, card=None):
    entry = {"task": number, "action": action, "why": "Because"}
    return entry if card is None else entry | {"card": card}


@pytest.mark.parametrize(
    ("answer", "errors"),
    [
        (replanned(), []),
        (replanned((), act(3, "drop"), act(4, "modify", {**TASK, "title": "Docs"})), []),
        (
            {**replanned(), "remaining": [act(3, "keep"), act(3, "drop"), act(7, "keep")]},
            [
                "task 7 is not one of the tasks left",
                "task 3 has 2 actions",
                "tasks 4 have no action",
            ],
        ),
        (replanned((NEW,), act(3, "modify")), ["task 3 is modified without its new card"]),
        (replanned((NEW,), act(4, "drop", TASK)), ["task 4 has a new card but is not modified"]),
        (
            replanned(
                ({**NEW, "goal": " "},), act(3, "modify", {**TASK, "title": "Docs", "criteria": []})
            ),
            ["new task 1 has no goal", "the new card of task 3 has no acceptance criteria"],
        ),
        (
            replanned(({**NEW, "title": "Router of dishes"},)),
            ["2 tasks have the title 'Router of dishes'"],
        ),
        (
            replanned(({**NEW, "title": "Schema of dishes"},)),
            ["2 tasks have the title 'Schema of dishes'"],
        ),
        (
            replanned((NEW,), act(3, "drop"), act(4, "modify", NEW)),
            ["2 tasks have the title 'Prices of dishes'"],
        ),
    ],
    ids=[
        "good",
        "no new tasks, dropped and modified",
        "wrong, repeated and missing actions",
        "modified without a card",
        "card without modify",
        "bad cards",
        "title of a task kept",
        "title of a task through",
        "title of a modified task",
    ],
)
def test_replan_errors(answer, errors):
    assert plan.validate_replan(answer, LEFT, ["Schema of dishes"]) == errors


def test_files_closed_to_the_sessions_go_back_to_the_planner():
    closed = [
        ".env.example",
        "config/server.PEM",
        "./.vscode/settings.json",
        ".Git/hooks/pre-commit",
        ".husky/pre-commit",
        ".claude/settings.json",
        "deploy/.env",
    ]
    open_ = ["app/main.py", ".gitignore", ".github/workflows/ci.yml", "app/key.py", ".claudeignore"]

    errors = plan.validate({**PLAN, "tasks": [{**TASK, "files": [*open_, *closed]}]})

    assert errors == [
        f"task 1 lists files closed to the sessions: {', '.join(closed)};"
        " the operator changes them by hand"
    ]


def test_tasks_of_a_plan_are_the_tasks_of_the_run(tmp_path):
    run_dir = tmp_path / "runs" / "r1"
    run_dir.mkdir(parents=True)
    plan.save(run_dir, PLAN)

    tasks = plan.cards(plan.load(run_dir))

    assert [title for title, _ in tasks] == ["Schema of dishes", "Router of dishes"]
    assert tasks[0][1] == {key: value for key, value in TASK.items() if key != "title"}
