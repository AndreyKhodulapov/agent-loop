"""Runs whose orchestrator died without cleanup: kill -9, a crash, a reboot.

A live orchestrator holds an exclusive lock on `runs/<run>/lock` for the whole run. The OS
releases the lock when the process dies, however it dies, so a running run with a free lock is
dead. Unlike a stored pid, the lock cannot be mistaken for another process that got the same pid
after a reboot.

A dead run is paused the way a live one pauses: its orphaned session is terminated, the work in
progress goes to the WIP ref of the task and the step is finished as paused, so the task resumes
the session (`step.how_to_resume`). A commit step stopped after the commit of the task is on the
branch is finished as completed instead: the task is committed. Services it has left up are
stopped with `down` of the frozen profile.
"""

from __future__ import annotations

import contextlib
import fcntl
import logging
import os
import signal
import subprocess
import time
from pathlib import Path
from typing import TextIO

from agent_loop import agent, commands, git, implement, log, step
from agent_loop.store import TASK_TRANSITIONS, Run, Step, Store

logger = logging.getLogger(__name__)

LOCK_FILE = "lock"
REPO_LOCK_FILE = "agent-loop.lock"
DIED = "died"


def hold(path: Path) -> TextIO | None:
    """Take the lock and keep it while the returned file is open; None if another process has it.

    A run takes its lock before it becomes running, otherwise `reap` may take it for dead.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = open(path, "a+", encoding="utf-8")  # noqa: SIM115 - the caller keeps it open
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        return None
    # For the operator only: liveness is the lock itself.
    lock.truncate(0)
    lock.write(f"{os.getpid()}\n")
    lock.flush()
    return lock


def hold_repo(repo: Path) -> TextIO | None:
    """Lock the working directory for one run; None if another run works in it.

    The lock is in the git directory of the worktree, where the agent cannot write.
    """
    return hold(Path(git.git(repo, "rev-parse", "--absolute-git-dir").strip()) / REPO_LOCK_FILE)


def reap(store: Store, home: Path) -> list[str]:
    """Pause the running runs whose orchestrator is gone; returns their ids."""
    reaped = []
    for run in store.runs("running"):
        run_dir = step.run_dir(home, run.id)
        lock = hold(run_dir / LOCK_FILE)
        if lock is None:
            continue
        with lock:
            # Another `loop` may have reaped it between the listing and the lock.
            if store.run(run.id).status != "running":
                continue
            with log.scope(run=run.id):
                try:
                    _pause(store, run)
                except git.GitError as exc:
                    # Its open steps stay open, so the next `loop` tries again; one broken run
                    # must not stop every command.
                    logger.error("cannot save the work in progress, the run stays running: %s", exc)
                    continue
                services = run_dir / commands.SERVICES_FILE
                if services.is_file():
                    commands.down(commands.frozen(run_dir), Path(run.repo))
                    services.unlink()
        reaped.append(run.id)
    return reaped


def _pause(store: Store, run: Run) -> None:
    logger.warning("the orchestrator of this run is gone, pausing the run")
    tasks = {task.id: task for task in store.tasks(run.id)}
    for open_step in store.open_steps(run.id):
        task = tasks[open_step.task_id]
        if open_step.session_id is not None:
            _terminate(open_step.session_id)
        # The step is finished only after the work is saved: if saving fails, the next `loop`
        # finds the step open and tries again.
        repo = Path(run.repo)
        if open_step.kind == implement.COMMIT and _finish_commit(store, run, task.seq, open_step):
            logger.info("task %d: its commit is on the branch, the task is committed", task.seq)
            store.move_task(task.id, "committed")
            continue
        if git.head(repo) != open_step.checkpoint:
            logger.warning(
                "HEAD has moved since task %d started, its work in progress goes on top of %s",
                task.seq,
                open_step.checkpoint,
            )
        ref = git.wip_ref(run.id, task.seq)
        git.save_wip(repo, ref, open_step.untracked, open_step.checkpoint)
        logger.info("task %d: work in progress saved in %s", task.seq, ref)
        store.finish_step(open_step.id, "paused", {"reason": DIED})
        # The orchestrator may have died between starting the step and moving the task.
        if "paused" in TASK_TRANSITIONS[task.status]:
            store.move_task(task.id, "paused", DIED)
    store.move_run(run.id, "paused", DIED)


def _finish_commit(store: Store, run: Run, task_seq: int, commit_step: Step) -> bool:
    """Finish an open commit step as completed if the loop has made its commit; False if not.

    The loop may have stopped after the branch moved to the commit and before the step was
    finished, maybe before the index was reset to the commit. The commit is the loop's when its
    parent is the checkpoint and its message is the one the task was to be committed with.
    """
    repo = Path(run.repo)
    head = git.head(repo)
    if head == commit_step.checkpoint:
        return False
    message = implement.last_answer(store.steps(commit_step.task_id))["commit_message"]
    parents, body = git.git(repo, "log", "-1", "--format=%P%n%B", head).split("\n", 1)
    if parents != commit_step.checkpoint or body.rstrip("\n") != message:
        return False
    git.git(repo, "reset", "--quiet")
    git.delete_ref(repo, git.wip_ref(run.id, task_seq))
    store.finish_step(commit_step.id, "completed", {"commit": head, "message": message})
    return True


def _terminate(session_id: str) -> None:
    """Stop an orphaned session: the CLI runs in its own process group and outlives the loop."""
    groups = session_groups(session_id)
    if not groups:
        return
    logger.warning("terminating the orphaned session %s", session_id)
    _signal(groups, signal.SIGTERM)
    deadline = time.monotonic() + agent.ORPHAN_GRACE
    while groups and time.monotonic() < deadline:
        time.sleep(0.1)
        groups = session_groups(session_id)
    _signal(groups, signal.SIGKILL)


def session_groups(session_id: str) -> set[int]:
    """Process groups of the CLI processes with this session id in their command line.

    Only a command that runs `claude` counts: `grep <id>` of the operator is not a session.
    """
    listing = subprocess.run(
        ["ps", "-A", "-o", "pgid=,command="], capture_output=True, text=True, check=True
    ).stdout
    groups = set()
    for line in listing.splitlines():
        pgid, _, command = line.strip().partition(" ")
        tokens = command.split()
        if session_id in tokens and any(Path(t).name == agent.CLAUDE for t in tokens):
            groups.add(int(pgid))
    return groups


def _signal(groups: set[int], signum: int) -> None:
    for pgid in groups:
        # The group may have exited since the listing.
        with contextlib.suppress(ProcessLookupError):
            os.killpg(pgid, signum)
