"""Markdown of a run for the operator, in the language of the docs: `PLAN.md` of the plan.

The files go to the run directory and, when `knowledge_dir` is set, to the history of the
repository there, `<knowledge_dir>/<repo>/<date>_<run>/`, next to its profile: the history is
synced between machines, the run directory is not.
"""

from __future__ import annotations

from pathlib import Path

from agent_loop.config import Settings
from agent_loop.files import write_atomic
from agent_loop.store import Run

PLAN_MD = "PLAN.md"
LABELS = {
    "en": {
        "plan": "Plan",
        "run": "Run `{run}` on `{branch}`.",
        "goal": "Goal",
        "criteria": "Acceptance criteria",
        "decisions": "Decisions",
        "tasks": "Tasks",
        "files": "Files",
        "tests": "Tests",
        "findings": "Findings of the critic",
        "blocking": "Blocking",
        "other": "Not blocking",
        "of_tasks": "tasks",
        "scenario": "Scenario",
        "no_findings": "The critic has found nothing.",
    },
    "ru": {
        "plan": "План",
        "run": "Прогон `{run}` на ветке `{branch}`.",
        "goal": "Цель",
        "criteria": "Критерии приёмки",
        "decisions": "Решения",
        "tasks": "Задачи",
        "files": "Файлы",
        "tests": "Тесты",
        "findings": "Находки критика",
        "blocking": "Блокирующие",
        "other": "Не блокирующие",
        "of_tasks": "задачи",
        "scenario": "Сценарий",
        "no_findings": "Критик ничего не нашёл.",
    },
}


def write_plan(
    run_dir: Path, run: Run, plan: dict, findings: list[dict], settings: Settings
) -> list[Path]:
    """Render `PLAN.md` of `plan` with the `findings` of its critic; returns where it is."""
    text = render_plan(run, plan, findings, settings.doc_lang)
    paths = [run_dir / PLAN_MD]
    if settings.knowledge_dir is not None:
        history = settings.knowledge_dir / Path(run.repo).name / f"{run.created_at[:10]}_{run.id}"
        paths.append(history / PLAN_MD)
    for path in paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        write_atomic(path, text)
    return paths


def render_plan(run: Run, plan: dict, findings: list[dict], lang: str) -> str:
    labels = LABELS[lang]
    lines = [f"# {labels['plan']}", "", labels["run"].format(run=run.id, branch=run.branch), ""]
    lines += [f"## {labels['goal']}", "", plan["goal"], ""]
    lines += _section(f"## {labels['criteria']}", plan["criteria"])
    lines += _section(f"## {labels['decisions']}", plan["decisions"])
    lines += [f"## {labels['tasks']}", ""]
    for number, task in enumerate(plan["tasks"], start=1):
        lines += [f"### {number}. {task['title']}", "", task["goal"], ""]
        for key in ("criteria", "files", "tests", "decisions"):
            lines += _section(f"{labels[key]}:", task[key])
    lines += [f"## {labels['findings']}", ""]
    if not findings:
        lines += [labels["no_findings"], ""]
    for title, blocking in ((labels["blocking"], True), (labels["other"], False)):
        items = [_finding(f, labels) for f in findings if f["blocking"] == blocking]
        lines += _section(f"### {title}", items)
    return "\n".join(lines)


def _section(title: str, items: list[str]) -> list[str]:
    if not items:
        return []
    # A line of an item that has several goes under the item, not after the list.
    return [title, "", *(f"- {item}".replace("\n", "\n  ") for item in items), ""]


def _finding(finding: dict, labels: dict[str, str]) -> str:
    text = finding["problem"]
    if finding["tasks"]:
        text += f" ({labels['of_tasks']} {', '.join(map(str, finding['tasks']))})"
    if finding["scenario"].strip():
        text += f"\n{labels['scenario']}: {finding['scenario']}"
    return text
