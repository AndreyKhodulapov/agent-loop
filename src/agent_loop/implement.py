"""The loop of one task: the implementer works, the gate checks, the fixer repairs what failed,
the loop commits what has passed.

Every session, gate and commit is a step in the store; a gate keeps its verdict in the detail of
its step, so the fixer can get the findings without running the commands again. The task moves
through implementing, gating and fixing until its work passes the gate and is committed, or until
it goes to a human, back to the planner or pauses. A paused step leaves the work in progress in
the WIP ref of the task. A commit message that breaks the rules goes back to its session once;
the answer goes through the gate again, as the session may have changed the files.

A stopped task goes on from its status and its last step: a paused session is resumed or
replaced by a new one that starts from the handoff, a step whose end the loop has not taken yet
is taken. Before each session the loop checks the subscription limits: when they leave no room,
the task keeps its status and the run waits.

All steps of a task share the checkpoint and the untracked files of its start: until the commit,
the work of the task is its work against that checkpoint, whichever session has done it. Files
the gate commands leave join the untracked files, they are not the work either.
"""

from __future__ import annotations

import logging
import time
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from agent_loop import agent, budget, gates, git, guard, log, prompts, step
from agent_loop.config import Config, Profile
from agent_loop.store import Run, Step, Store, Task

logger = logging.getLogger(__name__)

IMPLEMENT = "implement"
GATE = "gate"
FIX = "fix"
COMMIT = "commit"
INTERRUPT = "interrupt"
BUDGET = "budget"


@dataclass(frozen=True)
class Stop:
    """Why the run stops before the task is through; the task keeps its status."""

    # interrupt: the operator has stopped the run while a session was answering, the answer is
    # taken; budget: the subscription limits leave no room for the next session.
    reason: str
    # Unix seconds when the limits are expected to leave room, for a budget stop.
    resume_at: int | None = None


@dataclass(frozen=True)
class _Session:
    kind: str
    prompt: str
    # The paused session to go on with --resume; None starts a new one.
    session_id: str | None = None

    @property
    def role(self) -> str:
        return "implementer" if self.kind == IMPLEMENT else "fixer"

    @property
    def stage(self) -> str:
        return "implementing" if self.kind == IMPLEMENT else "fixing"


def start(
    cfg: Config, store: Store, run: Run, task: Task, total: int, profile: Profile, previous: str
) -> Stop | None:
    """Take a pending `task`, one of `total` tasks of `run`, through the loop.

    `profile` is the frozen profile of the run, `previous` is what `prompts.previous_tasks` tells
    about the tasks before this one. Returns why the run has to stop before the task is through;
    otherwise None, and the status of the task tells where it is.
    """
    with log.scope(task=f"{task.seq}/{total}"):
        return _Task(cfg, store, run, task, total, profile).start(previous)


def resume(
    cfg: Config, store: Store, run: Run, task: Task, total: int, profile: Profile, previous: str
) -> Stop | None:
    """Go on with a `task` that has stopped before it was through; the rest is as in `start`.

    The task is paused, or it is implementing, gating or fixing after the run stopped between
    its steps. All its steps are finished: `recovery.reap` finishes the steps of a dead run.
    """
    with log.scope(task=f"{task.seq}/{total}"):
        return _Task(cfg, store, run, task, total, profile).resume(previous)


