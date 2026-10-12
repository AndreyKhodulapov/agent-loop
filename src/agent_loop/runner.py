"""A run: the tasks of the run one after another on the current branch of the repository.

`start` begins a run with the tasks of a cards file, `resume` goes on with a paused one. A run
holds the lock of the working directory and its own lock, works with the profile frozen at its
start and keeps the services of the profile up while it works.

The run pauses when a task needs the operator: a question, the planner, a moved branch, Ctrl+C.
When the limits leave no room for a session, the run waits in the process and goes on by itself.
A run that stops on an error is left running for `recovery.reap` to pause, as a dead one.
"""

from __future__ import annotations

import contextlib
import json
import logging
import time
from pathlib import Path
from typing import TextIO

from agent_loop import (
    commands,
    gates,
    git,
    implement,
    log,
    prompts,
    protected,
    recovery,
    selfcheck,
    step,
)
from agent_loop.config import Config, Profile
from agent_loop.store import DB_FILE, Run, Store, StoreError

logger = logging.getLogger(__name__)


class RunError(Exception):
    pass


def load_tasks(path: Path) -> list[tuple[str, dict]]:
    """Read the cards file: a JSON list of objects with the `title` and the card of a task.

    The card is the `goal` and the lists of strings `criteria`, `files`, `tests`, `decisions`,
    as `prompts` takes it. Returns (title, card) pairs.
    """
    try:
        cards = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RunError(f"tasks: {exc}") from exc
    if not isinstance(cards, list) or not cards:
        raise RunError(f"{path}: expected a list of tasks")
    lists = ("criteria", "files", "tests", "decisions")
    tasks = []
    for number, card in enumerate(cards, start=1):
        where = f"{path}, task {number}"
        if not isinstance(card, dict):
            raise RunError(f"{where}: expected an object")
        fields = {"title", "goal", *lists}
        if card.keys() != fields:
            missing, unknown = sorted(fields - card.keys()), sorted(card.keys() - fields)
            raise RunError(f"{where}: missing {missing}, unknown {unknown}")
        for key in ("title", "goal"):
            if not isinstance(card[key], str) or not card[key].strip():
                raise RunError(f"{where}: {key} must be a non-empty string")
        for key in lists:
            if not isinstance(card[key], list) or not all(isinstance(x, str) for x in card[key]):
                raise RunError(f"{where}: {key} must be a list of strings")
        title = card.pop("title")
        tasks.append((title, card))
    return tasks


def start(cfg: Config, tasks_file: Path) -> bool:
    """Run the tasks of `tasks_file` on the current branch; True when all of them are through.

    The tracked files must have no changes: they would become the work of the first task.
    """
    repo = _repo(cfg)
    tasks = load_tasks(tasks_file)
    selfcheck.require(cfg.home)
    with _hold_repo(repo), contextlib.closing(Store(cfg.home / DB_FILE)) as store:
        branch = git.branch(repo)
        if branch == "HEAD":
            raise RunError("HEAD is detached, check out the branch to work on")
        changed = git.git(repo, "status", "--porcelain", "--untracked-files=no")
        if changed:
            raise RunError(f"the tracked files have changes, commit or stash them:\n{changed}")
        run_id = store.create_run(repo, branch, git.head(repo))
        for title, card in tasks:
            store.add_task(run_id, title, card)
        run_dir = step.run_dir(cfg.home, run_id)
        with log.scope(run=run_id), recovery.hold(run_dir / recovery.LOCK_FILE):
            profile = commands.freeze(cfg.repo, run_dir, run_id)
            protected.save(repo, run_dir)
            store.move_run(run_id, "approved")
            store.move_run(run_id, "running")
            logger.info("%d tasks on %s, the run is %s", len(tasks), branch, run_id)
            if "install" in profile.commands:
                logger.info("installing the dependencies")
                commands.run(profile, "install", repo)
            with commands.services(profile, repo):
                return _work(cfg, store, store.run(run_id), profile)


