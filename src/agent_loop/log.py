"""Terminal log: every line carries the run, task and stage it belongs to.

14:03:12 [run 3f2a] [task 2/7] [implement] ctx 84k/150k
"""

from __future__ import annotations

import contextlib
import contextvars
import logging
import sys
from collections.abc import Iterator
from dataclasses import dataclass, replace

LOGGER_NAME = "agent_loop"


@dataclass(frozen=True)
class Scope:
    run: str | None = None
    task: str | None = None
    stage: str | None = None

    def prefix(self) -> str:
        parts = []
        if self.run:
            parts.append(f"[run {self.run}]")
        if self.task:
            parts.append(f"[task {self.task}]")
        if self.stage:
            parts.append(f"[{self.stage}]")
        return " ".join(parts)


_current = contextvars.ContextVar("scope", default=Scope())  # noqa: B039 - Scope is frozen


@contextlib.contextmanager
def scope(
    run: str | None = None, task: str | None = None, stage: str | None = None
) -> Iterator[None]:
    """Narrow the scope for the lines logged inside the block; unset fields keep the outer value."""
    outer = _current.get()
    token = _current.set(
        replace(outer, run=run or outer.run, task=task or outer.task, stage=stage or outer.stage)
    )
    try:
        yield
    finally:
        _current.reset(token)


class _Formatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        parts = [self.formatTime(record, "%H:%M:%S")]
        if record.levelno >= logging.WARNING:
            parts.append(record.levelname)
        prefix = _current.get().prefix()
        if prefix:
            parts.append(prefix)
        parts.append(record.getMessage())
        line = " ".join(parts)
        if record.exc_info:
            line += "\n" + self.formatException(record.exc_info)
        return line


def setup(verbose: bool = False) -> None:
    logger = logging.getLogger(LOGGER_NAME)
    logger.handlers.clear()
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(_Formatter())
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    logger.propagate = False
