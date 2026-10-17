import json
import time
from dataclasses import dataclass

import pytest
from test_plan import PLAN

from agent_loop import agent, budget, git, guard, implement, planner, prompts, protected, step
from agent_loop.config import Config, Settings
from agent_loop.store import Store

SETTINGS = Settings()
REQUEST = "Add the admin of dishes"
QUESTIONS = ["Who may remove a dish?", "Are prices\nin cents?"]


def answered(output, reason=None):
    result = agent.Result("s", False, "completed", "", output, 3, 0.1, [])
    return step.Outcome("completed", reason, result, 1000)


def planned(**change):
    return answered({"status": "plan", "plan": PLAN | change})


def asked(questions=QUESTIONS):
    return answered({"status": "questions", "questions": questions})


def paused(reason, context_tokens=1000):
    return step.Outcome("paused", reason, None, context_tokens)


@dataclass
class Env:
    cfg: Config
    store: Store
    run_id: str
    run_dir: object
    sessions: list

    def go_on(self):
        return planner.go_on(self.cfg, self.store, self.store.run(self.run_id))

    def answer(self):
        return planner.answer(self.cfg, self.store, self.store.run(self.run_id))

    def script(self, monkeypatch, *outcomes):
        """Replace the planner sessions: each ends with its outcome, recorded like `step.run`
        does."""
        scripted = iter(outcomes)

        def run(session, cfg, store, run_id, step_id):
            outcome = next(scripted)
            self.sessions.append(session)
            output = outcome.result and outcome.result.structured_output
            detail = {"reason": outcome.reason, "context_tokens": outcome.context_tokens}
            store.finish_step(step_id, outcome.status, detail | {"answer": output})
            return outcome

        monkeypatch.setattr(step, "run", run)

    def questions_file(self):
        return self.run_dir / planner.QUESTIONS_FILE


@pytest.fixture
def env(repo, tmp_path):
    home = tmp_path / "home"
    db = Store(home / "loop.db")
    run_id = db.create_run(repo, "main", git.head(repo))
    run_dir = step.run_dir(home, run_id)
    protected.save(repo, run_dir)
    (run_dir / planner.REQUEST_FILE).write_text(REQUEST)
    yield Env(Config(home, repo, None, SETTINGS, {}), db, run_id, run_dir, [])
    db.close()


def test_plan_of_a_read_only_session_is_saved(env, monkeypatch):
    env.script(monkeypatch, planned())

    assert env.go_on() is None

    assert json.loads((env.run_dir / "plan.json").read_text()) == PLAN
    [session] = env.sessions
    assert session.model == SETTINGS.model_planner
    assert session.effort == SETTINGS.effort_planner
    assert session.tools == "Bash,Read"
    assert session.schema == planner.ANSWER_SCHEMA
    assert session.system_files == prompts.system_files("planner", SETTINGS)
    assert REQUEST in session.prompt
    assert not session.resume
    permissions = json.loads(session.settings.read_text())["permissions"]
    assert guard.WRITE_RULE in permissions["deny"]
    assert guard.WRITE_RULE not in permissions["allow"]
    [plan_step] = env.store.planner_steps(env.run_id)
    assert (plan_step.kind, plan_step.task_id, plan_step.session_id) == (
        "plan",
        None,
        session.session_id,
    )
    assert env.store.run(env.run_id).status == "planning"


def test_plan_with_errors_goes_back_to_its_session(env, monkeypatch):
    env.script(monkeypatch, planned(tasks=[]), answered({"status": "plan"}), planned())

    assert env.go_on() is None

    first, *retries = env.sessions
    assert [s.session_id for s in retries] == [first.session_id] * 2
    assert all(s.resume for s in retries)
    assert "- the plan has no tasks" in retries[0].prompt
    assert "- the answer has the status plan without the plan" in retries[1].prompt
    assert (env.run_dir / "plan.json").is_file()


