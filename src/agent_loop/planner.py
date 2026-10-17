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
errors goes back to the same session, at most `RETRIES` times. A critic reviews the plan a valid
answer leaves; blocking findings go back to the same session once, and its fixed answer is taken
without another critic. The plan the replan leaves goes to `plan.json` with the findings in
`critique.json` and `PLAN.md`. A replanning stopped by Ctrl+C or a dead orchestrator goes on with
`loop resume`. A replan that changes the acceptance criteria of the plan waits for the
operator after its critic: `loop approve` takes it (`approve_criteria`), an objection in
`QUESTIONS.md` goes back to the same session (`object_to_criteria`), and a critic reviews its
new answer.

The run stays in planning while the planner works: a stopped planner does not pause it, and
`go_on` picks the work up from the last step of the planner. A paused session is resumed or
replaced as `step.how_to_resume` decides, an answer the loop has not taken yet is taken.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, replace
from pathlib import Path

from agent_loop import (
    agent,
    budget,
    export,
    git,
    guard,
    implement,
    log,
    plan,
    prompts,
    replies,
    step,
)
from agent_loop.config import Config
from agent_loop.store import Run, Step, Store, Task

logger = logging.getLogger(__name__)

PLAN = "plan"
TALK = "talk"
CRITIQUE = "critique"
REPLAN = "replan"
QUESTIONS = "questions"
# Steps without a session: the operator objects to the change of the criteria of the plan a
# replan makes, or approves it.
OBJECTION = "objection"
APPROVAL = "approval"
# The plan still has errors after the retries: the run waits for the operator.
REJECTED = "rejected"
# The replan changes the acceptance criteria of the plan: the run waits for the operator.
CRITERIA = "criteria"
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
        "criteria": plan.STRINGS,
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
    "required": ["why", "criteria", "tasks", "remaining"],
    "additionalProperties": False,
}


class PlanError(Exception):
    pass


@dataclass(frozen=True)
class _Session:
    prompt: str
    # The session to go on with --resume; None starts a new one.
    session_id: str | None = None
    # PLAN for a session of the planner, CRITIQUE for one of the critic of a plan or of a
    # replan, REPLAN for one that replans a running run.
    kind: str = PLAN


@dataclass(frozen=True)
class _Replan:
    """A task that has gone back to the planner and what its replanning goes by."""

    task: Task
    # The tasks the run is through with, all before the task.
    done: list[Task]
    # The pending tasks after it: the replan gives each of them an action.
    left: list[Task]
    approved: dict
    # The session that replans the task anew.
    fresh: _Session
    # The last step of the task: the steps of its replanning come after it.
    since: int


