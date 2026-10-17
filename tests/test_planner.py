import json
import time
from dataclasses import dataclass, replace
from pathlib import Path

import pytest
from test_plan import PLAN, TASK

from agent_loop import (
    agent,
    budget,
    export,
    git,
    guard,
    implement,
    plan,
    planner,
    prompts,
    protected,
    replies,
    step,
)
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


def criticized(*findings):
    return answered({"findings": list(findings)})


def finding(blocking=True, scenario="The router fails without the schema"):
    return {"blocking": blocking, "tasks": [2], "problem": "No schema", "scenario": scenario}


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

    def talk(self):
        return planner.talk(self.cfg, self.store, self.store.run(self.run_id))

    def script_talks(self, monkeypatch, *codes):
        """Replace the conversations of the operator: each ends with its exit code."""
        scripted = iter(codes)

        def talk(session, system_file):
            self.sessions.append(session)
            return next(scripted)

        monkeypatch.setattr(agent, "talk", talk)

    def questions_file(self):
        return self.run_dir / replies.FILE


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
    env.script(monkeypatch, planned(), criticized())

    assert env.go_on() is None

    assert json.loads((env.run_dir / "plan.json").read_text()) == PLAN
    session, _ = env.sessions
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
    plan_step, _ = env.store.planner_steps(env.run_id)
    assert (plan_step.kind, plan_step.task_id, plan_step.session_id) == (
        "plan",
        None,
        session.session_id,
    )
    assert env.store.run(env.run_id).status == "planning"


def test_plan_with_errors_goes_back_to_its_session(env, monkeypatch):
    env.script(
        monkeypatch, planned(tasks=[]), answered({"status": "plan"}), planned(), criticized()
    )

    assert env.go_on() is None

    first, *retries, _ = env.sessions
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
    with pytest.raises(replies.RepliesError, match="no answer to question 2"):
        env.answer()
    # The replies of the operator stay when the planning goes on before the answer.
    assert env.go_on() == planner.QUESTIONS
    assert "The admin" in env.questions_file().read_text()

    env.questions_file().write_text(env.questions_file().read_text() + "Yes\n")
    env.script(monkeypatch, planned(), criticized())

    assert env.answer() is None

    asking, answering, _ = env.sessions
    assert (answering.session_id, answering.resume) == (asking.session_id, True)
    assert answering.prompt == prompts.planner_answers(QUESTIONS, ["The admin", "Yes"])
    with pytest.raises(planner.PlanError, match="has no questions to answer"):
        env.answer()


def test_questions_file_that_lost_its_questions_is_written_anew(env, monkeypatch):
    env.script(monkeypatch, asked())
    env.go_on()
    env.questions_file().write_text("# my notes\n")

    with pytest.raises(replies.RepliesError, match="written anew"):
        env.answer()

    assert "## Question 1\n\nWho may remove a dish?" in env.questions_file().read_text()


@pytest.mark.parametrize(
    ("pause", "resumed"),
    [(paused("timeout"), True), (paused("context", 125_000), False)],
    ids=["timeout", "full context"],
)
def test_paused_planner_goes_on(env, monkeypatch, pause, resumed):
    env.script(monkeypatch, pause, planned(), criticized())

    assert env.go_on() is None

    first, second, _ = env.sessions
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

    env.script(monkeypatch, planned(), criticized())
    assert env.go_on() is None
    first, second, _ = env.sessions
    assert (second.session_id, second.resume) == (first.session_id, True)


def test_failed_planner_session_plans_anew_next_time(env, monkeypatch):
    env.script(monkeypatch, step.Outcome("failed", "API error", None, 0))

    with pytest.raises(planner.PlanError, match="has failed: API error"):
        env.go_on()

    env.script(monkeypatch, planned(), criticized())
    assert env.go_on() is None
    first, second, _ = env.sessions
    assert second.session_id != first.session_id
    assert not second.resume


