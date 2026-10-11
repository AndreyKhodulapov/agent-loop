import os
import signal
import subprocess
import sys
import time
import uuid

import pytest

from agent_loop import git, recovery, step
from agent_loop.store import Store

ORPHAN = "import pathlib, sys, time; pathlib.Path(sys.argv[1]).touch(); time.sleep(60)"


@pytest.fixture
def home(tmp_path):
    return tmp_path / "home"


@pytest.fixture
def db(home):
    opened = Store(home / "loop.db")
    yield opened
    opened.close()


def running_run(db, repo):
    run_id = db.create_run(repo, "main")
    db.move_run(run_id, "approved")
    db.move_run(run_id, "running")
    task_id = db.add_task(run_id, "Add feature", {})
    db.move_task(task_id, "implementing")
    return run_id, task_id


def lock_path(home, run_id):
    return step.run_dir(home, run_id) / recovery.LOCK_FILE


@pytest.fixture
def orphan(tmp_path):
    """Start a process that looks like a session left by a dead loop; returns its session id."""
    session_id = str(uuid.uuid4())
    ready = tmp_path / "orphan-ready"
    proc = subprocess.Popen(
        [sys.executable, "-c", ORPHAN, ready, "--session-id", session_id], process_group=0
    )
    deadline = time.monotonic() + 10
    while not ready.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    yield session_id, proc
    if proc.poll() is None:
        proc.kill()
        proc.wait()


def test_hold_is_exclusive_and_released_on_close(tmp_path):
    path = tmp_path / "runs" / "3f2a" / "lock"

    first = recovery.hold(path)
    assert path.read_text() == f"{os.getpid()}\n"
    assert recovery.hold(path) is None
    first.close()

    second = recovery.hold(path)
    assert second is not None
    second.close()


def test_dead_run_is_paused_with_its_session_stopped_and_work_saved(home, db, repo, orphan):
    session_id, proc = orphan
    run_id, task_id = running_run(db, repo)
    (repo / "notes.txt").write_text("operator's\n")
    db.start_step(task_id, "implement", git.head(repo), git.untracked(repo), session_id)
    (repo / "feature.py").write_text("f = 1\n")
    (repo / "app.py").write_text("x = 2\n")

    assert recovery.reap(db, home) == [run_id]

    assert proc.wait(timeout=5) == -signal.SIGTERM
    [paused] = db.steps(task_id)
    assert (paused.outcome, paused.detail) == ("paused", {"reason": "died"})
    assert step.how_to_resume([paused]) == step.Resume("session", session_id)
    [task] = db.tasks(run_id)
    assert (task.status, task.reason) == ("paused", "died")
    run = db.run(run_id)
    assert (run.status, run.reason) == ("paused", "died")
    wip = git.git(repo, "ls-tree", "-r", "--name-only", git.wip_ref(run_id, 1)).split()
    assert sorted(wip) == ["app.py", "feature.py", "old.py"]
    assert (repo / "feature.py").is_file()


def test_live_run_is_left_alone(home, db, repo):
    run_id, _ = running_run(db, repo)
    lock = recovery.hold(lock_path(home, run_id))

    assert recovery.reap(db, home) == []
    assert db.run(run_id).status == "running"

    lock.close()
    assert recovery.reap(db, home) == [run_id]
    assert recovery.reap(db, home) == []
    assert db.run(run_id).status == "paused"