def go_on(cfg: Config, store: Store, run: Run) -> str | None:
    """Plan the request of the planning `run`, from where its planner has stopped if it has.

    Returns why the run waits: `QUESTIONS` for the replies of the operator, `REJECTED` or
    `step.INTERRUPT`; None when the plan is in `plan.PLAN_FILE` of the run directory.
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


def replan(cfg: Config, store: Store, run: Run, task: Task, previous: str) -> step.Stop | None:
    """Replan the tasks left in the running `run` after `task` has gone back to the planner.

    The work of the task goes to its WIP ref first (`implement.set_aside`). `previous` is what
    `prompts.previous_tasks` tells about the tasks before it. A session of the replanning or of
    its critic the run has left paused goes on as `step.how_to_resume` decides, an answer it has
    left is taken. Returns why the run stops: the limits leave no room (`step.BUDGET`), the
    operator has stopped it (`step.INTERRUPT`), the answer keeps its errors (`REJECTED`) or
    changes the acceptance criteria of the plan (`CRITERIA`); None when the store has the new
    tasks and `plan.json` the plan they make.
    """
    return _Planner(cfg, store, run).replan(task, previous)


def criteria_question(cfg: Config, store: Store, run: Run) -> str:
    """What the operator replies to when the replan of `run` changes the acceptance criteria of
    the plan: the approved criteria, the new ones and why the replanner changes them."""
    task, answered = _Planner(cfg, store, run).waiting()
    output = answered.detail["answer"]
    approved = plan.load(step.run_dir(cfg.home, run.id))["criteria"]
    return "\n\n".join(
        [
            f"The replan of task {task.seq}, {task.title!r}, changes the acceptance criteria of"
            " the plan.",
            "Approved criteria:\n" + "\n".join(f"- {criterion}" for criterion in approved),
            "New criteria:\n" + "\n".join(f"- {criterion}" for criterion in output["criteria"]),
            f"Why: {output['why']}",
            f"`loop approve {run.id}` takes the new criteria. To object, write the objection"
            " below: it goes to the replanner.",
        ]
    )


def object_to_criteria(cfg: Config, store: Store, run: Run, question: str, reply: str) -> None:
    """Record the objection `reply` of the operator to the change of the criteria of the plan
    the replan of `run` makes: the session that has made it goes on with it."""
    planner = _Planner(cfg, store, run)
    _, answered = planner.waiting()
    planner.record(OBJECTION, {"question": question, "reply": reply}, answered.session_id)


def approve_criteria(cfg: Config, store: Store, run: Run) -> None:
    """Record the approval by the operator of the change of the criteria of the plan the replan
    of `run` makes: the run takes the replan."""
    planner = _Planner(cfg, store, run)
    _, answered = planner.waiting()
    planner.record(APPROVAL, {"criteria": answered.detail["answer"]["criteria"]})


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

    def replan(self, task: Task, previous: str) -> step.Stop | None:
        tasks = self.store.tasks(self.run.id)
        left = [t for t in tasks if t.seq > task.seq and t.status == "pending"]
        done = [t for t in tasks if t.status in ("committed", "done")]
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
        approved = plan.load(self.run_dir)
        prompt = prompts.replanner(
            approved, task, left, previous, self.cfg.settings, said, verdict, wip
        )
        r = _Replan(task, done, left, approved, _Session(prompt, kind=REPLAN), steps[-1].id)
        upcoming = self._replan_upcoming(r)
        while isinstance(upcoming, _Session):
            resume_at = budget.no_room(self.cfg.home, self.cfg.settings)
            if resume_at is not None:
                return step.Stop(step.BUDGET, resume_at)
            outcome = self._session(upcoming)
            who = "critic of the replan" if upcoming.kind == CRITIQUE else "replanning session"
            if outcome.status == "failed":
                raise PlanError(f"the {who} has failed: {outcome.reason}")
            if outcome.status == "paused" and outcome.reason == step.INTERRUPT:
                return step.Stop(step.INTERRUPT)
            steps = self._replans(r.since)
            if outcome.status == "paused":
                upcoming = self._replan_resumed(r, steps)
                continue
            if steps[-1].detail["answer"] is None:
                raise PlanError(f"the {who} has ended without an answer")
            upcoming = self._take_replan(r, _chain(steps))
            if isinstance(upcoming, step.Stop):
                return upcoming
            if outcome.reason == step.INTERRUPT:
                # An answer the loop has not taken yet is taken by the next `loop resume`.
                return step.Stop(step.INTERRUPT)
        return upcoming

    def waiting(self) -> tuple[Task, Step]:
        """The task whose replan waits for the operator and the step with the answer of the
        replanner."""
        [task] = [t for t in self.store.tasks(self.run.id) if t.status == "replan"]
        steps = self._replans(self.store.steps(task.id)[-1].id)
        return task, _answered(_chain(steps), REPLAN)[0]

    def record(self, kind: str, detail: dict, session_id: str | None = None) -> None:
        """Record a step of the operator in the replanning: an objection or an approval."""
        step_id = self.store.start_step(
            self.run.id, None, kind, git.head(self.repo), [], session_id
        )
        self.store.finish_step(step_id, "completed", detail)

    def _replans(self, since: int) -> list[Step]:
        """The steps of the replanning sessions, of their critics and of the operator after the
        step `since`."""
        kinds = (REPLAN, CRITIQUE, OBJECTION, APPROVAL)
        return [
            s for s in self.store.planner_steps(self.run.id) if s.kind in kinds and s.id > since
        ]

    def _replan_upcoming(self, r: _Replan) -> _Session | step.Stop | None:
        """The session that goes on with the replanning the run has left, or why the run stops;
        None when the answer it has left is taken and the store has the new tasks."""
        steps = self._replans(r.since)
        if not steps:
            return r.fresh
        last = steps[-1]
        if last.kind == OBJECTION:
            objection = prompts.criteria_objection(last.detail["reply"])
            return _Session(objection, last.session_id, REPLAN)
        if last.outcome == "paused":
            return self._replan_resumed(r, steps)
        chain = _chain(steps)
        failed = last.outcome == "failed" or last.detail.get("answer") is None
        if failed and last.kind != APPROVAL:
            # The error is in the log of the run that has stopped on it: the step starts anew.
            if last.kind == CRITIQUE:
                return self._replan_critic(r, _answered(chain, REPLAN)[0].detail["answer"])
            return r.fresh
        upcoming = self._take_replan(r, chain)
        if isinstance(upcoming, step.Stop) and upcoming.reason == REJECTED:
            # An answer that keeps its errors has stopped the run, `loop resume` replans anew.
            return r.fresh
        return upcoming

    def _replan_resumed(self, r: _Replan, steps: list[Step]) -> _Session:
        """The session that goes on after the last of `steps` has paused."""
        kind = steps[-1].kind
        how = step.how_to_resume(
            [s for s in steps if s.kind == kind], self.cfg.settings.ctx_soft_tokens
        )
        critic = kind == CRITIQUE
        if how.mode == "session":
            resumed = prompts.CRITIC_RESUMED if critic else prompts.PLANNER_RESUMED
            return _Session(resumed, how.session_id, kind)
        if how.mode == "handoff" and critic:
            logger.warning("the context of the critic is full, a new critic reviews the replan")
            return self._replan_critic(r, _answered(_chain(steps), REPLAN)[0].detail["answer"])
        if how.mode == "handoff":
            # The replanner writes no handoff: a new session reads the repository again.
            logger.warning("the context of the replanner is full, replanning in a new session")
            return r.fresh
        who = "critic of the replan" if critic else "replanner"
        raise PlanError(f"the {who} keeps stopping ({how.reason})")

    def _take_replan(self, r: _Replan, chain: list[Step]) -> _Session | step.Stop | None:
        """Take the answer of the last step of `chain`, or the one the operator has approved
        after it: returns the session that fixes or reviews the replan, why the run stops, or
        None when the store has the new tasks."""
        approved = chain[-1].kind == APPROVAL
        last = chain[-2] if approved else chain[-1]
        if last.kind == CRITIQUE:
            findings = _findings(last.detail["answer"])
            blocking = [f["problem"] for f in findings if f["blocking"]]
            reviewed = _answered(chain, REPLAN)[0]
            if blocking:
                logger.warning(
                    "the critic has %d blocking findings, the replanner fixes the replan:\n%s",
                    len(blocking),
                    "\n".join(f"- {problem}" for problem in blocking),
                )
                return _Session(prompts.replan_critique(findings), reviewed.session_id, REPLAN)
            return self._settle(r, reviewed.detail["answer"], findings, False, approved)
        output = last.detail["answer"]
        errors = _replan_errors(r, output)
        if errors:
            logger.warning("the replan has errors:\n%s", "\n".join(errors))
            replans = _answered(chain, REPLAN)
            if sum(bool(_replan_errors(r, s.detail["answer"])) for s in replans) > RETRIES:
                logger.error(
                    "the replan still has errors after %d retries, `loop resume` replans anew",
                    RETRIES,
                )
                return step.Stop(REJECTED)
            return _Session(prompts.plan_errors(errors), last.session_id, REPLAN)
        critiques = _answered(chain, CRITIQUE)
        if not critiques:
            logger.info("a critic reviews the replan of task %d", r.task.seq)
            return self._replan_critic(r, output)
        return self._settle(r, output, _findings(critiques[0].detail["answer"]), True, approved)

    def _settle(
        self, r: _Replan, output: dict, findings: list[dict], fixed: bool, approved: bool
    ) -> step.Stop | None:
        """Apply the replan of `output`, unless it changes the acceptance criteria of the plan
        and the operator has not `approved` the change yet: then returns why the run stops."""
        if _changes_criteria(r, output) and not approved:
            logger.warning(
                "the replan of task %d changes the acceptance criteria of the plan, the operator"
                " approves the change:\n%s",
                r.task.seq,
                "\n".join(f"- {criterion}" for criterion in output["criteria"]),
            )
            return step.Stop(CRITERIA)
        self._apply(r, output, findings, fixed)
        return None

    def _replan_critic(self, r: _Replan, output: dict) -> _Session:
        """A new critic of the plan the replan of `output` leaves."""
        prompt = prompts.replan_critic(
            _replanned(r, output),
            self._request(),
            self.cfg.settings,
            len(r.done),
            r.task,
            r.left,
            output,
            r.approved["criteria"] if _changes_criteria(r, output) else None,
        )
        return _Session(prompt, kind=CRITIQUE)

    def _apply(self, r: _Replan, output: dict, findings: list[dict], fixed: bool) -> None:
        """Put the replan of `output` into the store, and the plan it leaves with the `findings`
        of its critic into `plan.json`, `critique.json` and `PLAN.md`; the replanner has `fixed`
        its answer after them."""
        replanned = _replanned(r, output)
        plan.save(self.run_dir, {"findings": findings}, CRITIQUE_FILE)
        plan.save(self.run_dir, replanned)
        by_seq = {t.seq: t for t in r.left}
        entries = output["remaining"]
        self.store.replan(
            r.task.id,
            [plan.card(new) for new in output["tasks"]],
            {
                by_seq[e["task"]].id: plan.card(e["card"])
                for e in entries
                if e["action"] == "modify"
            },
            [by_seq[e["task"]].id for e in entries if e["action"] == "drop"],
            plan.digest(self.run_dir),
        )
        logger.info(
            "task %d is replaced with %d new tasks: %s%s",
            r.task.seq,
            len(output["tasks"]),
            output["why"],
            "".join(f"\n- task {e['task']}: {e['action']}, {e['why']}" for e in entries),
        )
        paths = export.write_plan(
            self.run_dir, self.run, replanned, findings, self.cfg.settings, fixed
        )
        if fixed:
            logger.warning(
                "the replanner has fixed the replan after the findings of the critic, no critic"
                " has seen the fix"
            )
        logger.info(
            "the plan of %d tasks is in %s", len(replanned["tasks"]), ", ".join(map(str, paths))
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
                if outcome.reason == step.INTERRUPT:
                    return self._interrupted()
                upcoming = self._resumed(steps)
                continue
            if steps[-1].detail["answer"] is None:
                raise PlanError("the planner session has ended without an answer")
            upcoming = self._take(steps)
            if outcome.reason == step.INTERRUPT and isinstance(upcoming, _Session):
                return self._interrupted()
        return upcoming

    def _new(self) -> _Session:
        request = (self.run_dir / REQUEST_FILE).read_text(encoding="utf-8")
        return _Session(prompts.planner(request, self.cfg.settings))

    def _critic(self, steps: list[Step]) -> _Session:
        """A new critic of the last plan of `steps`."""
        output = _plan_step(steps).detail["answer"]
        prompt = prompts.plan_critic(output["plan"], self._request(), self.cfg.settings)
        return _Session(prompt, kind=CRITIQUE)

    def _request(self) -> str | None:
        """The request of the operator; None when it was given in a conversation."""
        if self.conversation:
            return None
        return (self.run_dir / REQUEST_FILE).read_text(encoding="utf-8")

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
        findings = _findings(steps[-1].detail["answer"])
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
        return step.INTERRUPT


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


def _chain(steps: list[Step]) -> list[Step]:
    """The steps of `steps` from the last session that has replanned the task anew or from the
    last objection of the operator: the retries of the answer and the critic of it start there.
    A resumed session keeps its id."""
    seen = set()
    start = 0
    for i, s in enumerate(steps):
        if s.kind == OBJECTION or s.kind == REPLAN and s.session_id not in seen:
            start = i
        if s.kind == REPLAN:
            seen.add(s.session_id)
    return steps[start:]


def _changes_criteria(r: _Replan, output: dict) -> bool:
    # Spaces around a criterion the replanner has copied are not worth the operator's time.
    new = [criterion.strip() for criterion in output["criteria"]]
    return new != [criterion.strip() for criterion in r.approved["criteria"]]


def _replanned(r: _Replan, output: dict) -> dict:
    """The approved plan with the criteria and the tasks the replan of `output` leaves: the
    tasks the run is through with, the new tasks, then the tasks left as it keeps or modifies
    them."""
    actions = {entry["task"]: entry for entry in output["remaining"]}
    kept = [
        {"title": t.title, **t.card}
        if actions[t.seq]["action"] == "keep"
        else actions[t.seq]["card"]
        for t in r.left
        if actions[t.seq]["action"] != "drop"
    ]
    done = [{"title": t.title, **t.card} for t in r.done]
    return {
        "goal": r.approved["goal"],
        "criteria": output["criteria"],
        "decisions": r.approved["decisions"],
        "tasks": [*done, *output["tasks"], *kept],
    }


def _replan_errors(r: _Replan, output: dict) -> list[str]:
    return plan.validate_replan(output, {t.seq: t.title for t in r.left}, [t.title for t in r.done])


def _findings(critique: dict) -> list[dict]:
    """The findings of the answer of a critic: one without a scenario does not block."""
    return [
        finding | {"blocking": finding["blocking"] and bool(finding["scenario"].strip())}
        for finding in critique["findings"]
    ]


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
