import contextlib
import json
import logging
import shutil
import sqlite3
import subprocess
import time
from pathlib import Path

import pytest

from agent_loop import git, implement, protected, recovery, runner, selfcheck, step
from agent_loop.config import Config, Profile, Settings
from agent_loop.store import Store

CARD = {
    "goal": "Make x fixed",
    "criteria": ["app.py says fixed"],
    "files": ["app.py"],
    "tests": [],
    "decisions": [],
}


@pytest.fixture(autouse=True)
def checked(monkeypatch):
    monkeypatch.setattr(selfcheck, "require", lambda home: None)


@pytest.fixture
def home(tmp_path):
    return tmp_path / "home"


@pytest.fixture
def cfg(home, repo):
    return Config(home, repo, None, Settings(), {})


@pytest.fixture
def tasks_file(tmp_path):
    path = tmp_path / "tasks.json"
    path.write_text(json.dumps([{"title": title, **CARD} for title in ("First", "Second")]))
    return path


class Tasks:
    """Replaces the loop of a task: each turn does what it does with the task and returns the
    stop of the loop."""

    def __init__(self, monkeypatch, *turns):
        self.calls = []
        turns = iter(turns)

        def loop(name):
            def go(cfg, store, run, task, total, profile, previous):
                self.calls.append((name, task.seq, total, previous))
                return next(turns)(store, task)

            return go

        monkeypatch.setattr(implement, "start", loop("start"))
        monkeypatch.setattr(implement, "resume", loop("resume"))


