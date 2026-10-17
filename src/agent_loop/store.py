"""Live state of runs on this machine: runs, tasks, steps and status transitions in SQLite.

A status change is checked against the transition table and written together with its
transition record in one transaction, so a crash or Ctrl+C leaves either the old state or the
new one. Several loops and `loop status` may open the same file at once: writes take the lock
before reading the current status, readers are not blocked thanks to WAL.
"""

from __future__ import annotations

import contextlib
import json
import secrets
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agent_loop.agent import Limits

DB_FILE = "loop.db"

RUN_TRANSITIONS = {
    "planning": {"approved"},
    "approved": {"running"},
    "running": {"paused", "done"},
    "paused": {"running"},
    "done": set(),
}

# Pause comes from any stage that runs a session or a long command; the orchestrator resumes
# into the stage of the step that was paused, its kind is in `steps`, or sends a task that keeps
# pausing back to the planner.
TASK_TRANSITIONS = {
    "pending": {"implementing", "dropped"},
    "implementing": {"gating", "paused", "needs_human", "replan"},
    "gating": {"committed", "fixing", "implementing", "paused", "needs_human", "replan"},
    "fixing": {"gating", "paused", "needs_human", "replan"},
    "committed": {"reviewing", "done"},
    "reviewing": {"fixing", "done", "paused"},
    "paused": {"implementing", "gating", "fixing", "reviewing", "replan"},
    "needs_human": {"implementing", "fixing"},
    "replan": {"pending", "dropped"},
    "done": set(),
    "dropped": set(),
}

# Applied in order; `PRAGMA user_version` holds how many are applied. Never edit a released one,
# append a new script instead.
MIGRATIONS = [
    """
    CREATE TABLE runs (
        id TEXT PRIMARY KEY,
        repo TEXT NOT NULL,
        branch TEXT NOT NULL,
        status TEXT NOT NULL,
        reason TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE TABLE tasks (
        id INTEGER PRIMARY KEY,
        run_id TEXT NOT NULL REFERENCES runs (id),
        seq INTEGER NOT NULL,
        title TEXT NOT NULL,
        card TEXT NOT NULL,
        status TEXT NOT NULL,
        reason TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE (run_id, seq)
    );
    CREATE TABLE steps (
        id INTEGER PRIMARY KEY,
        task_id INTEGER NOT NULL REFERENCES tasks (id),
        kind TEXT NOT NULL,
        checkpoint TEXT NOT NULL,
        session_id TEXT,
        outcome TEXT,
        detail TEXT,
        started_at TEXT NOT NULL,
        finished_at TEXT
    );
    CREATE INDEX steps_task ON steps (task_id);
    CREATE TABLE transitions (
        id INTEGER PRIMARY KEY,
        run_id TEXT NOT NULL REFERENCES runs (id),
        task_id INTEGER REFERENCES tasks (id),
        src TEXT NOT NULL,
        dst TEXT NOT NULL,
        reason TEXT,
        at TEXT NOT NULL
    );
    CREATE INDEX transitions_run ON transitions (run_id);
    """,
    # Utilization seen during a run; a window may be missing from an event.
    """
    CREATE TABLE limit_snapshots (
        id INTEGER PRIMARY KEY,
        run_id TEXT NOT NULL REFERENCES runs (id),
        step_id INTEGER REFERENCES steps (id),
        five_hour REAL,
        five_hour_resets_at INTEGER,
        seven_day REAL,
        seven_day_resets_at INTEGER,
        at TEXT NOT NULL
    );
    CREATE INDEX limit_snapshots_run ON limit_snapshots (run_id);
    """,
    # Untracked files before a step: only the files that appear after it belong to the step.
    """
    ALTER TABLE steps ADD COLUMN untracked TEXT NOT NULL DEFAULT '[]';
    """,
    # Tests found before the first step of a task: the gate checks that the task loses none.
    """
    ALTER TABLE tasks ADD COLUMN tests INTEGER;
    """,
    # HEAD at the start of the run: what the run has done is counted from it.
    """
    ALTER TABLE runs ADD COLUMN base TEXT;
    """,
    # A step belongs to its run: a planner step has no task. SQLite cannot drop NOT NULL, so the
    # table is rebuilt.
    """
    CREATE TABLE new_steps (
        id INTEGER PRIMARY KEY,
        run_id TEXT NOT NULL REFERENCES runs (id),
        task_id INTEGER REFERENCES tasks (id),
        kind TEXT NOT NULL,
        checkpoint TEXT NOT NULL,
        session_id TEXT,
        outcome TEXT,
        detail TEXT,
        started_at TEXT NOT NULL,
        finished_at TEXT,
        untracked TEXT NOT NULL DEFAULT '[]'
    );
    INSERT INTO new_steps (id, run_id, task_id, kind, checkpoint, session_id, outcome, detail,
        started_at, finished_at, untracked)
    SELECT steps.id, tasks.run_id, steps.task_id, steps.kind, steps.checkpoint,
        steps.session_id, steps.outcome, steps.detail, steps.started_at, steps.finished_at,
        steps.untracked
    FROM steps JOIN tasks ON tasks.id = steps.task_id;
    DROP TABLE steps;
    ALTER TABLE new_steps RENAME TO steps;
    CREATE INDEX steps_task ON steps (task_id);
    """,
    # The sha256 of the plan the operator has approved: a run starts only from that plan.
    """
    ALTER TABLE runs ADD COLUMN plan_hash TEXT;
    """,
]