class _Task:
    def __init__(
        self, cfg: Config, store: Store, run: Run, task: Task, total: int, profile: Profile
    ) -> None:
        self.cfg = cfg
        self.store = store
        self.run = run
        self.task = task
        self.status = task.status
        self.total = total
        self.profile = profile
        self.repo = Path(run.repo)
        self.run_dir = step.run_dir(cfg.home, run.id)
        self.handoff = self.repo / guard.HANDOFF
        # The handoff is for the next session, it is not the work of the task.
        git.exclude(self.repo, f"/{guard.HANDOFF.parent}/")
        steps = store.steps(task.id)
        self.checkpoint = steps[0].checkpoint if steps else git.head(self.repo)
        self.untracked = steps[0].untracked if steps else git.untracked(self.repo)
        for gate in steps:
            if gate.kind == GATE and gate.outcome == "completed":
                self.untracked = [*self.untracked, *gate.detail["artifacts"]]
        self.tests = task.tests

    def start(self, previous: str) -> Stop | None:
        self.tests = gates.count_tests(self.profile, self.repo)
        if self.tests is not None:
            self.store.set_tests(self.task.id, self.tests)
        return self._loop(self._implementer(previous))

    def resume(self, previous: str) -> Stop | None:
        steps = self.store.steps(self.task.id)
        if not steps:
            # The run stopped before the first session of the task.
            return self._loop(self._implementer(previous))
        last = steps[-1]
        if last.outcome == "paused":
            if self.status != "paused":
                # The run stopped between pausing the step and pausing the task.
                self._pause(last.detail["reason"])
            return self._resume_paused(steps, previous)
        if last.kind == COMMIT:
            # The run stopped between the commit and the move of the task.
            if self.status == "gating":
                self._move("committed")
            return None
        if last.kind == GATE:
            verdict = _verdict(last.detail)
            # Still gating: the verdict has passed, or the run stopped before it moved the task.
            upcoming = (
                self._after_gate(verdict) if self.status == "gating" else self._fixer(verdict)
            )
            return None if upcoming is None else self._loop(upcoming)
        # After a session the task is gating once its end is taken.
        if self.status != "gating" and not self._take(last):
            return None
        return self._loop(None)

    def _resume_paused(self, steps: list[Step], previous: str) -> Stop | None:
        last = steps[-1]
        if last.kind in (GATE, COMMIT):
            # The branch has moved: the work is checked again before it is committed.
            return self._loop(None)
        how = step.how_to_resume(steps, self.cfg.settings.ctx_soft_tokens)
        if how.mode == "replan":
            logger.error("the task keeps pausing (%s), it goes back to the planner", how.reason)
            self._move("replan", how.reason)
            return None
        if how.mode == "session":
            return self._loop(_Session(last.kind, prompts.RESUMED, how.session_id))
        # A session stopped hard has not written the handoff: the work in progress tells enough.
        handoff = self.handoff.read_text(encoding="utf-8") if self.handoff.is_file() else None
        if last.kind == IMPLEMENT:
            return self._loop(self._implementer(previous, handoff, self._work()))
        gate = next(s for s in reversed(steps) if s.kind == GATE)
        return self._loop(self._fixer(_verdict(gate.detail), handoff))

    def _loop(self, upcoming: _Session | None) -> Stop | None:
        """Run the `upcoming` session, or the gate if it is None, and go on until the task is
        through with the loop or the run has to stop."""
        while True:
            if upcoming is not None:
                stop = self._budget()
                if stop is not None:
                    return stop
                self._enter(upcoming.stage)
                try:
                    outcome = self._session(upcoming)
                except agent.AgentError as exc:
                    # The step has failed with the error, its end sends the task to a human.
                    logger.error("the session has failed: %s", exc)
                    self._take(self.store.steps(self.task.id)[-1])
                    return None
                to_gate = self._take(self.store.steps(self.task.id)[-1])
                if outcome.status == "completed" and outcome.reason == INTERRUPT:
                    logger.warning("stopped by the operator after the answer of the session")
                    return Stop(INTERRUPT)
                if not to_gate:
                    return None
            self._enter("gating")
            upcoming = self._after_gate(self._gate())
            if upcoming is None:
                return None

    def _implementer(
        self, previous: str, handoff: str | None = None, work: str | None = None
    ) -> _Session:
        settings = self.cfg.settings
        prompt = prompts.implementer(self.task, self.total, settings, previous, handoff, work)
        return _Session(IMPLEMENT, prompt)

    def _fixer(self, verdict: gates.Verdict, handoff: str | None = None) -> _Session:
        settings = self.cfg.settings
        prompt = prompts.fixer(self.task, self.total, settings, verdict, self._work(), handoff)
        return _Session(FIX, prompt)

    def _work(self) -> str:
        return git.diff(self.repo, self.checkpoint, self._new_files())

    def _new_files(self) -> list[str]:
        """The files the task has added. Those that look like secrets stay out of the prompts as
        they stay out of the commit."""
        added = git.new_files(self.repo, self.checkpoint, self.untracked)
        secrets = gates.secret_files(added, self.profile)
        return [path for path in added if path not in secrets]

    def _budget(self) -> Stop | None:
        limits = budget.load_limits(self.cfg.home / budget.LIMITS_FILE)
        if limits is None:
            return None
        verdict = budget.check(limits, time.time(), self.cfg.settings)
        if verdict.allowed:
            return None
        resume_at = time.strftime("%Y-%m-%d %H:%M", time.localtime(verdict.resume_at))
        logger.warning(
            "no room for a session in the limits (%s) until %s", verdict.summary, resume_at
        )
        return Stop(BUDGET, verdict.resume_at)

    def _session(self, upcoming: _Session) -> step.Outcome:
        settings = self.cfg.settings
        # The handoff of the previous session is in the prompt or in the resumed context; a stale
        # one would be taken for the handoff of this session.
        self.handoff.unlink(missing_ok=True)
        session = agent.Session(
            prompt=upcoming.prompt,
            model=settings.model_implementer,
            cwd=self.repo,
            settings=guard.prepare(self.run_dir, self.cfg.home, settings, self.repo),
            effort=settings.effort_implementer,
            system_files=prompts.system_files(upcoming.role, settings),
            schema=prompts.ANSWER_SCHEMA,
            env=self.profile.env,
        )
        if upcoming.session_id is not None:
            session = replace(session, session_id=upcoming.session_id, resume=True)
        step_id = self.store.start_step(
            self.task.id, upcoming.kind, self.checkpoint, self.untracked, session.session_id
        )
        with log.scope(stage=upcoming.kind):
            return step.run(session, self.cfg, self.store, self.run.id, step_id)

    def _take(self, session_step: Step) -> bool:
        """Move the task by how its session has ended; True when its work goes to the gate."""
        # A step failed by an error of the CLI has the error in its detail and no reason.
        reason = session_step.detail.get("reason")
        if session_step.outcome == "paused":
            self._pause(reason)
            return False
        if session_step.outcome == "failed":
            settings_changed = reason == step.PROJECT_SETTINGS
            self._move(
                "needs_human", step.PROJECT_SETTINGS if settings_changed else "session_failed"
            )
            return False
        output = session_step.detail.get("answer")
        if output is None:
            logger.error("the session has ended without an answer")
            self._move("needs_human", "no_answer")
            return False
        answer = prompts.answer(output)
        if answer.status == "done":
            self._move("gating")
            return True
        # Status and reason the task moves with on an answer other than done.
        status, reason = {
            "too_big": ("replan", "too_big"),
            "needs_human": ("needs_human", "question"),
            "blocked": ("needs_human", "blocked"),
        }[answer.status]
        logger.warning("%s: %s", reason, answer.question or answer.summary)
        self._move(status, reason)
        return False

    def _gate(self) -> gates.Verdict:
        """Check the work of the task; the verdict is kept in the detail of a gate step."""
        step_id = self.store.start_step(self.task.id, GATE, self.checkpoint, self.untracked)
        with log.scope(stage=GATE):
            verdict = gates.check(
                self.repo,
                self.run_dir,
                self.run.branch,
                self.store.steps(self.task.id)[-1],
                self.profile,
                self.cfg.settings,
                self.tests,
            )
        outcome = "paused" if verdict.status == "paused" else "completed"
        self.store.finish_step(step_id, outcome, asdict(verdict))
        self.untracked = [*self.untracked, *verdict.artifacts]
        return verdict

    def _after_gate(self, verdict: gates.Verdict) -> _Session | None:
        """Move the task by the verdict of its last step, the gate; returns the next session of
        the task, None when the task is through with the loop one way or another."""
        if verdict.status == "pass":
            return self._commit()
        if verdict.status == "paused":
            self._pause(verdict.reason)
            return None
        status, reason = verdict.status, verdict.reason
        if status == "fixing":
            steps = self.store.steps(self.task.id)
            weakened = [
                s
                for s in steps[:-1]
                if s.kind == GATE and s.detail["reason"] == gates.WEAKENED_TESTS
            ]
            # A fixer asked for another commit message answers twice in one session.
            fixes = {s.session_id for s in steps if s.kind == FIX and s.outcome == "completed"}
            if reason == gates.WEAKENED_TESTS and weakened:
                logger.error("the tests are weakened again, a human has to look")
                status = "needs_human"
            elif len(fixes) >= self.cfg.settings.fix_attempts:
                logger.error("%d fixes have not passed the gate", len(fixes))
                status, reason = "replan", "fix_attempts"
        self._move(status, reason)
        return self._fixer(verdict) if status == "fixing" else None

    def _commit(self) -> _Session | None:
        """Commit the work that has passed the gate with the message of the last session; returns
        the session to ask for another message when this one breaks the rules."""
        added = self._new_files()
        if not git.diff(self.repo, self.checkpoint, added, "--name-only"):
            logger.error("the session has answered done without changing anything")
            self._move("needs_human", "no_changes")
            return None
        steps = self.store.steps(self.task.id)
        message = last_answer(steps)["commit_message"]
        if not gates.commit_message_ok(message):
            return self._ask_message(steps, message)
        step_id = self.store.start_step(self.task.id, COMMIT, self.checkpoint, self.untracked)
        with log.scope(stage=COMMIT):
            # The verdict may be from before the run stopped, and the operator may have moved
            # the branch since.
            moved = gates.branch_moved(self.repo, self.run.branch, self.checkpoint)
            if moved is not None:
                logger.error("%s, pausing", moved)
                self.store.finish_step(step_id, "paused", {"reason": gates.BRANCH_CHANGED})
                self._pause(gates.BRANCH_CHANGED)
                return None
            commit = git.commit(self.repo, self.checkpoint, added, message)
            logger.info("committed %s %s", commit[:12], message)
        self.store.finish_step(step_id, "completed", {"commit": commit, "message": message})
        self._move("committed")
        return None

    def _ask_message(self, steps: list[Step], message: str) -> _Session | None:
        sessions = [s for s in steps if s.kind in (IMPLEMENT, FIX)]
        last = sessions[-1]
        # A session answers once, unless it has been asked for another message already.
        if any(s.session_id == last.session_id and s.outcome == "completed" for s in sessions[:-1]):
            logger.error("the commit message %r still breaks the rules", message)
            self._move("needs_human", "commit_message")
            return None
        logger.warning("the commit message %r breaks the rules, asking the session again", message)
        return _Session(last.kind, prompts.commit_message(message), last.session_id)

    def _pause(self, reason: str) -> None:
        ref = git.wip_ref(self.run.id, self.task.seq)
        git.save_wip(self.repo, ref, self.untracked, self.checkpoint)
        logger.info("work in progress saved in %s", ref)
        self._move("paused", reason)

    def _enter(self, stage: str) -> None:
        # A pending or paused task moves into the stage of its next step; otherwise the move to
        # the stage has come with the end of the previous step.
        if self.status != stage:
            self._move(stage)

    def _move(self, status: str, reason: str | None = None) -> None:
        self.store.move_task(self.task.id, status, reason)
        self.status = status


def last_answer(steps: list[Step]) -> dict:
    """The answer of the last session of a task; a committed task is committed with it."""
    return next(s for s in reversed(steps) if s.kind in (IMPLEMENT, FIX)).detail["answer"]


def _verdict(detail: dict) -> gates.Verdict:
    return gates.Verdict(
        **detail
        | {
            "findings": tuple(detail["findings"]),
            "secrets": tuple(detail["secrets"]),
            "artifacts": tuple(detail["artifacts"]),
        }
    )
