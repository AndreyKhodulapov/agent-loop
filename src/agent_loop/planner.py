"""The loop of the planner: its sessions turn the request of the operator into a plan.

The request comes from a file or from a conversation of the operator with the planner (`talk`),
after which the loop asks the same session for the plan. The planner answers with a plan or with
questions. A plan that fails `plan.validate` goes back to the same session with its errors, at
most `RETRIES` times after the request or the last conversation; after that the run waits for the
operator with the draft in the run directory. Questions on a request from a file go to
`QUESTIONS.md` in the run directory, and `answer` gives the replies of the operator to the same
session; questions and errors of a conversation are talked over when `talk` reopens it.

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
from agent_loop.store import Run, Step, Store

logger = logging.getLogger(__name__)

PLAN = "plan"
TALK = "talk"
QUESTIONS = "questions"
# The plan still has errors after the retries: the run waits for the operator.
REJECTED = "rejected"
REQUEST_FILE = "request.md"
DRAFT_FILE = "plan-draft.json"
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


class PlanError(Exception):
    pass


@dataclass(frozen=True)
class _Session:
    prompt: str
    # The session to go on with --resume; None starts a new one.
    session_id: str | None = None


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
            # The error is in the log of the last `loop plan`: the operator plans again.
            return self._loop(self._new())
        return self._loop(self._take(steps))

    def talk(self) -> str | None:
        steps = self.store.planner_steps(self.run.id)
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
        output = steps[-1].detail.get("answer") if steps else None
        if output is None or output["status"] != QUESTIONS or _errors(output):
            raise PlanError(f"the planner of run {self.run.id} has no questions to answer")
        questions = output["questions"]
        answers = replies.read(self.run_dir, self.run.id, questions)
        logger.info("the planner goes on with the answers to %d questions", len(questions))
        return self._loop(
            _Session(prompts.planner_answers(questions, answers), steps[-1].session_id)
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

    def _resumed(self, steps: list[Step]) -> _Session:
        how = step.how_to_resume(steps, self.cfg.settings.ctx_soft_tokens)
        if how.mode == "session":
            return _Session(prompts.PLANNER_RESUMED, how.session_id)
        if how.mode == "handoff":
            if self.conversation:
                raise PlanError(
                    "the conversation has filled the context of the planner: `/compact` it in"
                    " `loop plan`, or start anew with `loop plan --new`"
                )
            # The planner writes no handoff: a new session reads the repository again.
            logger.warning("the context of the planner is full, planning anew in a new session")
            return self._new()
        raise PlanError(f"the planner keeps stopping ({how.reason}), split the request")

    def _take(self, steps: list[Step]) -> _Session | str | None:
        """Take the answer of the last step: returns the session that fixes the plan, why the
        run waits, or None when the plan is saved."""
        last = steps[-1]
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
            plan.save(self.run_dir, output["plan"])
            logger.info(
                "the plan of %d tasks is in %s",
                len(output["plan"]["tasks"]),
                self.run_dir / plan.PLAN_FILE,
            )
            return None
        logger.warning("the plan has errors:\n%s", "\n".join(errors))
        rejected = 0
        # The retries start anew after a conversation: the operator may have fixed the plan in it.
        for s in reversed(steps):
            if s.kind == TALK:
                break
            if s.outcome == "completed" and s.detail["answer"] is not None:
                rejected += bool(_errors(s.detail["answer"]))
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

    def _session(self, upcoming: _Session) -> step.Outcome:
        session = self._agent_session(upcoming, ANSWER_SCHEMA)
        # The planner changes no files: nothing to roll back or to tell from its work.
        step_id = self.store.start_step(
            self.run.id, None, PLAN, git.head(self.repo), [], session.session_id
        )
        with log.scope(stage=PLAN):
            return step.run(session, self.cfg, self.store, self.run.id, step_id)

    def _agent_session(self, upcoming: _Session, schema: dict | None) -> agent.Session:
        settings = self.cfg.settings
        session = agent.Session(
            prompt=upcoming.prompt,
            model=settings.model_planner,
            cwd=self.repo,
            settings=guard.prepare(
                self.run_dir, self.cfg.home, settings, self.repo, read_only=True
            ),
            effort=settings.effort_planner,
            system_files=prompts.system_files("planner", settings),
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
