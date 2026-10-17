import contextlib
import sqlite3
from pathlib import Path

import pytest

from agent_loop import store
from agent_loop.agent import Limits, Window

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
    return db.create_run(Path("/work/service"), "feature/x", "abc123")


def connect(path):
    return contextlib.closing(sqlite3.connect(path))


def user_version(path):
    with connect(path) as conn:
        return conn.execute("PRAGMA user_version").fetchone()[0]


def test_new_database_is_migrated_and_reopened_without_loss(db_path):
    first = store.Store(db_path)
    run_id = first.create_run(Path("/work/service"), "main", "abc123")
    first.close()

    second = store.Store(db_path)
    try:
        assert second.run(run_id).branch == "main"
        assert second.run(run_id).base == "abc123"
    finally:
        second.close()
    assert user_version(db_path) == len(store.MIGRATIONS)


def test_newer_schema_is_rejected(db_path):
    store.Store(db_path).close()
    with connect(db_path) as conn:
        conn.execute("PRAGMA user_version = 99")

    with pytest.raises(store.StoreError, match="schema v99 is newer"):
        store.Store(db_path)


@pytest.mark.parametrize(
    ("broken", "error", "match"),
    [
        ("INSERT INTO missing VALUES (1);", sqlite3.OperationalError, "missing"),
        ("DELETE FROM runs;", store.StoreError, "breaks 1 references"),
    ],
    ids=["error", "broken reference"],
)
def test_failed_migration_leaves_previous_version(db_path, monkeypatch, broken, error, match):
    released_db = store.Store(db_path)
    released_db.add_task(released_db.create_run(Path("/work/service"), "main", "abc123"), "x", CARD)
    released_db.close()
    released = len(store.MIGRATIONS)
    monkeypatch.setattr(
        store, "MIGRATIONS", [*store.MIGRATIONS, f"CREATE TABLE limits (at TEXT); {broken}"]
    )

    with pytest.raises(error, match=match):
        store.Store(db_path)

    assert user_version(db_path) == released
    with connect(db_path) as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master")}
    assert "limits" not in tables


def test_older_database_is_migrated_with_its_data(db_path, monkeypatch):
    monkeypatch.setattr(store, "MIGRATIONS", store.MIGRATIONS[:1])
    old = store.Store(db_path)
    run_id = "0a1b2c3d"
    old._conn.execute(
        "INSERT INTO runs (id, repo, branch, status, created_at, updated_at)"
        " VALUES (?, '/work/service', 'main', 'planning', ?, ?)",
        (run_id, "2026-10-11T10:00:00+00:00", "2026-10-11T10:00:00+00:00"),
    )
    task_id = old.add_task(run_id, "store", CARD)
    old._conn.execute(
        "INSERT INTO steps (task_id, kind, checkpoint, started_at) VALUES (?, 'implement', ?, ?)",
        (task_id, "abc123", "2026-10-11T10:00:00+00:00"),
    )
    old.close()
    monkeypatch.undo()

    db = store.Store(db_path)
    try:
        db.record_limits(run_id, None, Limits(None, Window(0.64, 1791813600)))
        assert db.run(run_id).branch == "main"
        assert db.run(run_id).base is None
        assert db.run(run_id).plan_hash is None
        assert len(db.limit_snapshots(run_id)) == 1
        assert db.steps(task_id)[0].untracked == []
        assert db.steps(task_id)[0].run_id == run_id
        assert db.tasks(run_id)[0].tests is None
    finally:
        db.close()
    assert user_version(db_path) == len(store.MIGRATIONS)


def test_steps_move_to_their_run_with_their_limit_snapshots(db_path, monkeypatch):
    # The database before the table of steps is rebuilt.
    monkeypatch.setattr(store, "MIGRATIONS", store.MIGRATIONS[:5])
    old = store.Store(db_path)
    run_id = old.create_run(Path("/work/service"), "main", "abc123")
    task_id = old.add_task(run_id, "store", CARD)
    step_id = old._conn.execute(
        "INSERT INTO steps (task_id, kind, checkpoint, started_at) VALUES (?, 'implement', ?, ?)",
        (task_id, "abc123", "2026-10-11T10:00:00+00:00"),
    ).lastrowid
    old.record_limits(run_id, step_id, Limits(None, Window(0.64, 1791813600)))
    old.close()
    monkeypatch.undo()

    db = store.Store(db_path)
    try:
        [task_step] = db.steps(task_id)
        assert (task_step.id, task_step.run_id, task_step.task_id) == (step_id, run_id, task_id)
        assert [s.step_id for s in db.limit_snapshots(run_id)] == [step_id]
        planner_step = db.start_step(run_id, None, "plan", "abc123", [], "s-1")
        assert [(s.id, s.task_id) for s in db.session_steps("s-1")] == [(planner_step, None)]
        # The planner's open step has no work in progress to save.
        assert db.open_steps(run_id) == [task_step]
        with pytest.raises(sqlite3.IntegrityError):
            db.start_step("missing", None, "plan", "abc123", [])
    finally:
        db.close()


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
    assert db.runs("done") == [run]
    assert db.runs("running") == []
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


