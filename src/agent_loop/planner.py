"""The loop of the planner: its sessions turn the request of the operator into a plan.

The request comes from a file or from a conversation of the operator with the planner (`talk`),
after which the loop asks the same session for the plan. The planner answers with a plan or with
questions. A plan that fails `plan.validate` goes back to the same session with its errors, at
most `RETRIES` times after the request or the last conversation; after that the run waits for the
operator with the draft in the run directory. Questions on a request from a file go to
`QUESTIONS.md` in the run directory, and `answer` gives the replies of the operator to the same
session; questions and errors of a conversation are talked over when `talk` reopens it.

A valid plan goes to a critic, a new session each round that has not seen the planner at work.
Blocking findings go back to the planner session, and the plan it fixes goes to the next critic,
at most `plan_critic_rounds` critics after the request or the last conversation. The plan is
saved with the findings of the last critic; the operator decides on those that still block.

A task of a running run that goes back to the planner is replanned at once (`replan`): a new
session gets the approved plan, what the task has left and the tasks after it, and its answer
replaces the task with new ones and keeps, modifies or drops each task after it. An answer with
errors goes back to the same session, at most `RETRIES` times; a stopped session pauses the run,
and `loop resume` replans in a new one.

The run stays in planning while the planner works: a stopped planner does not pause it, and
`go_on` picks the work up from the last step of the planner. A paused session is resumed or
replaced as `step.how_to_resume` decides, an answer the loop has not taken yet is taken.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, replace
from pathlib import Path

from agent_loop import agent, budget, git, guard, implement, log, plan, prompts, replies, step
from agent_loop.config import Config
from agent_loop.store import Run, Step, Store, Task

logger = logging.getLogger(__name__)

PLAN = "plan"
TALK = "talk"
CRITIQUE = "critique"
REPLAN = "replan"
QUESTIONS = "questions"
# The plan still has errors after the retries: the run waits for the operator.
REJECTED = "rejected"
REQUEST_FILE = "request.md"
DRAFT_FILE = "plan-draft.json"
CRITIQUE_FILE = "critique.json"
RETRIES = 2
ANSWER_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {"enum": [PLAN, QUESTIONS]},
        "plan": plan.PLAN_SCHEMA,
        "questions": plan.STRINGS,
    },
    "required": ["status"],
    "additionalProperties": False,
}
CRITIQUE_SCHEMA = {
    "type": "object",
    "properties": {
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "blocking": {"type": "boolean"},
                    "tasks": {"type": "array", "items": {"type": "integer"}},
                    "problem": {"type": "string"},
                    "scenario": {"type": "string"},
                },
                "required": ["blocking", "tasks", "problem", "scenario"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["findings"],
    "additionalProperties": False,
}
REPLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "why": {"type": "string"},
        "tasks": {"type": "array", "items": plan.TASK_SCHEMA},
        "remaining": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "task": {"type": "integer"},
                    "action": {"enum": ["keep", "modify", "drop"]},
                    "why": {"type": "string"},
                    "card": plan.TASK_SCHEMA,
                },
                "required": ["task", "action", "why"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["why", "tasks", "remaining"],
    "additionalProperties": False,
}


class PlanError(Exception):
    pass


@dataclass(frozen=True)
class _Session:
    prompt: str
    # The session to go on with --resume; None starts a new one.
    session_id: str | None = None
    # PLAN for a session of the planner, CRITIQUE for one of the critic, REPLAN for one that
    # replans a running run.
    kind: str = PLAN


def go_on(cfg: Config, store: Store, run: Run) -> str | None:
    """Plan the request of the planning `run`, from where its planner has stopped if it has.

    Returns why the run waits: `QUESTIONS` for the replies of the operator, `REJECTED` or
    `implement.INTERRUPT`; None when the plan is in `plan.PLAN_FILE` of the run directory.
    """
    return _Planner(cfg, store, run).go_on()


def talk(cfg: Config, store: Store, run: Run) -> str | None:
    """Open the conversation of the operator with the planner of `run` in the terminal, the one
    the run has if it has one, and ask the planner for the plan when the operator has left;
    returns as `go_on`."""
    return _Planner(cfg, store, run).talk()


def answer(cfg: Config, store: Store, run: Run) -> str | None:
    """Give the planner of `run` the replies of the operator from `QUESTIONS.md`; returns as
    `go_on`."""
    return _Planner(cfg, store, run).answer()


def replan(cfg: Config, store: Store, run: Run, task: Task, previous: str) -> implement.Stop | None:
    """Replan the tasks left in the running `run` after `task` has gone back to the planner.

    The work of the task goes to its WIP ref first (`implement.set_aside`). `previous` is what
    `prompts.previous_tasks` tells about the tasks before it. Returns why the run stops: the
    limits leave no room (`implement.BUDGET`), the session has paused (its reason) or its answer
    keeps its errors (`REJECTED`); None when the store has the new tasks.
    """
    return _Planner(cfg, store, run).replan(task, previous)


class _Planner:
    def __init__(self, cfg: Config, store: Store, run: Run) -> None:
        self.cfg = cfg
        self.store = store
        self.run = run
        self.repo = Path(run.repo)
        self.run_dir = step.run_dir(cfg.home, run.id)
        # A run that plans in a conversation has no request file.
        self.conversation = not (self.run_dir / REQUEST_FILE).exists()

    def go_on(self) -> str | None:
        steps = self.store.planner_steps(self.run.id)
        if not steps:
            return self._loop(self._new())
        last = steps[-1]
        if last.outcome == "paused":
            return self._loop(self._resumed(steps))
        if last.outcome == "failed" or last.detail.get("answer") is None:
            # The error is in the log of the last `loop plan`: the step starts anew.
            return self._loop(self._critic(steps) if last.kind == CRITIQUE else self._new())
        return self._loop(self._take(steps))

    def talk(self) -> str | None:
        steps = [s for s in self.store.planner_steps(self.run.id) if s.kind != CRITIQUE]
        # A conversation that has failed to start has nothing to go on with.
        if steps and not (steps[-1].kind == TALK and steps[-1].outcome == "failed"):
            upcoming = _Session("", steps[-1].session_id)
        else:
            upcoming = _Session(prompts.planner_talk(self.cfg.settings))
        self._wait_for_room()
        session = self._agent_session(upcoming, schema=None)
        step_id = self.store.start_step(
            self.run.id, None, TALK, git.head(self.repo), [], session.session_id
        )
        logger.info(
            "talk the request over with the planner, the loop asks for the plan after /exit"
        )
        code = agent.talk(session, self.run_dir / f"step-{step_id}.system.md")
        if code != 0:
            self.store.finish_step(step_id, "failed", {"exit_code": code})
            raise PlanError(f"claude has exited with code {code}")
        self.store.finish_step(step_id, "completed", {"exit_code": code})
        logger.info("the conversation is over, asking the planner for the plan")
        return self._loop(_Session(prompts.PLANNER_TALKED, session.session_id))

    def answer(self) -> str | None:
        if self.conversation:
            raise PlanError(f"run {self.run.id} plans in a conversation, `loop plan` reopens it")
        steps = self.store.planner_steps(self.run.id)
        output = steps[-1].detail.get("answer") if steps and steps[-1].kind == PLAN else None
        if output is None or output["status"] != QUESTIONS or _errors(output):
            raise PlanError(f"the planner of run {self.run.id} has no questions to answer")
        questions = output["questions"]
        answers = replies.read(self.run_dir, self.run.id, questions)
        logger.info("the planner goes on with the answers to %d questions", len(questions))
        return self._loop(
            _Session(prompts.planner_answers(questions, answers), steps[-1].session_id)
        )

    def replan(self, task: Task, previous: str) -> implement.Stop | None:
        tasks = self.store.tasks(self.run.id)
        left = [t for t in tasks if t.seq > task.seq and t.status == "pending"]
        through = [t.title for t in tasks if t.status in ("committed", "done")]
        logger.warning(
            "task %d has gone back to the planner (%s), replanning it and %d tasks after it",
            task.seq,
            task.reason,
            len(left),
        )
        ref = implement.set_aside(self.store, self.run, task)
        steps = self.store.steps(task.id)
        said = None
        if task.reason == "too_big":
            said = prompts.answer(implement.last_answer(steps))
        verdict = None
        if task.reason in ("diff_too_big", "fix_attempts"):
            verdict = implement.last_verdict(steps)
        wip = None if ref is None else (ref, git.git(self.repo, "show", "--stat", "--format=", ref))
        prompt = prompts.replanner(
            plan.load(self.run_dir), task, left, previous, self.cfg.settings, said, verdict, wip
        )
        upcoming = _Session(prompt, kind=REPLAN)
        for _ in range(RETRIES + 1):
            resume_at = budget.no_room(self.cfg.home, self.cfg.settings)
            if resume_at is not None:
                return implement.Stop(implement.BUDGET, resume_at)
            outcome = self._session(upcoming)
            if outcome.status == "failed":
                raise PlanError(f"the replanning session has failed: {outcome.reason}")
            if outcome.status == "paused":
                return implement.Stop(outcome.reason)
            last = self.store.planner_steps(self.run.id)[-1]
            output = last.detail["answer"]
            if output is None:
                raise PlanError("the replanning session has ended without an answer")
            errors = plan.validate_replan(output, {t.seq: t.title for t in left}, through)
            if not errors:
                self._replace(task, left, output)
                if outcome.reason == implement.INTERRUPT:
                    return implement.Stop(implement.INTERRUPT)
                return None
            logger.warning("the replan has errors:\n%s", "\n".join(errors))
            upcoming = _Session(prompts.plan_errors(errors), last.session_id, REPLAN)
        logger.error(
            "the replan still has errors after %d retries, `loop resume` replans anew", RETRIES
        )
        return implement.Stop(REJECTED)

    def _replace(self, task: Task, left: list[Task], output: dict) -> None:
        """Put the replan of `output` into the store: new tasks for `task`, actions on `left`."""
        by_seq = {t.seq: t for t in left}
        entries = output["remaining"]
        self.store.replan(
            task.id,
            [plan.card(new) for new in output["tasks"]],
            {
                by_seq[e["task"]].id: plan.card(e["card"])
                for e in entries
                if e["action"] == "modify"
            },
            [by_seq[e["task"]].id for e in entries if e["action"] == "drop"],
        )
        logger.info(
            "task %d is replaced with %d new tasks: %s%s",
            task.seq,
            len(output["tasks"]),
            output["why"],
            "".join(f"\n- task {e['task']}: {e['action']}, {e['why']}" for e in entries),
        )

    def _loop(self, upcoming: _Session | str | None) -> str | None:
        """Run the `upcoming` session and the ones it leads to; returns as `go_on`."""
        while isinstance(upcoming, _Session):
            self._wait_for_room()
            outcome = self._session(upcoming)
            steps = self.store.planner_steps(self.run.id)
            if outcome.status == "failed":
                raise PlanError(f"the planner session has failed: {outcome.reason}")
            if outcome.status == "paused":
                if outcome.reason == implement.INTERRUPT:
                    return self._interrupted()
                upcoming = self._resumed(steps)
                continue
            if steps[-1].detail["answer"] is None:
                raise PlanError("the planner session has ended without an answer")
            upcoming = self._take(steps)
            if outcome.reason == implement.INTERRUPT and isinstance(upcoming, _Session):
                return self._interrupted()
        return upcoming

    def _new(self) -> _Session:
        request = (self.run_dir / REQUEST_FILE).read_text(encoding="utf-8")
        return _Session(prompts.planner(request, self.cfg.settings))

    def _critic(self, steps: list[Step]) -> _Session:
        """A new critic of the last plan of `steps`."""
        request = None
        if not self.conversation:
            request = (self.run_dir / REQUEST_FILE).read_text(encoding="utf-8")
        output = _plan_step(steps).detail["answer"]
        prompt = prompts.plan_critic(output["plan"], request, self.cfg.settings)
        return _Session(prompt, kind=CRITIQUE)

    def _resumed(self, steps: list[Step]) -> _Session:
        kind = steps[-1].kind
        how = step.how_to_resume(
            [s for s in steps if s.kind == kind], self.cfg.settings.ctx_soft_tokens
        )
        if how.mode == "session" and kind == CRITIQUE:
            return _Session(prompts.CRITIC_RESUMED, how.session_id, CRITIQUE)
        if how.mode == "session":
            return _Session(prompts.PLANNER_RESUMED, how.session_id)
        if how.mode == "handoff" and kind == CRITIQUE:
            logger.warning("the context of the critic is full, a new critic reviews the plan")
            return self._critic(steps)
        if how.mode == "handoff":
            if self.conversation:
                raise PlanError(
                    "the conversation has filled the context of the planner: `/compact` it in"
                    " `loop plan`, or start anew with `loop plan --new`"
                )
            # The planner writes no handoff: a new session reads the repository again.
            logger.warning("the context of the planner is full, planning anew in a new session")
            return self._new()
        who = "critic" if kind == CRITIQUE else "planner"
        raise PlanError(f"the {who} keeps stopping ({how.reason}), split the request")

    def _take(self, steps: list[Step]) -> _Session | str | None:
        """Take the answer of the last step: returns the session that fixes or reviews the plan,
        why the run waits, or None when the plan is saved."""
        last = steps[-1]
        if last.kind == CRITIQUE:
            return self._take_critique(steps)
        output = last.detail["answer"]
        errors = _errors(output)
        if not errors and output["status"] == QUESTIONS and self.conversation:
            logger.warning(
                "the planner has questions, `loop plan` reopens the conversation to talk them"
                " over:\n%s",
                "\n".join(f"- {question}" for question in output["questions"]),
            )
            return QUESTIONS
        if not errors and output["status"] == QUESTIONS:
            path = replies.ask(self.run_dir, self.run.id, output["questions"])
            logger.warning(
                "the planner has questions: answer them in %s and run `loop answer %s`",
                path,
                self.run.id,
            )
            return QUESTIONS
        if not errors:
            logger.info("a critic reviews the plan of %d tasks", len(output["plan"]["tasks"]))
            return self._critic(steps)
        logger.warning("the plan has errors:\n%s", "\n".join(errors))
        rejected = sum(bool(_errors(s.detail["answer"])) for s in _answered(steps, PLAN))
        if rejected <= RETRIES:
            return _Session(prompts.plan_errors(errors), last.session_id)
        if "plan" in output:
            plan.save(self.run_dir, output["plan"], DRAFT_FILE)
        logger.error(
            "the plan still has errors after %d retries, the draft is in %s: %s",
            RETRIES,
            self.run_dir / DRAFT_FILE,
            "`loop plan` reopens the conversation to fix it"
            if self.conversation
            else "change the request and plan again",
        )
        return REJECTED

    def _take_critique(self, steps: list[Step]) -> _Session | None:
        """Take the findings of the critic: returns the planner session that fixes the plan, or
        None when the plan is saved with them."""
        findings = [
            finding | {"blocking": finding["blocking"] and bool(finding["scenario"].strip())}
            for finding in steps[-1].detail["answer"]["findings"]
        ]
        blocking = [finding for finding in findings if finding["blocking"]]
        rounds = len(_answered(steps, CRITIQUE))
        listed = "\n".join(f"- {finding['problem']}" for finding in blocking)
        if blocking and rounds < self.cfg.settings.plan_critic_rounds:
            logger.warning(
                "the critic has %d blocking findings, the planner fixes the plan:\n%s",
                len(blocking),
                listed,
            )
            return _Session(prompts.plan_critique(findings), _plan_step(steps).session_id)
        output = _plan_step(steps).detail["answer"]
        plan.save(self.run_dir, {"findings": findings}, CRITIQUE_FILE)
        # Written last: the run with its plan is through with planning.
        plan.save(self.run_dir, output["plan"])
        if blocking:
            logger.warning(
                "the critic has left %d blocking findings to the operator, they are in %s:\n%s",
                len(blocking),
                self.run_dir / CRITIQUE_FILE,
                listed,
            )
        logger.info(
            "the plan of %d tasks is in %s",
            len(output["plan"]["tasks"]),
            self.run_dir / plan.PLAN_FILE,
        )
        return None

    def _session(self, upcoming: _Session) -> step.Outcome:
        schema = {PLAN: ANSWER_SCHEMA, CRITIQUE: CRITIQUE_SCHEMA, REPLAN: REPLAN_SCHEMA}
        session = self._agent_session(upcoming, schema[upcoming.kind])
        # The planner, the critic and the replanner change no files: nothing to roll back or to
        # tell from their work.
        step_id = self.store.start_step(
            self.run.id, None, upcoming.kind, git.head(self.repo), [], session.session_id
        )
        with log.scope(stage=upcoming.kind):
            return step.run(session, self.cfg, self.store, self.run.id, step_id)

    def _agent_session(self, upcoming: _Session, schema: dict | None) -> agent.Session:
        settings = self.cfg.settings
        critic = upcoming.kind == CRITIQUE
        role = {PLAN: "planner", CRITIQUE: "plan_critic", REPLAN: "replanner"}
        session = agent.Session(
            prompt=upcoming.prompt,
            model=settings.model_critic if critic else settings.model_planner,
            cwd=self.repo,
            settings=guard.prepare(
                self.run_dir, self.cfg.home, settings, self.repo, read_only=True
            ),
            effort=settings.effort_critic if critic else settings.effort_planner,
            system_files=prompts.system_files(role[upcoming.kind], settings),
            schema=schema,
            tools="Bash,Read",
        )
        if upcoming.session_id is not None:
            session = replace(session, session_id=upcoming.session_id, resume=True)
        return session

    def _wait_for_room(self) -> None:
        resume_at = budget.no_room(self.cfg.home, self.cfg.settings)
        if resume_at is not None:
            time.sleep(max(0, resume_at - time.time()))

    def _interrupted(self) -> str:
        logger.warning("the planner is stopped by the operator, the same `loop plan` goes on")
        return implement.INTERRUPT


def _answered(steps: list[Step], kind: str) -> list[Step]:
    """The steps of `kind` that have answered since the last conversation: the retries and the
    rounds of the critic start anew after it, the operator may have changed the plan there."""
    answered = []
    for s in reversed(steps):
        if s.kind == TALK:
            break
        if s.kind == kind and s.outcome == "completed" and s.detail["answer"] is not None:
            answered.append(s)
    return answered


def _plan_step(steps: list[Step]) -> Step:
    """The last step of the planner with an answer: the plan the critic reviews, from the
    session that fixes it."""
    return next(s for s in reversed(steps) if s.kind == PLAN and s.detail.get("answer"))


def _errors(output: dict) -> list[str]:
    """What keeps the loop from taking an answer of the planner."""
    if output["status"] == QUESTIONS:
        questions = output.get("questions", [])
        if not questions or not all(question.strip() for question in questions):
            return ["the answer has the status questions without the questions"]
        return []
    if "plan" not in output:
        return ["the answer has the status plan without the plan"]
    return plan.validate(output["plan"])