def test_planner_waits_for_room_in_the_limits(env, monkeypatch):
    resume_at = int(time.time()) + 3600
    monkeypatch.setattr(budget, "no_room", lambda home, settings: resume_at)
    waits = []
    monkeypatch.setattr(time, "sleep", waits.append)
    env.script(monkeypatch, planned(), criticized())

    assert env.go_on() is None

    # Before the planner and before the critic.
    assert len(waits) == 2
    assert all(3590 < wait <= 3600 for wait in waits)


@pytest.mark.parametrize(
    "findings",
    [[], [finding(blocking=False)], [finding(scenario=" ")]],
    ids=["none", "not blocking", "blocking without a scenario"],
)
def test_critic_reviews_the_plan_in_a_new_read_only_session(env, monkeypatch, findings):
    env.script(monkeypatch, planned(), criticized(*findings))

    assert env.go_on() is None

    planning, critic = env.sessions
    assert critic.model == SETTINGS.model_critic
    assert critic.effort == SETTINGS.effort_critic
    assert critic.tools == "Bash,Read"
    assert critic.schema == planner.CRITIQUE_SCHEMA
    assert critic.system_files == prompts.system_files("plan_critic", SETTINGS)
    assert critic.prompt == prompts.plan_critic(PLAN, REQUEST, SETTINGS)
    assert critic.session_id != planning.session_id
    assert not critic.resume
    assert guard.WRITE_RULE in json.loads(critic.settings.read_text())["permissions"]["deny"]
    _, critique_step = env.store.planner_steps(env.run_id)
    assert (critique_step.kind, critique_step.session_id) == (planner.CRITIQUE, critic.session_id)
    critique = json.loads((env.run_dir / planner.CRITIQUE_FILE).read_text())
    assert critique == {"findings": [f | {"blocking": False} for f in findings]}
    assert (env.run_dir / "plan.json").is_file()


def test_blocking_findings_go_back_to_the_planner_for_the_rounds_of_the_critic(
    env, monkeypatch, caplog
):
    fixed = PLAN | {"decisions": ["The schema comes first"]}
    blocking = finding()
    env.script(
        monkeypatch,
        planned(),
        criticized(blocking, finding(blocking=False)),
        planned(**fixed),
        criticized(blocking),
    )

    assert env.go_on() is None

    planning, first, fixing, second = env.sessions
    assert (fixing.session_id, fixing.resume) == (planning.session_id, True)
    assert fixing.prompt == prompts.plan_critique([blocking, finding(blocking=False)])
    assert fixing.schema == planner.ANSWER_SCHEMA
    assert second.session_id not in (first.session_id, planning.session_id)
    assert second.prompt == prompts.plan_critic(fixed, REQUEST, SETTINGS)
    assert SETTINGS.plan_critic_rounds == 2
    # The findings of the last round go to the operator with the plan.
    assert json.loads((env.run_dir / "plan.json").read_text()) == fixed
    critique = json.loads((env.run_dir / planner.CRITIQUE_FILE).read_text())
    assert critique == {"findings": [blocking]}
    assert "left 1 blocking findings to the operator" in caplog.text


@pytest.mark.parametrize(
    ("pause", "resumed"),
    [(paused("timeout"), True), (paused("context", 125_000), False)],
    ids=["timeout", "full context"],
)
def test_paused_critic_goes_on(env, monkeypatch, pause, resumed):
    env.script(monkeypatch, planned(), pause, criticized())

    assert env.go_on() is None

    _, first, second = env.sessions
    assert (second.session_id == first.session_id) == second.resume == resumed
    assert second.prompt == (prompts.CRITIC_RESUMED if resumed else first.prompt)
    assert second.schema == planner.CRITIQUE_SCHEMA


def test_failed_critic_reviews_the_same_plan_anew_next_time(env, monkeypatch):
    env.script(monkeypatch, planned(), step.Outcome("failed", "API error", None, 0))

    with pytest.raises(planner.PlanError, match="has failed: API error"):
        env.go_on()
    assert not (env.run_dir / "plan.json").exists()

    env.script(monkeypatch, criticized())
    assert env.go_on() is None
    _, failed, critic = env.sessions
    assert (critic.prompt, critic.resume) == (failed.prompt, False)
    assert critic.session_id != failed.session_id