def test_plan_that_keeps_its_errors_waits_for_the_operator(env, monkeypatch):
    env.script(monkeypatch, *[planned(tasks=[])] * (planner.RETRIES + 1))

    assert env.go_on() == planner.REJECTED

    assert len(env.sessions) == planner.RETRIES + 1
    draft = json.loads((env.run_dir / planner.DRAFT_FILE).read_text())
    assert draft["tasks"] == []
    assert not (env.run_dir / "plan.json").exists()
    # Planning the same request again finds it where it has stopped.
    assert env.go_on() == planner.REJECTED
    assert len(env.sessions) == planner.RETRIES + 1


def test_questions_go_to_the_operator_and_the_replies_to_the_session(env, monkeypatch):
    env.script(monkeypatch, asked())

    assert env.go_on() == planner.QUESTIONS

    text = env.questions_file().read_text()
    assert "## Question 2\n\nAre prices\nin cents?\n\n### Answer\n" in text
    env.questions_file().write_text(text.replace("### Answer\n", "### Answer\n\nThe admin\n", 1))
    with pytest.raises(planner.PlanError, match="no answer to question 2"):
        env.answer()
    # The replies of the operator stay when the planning goes on before the answer.
    assert env.go_on() == planner.QUESTIONS
    assert "The admin" in env.questions_file().read_text()

    env.questions_file().write_text(env.questions_file().read_text() + "Yes\n")
    env.script(monkeypatch, planned())

    assert env.answer() is None

    asking, answering = env.sessions
    assert (answering.session_id, answering.resume) == (asking.session_id, True)
    assert answering.prompt == prompts.planner_answers(QUESTIONS, ["The admin", "Yes"])
    with pytest.raises(planner.PlanError, match="has no questions to answer"):
        env.answer()


def test_questions_file_that_lost_its_questions_is_written_anew(env, monkeypatch):
    env.script(monkeypatch, asked())
    env.go_on()
    env.questions_file().write_text("# my notes\n")

    with pytest.raises(planner.PlanError, match="written anew"):
        env.answer()

    assert "## Question 1\n\nWho may remove a dish?" in env.questions_file().read_text()


@pytest.mark.parametrize(
    ("pause", "resumed"),
    [(paused("timeout"), True), (paused("context", 125_000), False)],
    ids=["timeout", "full context"],
)
def test_paused_planner_goes_on(env, monkeypatch, pause, resumed):
    env.script(monkeypatch, pause, planned())

    assert env.go_on() is None

    first, second = env.sessions
    assert (second.session_id == first.session_id) == second.resume == resumed
    assert second.prompt == (prompts.PLANNER_RESUMED if resumed else first.prompt)


@pytest.mark.parametrize(
    "stop",
    [paused("interrupt"), answered({"status": "plan"}, "interrupt")],
    ids=["in the session", "after an answer to fix"],
)
def test_operator_stop_leaves_the_planner_to_the_next_loop_plan(env, monkeypatch, stop):
    env.script(monkeypatch, stop)

    assert env.go_on() == implement.INTERRUPT

    env.script(monkeypatch, planned())
    assert env.go_on() is None
    first, second = env.sessions
    assert (second.session_id, second.resume) == (first.session_id, True)


def test_failed_planner_session_plans_anew_next_time(env, monkeypatch):
    env.script(monkeypatch, step.Outcome("failed", "API error", None, 0))

    with pytest.raises(planner.PlanError, match="has failed: API error"):
        env.go_on()

    env.script(monkeypatch, planned())
    assert env.go_on() is None
    first, second = env.sessions
    assert second.session_id != first.session_id
    assert not second.resume


def test_planner_waits_for_room_in_the_limits(env, monkeypatch):
    resume_at = int(time.time()) + 3600
    monkeypatch.setattr(budget, "no_room", lambda home, settings: resume_at)
    waits = []
    monkeypatch.setattr(time, "sleep", waits.append)
    env.script(monkeypatch, planned())

    assert env.go_on() is None

    [wait] = waits
    assert 3590 < wait <= 3600