def commits(notes="", actions=()):
    """The task commits a module of its own and leaves `notes` for the next ones and `actions`
    for the operator."""

    def turn(store, task):
        repo = Path(store.run(task.run_id).repo)
        checkpoint = git.head(repo)
        if task.status != "implementing":
            store.move_task(task.id, "implementing")
        answer = {"status": "done", "summary": "", "commit_message": "", "notes_for_next": notes}
        answer["operator_actions"] = list(actions)
        step_id = store.start_step(task.run_id, task.id, implement.IMPLEMENT, checkpoint, [], "s")
        store.finish_step(step_id, "completed", {"reason": None, "answer": answer})
        store.move_task(task.id, "gating")
        (repo / f"task{task.seq}.py").write_text("def helper():\n    pass\n")
        subprocess.run(["git", "add", "."], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", task.title], cwd=repo, check=True)
        step_id = store.start_step(task.run_id, task.id, implement.COMMIT, checkpoint, [])
        store.finish_step(step_id, "completed", {"commit": git.head(repo)})
        store.move_task(task.id, "committed")

    return turn


def moves(*statuses, reason=None, stop=None):
    """The task moves through `statuses`, the last one with `reason`, and the loop stops."""

    def turn(store, task):
        for status in statuses:
            store.move_task(task.id, status, reason if status == statuses[-1] else None)
        return stop

    return turn


@contextlib.contextmanager
def opened(home):
    with contextlib.closing(Store(home / "loop.db")) as store:
        yield store


def the_run(home):
    with opened(home) as store:
        [run] = [run for status in ("paused", "done") for run in store.runs(status)]
        transitions = store.transitions(run.id)
        moved = [(t.dst, t.reason) for t in transitions if t.task_id is None]
        return run, store.tasks(run.id), moved


def test_tasks_run_one_after_another_until_the_run_is_done(
    home, repo, tmp_path, tasks_file, monkeypatch, caplog
):
    services = tmp_path / "services.log"
    profile = Profile(
        commands={name: f"echo {name} >> {services}" for name in ("install", "up", "down")}
    )
    cfg = Config(home, repo, None, Settings(), {}, profile)
    base = git.head(repo)
    tasks = Tasks(
        monkeypatch,
        commits("tasks.py has helper()", ["add JWT_SECRET= to .env.example"]),
        commits(actions=["add JWT_EXPIRE_MINUTES=60 to .env.example"]),
    )

    with caplog.at_level(logging.INFO, logger="agent_loop"):
        assert runner.start(cfg, tasks_file) is None

    run, [first, second], run_moves = the_run(home)
    assert (run.branch, run.base) == ("main", base)
    assert run_moves == [("approved", None), ("running", None), ("done", None)]
    assert (first.title, first.card, first.status) == ("First", CARD, "committed")
    assert second.status == "committed"
    [(_, _, _, nothing), (name, seq, total, previous)] = tasks.calls
    assert (name, seq, total, nothing) == ("start", 2, 2, "")
    assert "A task1.py: helper" in previous
    assert "- First: tasks.py has helper()" in previous
    assert services.read_text() == "install\nup\ndown\n"
    run_dir = step.run_dir(home, run.id)
    assert (run_dir / protected.COPY_DIR).is_dir()
    assert (run_dir / "profile.json").is_file()
    through = caplog.messages.index("all 2 tasks are through")
    assert caplog.messages[through + 1] == (
        "the tasks need this from the operator:\n"
        "task 1: add JWT_SECRET= to .env.example\n"
        "task 2: add JWT_EXPIRE_MINUTES=60 to .env.example"
    )


def detach(repo):
    subprocess.run(["git", "checkout", "-q", "--detach"], cwd=repo, check=True)


@pytest.mark.parametrize(
    "spoil, error",
    [
        (lambda repo: (repo / "app.py").write_text("x = 2\n"), "the tracked files have changes"),
        (detach, "HEAD is detached"),
        (recovery.hold_repo, "another run works in"),
    ],
)
def test_run_does_not_start_where_it_would_mix_with_other_work(
    home, repo, cfg, tasks_file, spoil, error
):
    held = spoil(repo)

    with pytest.raises(runner.RunError, match=error):
        runner.start(cfg, tasks_file)

    assert not (home / "runs").exists()
    del held


@pytest.mark.parametrize(
    "content, error",
    [
        ("[{", "tasks: "),
        ("{}", "expected a list of tasks"),
        ("[]", "expected a list of tasks"),
        ('["x"]', "task 1: expected an object"),
        (json.dumps([CARD]), r"task 1: missing \['title'\], unknown \[\]"),
        (json.dumps([{"title": "t", **CARD, "notes": ""}]), r"missing \[\], unknown \['notes'\]"),
        (json.dumps([{**CARD, "title": " "}]), "title must be a non-empty string"),
        (json.dumps([{"title": "t", **CARD, "goal": 1}]), "goal must be a non-empty string"),
        (json.dumps([{"title": "t", **CARD, "files": "app.py"}]), "files must be a list of"),
        (json.dumps([{"title": "t", **CARD, "tests": [1]}]), "tests must be a list of strings"),
    ],
)
def test_broken_tasks_file_is_rejected(tmp_path, content, error):
    path = tmp_path / "tasks.json"
    path.write_text(content)

    with pytest.raises(runner.RunError, match=error):
        runner.load_tasks(path)


@pytest.mark.parametrize("status, reason", [("needs_human", "question"), ("replan", "too_big")])
def test_task_that_waits_for_the_operator_pauses_the_run(
    home, cfg, tasks_file, monkeypatch, status, reason
):
    tasks = Tasks(monkeypatch, moves("implementing", status, reason=reason))

    assert runner.start(cfg, tasks_file) == status

    run, [first, second], run_moves = the_run(home)
    assert run_moves[-1] == ("paused", status)
    assert (first.status, second.status) == (status, "pending")
    assert len(tasks.calls) == 1
    with pytest.raises(runner.RunError, match=f"task 1 waits in {status} \\({reason}\\)"):
        runner.resume(cfg, None)


def test_run_waits_for_the_limits_and_goes_on(home, cfg, tasks_file, monkeypatch):
    resume_at = int(time.time()) + 600
    slept = []
    monkeypatch.setattr(time, "sleep", slept.append)
    tasks = Tasks(
        monkeypatch,
        moves(stop=implement.Stop(implement.BUDGET, resume_at)),
        commits(),
        commits(),
    )

    assert runner.start(cfg, tasks_file) is None

    _, _, run_moves = the_run(home)
    assert run_moves == [
        ("approved", None),
        ("running", None),
        ("paused", "budget"),
        ("running", None),
        ("done", None),
    ]
    assert 590 < slept[0] <= 600
    assert [(name, seq) for name, seq, _, _ in tasks.calls] == [
        ("start", 1),
        ("start", 1),
        ("start", 2),
    ]


def test_ctrl_c_while_waiting_for_the_limits_leaves_the_run_paused(
    home, cfg, tasks_file, monkeypatch
):
    def interrupted(seconds):
        raise KeyboardInterrupt

    monkeypatch.setattr(time, "sleep", interrupted)
    Tasks(monkeypatch, moves(stop=implement.Stop(implement.BUDGET, int(time.time()) + 600)))

    assert runner.start(cfg, tasks_file) == "interrupt"
    run, _, run_moves = the_run(home)
    assert run_moves[-1] == ("paused", "budget")

    tasks = Tasks(monkeypatch, commits(), commits())
    assert runner.resume(cfg, run.id) is None
    assert [name for name, _, _, _ in tasks.calls] == ["start", "start"]


@pytest.mark.parametrize(
    "turn, reason",
    [
        (moves("implementing", "gating", stop=implement.Stop(implement.INTERRUPT)), "interrupt"),
        (moves("implementing", "paused", reason="interrupt"), "interrupt"),
        (moves("implementing", "paused", reason="branch_changed"), "branch_changed"),
    ],
)
def test_operator_stops_pause_the_run_and_resume_goes_on(
    home, cfg, tasks_file, monkeypatch, turn, reason
):
    Tasks(monkeypatch, turn)

    assert runner.start(cfg, tasks_file) == reason
    _, _, run_moves = the_run(home)
    assert run_moves[-1] == ("paused", reason)

    tasks = Tasks(monkeypatch, commits(), commits())
    assert runner.resume(cfg, None) is None
    assert [name for name, _, _, _ in tasks.calls] == ["resume", "start"]


def test_task_paused_for_the_context_goes_on_at_once(home, cfg, tasks_file, monkeypatch):
    tasks = Tasks(
        monkeypatch, moves("implementing", "paused", reason="context"), commits(), commits()
    )

    assert runner.start(cfg, tasks_file) is None

    _, _, run_moves = the_run(home)
    assert run_moves == [("approved", None), ("running", None), ("done", None)]
    assert [name for name, _, _, _ in tasks.calls] == ["start", "resume", "start"]


def test_resume_needs_the_branch_where_the_run_has_left_it(
    home, repo, cfg, tasks_file, monkeypatch
):
    Tasks(monkeypatch, commits(), moves("implementing", "paused", reason="interrupt"))
    runner.start(cfg, tasks_file)

    (repo / "app.py").write_text("x = 2\n")
    subprocess.run(["git", "commit", "-q", "-am", "by hand"], cwd=repo, check=True)
    with pytest.raises(runner.RunError, match="the run has left main at .* now main is at"):
        runner.resume(cfg, None)
    subprocess.run(["git", "reset", "-q", "--hard", "HEAD~1"], cwd=repo, check=True)
    subprocess.run(["git", "checkout", "-q", "-b", "other"], cwd=repo, check=True)
    with pytest.raises(runner.RunError, match="now other is at"):
        runner.resume(cfg, None)
    subprocess.run(["git", "checkout", "-q", "main"], cwd=repo, check=True)

    Tasks(monkeypatch, commits())
    assert runner.resume(cfg, None) is None


def test_resume_without_a_paused_run(home, cfg):
    with pytest.raises(runner.RunError, match="no paused run in"):
        runner.resume(cfg, None)
    with pytest.raises(runner.RunError, match="run 0a1b2c3d not found"):
        runner.resume(cfg, "0a1b2c3d")


def forget_the_base(home, run_dir):
    with contextlib.closing(sqlite3.connect(home / "loop.db")) as conn, conn:
        conn.execute("UPDATE runs SET base = NULL")


@pytest.mark.parametrize(
    "spoil, error",
    [
        (forget_the_base, "has no record of the HEAD it started from"),
        (lambda home, run_dir: shutil.rmtree(run_dir / protected.COPY_DIR), "protected of the run"),
        (lambda home, run_dir: (run_dir / "profile.json").unlink(), "profile.json of the run"),
    ],
)
def test_resume_refuses_a_run_it_cannot_check(home, cfg, tasks_file, monkeypatch, spoil, error):
    Tasks(monkeypatch, moves("implementing", "paused", reason="interrupt"))
    runner.start(cfg, tasks_file)
    run, _, _ = the_run(home)

    spoil(home, step.run_dir(home, run.id))

    with pytest.raises(runner.RunError, match=error):
        runner.resume(cfg, None)