@pytest.fixture
def conversation(env):
    (env.run_dir / planner.REQUEST_FILE).unlink()
    return env


def test_conversation_ends_with_the_plan_asked_in_it(conversation, monkeypatch):
    conversation.script_talks(monkeypatch, 0)
    conversation.script(monkeypatch, planned(), criticized())

    assert conversation.talk() is None

    talk, asking, critic = conversation.sessions
    assert talk.prompt == prompts.planner_talk(SETTINGS)
    assert (talk.schema, talk.resume, talk.tools) == (None, False, "Bash,Read")
    assert guard.WRITE_RULE in json.loads(talk.settings.read_text())["permissions"]["deny"]
    assert (asking.session_id, asking.resume) == (talk.session_id, True)
    assert (asking.prompt, asking.schema) == (prompts.PLANNER_TALKED, planner.ANSWER_SCHEMA)
    assert critic.prompt == prompts.plan_critic(PLAN, None, SETTINGS)
    steps = conversation.store.planner_steps(conversation.run_id)
    assert [s.kind for s in steps] == [planner.TALK, planner.PLAN, planner.CRITIQUE]
    assert (conversation.run_dir / "plan.json").is_file()


@pytest.mark.parametrize(
    ("first", "waits"),
    [
        ([asked()], planner.QUESTIONS),
        ([paused("interrupt")], implement.INTERRUPT),
        ([planned(tasks=[])] * (planner.RETRIES + 1), planner.REJECTED),
    ],
    ids=["questions", "interrupt", "rejected"],
)
def test_next_talk_reopens_the_conversation(conversation, monkeypatch, first, waits):
    conversation.script_talks(monkeypatch, 0, 0)
    conversation.script(monkeypatch, *first)

    assert conversation.talk() == waits

    assert not conversation.questions_file().exists()
    with pytest.raises(planner.PlanError, match="plans in a conversation"):
        conversation.answer()
    # The retries start anew after the conversation.
    conversation.script(monkeypatch, planned(tasks=[]), planned(), criticized())
    assert conversation.talk() is None
    opened, reopened = conversation.sessions[0], conversation.sessions[-4]
    assert (reopened.session_id, reopened.resume) == (opened.session_id, True)
    assert (reopened.prompt, reopened.schema) == ("", None)


def test_conversation_that_failed_to_start_is_opened_anew(conversation, monkeypatch):
    conversation.script_talks(monkeypatch, 1, 0)
    conversation.script(monkeypatch, planned(), criticized())

    with pytest.raises(planner.PlanError, match="exited with code 1"):
        conversation.talk()
    assert conversation.talk() is None

    failed, opened, _, _ = conversation.sessions
    assert opened.session_id != failed.session_id
    assert (opened.resume, opened.prompt) == (False, prompts.planner_talk(SETTINGS))


def test_full_context_of_a_conversation_goes_back_to_the_operator(conversation, monkeypatch):
    conversation.script_talks(monkeypatch, 0)
    conversation.script(monkeypatch, paused("context", 125_000))

    with pytest.raises(planner.PlanError, match="/compact"):
        conversation.talk()


@pytest.fixture
def replanning(env):
    """The run of `env` works on its plan: task 1 has found itself too big with work in the
    tree, task 2 is left."""
    plan.save(env.run_dir, PLAN)
    env.store.approve(env.run_id, "f00d", plan.cards(PLAN))
    repo = Path(env.store.run(env.run_id).repo)
    env.store.start(env.run_id, "main", git.head(repo))
    first, _ = env.store.tasks(env.run_id)
    env.store.move_task(first.id, "implementing")
    step_id = env.store.start_step(
        env.run_id, first.id, implement.IMPLEMENT, git.head(repo), [], "s1"
    )
    answer = {"status": "too_big", "summary": "Two modules", "commit_message": ""}
    answer |= {"notes_for_next": "", "split": ["Prices", "Schema"]}
    env.store.finish_step(step_id, "completed", {"reason": None, "answer": answer})
    env.store.move_task(first.id, "replan", "too_big")
    (repo / "app.py").write_text("x = 2\n")
    return env


