"""A run: the tasks of the run one after another on the current branch of the repository.

`plan_request` begins a run with the request of the operator, from a file or in a conversation
with the planner, which stays in planning while `planner` works on it. `answer` gives the replies
of the operator to the planner of a planning run, or to the task a paused run waits for, and goes
on. `approve` makes the plan of a planning run the tasks of the run, `start` runs them and
`resume` goes on with a paused run. A run holds the lock of the working directory and its own
lock, works with the profile frozen at its start and keeps the services of the profile up while
it works.

The run pauses when a task needs the operator: a question, a moved branch, Ctrl+C. A task that
goes back to the planner is replanned at once, see `planner.replan`; a replan that changes the
acceptance criteria of the plan pauses the run until `approve` takes it or `answer` gives the
replanner an objection.
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


def start(cfg: Config, run_id: str | None) -> str | None:
    """Run the tasks of the approved run `run_id`, or of the last approved run of the
    repository, on the current branch from its HEAD; returns why the run has paused, None when
    all the tasks are through.

    `plan.json` must be the one the operator has approved. The tracked files must have no
    changes: they would become the work of the first task.
    """
    repo = _repo(cfg)
    selfcheck.require(cfg.home)
    with _hold_repo(repo), contextlib.closing(Store(cfg.home / DB_FILE)) as store:
        run = _waiting(store, repo, run_id, ("approved",))
        branch = _branch(repo)
        changed = git.git(repo, "status", "--porcelain", "--untracked-files=no")
        if changed:
            raise RunError(f"the tracked files have changes, commit or stash them:\n{changed}")
        run_dir = step.run_dir(cfg.home, run.id)
        missing = [
            name
            for name in (plan.PLAN_FILE, planner.CRITIQUE_FILE)
            if not (run_dir / name).exists()
        ]
        if missing:
            raise RunError(f"{', '.join(missing)} of the run is gone from {run_dir}")
        if plan.digest(run_dir) != run.plan_hash:
            raise RunError(
                f"{run_dir / plan.PLAN_FILE} has changed since it was approved:"
                " put it back or plan anew"
            )
        with log.scope(run=run.id), recovery.hold(run_dir / recovery.LOCK_FILE):
            profile = commands.freeze(cfg.repo, run_dir, run.id)
            # Taken here, not at planning: what the operator changes before the run is theirs.
            protected.save(repo, run_dir)
            store.start(run.id, branch, git.head(repo))
            run = store.run(run.id)
            # The branch of the run may differ from the one it was planned on.
            _write_plan(cfg, run, plan.load(run_dir))
            logger.info("%d tasks on %s, the run is %s", len(store.tasks(run.id)), branch, run.id)
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
            if request is not None:
                run_dir = step.run_dir(cfg.home, run.id)
                run_dir.mkdir(parents=True)
                # A run with its request is set up.
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
    run goes to the task that waits for it, or to the replanner as an objection to its change of
    the criteria of the plan, and the run resumes. Returns as `plan_request` or `resume`.
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


def approve(cfg: Config, run_id: str | None) -> str | None:
    """Approve the plan of the planning run `run_id`, or of the last planning run of the
    repository with a plan: its tasks become the tasks of the run, with the hash of `plan.json`,
    and `PLAN.md` shows it with the findings of the critic.

    Blocking findings the critic has left do not stop the approval: the operator decides on them.
    A run paused on a replan that changes the acceptance criteria of the plan takes the replan
    and goes on; returns as `resume`.
    """
    repo = _repo(cfg)
    with contextlib.closing(Store(cfg.home / DB_FILE)) as store:
        run = _planned(store, cfg.home, repo, run_id)
        if run.status == "paused":
            selfcheck.require(cfg.home)
            with _hold_repo(repo):
                return _go_on(cfg, store, store.run(run.id), answered=False, approved=True)
        run_dir = step.run_dir(cfg.home, run.id)
        lock = recovery.hold(run_dir / recovery.LOCK_FILE)
        if lock is None:
            raise RunError(f"`loop plan` of run {run.id} has not exited yet")
        with lock, log.scope(run=run.id):
            approved = plan.load(run_dir)
            # Before the approval: an approved run without its `PLAN.md` cannot get it again.
            findings, paths = _write_plan(cfg, run, approved)
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
    return None


def resume(cfg: Config, run_id: str | None) -> str | None:
    """Go on with the paused run `run_id`, or with the last paused run of the repository.

    The branch and HEAD must be where the run has left them. Returns as `start`.
    """
    repo = _repo(cfg)
    selfcheck.require(cfg.home)
    with _hold_repo(repo), contextlib.closing(Store(cfg.home / DB_FILE)) as store:
        return _go_on(cfg, store, _waiting(store, repo, run_id, ("paused",)), answered=False)


def _go_on(
    cfg: Config, store: Store, run: Run, answered: bool, approved: bool = False
) -> str | None:
    """Resume the paused `run`; when the operator has `answered`, the task that waits for the
    reply gets it first, or the replanner the objection to its change of the criteria of the
    plan; when the operator has `approved` the change, the run takes the replan."""
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
        criteria = run.reason == planner.CRITERIA
        if criteria and not (answered or approved):
            raise RunError(
                "the replan changes the acceptance criteria of the plan: `loop approve"
                f" {run.id}` takes it, `loop answer {run.id}` gives the replanner the objection"
                f" in {replies.FILE}"
            )
        if approved and not criteria:
            raise RunError(f"run {run.id} is paused for {run.reason}, not for a replan to approve")
        task = None
        if criteria:
            question = planner.criteria_question(cfg, store, run)
        else:
            # A task in replan is replanned by the run itself.
            waiting = [t for t in store.tasks(run.id) if t.status == "needs_human"]
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
        if answered and task is not None:
            implement.reply(store, task, question, reply)
            logger.info("task %d goes on with the reply of the operator", task.seq)
        profile = commands.frozen(run_dir)
        store.move_run(run.id, "running")
        # Recorded after the move: a run stopped between them is paused as a dead one and its
        # replan asks the operator again. Recorded before, the run would still wait for the
        # operator with the objection or the approval taken.
        if criteria and answered:
            planner.object_to_criteria(cfg, store, run, question, reply)
            logger.info("the replanner goes on with the objection of the operator")
        elif criteria:
            planner.approve_criteria(cfg, store, run)
            logger.info("the operator has approved the new criteria of the plan")
        logger.info("resuming the run paused for %s", run.reason)
        try:
            with commands.services(profile, repo, run_dir):
                return _work(cfg, store, run, profile)
        except KeyboardInterrupt:
            return _interrupted(store, run)


def _write_plan(cfg: Config, run: Run, approved: dict) -> tuple[list[dict], list[Path]]:
    """Write `PLAN.md` of the `approved` plan of `run` with the findings of its critic; returns
    the findings and where `PLAN.md` is."""
    run_dir = step.run_dir(cfg.home, run.id)
    critique = (run_dir / planner.CRITIQUE_FILE).read_text(encoding="utf-8")
    findings = json.loads(critique)["findings"]
    return findings, export.write_plan(run_dir, run, approved, findings, cfg.settings)


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
        if task.status == "replan":
            stop = planner.replan(cfg, store, run, task, previous)
        else:
            go_on = implement.start if task.status == "pending" else implement.resume
            stop = go_on(cfg, store, run, task, len(tasks), profile, previous)
        if stop is not None and stop.reason == step.BUDGET:
            if not _wait(store, run, stop.resume_at):
                return step.INTERRUPT
            continue
        if stop is not None and stop.reason == planner.CRITERIA:
            question = planner.criteria_question(cfg, store, run)
            path = replies.ask(step.run_dir(cfg.home, run.id), run.id, [question])
            store.move_run(run.id, "paused", stop.reason)
            logger.warning(
                "the run is paused, `loop approve %s` takes the new criteria, `loop answer %s`"
                " gives the replanner the objection written in %s",
                run.id,
                run.id,
                path,
            )
            return stop.reason
        if stop is not None:
            return _pause(store, run, stop.reason)
        [task] = [t for t in store.tasks(run.id) if t.id == task.id]
        if task.status == "needs_human":
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
        # Other pauses and a task in replan go on at once: the task resumes its session or goes
        # to the planner, the planner replans it.
        if task.status == "paused" and task.reason in (step.INTERRUPT, gates.BRANCH_CHANGED):
            return _pause(store, run, task.reason)


def _wait(store: Store, run: Run, resume_at: int) -> bool:
    store.move_run(run.id, "paused", step.BUDGET)
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
        recovery.pause(store, run, step.INTERRUPT)
        logger.warning("the run is paused (%s), `loop resume %s` goes on", step.INTERRUPT, run.id)
    return step.INTERRUPT


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
    """The run `run_id` of `repo` that waits for the operator to approve its plan, or the last
    such run of `repo`: a planning run with its plan, or a run paused on a replan that changes
    the acceptance criteria of the plan."""

    def has_plan(run: Run) -> bool:
        return (step.run_dir(home, run.id) / plan.PLAN_FILE).exists()

    if run_id is not None:
        run = _waiting(store, repo, run_id, ("planning", "paused"))
        if run.status == "paused" and run.reason != planner.CRITERIA:
            raise RunError(f"run {run_id} is paused for {run.reason}, not for a replan to approve")
        if run.status == "planning" and not has_plan(run):
            raise RunError(f"run {run_id} has no plan yet")
        return run
    planned = [r for r in store.runs("planning") if has_plan(r)]
    replanned = [r for r in store.runs("paused") if r.reason == planner.CRITERIA]
    runs = [r for r in (*planned, *replanned) if r.repo == str(repo)]
    if not runs:
        raise RunError(f"no planning run in {repo} has a plan, `loop plan` makes one")
    return max(runs, key=lambda r: r.created_at)


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