def test_approved_run_gets_its_tasks_and_the_hash_of_its_plan(db, run_id):
    db.approve(run_id, "f00d", [("schema", CARD), ("router", {})])

    run = db.run(run_id)
    assert (run.status, run.plan_hash) == ("approved", "f00d")
    assert [(t.seq, t.title, t.card) for t in db.tasks(run_id)] == [
        (1, "schema", CARD),
        (2, "router", {}),
    ]
    assert [t.dst for t in db.transitions(run_id)] == ["approved"]


def test_failed_approval_changes_nothing(db, run_id):
    with pytest.raises(TypeError):
        db.approve(run_id, "f00d", [("schema", CARD), ("router", {"owner": object()})])

    run = db.run(run_id)
    assert (run.status, run.plan_hash) == ("planning", None)
    assert db.tasks(run_id) == []
    assert db.transitions(run_id) == []


def replanning(db, run_id, titles):
    """The tasks of `titles` in the run: the first is committed, the second goes to replan."""
    db.approve(run_id, "f00d", [(title, {}) for title in titles])
    tasks = db.tasks(run_id)
    for status in ("implementing", "gating", "committed"):
        db.move_task(tasks[0].id, status)
    for status in ("implementing", "replan"):
        db.move_task(tasks[1].id, status, "too_big" if status == "replan" else None)
    return tasks


def test_replan_puts_new_tasks_in_place_of_the_task(db, run_id):
    first, big, docs, router, admin = replanning(
        db, run_id, ["schema", "big", "docs", "router", "admin"]
    )

    db.replan(
        big.id,
        [("prices", CARD), ("big rest", {})],
        {router.id: ("router v2", {"goal": "v2"})},
        [docs.id],
    )

    tasks = db.tasks(run_id)
    assert [(t.seq, t.title, t.status, t.reason) for t in tasks] == [
        (1, "schema", "committed", None),
        (2, "big", "dropped", "replanned"),
        (3, "prices", "pending", None),
        (4, "big rest", "pending", None),
        (5, "docs", "dropped", "replanned"),
        (6, "router v2", "pending", None),
        (7, "admin", "pending", None),
    ]
    assert [t.id for t in tasks if t.title in ("docs", "router v2", "admin")] == [
        docs.id,
        router.id,
        admin.id,
    ]
    assert (tasks[2].card, tasks[5].card) == (CARD, {"goal": "v2"})
    moves = [(t.task_id, t.dst) for t in db.transitions(run_id)][-2:]
    assert moves == [(big.id, "dropped"), (docs.id, "dropped")]


def test_failed_replan_changes_nothing(db, run_id):
    _, big, docs = replanning(db, run_id, ["schema", "big", "docs"])
    before = db.tasks(run_id)

    with pytest.raises(store.StoreError, match="committed -> dropped is not allowed"):
        db.replan(big.id, [("prices", {})], {docs.id: ("docs v2", {})}, [before[0].id])

    assert db.tasks(run_id) == before


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
    other = db.create_run(Path("/work/other"), "main", "abc123")

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

    db.set_tests(tasks[1].id, 228)
    assert [t.tests for t in db.tasks(run_id)] == [None, 228]


def test_step_is_started_and_finished_once(db, run_id):
    task_id = db.add_task(run_id, "store", CARD)

    step_id = db.start_step(run_id, task_id, "implement", "abc123", ["notes.txt"], session_id="s-1")
    started = db.steps(task_id)[0]
    assert (started.kind, started.checkpoint, started.session_id, started.finished_at) == (
        "implement",
        "abc123",
        "s-1",
        None,
    )
    assert started.untracked == ["notes.txt"]
    assert db.open_steps(run_id) == [started]

    db.finish_step(step_id, "paused", detail={"reason": "контекст"})
    finished = db.steps(task_id)[0]
    assert finished.outcome == "paused"
    assert finished.detail == {"reason": "контекст"}
    assert finished.finished_at is not None
    assert db.open_steps(run_id) == []

    with pytest.raises(store.StoreError, match="already finished"):
        db.finish_step(step_id, "done")


def test_limit_snapshots_keep_missing_windows_empty(db, run_id):
    task_id = db.add_task(run_id, "budget", CARD)
    step_id = db.start_step(run_id, task_id, "implement", "abc123", [])

    db.record_limits(run_id, None, Limits(Window(0.0, 1791670200), Window(0.64, 1791813600)))
    db.record_limits(run_id, step_id, Limits(Window(0.05, 1791670200), None))

    snapshots = db.limit_snapshots(run_id)
    assert [
        (s.step_id, s.five_hour, s.five_hour_resets_at, s.seven_day, s.seven_day_resets_at)
        for s in snapshots
    ] == [
        (None, 0.0, 1791670200, 0.64, 1791813600),
        (step_id, 0.05, 1791670200, None, None),
    ]