def replan(env):
    run = env.store.run(env.run_id)
    task = next(t for t in env.store.tasks(env.run_id) if t.status == "replan")
    return planner.replan(env.cfg, env.store, run, task, "Nothing yet")


# The new task repeats the title of the task left.
REPEATED = [TASK | {"title": "Router of dishes"}]


def replanned(*remaining, tasks=(TASK | {"title": "Prices"},)):
    entries = list(remaining) or [{"task": 2, "action": "keep", "why": "Still needed"}]
    return answered({"why": "Prices first", "tasks": list(tasks), "remaining": entries})


def test_task_gone_back_is_replanned_in_a_read_only_session(replanning, monkeypatch):
    env = replanning
    modified = {"task": 2, "action": "modify", "why": "Prices", "card": TASK | {"title": "Rt"}}
    new = [TASK | {"title": t} for t in "AB"]
    env.script(monkeypatch, replanned(modified, tasks=new), criticized(finding(blocking=False)))

    assert replan(env) is None

    tasks = env.store.tasks(env.run_id)
    assert [(t.seq, t.title, t.status) for t in tasks] == [
        (1, "Schema of dishes", "dropped"),
        (2, "A", "pending"),
        (3, "B", "pending"),
        (4, "Rt", "pending"),
    ]
    session, critic = env.sessions
    assert session.schema == planner.REPLAN_SCHEMA
    assert session.system_files == prompts.system_files("replanner", SETTINGS)
    assert (session.model, session.tools) == (SETTINGS.model_planner, "Bash,Read")
    permissions = json.loads(session.settings.read_text())["permissions"]
    assert guard.WRITE_RULE in permissions["deny"]
    ref = f"refs/loop/wip/{env.run_id}/1"
    assert "Its split:\n- Prices\n- Schema" in session.prompt
    assert f"`git show {ref}`" in session.prompt
    assert " app.py | 2 +-" in session.prompt
    assert '"task": 2,\n    "title": "Router of dishes"' in session.prompt
    assert "## Done so far\n\nNothing yet" in session.prompt
    repo = Path(env.store.run(env.run_id).repo)
    assert git.git(repo, "status", "--porcelain") == ""
    assert git.git(repo, "show", f"{ref}:app.py") == "x = 2\n"
    replan_step, critique_step = env.store.planner_steps(env.run_id)
    assert (replan_step.kind, replan_step.task_id) == ("replan", None)
    assert (critique_step.kind, critique_step.session_id) == ("critique", critic.session_id)
    # The critic reviews the plan the replan leaves before the store takes it.
    replanned_plan = PLAN | {"tasks": [*new, TASK | {"title": "Rt"}]}
    assert json.dumps(replanned_plan, ensure_ascii=False, indent=2) in critic.prompt
    assert "The planner has replaced it with tasks 1 to 2: Prices first" in critic.prompt
    assert "- 'Router of dishes': modify, Prices" in critic.prompt
    assert f"```markdown\n{REQUEST}\n```" in critic.prompt
    assert (critic.model, critic.schema) == (SETTINGS.model_critic, planner.CRITIQUE_SCHEMA)
    assert critic.system_files == prompts.system_files("plan_critic", SETTINGS)
    assert guard.WRITE_RULE in json.loads(critic.settings.read_text())["permissions"]["deny"]
    assert plan.load(env.run_dir) == replanned_plan
    assert env.store.run(env.run_id).plan_hash == plan.digest(env.run_dir)
    critique = json.loads((env.run_dir / planner.CRITIQUE_FILE).read_text())
    assert critique == {"findings": [finding(blocking=False)]}
    plan_md = (env.run_dir / export.PLAN_MD).read_text()
    assert "### 3. Rt" in plan_md
    assert "### Not blocking\n\n- No schema (tasks 2)" in plan_md


