import json

import pytest

from agent_loop import plan, runner

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
    cards = tmp_path / "cards.json"
    cards.write_text(json.dumps(plan.load(run_dir)["tasks"]))

    tasks = plan.cards(plan.load(run_dir))

    assert [title for title, _ in tasks] == ["Schema of dishes", "Router of dishes"]
    # The tasks of `plan.json` go to `loop run` as they are.
    assert runner.load_tasks(cards) == tasks
