from agent_loop import export
from agent_loop.store import Run

RUN = Run(
    id="0a1b2c3d",
    repo="/work/service",
    branch="admin-dishes",
    status="planning",
    reason=None,
    created_at="2026-10-17T09:00:00+00:00",
    updated_at="2026-10-17T09:00:00+00:00",
    base="abc123",
    plan_hash=None,
)
PLAN = {
    "goal": "The admin of dishes",
    "criteria": ["The admin lists, adds and removes dishes"],
    "decisions": [],
    "tasks": [
        {
            "title": "Schema of dishes",
            "goal": "Add the schema of a dish.",
            "criteria": ["A dish without a name is rejected"],
            "files": ["app/schemas.py"],
            "tests": [],
            "decisions": [],
        }
    ],
}


def test_plan_md_shows_the_plan_and_the_findings_of_its_critic():
    findings = [
        {"blocking": False, "tasks": [], "problem": "Prices are not in cents", "scenario": ""},
        {
            "blocking": True,
            "tasks": [1, 2],
            "problem": "No schema",
            "scenario": "The router fails\nwithout the schema",
        },
    ]

    assert export.render_plan(RUN, PLAN, findings, "en") == (
        "# Plan\n\n"
        "Run `0a1b2c3d` on `admin-dishes`.\n\n"
        "## Goal\n\n"
        "The admin of dishes\n\n"
        "## Acceptance criteria\n\n"
        "- The admin lists, adds and removes dishes\n\n"
        "## Tasks\n\n"
        "### 1. Schema of dishes\n\n"
        "Add the schema of a dish.\n\n"
        "Acceptance criteria:\n\n"
        "- A dish without a name is rejected\n\n"
        "Files:\n\n"
        "- app/schemas.py\n\n"
        "## Findings of the critic\n\n"
        "### Blocking\n\n"
        "- No schema (tasks 1, 2)\n"
        "  Scenario: The router fails\n"
        "  without the schema\n\n"
        "### Not blocking\n\n"
        "- Prices are not in cents\n"
    )


def test_plan_md_is_in_the_language_of_the_docs():
    assert export.LABELS["ru"].keys() == export.LABELS["en"].keys()

    text = export.render_plan(RUN, PLAN, [], "ru")

    assert "Прогон `0a1b2c3d` на ветке `admin-dishes`." in text
    assert "## Критерии приёмки" in text
    assert text.endswith("## Находки критика\n\nКритик ничего не нашёл.\n")