def test_replan_with_errors_goes_back_to_its_session(replanning, monkeypatch):
    env = replanning
    env.script(monkeypatch, replanned(tasks=REPEATED), replanned(), criticized())

    assert replan(env) is None

    first, second, critic = env.sessions
    assert (second.session_id, second.resume) == (first.session_id, True)
    assert "- 2 tasks have the title 'Router of dishes'" in second.prompt
    assert critic.schema == planner.CRITIQUE_SCHEMA
    assert [t.title for t in env.store.tasks(env.run_id)][1:] == ["Prices", "Router of dishes"]


def test_replan_that_keeps_its_errors_stops_the_run(replanning, monkeypatch):
    env = replanning
    # The answer the critic has found blocking keeps its errors once fixed.
    rejected = [replanned(tasks=REPEATED)] * (planner.RETRIES + 1)
    env.script(monkeypatch, replanned(), criticized(finding()), *rejected)
    before = env.store.tasks(env.run_id)

    assert replan(env) == implement.Stop(planner.REJECTED)

    assert env.store.tasks(env.run_id) == before
    assert not (env.run_dir / planner.CRITIQUE_FILE).exists()

    env.script(monkeypatch, replanned(), criticized())
    assert replan(env) is None
    first, anew, critic = env.sessions[0], *env.sessions[-2:]
    assert (anew.prompt, anew.resume) == (first.prompt, False)
    # The answer of the new session gets a critic of its own.
    assert critic.schema == planner.CRITIQUE_SCHEMA


def test_blocking_findings_of_the_critic_go_back_to_the_replanner_once(
    replanning, monkeypatch, caplog
):
    env = replanning
    blocking = finding()
    fixed = [TASK | {"title": "Schema first"}]
    env.script(
        monkeypatch,
        replanned(),
        criticized(blocking, finding(blocking=False)),
        replanned(tasks=REPEATED),
        replanned(tasks=fixed),
    )

    assert replan(env) is None

    first, _, fixing, retried = env.sessions
    assert (fixing.session_id, fixing.resume) == (first.session_id, True)
    assert fixing.prompt == prompts.replan_critique([blocking, finding(blocking=False)])
    assert fixing.schema == planner.REPLAN_SCHEMA
    # The errors of the fixed answer go back as any others, no critic reviews it again.
    assert (retried.session_id, retried.schema) == (first.session_id, planner.REPLAN_SCHEMA)
    assert [t.title for t in env.store.tasks(env.run_id)][1:] == [
        "Schema first",
        "Router of dishes",
    ]
    critique = json.loads((env.run_dir / planner.CRITIQUE_FILE).read_text())
    assert critique == {"findings": [blocking, finding(blocking=False)]}
    plan_md = (env.run_dir / export.PLAN_MD).read_text()
    assert "## Findings of the critic\n\nThe replanner has fixed the plan" in plan_md
    assert "no critic has seen the fix" in caplog.text


@pytest.mark.parametrize(
    ("outcomes", "applied"),
    [
        ([paused(implement.INTERRUPT)], False),
        ([replace(replanned(), reason=implement.INTERRUPT)], False),
        ([replanned(), replace(criticized(), reason=implement.INTERRUPT)], True),
    ],
    ids=["in the session", "after the answer", "after the critic"],
)
def test_operator_stop_stops_the_run(replanning, monkeypatch, outcomes, applied):
    env = replanning
    env.script(monkeypatch, *outcomes)

    assert replan(env) == implement.Stop(implement.INTERRUPT)

    statuses = [t.status for t in env.store.tasks(env.run_id)]
    assert statuses[0] == ("dropped" if applied else "replan")