class StoreError(Exception):
    pass


@dataclass(frozen=True)
class Run:
    id: str
    repo: str
    branch: str
    status: str
    reason: str | None
    created_at: str
    updated_at: str
    # None for a run created before the column.
    base: str | None
    # None until the plan is approved and for a run approved from a cards file.
    plan_hash: str | None


@dataclass(frozen=True)
class Task:
    id: int
    run_id: str
    seq: int
    title: str
    card: dict
    status: str
    reason: str | None
    created_at: str
    updated_at: str
    tests: int | None


@dataclass(frozen=True)
class Step:
    id: int
    run_id: str
    # None for a step of the planner.
    task_id: int | None
    kind: str
    checkpoint: str
    session_id: str | None
    outcome: str | None
    detail: dict | None
    started_at: str
    finished_at: str | None
    untracked: list[str]


@dataclass(frozen=True)
class Transition:
    id: int
    run_id: str
    task_id: int | None
    src: str
    dst: str
    reason: str | None
    at: str


@dataclass(frozen=True)
class LimitSnapshot:
    id: int
    run_id: str
    step_id: int | None
    five_hour: float | None
    five_hour_resets_at: int | None
    seven_day: float | None
    seven_day_resets_at: int | None
    at: str


class Store:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        # autocommit=True: transactions are opened only by `_transaction`, and executescript
        # does not commit a migration halfway.
        self._conn = sqlite3.connect(path, autocommit=True)
        try:
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode = WAL")
            # Off while migrating: a rebuilt table is dropped under the references of other
            # tables. `_migrate` checks the references before it commits.
            self._migrate()
            self._conn.execute("PRAGMA foreign_keys = ON")
        except BaseException:
            self._conn.close()
            raise

    def close(self) -> None:
        self._conn.close()

    def create_run(self, repo: Path, branch: str, base: str) -> str:
        run_id = secrets.token_hex(4)
        now = _now()
        self._conn.execute(
            "INSERT INTO runs (id, repo, branch, base, status, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, 'planning', ?, ?)",
            (run_id, str(repo), branch, base, now, now),
        )
        return run_id

    def add_task(self, run_id: str, title: str, card: dict) -> int:
        now = _now()
        cursor = self._conn.execute(
            "INSERT INTO tasks (run_id, seq, title, card, status, created_at, updated_at)"
            " SELECT ?, COALESCE(MAX(seq), 0) + 1, ?, ?, 'pending', ?, ? FROM tasks"
            " WHERE run_id = ?",
            (run_id, title, json.dumps(card, ensure_ascii=False), now, now, run_id),
        )
        return cursor.lastrowid

    def approve(self, run_id: str, plan_hash: str, tasks: list[tuple[str, dict]]) -> None:
        """Approve the planning run with the (title, card) pairs of its tasks, in their order,
        and the hash of the plan they come from."""
        with self._transaction():
            self._move_run(run_id, "approved", None)
            for title, card in tasks:
                self.add_task(run_id, title, card)
            self._conn.execute("UPDATE runs SET plan_hash = ? WHERE id = ?", (plan_hash, run_id))

    def move_run(self, run_id: str, dst: str, reason: str | None = None) -> None:
        """Change the run status; `reason` is kept until the next move and cleared by it."""
        with self._transaction():
            self._move_run(run_id, dst, reason)

    def move_task(self, task_id: int, dst: str, reason: str | None = None) -> None:
        """Change the task status; `reason` is kept until the next move and cleared by it."""
        with self._transaction():
            row = self._conn.execute(
                "SELECT run_id, status FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            if row is None:
                raise StoreError(f"task {task_id} not found")
            _check(TASK_TRANSITIONS, f"task {task_id}", row["status"], dst)
            now = _now()
            self._conn.execute(
                "UPDATE tasks SET status = ?, reason = ?, updated_at = ? WHERE id = ?",
                (dst, reason, now, task_id),
            )
            self._record(row["run_id"], task_id, row["status"], dst, reason, now)

    def set_tests(self, task_id: int, count: int) -> None:
        self._conn.execute("UPDATE tasks SET tests = ? WHERE id = ?", (count, task_id))

    def start_step(
        self,
        run_id: str,
        task_id: int | None,
        kind: str,
        checkpoint: str,
        untracked: list[str],
        session_id: str | None = None,
    ) -> int:
        """Record a step of `run_id` before it runs; `task_id` is None for the planner.

        `checkpoint` is HEAD to roll back to if it never ends, `untracked` are the untracked
        files before it. The session id is stored up front, so a session that outlives a killed
        orchestrator can still be found and resumed.
        """
        cursor = self._conn.execute(
            "INSERT INTO steps"
            " (run_id, task_id, kind, checkpoint, untracked, session_id, started_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                run_id,
                task_id,
                kind,
                checkpoint,
                json.dumps(untracked, ensure_ascii=False),
                session_id,
                _now(),
            ),
        )
        return cursor.lastrowid

    def finish_step(self, step_id: int, outcome: str, detail: dict | None = None) -> None:
        cursor = self._conn.execute(
            "UPDATE steps SET outcome = ?, detail = ?, finished_at = ?"
            " WHERE id = ? AND finished_at IS NULL",
            (
                outcome,
                None if detail is None else json.dumps(detail, ensure_ascii=False),
                _now(),
                step_id,
            ),
        )
        if cursor.rowcount == 0:
            raise StoreError(f"step {step_id} is already finished or does not exist")

    def record_limits(self, run_id: str, step_id: int | None, limits: Limits) -> None:
        """Keep a snapshot of the limits; `step_id` is None for a probe outside any step."""
        five_hour, seven_day = limits.five_hour, limits.seven_day
        self._conn.execute(
            "INSERT INTO limit_snapshots (run_id, step_id, five_hour, five_hour_resets_at,"
            " seven_day, seven_day_resets_at, at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                run_id,
                step_id,
                five_hour and five_hour.utilization,
                five_hour and five_hour.resets_at,
                seven_day and seven_day.utilization,
                seven_day and seven_day.resets_at,
                _now(),
            ),
        )

    def run(self, run_id: str) -> Run:
        row = self._conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            raise StoreError(f"run {run_id} not found")
        return Run(**row)

    def runs(self, status: str) -> list[Run]:
        rows = self._conn.execute(
            "SELECT * FROM runs WHERE status = ? ORDER BY created_at", (status,)
        ).fetchall()
        return [Run(**row) for row in rows]

    def tasks(self, run_id: str) -> list[Task]:
        rows = self._conn.execute(
            "SELECT * FROM tasks WHERE run_id = ? ORDER BY seq", (run_id,)
        ).fetchall()
        return [Task(**{**row, "card": json.loads(row["card"])}) for row in rows]

    def steps(self, task_id: int) -> list[Step]:
        rows = self._conn.execute(
            "SELECT * FROM steps WHERE task_id = ? ORDER BY id", (task_id,)
        ).fetchall()
        return [_step(row) for row in rows]

    def planner_steps(self, run_id: str) -> list[Step]:
        """Steps of the run without a task: the sessions of its planner."""
        rows = self._conn.execute(
            "SELECT * FROM steps WHERE run_id = ? AND task_id IS NULL ORDER BY id", (run_id,)
        ).fetchall()
        return [_step(row) for row in rows]

    def session_steps(self, session_id: str) -> list[Step]:
        rows = self._conn.execute(
            "SELECT * FROM steps WHERE session_id = ? ORDER BY id", (session_id,)
        ).fetchall()
        return [_step(row) for row in rows]

    def open_steps(self, run_id: str) -> list[Step]:
        """Steps of the tasks of the run that started and never finished."""
        rows = self._conn.execute(
            "SELECT steps.* FROM steps JOIN tasks ON tasks.id = steps.task_id"
            " WHERE tasks.run_id = ? AND steps.finished_at IS NULL ORDER BY steps.id",
            (run_id,),
        ).fetchall()
        return [_step(row) for row in rows]

    def transitions(self, run_id: str) -> list[Transition]:
        rows = self._conn.execute(
            "SELECT * FROM transitions WHERE run_id = ? ORDER BY id", (run_id,)
        ).fetchall()
        return [Transition(**row) for row in rows]

    def limit_snapshots(self, run_id: str) -> list[LimitSnapshot]:
        rows = self._conn.execute(
            "SELECT * FROM limit_snapshots WHERE run_id = ? ORDER BY id", (run_id,)
        ).fetchall()
        return [LimitSnapshot(**row) for row in rows]

    @contextlib.contextmanager
    def _transaction(self) -> Iterator[None]:
        # IMMEDIATE takes the write lock up front, so a concurrent writer cannot change the
        # status between our read and our update.
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        self._conn.execute("COMMIT")

    def _move_run(self, run_id: str, dst: str, reason: str | None) -> None:
        row = self._conn.execute("SELECT status FROM runs WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            raise StoreError(f"run {run_id} not found")
        _check(RUN_TRANSITIONS, f"run {run_id}", row["status"], dst)
        now = _now()
        self._conn.execute(
            "UPDATE runs SET status = ?, reason = ?, updated_at = ? WHERE id = ?",
            (dst, reason, now, run_id),
        )
        self._record(run_id, None, row["status"], dst, reason, now)

    def _record(
        self, run_id: str, task_id: int | None, src: str, dst: str, reason: str | None, at: str
    ) -> None:
        self._conn.execute(
            "INSERT INTO transitions (run_id, task_id, src, dst, reason, at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (run_id, task_id, src, dst, reason, at),
        )

    def _migrate(self) -> None:
        with self._transaction():
            version = self._conn.execute("PRAGMA user_version").fetchone()[0]
            if version > len(MIGRATIONS):
                raise StoreError(
                    f"database schema v{version} is newer than this agent-loop"
                    f" (v{len(MIGRATIONS)}), update agent-loop"
                )
            for number, script in enumerate(MIGRATIONS[version:], start=version + 1):
                self._conn.executescript(script)
                self._conn.execute(f"PRAGMA user_version = {number}")
            if version < len(MIGRATIONS):
                broken = self._conn.execute("PRAGMA foreign_key_check").fetchall()
                if broken:
                    raise StoreError(f"the migration breaks {len(broken)} references")


def _check(allowed: dict[str, set[str]], what: str, src: str, dst: str) -> None:
    if dst not in allowed[src]:
        raise StoreError(f"{what}: {src} -> {dst} is not allowed")


def _step(row: sqlite3.Row) -> Step:
    detail = None if row["detail"] is None else json.loads(row["detail"])
    return Step(**{**row, "detail": detail, "untracked": json.loads(row["untracked"])})


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")