def resume(cfg: Config, run_id: str | None) -> bool:
    """Go on with the paused run `run_id`, or with the last paused run of the repository.

    The branch and HEAD must be where the run has left them. Returns as `start`.
    """
    repo = _repo(cfg)
    selfcheck.require(cfg.home)
    with _hold_repo(repo), contextlib.closing(Store(cfg.home / DB_FILE)) as store:
        run = _paused(store, repo, run_id)
        run_dir = step.run_dir(cfg.home, run.id)
        with log.scope(run=run.id), recovery.hold(run_dir / recovery.LOCK_FILE):
            # They wait for `loop answer` and the planner.
            waiting = [t for t in store.tasks(run.id) if t.status in ("needs_human", "replan")]
            if waiting:
                task = waiting[0]
                raise RunError(f"task {task.seq} waits in {task.status} ({task.reason})")
            branch, head, expected = git.branch(repo), git.head(repo), _head(store, run)
            if branch != run.branch or head != expected:
                raise RunError(
                    f"the run has left {run.branch} at {expected[:12]}, now {branch} is at"
                    f" {head[:12]}: put them back"
                )
            profile = commands.frozen(run_dir)
            store.move_run(run.id, "running")
            logger.info("resuming the run paused for %s", run.reason)
            with commands.services(profile, repo):
                return _work(cfg, store, run, profile)


def _work(cfg: Config, store: Store, run: Run, profile: Profile) -> bool:
    repo = Path(run.repo)
    while True:
        tasks = store.tasks(run.id)
        # A committed task is through with the run until the review.
        task = next((t for t in tasks if t.status not in ("committed", "done", "dropped")), None)
        if task is None:
            store.move_run(run.id, "done")
            logger.info("all %d tasks are through", len(tasks))
            return True
        notes = [
            (t.title, implement.last_answer(store.steps(t.id))["notes_for_next"])
            for t in tasks
            if t.status in ("committed", "done")
        ]
        previous = prompts.previous_tasks(repo, run.base, notes)
        go_on = implement.start if task.status == "pending" else implement.resume
        stop = go_on(cfg, store, run, task, len(tasks), profile, previous)
        if stop is not None and stop.reason == implement.BUDGET:
            if not _wait(store, run, stop.resume_at):
                return False
            continue
        if stop is not None:
            return _pause(store, run, stop.reason)
        [task] = [t for t in store.tasks(run.id) if t.id == task.id]
        if task.status in ("needs_human", "replan"):
            logger.warning("task %d waits in %s (%s)", task.seq, task.status, task.reason)
            return _pause(store, run, task.status)
        # Other pauses go on at once: the task resumes its session or goes to the planner.
        if task.status == "paused" and task.reason in (implement.INTERRUPT, gates.BRANCH_CHANGED):
            return _pause(store, run, task.reason)


def _wait(store: Store, run: Run, resume_at: int) -> bool:
    """Keep the run paused until the limits leave room; False if the operator stops it."""
    store.move_run(run.id, "paused", implement.BUDGET)
    try:
        time.sleep(max(0, resume_at - time.time()))
    except KeyboardInterrupt:
        logger.warning("stopped while waiting for the limits, `loop resume` goes on")
        return False
    store.move_run(run.id, "running")
    return True


def _pause(store: Store, run: Run, reason: str) -> bool:
    store.move_run(run.id, "paused", reason)
    logger.warning("the run is paused (%s), `loop resume %s` goes on", reason, run.id)
    return False


def _head(store: Store, run: Run) -> str:
    """HEAD as the run has left it: its last commit or its start."""
    head = run.base
    for task in store.tasks(run.id):
        for s in store.steps(task.id):
            if s.kind == implement.COMMIT and s.outcome == "completed":
                head = s.detail["commit"]
    return head


def _paused(store: Store, repo: Path, run_id: str | None) -> Run:
    if run_id is None:
        runs = [r for r in store.runs("paused") if r.repo == str(repo)]
        if not runs:
            raise RunError(f"no paused run in {repo}")
        return runs[-1]
    try:
        run = store.run(run_id)
    except StoreError as exc:
        raise RunError(str(exc)) from exc
    if run.repo != str(repo):
        raise RunError(f"run {run_id} works in {run.repo}")
    if run.status != "paused":
        raise RunError(f"run {run_id} is {run.status}, not paused")
    return run


def _repo(cfg: Config) -> Path:
    if cfg.repo_root is None:
        raise RunError("not in a git repository")
    return cfg.repo_root


def _hold_repo(repo: Path) -> TextIO:
    lock = recovery.hold_repo(repo)
    if lock is None:
        raise RunError(f"another run works in {repo}")
    return lock
