"""A run: the tasks of the run one after another on the current branch of the repository.

`start` begins a run with the tasks of a cards file, `resume` goes on with a paused one.
`plan_request` begins a run with the request of the operator, from a file or in a conversation
with the planner, which stays in planning while `planner` works on it. `answer` gives the replies
of the operator to the planner of a planning run, or to the task a paused run waits for, and goes
on. `approve` makes the plan of a planning run the tasks of the run. A run holds the lock of the
working directory and its own lock, works with the profile frozen at its start and keeps the
services of the profile up while it works.

The run pauses when a task needs the operator: a question, the planner, a moved branch, Ctrl+C.
Ctrl+C outside a session pauses the run with its open steps as `recovery` pauses a dead one.
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
    export,
    gates,
    git,
    implement,
    log,
    plan,
    planner,
    prompts,
    protected,
    recovery,
    replies,
    selfcheck,
    step,
)
from agent_loop.config import Config, Profile
from agent_loop.store import DB_FILE, Run, Store, StoreError, Task

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


def start(cfg: Config, tasks_file: Path) -> str | None:
    """Run the tasks of `tasks_file` on the current branch; returns why the run has paused, None
    when all the tasks are through.

    The tracked files must have no changes: they would become the work of the first task.
    """
    repo = _repo(cfg)
    tasks = load_tasks(tasks_file)
    selfcheck.require(cfg.home)
    with _hold_repo(repo), contextlib.closing(Store(cfg.home / DB_FILE)) as store:
        branch = _branch(repo)
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
            run = store.run(run_id)
            try:
                if "install" in profile.commands:
                    logger.info("installing the dependencies")
                    commands.run(profile, "install", repo)
                with commands.services(profile, repo, run_dir):
                    return _work(cfg, store, run, profile)
            except KeyboardInterrupt:
                return _interrupted(store, run)


def plan_request(cfg: Config, request_file: Path | None, new: bool = False) -> str | None:
    """Plan the request of `request_file` for the current branch, or the one the operator gives
    in a conversation with the planner when there is no file; returns why the run waits for the
    operator, as `planner.go_on`, None when the plan is ready.

    The last run that has stopped planning the same request goes on; another request begins a
    new run. Without a file, the last conversation that has not ended with a plan goes on,
    unless the operator asks for a `new` one.
    """
    repo = _repo(cfg)
    request = None
    if request_file is not None:
        try:
            request = request_file.read_text(encoding="utf-8")
        except OSError as exc:
            raise RunError(f"request: {exc}") from exc
        if not request.strip():
            raise RunError(f"{request_file} is empty")
    selfcheck.require(cfg.home)
    with _hold_repo(repo), contextlib.closing(Store(cfg.home / DB_FILE)) as store:
        if request is not None:
            run = _planning(store, cfg.home, repo, request)
        else:
            run = None if new else _conversation(store, cfg.home, repo)
        if run is None:
            run = store.run(store.create_run(repo, _branch(repo), git.head(repo)))
            run_dir = step.run_dir(cfg.home, run.id)
            protected.save(repo, run_dir)
            if request is not None:
                # Written last: a run with its request is set up.
                (run_dir / planner.REQUEST_FILE).write_text(request, encoding="utf-8")
        run_dir = step.run_dir(cfg.home, run.id)
        with log.scope(run=run.id), recovery.hold(run_dir / recovery.LOCK_FILE):
            if request is None:
                logger.info("planning in a conversation on %s, the run is %s", run.branch, run.id)
                return planner.talk(cfg, store, run)
            logger.info("planning %s on %s, the run is %s", request_file, run.branch, run.id)
            return planner.go_on(cfg, store, run)


def answer(cfg: Config, run_id: str | None) -> str | None:
    """Give the replies of the operator in `QUESTIONS.md` of the run `run_id`, or of the last
    planning or paused run of the repository, and go on with it.

    The replies of a planning run go to its planner, as `planner.answer`; the reply of a paused
    run goes to the task that waits for it, and the run resumes. Returns as `plan_request` or
    `resume`.
    """
    repo = _repo(cfg)
    selfcheck.require(cfg.home)
    with _hold_repo(repo), contextlib.closing(Store(cfg.home / DB_FILE)) as store:
        run = _waiting(store, repo, run_id, ("planning", "paused"))
        if run.status == "paused":
            return _go_on(cfg, store, run, answered=True)
        run_dir = step.run_dir(cfg.home, run.id)
        with log.scope(run=run.id), recovery.hold(run_dir / recovery.LOCK_FILE):
            return planner.answer(cfg, store, run)


def approve(cfg: Config, run_id: str | None) -> None:
    """Approve the plan of the planning run `run_id`, or of the last planning run of the
    repository with a plan: its tasks become the tasks of the run, with the hash of `plan.json`,
    and `PLAN.md` shows it with the findings of the critic.

    Blocking findings the critic has left do not stop the approval: the operator decides on them.
    """
    repo = _repo(cfg)
    with contextlib.closing(Store(cfg.home / DB_FILE)) as store:
        run = _planned(store, cfg.home, repo, run_id)
        run_dir = step.run_dir(cfg.home, run.id)
        lock = recovery.hold(run_dir / recovery.LOCK_FILE)
        if lock is None:
            raise RunError(f"`loop plan` of run {run.id} has not exited yet")
        with lock, log.scope(run=run.id):
            approved = plan.load(run_dir)
            critique = (run_dir / planner.CRITIQUE_FILE).read_text(encoding="utf-8")
            findings = json.loads(critique)["findings"]
            # Before the approval: an approved run without its `PLAN.md` cannot get it again.
            paths = export.write_plan(run_dir, run, approved, findings, cfg.settings)
            store.approve(run.id, plan.digest(run_dir), plan.cards(approved))
            blocking = [f["problem"] for f in findings if f["blocking"]]
            if blocking:
                logger.warning(
                    "the plan is approved with %d blocking findings of the critic:\n%s",
                    len(blocking),
                    "\n".join(f"- {problem}" for problem in blocking),
                )
            logger.info(
                "the plan of %d tasks is approved, it is in %s",
                len(approved["tasks"]),
                ", ".join(map(str, paths)),
            )


def resume(cfg: Config, run_id: str | None) -> str | None:
    """Go on with the paused run `run_id`, or with the last paused run of the repository.

    The branch and HEAD must be where the run has left them. Returns as `start`.
    """
    repo = _repo(cfg)
    selfcheck.require(cfg.home)
    with _hold_repo(repo), contextlib.closing(Store(cfg.home / DB_FILE)) as store:
        return _go_on(cfg, store, _waiting(store, repo, run_id, ("paused",)), answered=False)


def _go_on(cfg: Config, store: Store, run: Run, answered: bool) -> str | None:
    """Resume the paused `run`; when the operator has `answered`, the task that waits for the
    reply gets it first."""
    repo = Path(run.repo)
    if run.base is None:
        raise RunError(f"run {run.id} has no record of the HEAD it started from, start a new run")
    run_dir = step.run_dir(cfg.home, run.id)
    # Removed by hand: without the copy `protected.restore` would take every protected file of
    # the operator for a new one.
    missing = [
        name
        for name in (commands.PROFILE_FILE, protected.COPY_DIR)
        if not (run_dir / name).exists()
    ]
    if missing:
        raise RunError(f"{', '.join(missing)} of the run is gone from {run_dir}")
    with log.scope(run=run.id), recovery.hold(run_dir / recovery.LOCK_FILE):
        # They wait for `loop answer` and the planner.
        waiting = [t for t in store.tasks(run.id) if t.status in ("needs_human", "replan")]
        task = waiting[0] if waiting else None
        question = None if task is None else implement.question(store, task)
        if task is not None and (question is None or not answered):
            hint = "" if question is None else f", `loop answer {run.id}` gives it the reply"
            raise RunError(f"task {task.seq} waits in {task.status} ({task.reason}){hint}")
        if answered and task is None:
            raise RunError(f"no task of run {run.id} waits for a reply")
        branch, head, expected = git.branch(repo), git.head(repo), _head(store, run)
        if branch != run.branch or head != expected:
            raise RunError(
                f"the run has left {run.branch} at {expected[:12]}, now {branch} is at"
                f" {head[:12]}: put them back"
            )
        if answered:
            [reply] = replies.read(run_dir, run.id, [question])
            implement.reply(store, task, question, reply)
            logger.info("task %d goes on with the reply of the operator", task.seq)
        profile = commands.frozen(run_dir)
        store.move_run(run.id, "running")
        logger.info("resuming the run paused for %s", run.reason)
        try:
            with commands.services(profile, repo, run_dir):
                return _work(cfg, store, run, profile)
        except KeyboardInterrupt:
            return _interrupted(store, run)


def _work(cfg: Config, store: Store, run: Run, profile: Profile) -> str | None:
    repo = Path(run.repo)
    while True:
        tasks = store.tasks(run.id)
        # A committed task is through with the run until the review.
        task = next((t for t in tasks if t.status not in ("committed", "done", "dropped")), None)
        if task is None:
            store.move_run(run.id, "done")
            logger.info("all %d tasks are through", len(tasks))
            _report_operator_actions(store, tasks)
            return None
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
                return implement.INTERRUPT
            continue
        if stop is not None:
            return _pause(store, run, stop.reason)
        [task] = [t for t in store.tasks(run.id) if t.id == task.id]
        if task.status in ("needs_human", "replan"):
            logger.warning("task %d waits in %s (%s)", task.seq, task.status, task.reason)
            question = implement.question(store, task)
            if question is None:
                return _pause(store, run, task.status)
            path = replies.ask(step.run_dir(cfg.home, run.id), run.id, [question])
            store.move_run(run.id, "paused", task.status)
            logger.warning(
                "the run is paused, reply to task %d in %s and run `loop answer %s`",
                task.seq,
                path,
                run.id,
            )
            return task.status
        # Other pauses go on at once: the task resumes its session or goes to the planner.
        if task.status == "paused" and task.reason in (implement.INTERRUPT, gates.BRANCH_CHANGED):
            return _pause(store, run, task.reason)


def _wait(store: Store, run: Run, resume_at: int) -> bool:
    store.move_run(run.id, "paused", implement.BUDGET)
    try:
        time.sleep(max(0, resume_at - time.time()))
    except KeyboardInterrupt:
        # The run stays paused for the limits: they are what `loop resume` waits for.
        logger.warning("stopped while waiting for the limits, `loop resume` goes on")
        return False
    store.move_run(run.id, "running")
    return True


def _pause(store: Store, run: Run, reason: str) -> str:
    store.move_run(run.id, "paused", reason)
    logger.warning("the run is paused (%s), `loop resume %s` goes on", reason, run.id)
    return reason


def _interrupted(store: Store, run: Run) -> str:
    """Pause the run the operator has stopped outside a session: in `install` or `up`, the gate,
    the commit or between the tasks."""
    logger.warning("stopped by the operator")
    # Ctrl+C in `down` comes after the run has paused or is done.
    if store.run(run.id).status == "running":
        recovery.pause(store, run, implement.INTERRUPT)
        logger.warning(
            "the run is paused (%s), `loop resume %s` goes on", implement.INTERRUPT, run.id
        )
    return implement.INTERRUPT


def _report_operator_actions(store: Store, tasks: list[Task]) -> None:
    """List what the tasks have left for the operator to do by hand, once more at the end of
    the run: the warning after each commit is easy to lose among the lines of the run."""
    actions = [
        f"task {t.seq}: {action}"
        for t in tasks
        if t.status in ("committed", "done")
        for action in prompts.answer(implement.last_answer(store.steps(t.id))).operator_actions
    ]
    if actions:
        logger.warning("the tasks need this from the operator:\n%s", "\n".join(actions))


def _head(store: Store, run: Run) -> str:
    head = run.base
    for task in store.tasks(run.id):
        for s in store.steps(task.id):
            if s.kind == implement.COMMIT and s.outcome == "completed":
                head = s.detail["commit"]
    return head


def _planning(store: Store, home: Path, repo: Path, request: str) -> Run | None:
    """The last run of `repo` that has stopped planning `request`; None if there is none."""
    for run in reversed(store.runs("planning")):
        run_dir = step.run_dir(home, run.id)
        request_file = run_dir / planner.REQUEST_FILE
        # A run without its request has stopped before it was set up.
        if (
            run.repo == str(repo)
            and request_file.is_file()
            and not (run_dir / plan.PLAN_FILE).exists()
            and request_file.read_text(encoding="utf-8") == request
        ):
            return run
    return None


def _conversation(store: Store, home: Path, repo: Path) -> Run | None:
    """The last run of `repo` whose conversation with the planner has not ended with a plan;
    None if there is none."""
    for run in reversed(store.runs("planning")):
        # A run without a conversation has stopped before it was set up or plans a file.
        if (
            run.repo == str(repo)
            and not (step.run_dir(home, run.id) / plan.PLAN_FILE).exists()
            and any(s.kind == planner.TALK for s in store.planner_steps(run.id))
        ):
            return run
    return None


def _planned(store: Store, home: Path, repo: Path, run_id: str | None) -> Run:
    """The planning run `run_id` of `repo` with its plan, or the last such run of `repo`."""
    if run_id is not None:
        run = _waiting(store, repo, run_id, ("planning",))
        if not (step.run_dir(home, run.id) / plan.PLAN_FILE).exists():
            raise RunError(f"run {run_id} has no plan yet")
        return run
    for run in reversed(store.runs("planning")):
        if run.repo == str(repo) and (step.run_dir(home, run.id) / plan.PLAN_FILE).exists():
            return run
    raise RunError(f"no planning run in {repo} has a plan, `loop plan` makes one")


def _waiting(store: Store, repo: Path, run_id: str | None, statuses: tuple[str, ...]) -> Run:
    """The run `run_id` of `repo` in one of `statuses`, or the last such run of `repo`."""
    expected = " or ".join(statuses)
    if run_id is None:
        runs = [r for status in statuses for r in store.runs(status) if r.repo == str(repo)]
        if not runs:
            raise RunError(f"no {expected} run in {repo}")
        return max(runs, key=lambda r: r.created_at)
    try:
        run = store.run(run_id)
    except StoreError as exc:
        raise RunError(str(exc)) from exc
    if run.repo != str(repo):
        raise RunError(f"run {run_id} works in {run.repo}")
    if run.status not in statuses:
        raise RunError(f"run {run_id} is {run.status}, not {expected}")
    return run


def _repo(cfg: Config) -> Path:
    if cfg.repo_root is None:
        raise RunError("not in a git repository")
    return cfg.repo_root


def _branch(repo: Path) -> str:
    branch = git.branch(repo)
    if branch == "HEAD":
        raise RunError("HEAD is detached, check out the branch to work on")
    return branch


def _hold_repo(repo: Path) -> TextIO:
    lock = recovery.hold_repo(repo)
    if lock is None:
        raise RunError(f"another run works in {repo}")
    return lock
