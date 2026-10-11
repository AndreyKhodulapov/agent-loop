"""Runs whose orchestrator died without cleanup: kill -9, a crash, a reboot.

A live orchestrator holds an exclusive lock on `runs/<run>/lock` for the whole run. The OS
releases the lock when the process dies, however it dies, so a running run with a free lock is
dead. Unlike a stored pid, the lock cannot be mistaken for another process that got the same pid
after a reboot.

A dead run is paused the way a live one pauses: its orphaned session is terminated, the work in
progress goes to the WIP ref of the task and the step is finished as paused, so the task resumes
the session (`step.how_to_resume`).
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

from agent_loop import git, log, step
from agent_loop.store import TASK_TRANSITIONS, Run, Store

logger = logging.getLogger(__name__)

LOCK_FILE = "lock"
DIED = "died"
# Time an orphaned session has to exit after SIGTERM before it is killed.
ORPHAN_GRACE = 10


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


def reap(store: Store, home: Path) -> list[str]:
    """Pause the running runs whose orchestrator is gone; returns their ids."""
    reaped = []
    for run in store.runs("running"):
        lock = hold(step.run_dir(home, run.id) / LOCK_FILE)
        if lock is None:
            continue
        with lock:
            # Another `loop` may have reaped it between the listing and the lock.
            if store.run(run.id).status != "running":
                continue
            with log.scope(run=run.id):
                _pause(store, run)
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
        ref = git.wip_ref(run.id, task.seq)
        git.save_wip(Path(run.repo), ref, open_step.untracked)
        logger.info("task %d: work in progress saved in %s", task.seq, ref)
        store.finish_step(open_step.id, "paused", {"reason": DIED})
        # The orchestrator may have died between starting the step and moving the task.
        if "paused" in TASK_TRANSITIONS[task.status]:
            store.move_task(task.id, "paused", DIED)
    store.move_run(run.id, "paused", DIED)


def _terminate(session_id: str) -> None:
    """Stop an orphaned session: the CLI runs in its own process group and outlives the loop."""
    groups = session_groups(session_id)
    if not groups:
        return
    logger.warning("terminating the orphaned session %s", session_id)
    _signal(groups, signal.SIGTERM)
    deadline = time.monotonic() + ORPHAN_GRACE
    while groups and time.monotonic() < deadline:
        time.sleep(0.1)
        groups = session_groups(session_id)
    _signal(groups, signal.SIGKILL)


def session_groups(session_id: str) -> set[int]:
    """Process groups of the CLI processes with this session id in their command line."""
    listing = subprocess.run(
        ["ps", "-A", "-o", "pgid=,command="], capture_output=True, text=True, check=True
    ).stdout
    groups = set()
    for line in listing.splitlines():
        pgid, _, command = line.strip().partition(" ")
        if session_id in command.split():
            groups.add(int(pgid))
    return groups


def _signal(groups: set[int], signum: int) -> None:
    for pgid in groups:
        # The group may have exited since the listing.
        with contextlib.suppress(ProcessLookupError):
            os.killpg(pgid, signum)