def test_replanning_stopped_by_the_operator_goes_on_in_its_session(replanning, monkeypatch):
    env = replanning
    env.script(monkeypatch, paused(implement.INTERRUPT))
    assert replan(env) == implement.Stop(implement.INTERRUPT)

    env.script(monkeypatch, replanned(), criticized())
    assert replan(env) is None

    first, second, _ = env.sessions
    assert (second.session_id, second.resume) == (first.session_id, True)
    assert (second.prompt, second.schema) == (prompts.PLANNER_RESUMED, planner.REPLAN_SCHEMA)


def test_answer_the_run_has_left_goes_to_the_critic(replanning, monkeypatch):
    env = replanning
    env.script(monkeypatch, replace(replanned(), reason=implement.INTERRUPT))
    assert replan(env) == implement.Stop(implement.INTERRUPT)

    env.script(monkeypatch, criticized())
    assert replan(env) is None

    _, critic = env.sessions
    assert critic.schema == planner.CRITIQUE_SCHEMA
    assert env.store.tasks(env.run_id)[0].status == "dropped"


def test_failed_critic_of_the_replan_reviews_it_anew_next_time(replanning, monkeypatch):
    env = replanning
    env.script(monkeypatch, replanned(), step.Outcome("failed", "API error", None, 0))
    with pytest.raises(planner.PlanError, match="critic of the replan has failed: API error"):
        replan(env)

    env.script(monkeypatch, criticized())
    assert replan(env) is None

    _, failed, critic = env.sessions
    assert (critic.prompt, critic.resume) == (failed.prompt, False)
    assert critic.session_id != failed.session_id


@pytest.mark.parametrize(
    ("pause", "resumed"),
    [(paused("timeout"), True), (paused("context", 125_000), False)],
    ids=["timeout", "full context"],
)
def test_paused_replanning_goes_on(replanning, monkeypatch, pause, resumed):
    env = replanning
    env.script(monkeypatch, pause, replanned(), criticized())

    assert replan(env) is None

    first, second, _ = env.sessions
    assert (second.session_id == first.session_id) == second.resume == resumed
    assert second.prompt == (prompts.PLANNER_RESUMED if resumed else first.prompt)
    assert second.schema == planner.REPLAN_SCHEMA


@pytest.mark.parametrize(
    ("pause", "resumed"),
    [(paused("timeout"), True), (paused("context", 125_000), False)],
    ids=["timeout", "full context"],
)
def test_paused_critic_of_the_replan_goes_on(replanning, monkeypatch, pause, resumed):
    env = replanning
    env.script(monkeypatch, replanned(), pause, criticized())

    assert replan(env) is None

    _, first, second = env.sessions
    assert (second.session_id == first.session_id) == second.resume == resumed
    assert second.prompt == (prompts.CRITIC_RESUMED if resumed else first.prompt)
    assert second.schema == planner.CRITIQUE_SCHEMA


def test_replanner_that_keeps_stopping_fails_the_run(replanning, monkeypatch):
    env = replanning
    # A stall of an earlier replan of the run is not one of this task.
    step_id = env.store.start_step(env.run_id, None, planner.REPLAN, "abc123", [], "s0")
    env.store.finish_step(step_id, "paused", {"reason": "timeout"})
    task = next(t for t in env.store.tasks(env.run_id) if t.status == "replan")
    [answered_step] = env.store.steps(task.id)
    later = env.store.start_step(env.run_id, task.id, implement.IMPLEMENT, "abc123", [], "s1")
    env.store.finish_step(later, "completed", answered_step.detail)
    env.script(monkeypatch, *[paused("timeout")] * 3)

    with pytest.raises(planner.PlanError, match=r"keeps stopping \(stall_pauses\)"):
        replan(env)

    assert len(env.sessions) == 3


def test_replanning_waits_for_room_in_the_limits(replanning, monkeypatch):
    env = replanning
    resume_at = int(time.time()) + 600
    monkeypatch.setattr(budget, "no_room", lambda home, settings: resume_at)
    env.script(monkeypatch)

    assert replan(env) == implement.Stop(implement.BUDGET, resume_at)

    assert env.sessions == []
