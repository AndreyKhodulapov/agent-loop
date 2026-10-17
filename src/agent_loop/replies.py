"""`QUESTIONS.md` of a run: the questions to the operator, from the planner or from a task, and
the replies the operator writes under them for `loop answer`."""

from __future__ import annotations

import re
from pathlib import Path

from agent_loop.files import write_atomic

FILE = "QUESTIONS.md"
ANSWER_HEADING = "### Answer"


class RepliesError(Exception):
    pass


def ask(run_dir: Path, run_id: str, questions: list[str]) -> Path:
    """Write `questions` to the file of the run, unless it holds them already: then the replies
    the operator has begun stay. Returns the path of the file."""
    path = run_dir / FILE
    if [question for question, _ in _pairs(path)] != [question.strip() for question in questions]:
        sections = [
            f"# Questions of run {run_id}",
            f"Write each answer under its `{ANSWER_HEADING}` line, then run"
            f" `loop answer {run_id}`.",
        ]
        for number, question in enumerate(questions, start=1):
            sections += [f"## Question {number}", question.strip(), ANSWER_HEADING]
        write_atomic(path, "\n\n".join(sections) + "\n\n")
    return path


def read(run_dir: Path, run_id: str, questions: list[str]) -> list[str]:
    """The replies of the operator to `questions`, in their order.

    Raises `RepliesError` when a reply is missing, or when the file does not hold these
    questions: then it is written anew.
    """
    path = run_dir / FILE
    pairs = _pairs(path)
    if [question for question, _ in pairs] != [question.strip() for question in questions]:
        ask(run_dir, run_id, questions)
        raise RepliesError(f"{path} is written anew with the questions, answer them there")
    replies = [reply for _, reply in pairs]
    missing = [str(number) for number, reply in enumerate(replies, start=1) if not reply]
    if missing:
        raise RepliesError(f"{path}: no answer to question {', '.join(missing)}")
    return replies


def _pairs(path: Path) -> list[tuple[str, str]]:
    """(question, reply) pairs of the file, both stripped; none without the file."""
    if not path.is_file():
        return []
    sections = re.split(r"^## Question \d+$", path.read_text(encoding="utf-8"), flags=re.M)
    pairs = []
    for section in sections[1:]:
        question, _, reply = section.partition(f"\n{ANSWER_HEADING}\n")
        pairs.append((question.strip(), reply.strip()))
    return pairs
