import os
import signal
import subprocess
import sys
import time
import uuid

import pytest

from agent_loop import commands, git, recovery, step
from agent_loop.config import Profile, Settings
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
    run_id = db.create_run(repo, "main", "abc123")
    db.move_run(run_id, "approved")
    db.move_run(run_id, "running")
    task_id = db.add_task(run_id, "Add feature", {})
    db.move_task(task_id, "implementing")
    return run_id, task_id


def lock_path(home, run_id):
    return step.run_dir(home, run_id) / recovery.LOCK_FILE


def start(command, ready, *args):
    proc = subprocess.Popen([*command, ready, *args], process_group=0)
    deadline = time.monotonic() + 10
    while not ready.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    return proc


@pytest.fixture
def orphan(tmp_path):
    """Start a process that looks like a session left by a dead loop and another one that has
    the same session id in its command line but is not a session; returns the session id and
    both processes."""
    session_id = str(uuid.uuid4())
    script = tmp_path / "orphan" / "claude"
    script.parent.mkdir()
    script.write_text(f"#!{sys.executable}\n{ORPHAN}\n")
    script.chmod(0o755)
    procs = [
        start([script], tmp_path / "orphan-ready", "--session-id", session_id),
        start([sys.executable, "-c", ORPHAN], tmp_path / "bystander-ready", session_id),
    ]
    yield session_id, *procs
    for proc in procs:
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


def test_working_directory_is_locked_in_the_git_dir_of_its_worktree(repo, tmp_path):
    worktree = tmp_path / "worktree"
    git.git(repo, "worktree", "add", "-q", "-b", "side", str(worktree))

    first = recovery.hold_repo(repo)
    assert (repo / ".git" / recovery.REPO_LOCK_FILE).is_file()
    assert recovery.hold_repo(repo) is None
    other = recovery.hold_repo(worktree)
    assert other is not None
    first.close()
    other.close()


def test_dead_run_is_paused_with_its_session_stopped_and_work_saved(home, db, repo, orphan):
    session_id, proc, bystander = orphan
    run_id, task_id = running_run(db, repo)
    (repo / "notes.txt").write_text("operator's\n")
    db.start_step(task_id, "implement", git.head(repo), git.untracked(repo), session_id)
    (repo / "feature.py").write_text("f = 1\n")
    (repo / "app.py").write_text("x = 2\n")

    assert recovery.reap(db, home) == [run_id]

    assert proc.wait(timeout=5) == -signal.SIGTERM
    assert bystander.poll() is None
    [paused] = db.steps(task_id)
    assert (paused.outcome, paused.detail) == ("paused", {"reason": "died"})
    assert step.how_to_resume([paused], Settings().ctx_soft_tokens) == step.Resume(
        "session", session_id
    )
    [task] = db.tasks(run_id)
    assert (task.status, task.reason) == ("paused", "died")
    run = db.run(run_id)
    assert (run.status, run.reason) == ("paused", "died")
    wip = git.git(repo, "ls-tree", "-r", "--name-only", git.wip_ref(run_id, 1)).split()
    assert sorted(wip) == ["app.py", "feature.py", "old.py"]
    assert (repo / "feature.py").is_file()


def test_live_run_is_left_alone_and_dead_one_stops_its_services(home, db, repo, tmp_path):
    run_id, _ = running_run(db, repo)
    lock = recovery.hold(lock_path(home, run_id))
    services = tmp_path / "services.log"
    profile = Profile(commands={"down": f"echo down {{run_id}} >> {services}"})
    commands.freeze(profile, step.run_dir(home, run_id), run_id)

    assert recovery.reap(db, home) == []
    assert db.run(run_id).status == "running"
    assert not services.exists()

    lock.close()
    assert recovery.reap(db, home) == [run_id]
    assert recovery.reap(db, home) == []
    assert db.run(run_id).status == "paused"
    assert services.read_text() == f"down {run_id}\n"


def test_broken_run_does_not_block_the_others(home, db, repo, tmp_path):
    gone = tmp_path / "moved-away"
    gone.mkdir()
    broken_id, broken_task = running_run(db, gone)
    db.start_step(broken_task, "implement", "abc123", [])
    gone.rmdir()
    run_id, task_id = running_run(db, repo)
    db.start_step(task_id, "implement", git.head(repo), [])

    assert recovery.reap(db, home) == [run_id]

    assert db.run(broken_id).status == "running"
    [still_open] = db.steps(broken_task)
    assert still_open.outcome is None
    assert db.run(run_id).status == "paused"
