"""The loop of one task: the implementer works, the gate checks, the fixer repairs what failed.

Every session and every gate is a step in the store; a gate keeps its verdict in the detail of
its step, so the fixer can get the findings without running the commands again. The task moves
through implementing, gating and fixing until its work passes the gate, which leaves the task in
gating for the commit, or until it goes to a human, back to the planner or pauses. A paused step
leaves the work in progress in the WIP ref of the task.

All steps of a task share the checkpoint and the untracked files of its start: until the commit,
the work of the task is its work against that checkpoint, whichever session has done it.
"""

from __future__ import annotations

import logging
from dataclasses import asdict
from pathlib import Path

from agent_loop import agent, gates, git, guard, log, prompts, step
from agent_loop.config import Config, Profile
from agent_loop.store import Run, Store, Task

logger = logging.getLogger(__name__)

IMPLEMENT = "implement"
GATE = "gate"
FIX = "fix"
INTERRUPT = "interrupt"
NO_ANSWER = "no_answer"
SESSION_FAILED = "session_failed"
FIX_ATTEMPTS = "fix_attempts"
# Status and reason the task moves with on an answer other than done.
ANSWERS = {
    "too_big": ("replan", "too_big"),
    "needs_human": ("needs_human", "question"),
    "blocked": ("needs_human", "blocked"),
}


def start(
    cfg: Config, store: Store, run: Run, task: Task, total: int, profile: Profile, previous: str
) -> str | None:
    """Take a pending `task`, one of `total` tasks of `run`, through the loop.

    `profile` is the frozen profile of the run, `previous` is what `prompts.previous_tasks` tells
    about the tasks before this one. Returns `interrupt` when the operator has stopped the run
    while a session was answering: the answer is taken, the task keeps the status it has moved
    to and the run stops before the next step. Otherwise returns None, and the status of the task
    tells where it is.
    """
    with log.scope(task=f"{task.seq}/{total}"):
        return _Task(cfg, store, run, task, total, profile).start(previous)


class _Task:
    def __init__(
        self, cfg: Config, store: Store, run: Run, task: Task, total: int, profile: Profile
    ) -> None:
        self.cfg = cfg
        self.store = store
        self.run = run
        self.task = task
        self.total = total
        self.profile = profile
        self.repo = Path(run.repo)
        self.run_dir = step.run_dir(cfg.home, run.id)
        self.checkpoint = git.head(self.repo)
        self.untracked = git.untracked(self.repo)
        self.tests: int | None = None

    def start(self, previous: str) -> str | None:
        self.tests = gates.count_tests(self.profile, self.repo)
        if self.tests is not None:
            self.store.set_tests(self.task.id, self.tests)
        self._move("implementing")
        kind, role = IMPLEMENT, "implementer"
        prompt = prompts.implementer(self.task, self.total, self.cfg.settings, previous)
        while True:
            outcome = self._session(kind, role, prompt)
            to_gate = self._take(outcome)
            if outcome.status == "completed" and outcome.reason == INTERRUPT:
                logger.warning("stopped by the operator after the answer of the session")
                return INTERRUPT
            if not to_gate:
                return None
            verdict = self._gate()
            if verdict is None:
                return None
            # Files that look like secrets stay out of the prompt as they stay out of the commit.
            added = git.new_files(self.repo, self.checkpoint, self.untracked)
            work = git.diff(
                self.repo, self.checkpoint, [path for path in added if path not in verdict.secrets]
            )
            kind, role = FIX, "fixer"
            prompt = prompts.fixer(self.task, self.total, self.cfg.settings, verdict, work)

    def _session(self, kind: str, role: str, prompt: str) -> step.Outcome:
        settings = self.cfg.settings
        session = agent.Session(
            prompt=prompt,
            model=settings.model_implementer,
            cwd=self.repo,
            settings=guard.prepare(self.run_dir, self.cfg.home, settings, self.repo),
            effort=settings.effort_implementer,
            system_files=prompts.system_files(role, settings),
            schema=prompts.ANSWER_SCHEMA,
            env=self.profile.env,
        )
        step_id = self.store.start_step(
            self.task.id, kind, self.checkpoint, self.untracked, session.session_id
        )
        with log.scope(stage=kind):
            return step.run(session, self.cfg, self.store, self.run.id, step_id)

    def _take(self, outcome: step.Outcome) -> bool:
        """Move the task by how the session has ended; True when its work goes to the gate."""
        if outcome.status == "paused":
            self._pause(outcome.reason)
            return False
        if outcome.status == "failed":
            settings_changed = outcome.reason == step.PROJECT_SETTINGS
            self._move("needs_human", step.PROJECT_SETTINGS if settings_changed else SESSION_FAILED)
            return False
        if outcome.result.structured_output is None:
            logger.error("the session has ended without an answer")
            self._move("needs_human", NO_ANSWER)
            return False
        answer = prompts.answer(outcome.result.structured_output)
        if answer.status == "done":
            self._move("gating")
            return True
        status, reason = ANSWERS[answer.status]
        logger.warning("%s: %s", reason, answer.question or answer.summary)
        self._move(status, reason)
        return False

    def _gate(self) -> gates.Verdict | None:
        """Check the work; returns the verdict for the fixer, None when the task is through
        with the gate one way or another."""
        step_id = self.store.start_step(self.task.id, GATE, self.checkpoint, self.untracked)
        steps = self.store.steps(self.task.id)
        with log.scope(stage=GATE):
            verdict = gates.check(
                self.repo,
                self.run_dir,
                self.run.branch,
                steps[-1],
                self.profile,
                self.cfg.settings,
                self.tests,
            )
        outcome = "paused" if verdict.status == "paused" else "completed"
        self.store.finish_step(step_id, outcome, asdict(verdict))
        if verdict.status == "pass":
            return None
        if verdict.status == "paused":
            self._pause(verdict.reason)
            return None
        status, reason = verdict.status, verdict.reason
        if status == "fixing":
            weakened = [
                s
                for s in steps[:-1]
                if s.kind == GATE and s.detail["reason"] == gates.WEAKENED_TESTS
            ]
            fixes = [s for s in steps if s.kind == FIX and s.outcome == "completed"]
            if reason == gates.WEAKENED_TESTS and weakened:
                logger.error("the tests are weakened again, a human has to look")
                status = "needs_human"
            elif len(fixes) >= self.cfg.settings.fix_attempts:
                logger.error("%d fixes have not passed the gate", len(fixes))
                status, reason = "replan", FIX_ATTEMPTS
        self._move(status, reason)
        return verdict if status == "fixing" else None

    def _pause(self, reason: str) -> None:
        ref = git.wip_ref(self.run.id, self.task.seq)
        git.save_wip(self.repo, ref, self.untracked, self.checkpoint)
        logger.info("work in progress saved in %s", ref)
        self._move("paused", reason)

    def _move(self, status: str, reason: str | None = None) -> None:
        self.store.move_task(self.task.id, status, reason)
