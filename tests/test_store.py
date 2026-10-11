import contextlib
import sqlite3
from pathlib import Path

import pytest

from agent_loop import store

CARD = {"goal": "Добавить схему", "acceptance": ["tests pass"]}


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "state" / "loop.db"


@pytest.fixture
def db(db_path):
    opened = store.Store(db_path)
    yield opened
    opened.close()


@pytest.fixture
def run_id(db):
    return db.create_run(Path("/work/service"), "feature/x")


def connect(path):
    return contextlib.closing(sqlite3.connect(path))


def user_version(path):
    with connect(path) as conn:
        return conn.execute("PRAGMA user_version").fetchone()[0]


def test_new_database_is_migrated_and_reopened_without_loss(db_path):
    first = store.Store(db_path)
    run_id = first.create_run(Path("/work/service"), "main")
    first.close()

    second = store.Store(db_path)
    try:
        assert second.run(run_id).branch == "main"
    finally:
        second.close()
    assert user_version(db_path) == len(store.MIGRATIONS)


def test_newer_schema_is_rejected(db_path):
    store.Store(db_path).close()
    with connect(db_path) as conn:
        conn.execute("PRAGMA user_version = 99")

    with pytest.raises(store.StoreError, match="schema v99 is newer"):
        store.Store(db_path)


def test_failed_migration_leaves_previous_version(db_path, monkeypatch):
    store.Store(db_path).close()
    broken = "CREATE TABLE limits (at TEXT); INSERT INTO missing VALUES (1);"
    monkeypatch.setattr(store, "MIGRATIONS", [*store.MIGRATIONS, broken])

    with pytest.raises(sqlite3.OperationalError, match="missing"):
        store.Store(db_path)

    assert user_version(db_path) == 1
    with connect(db_path) as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master")}
    assert "limits" not in tables


def test_run_lifecycle_records_transitions(db, run_id):
    assert db.run(run_id).status == "planning"

    db.move_run(run_id, "approved")
    db.move_run(run_id, "running")
    db.move_run(run_id, "paused", reason="budget")
    assert db.run(run_id).reason == "budget"
    db.move_run(run_id, "running")
    db.move_run(run_id, "done")

    run = db.run(run_id)
    assert (run.status, run.reason) == ("done", None)
    assert [(t.task_id, t.src, t.dst, t.reason) for t in db.transitions(run_id)] == [
        (None, "planning", "approved", None),
        (None, "approved", "running", None),
        (None, "running", "paused", "budget"),
        (None, "paused", "running", None),
        (None, "running", "done", None),
    ]


def test_task_goes_through_fix_and_review_loops(db, run_id):
    task_id = db.add_task(run_id, "store", CARD)
    path = [
        "implementing",
        "gating",
        "fixing",
        "gating",
        "committed",
        "reviewing",
        "fixing",
        "gating",
        "committed",
        "done",
    ]

    for status in path:
        db.move_task(task_id, status)

    assert db.tasks(run_id)[0].status == "done"
    assert [t.dst for t in db.transitions(run_id) if t.task_id == task_id] == path


@pytest.mark.parametrize(
    ("path", "bad"),
    [
        ([], "committed"),
        (["implementing", "gating", "committed", "done"], "implementing"),
        (["implementing", "paused"], "done"),
        ([], "unknown"),
    ],
)
def test_disallowed_task_move_changes_nothing(db, run_id, path, bad):
    task_id = db.add_task(run_id, "store", CARD)
    for status in path:
        db.move_task(task_id, status)

    with pytest.raises(store.StoreError, match=f"-> {bad} is not allowed"):
        db.move_task(task_id, bad)

    assert db.tasks(run_id)[0].status == (path[-1] if path else "pending")
    assert len(db.transitions(run_id)) == len(path)


def test_interrupted_move_is_rolled_back(db, run_id, monkeypatch):
    def interrupt(*args):
        raise KeyboardInterrupt

    monkeypatch.setattr(db, "_record", interrupt)

    with pytest.raises(KeyboardInterrupt):
        db.move_run(run_id, "approved")

    monkeypatch.undo()
    assert db.run(run_id).status == "planning"
    db.move_run(run_id, "approved")
    assert [t.dst for t in db.transitions(run_id)] == ["approved"]


def test_disallowed_run_move(db, run_id):
    with pytest.raises(store.StoreError, match="planning -> running is not allowed"):
        db.move_run(run_id, "running")

    assert db.run(run_id).status == "planning"


@pytest.mark.parametrize(
    "call",
    [
        lambda db: db.run("nope"),
        lambda db: db.move_run("nope", "approved"),
        lambda db: db.move_task(404, "implementing"),
    ],
)
def test_missing_run_or_task(db, call):
    with pytest.raises(store.StoreError, match="not found"):
        call(db)


@pytest.mark.parametrize("table", [store.RUN_TRANSITIONS, store.TASK_TRANSITIONS])
def test_transition_targets_are_known_states(table):
    assert set().union(*table.values()) <= set(table)


def test_tasks_are_numbered_per_run(db, run_id):
    other = db.create_run(Path("/work/other"), "main")

    db.add_task(run_id, "first", CARD)
    db.add_task(other, "elsewhere", {})
    db.add_task(run_id, "second", {})

    tasks = db.tasks(run_id)
    assert [(t.seq, t.title, t.status) for t in tasks] == [
        (1, "first", "pending"),
        (2, "second", "pending"),
    ]
    assert tasks[0].card == CARD
    assert [t.seq for t in db.tasks(other)] == [1]


def test_step_is_started_and_finished_once(db, run_id):
    task_id = db.add_task(run_id, "store", CARD)

    step_id = db.start_step(task_id, "implement", checkpoint="abc123")
    started = db.steps(task_id)[0]
    assert (started.kind, started.checkpoint, started.finished_at) == ("implement", "abc123", None)

    db.finish_step(step_id, "paused", session_id="s-1", detail={"reason": "контекст"})
    finished = db.steps(task_id)[0]
    assert (finished.outcome, finished.session_id) == ("paused", "s-1")
    assert finished.detail == {"reason": "контекст"}
    assert finished.finished_at is not None

    with pytest.raises(store.StoreError, match="already finished"):
        db.finish_step(step_id, "done")
