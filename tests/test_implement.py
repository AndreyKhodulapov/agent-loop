import subprocess
from dataclasses import dataclass

import pytest

from agent_loop import agent, git, implement, prompts, protected, step
from agent_loop.config import Config, Profile, Settings
from agent_loop.store import Store

SETTINGS = Settings(fix_attempts=2)
CARD = {
    "goal": "Make x fixed",
    "criteria": ["app.py says fixed"],
    "files": ["app.py"],
    "tests": [],
    "decisions": [],
}
# The work passes the gate once app.py says fixed.
PROFILE = Profile(
    commands={
        "collect": "echo 3 tests collected",
        "test": "grep -q fixed app.py || { echo x is not fixed; exit 1; }",
    },
    env={"PORT": "5433"},
)
TEST_FILE = "def test_x():\n    assert True\n"


def answered(status="done", reason=None, **fields):
    output = {"status": status, "summary": "did x", "commit_message": "feat(app): x"}
    output |= {"notes_for_next": ""} | fields
    return step.Outcome("completed", reason, result(output), 1000)


def result(structured_output):
    return agent.Result("s", False, "completed", "", structured_output, 3, 0.1, [])


def write(path, text):
    def action(repo):
        (repo / path).parent.mkdir(parents=True, exist_ok=True)
        (repo / path).write_text(text)

    return action


def commit(repo):
    subprocess.run(["git", "commit", "-q", "-am", "by hand"], cwd=repo, check=True)


@dataclass
class Env:
    cfg: Config
    store: Store
    run_id: str
    task_id: int
    repo: object
    sessions: list

    def start(self, profile=PROFILE, previous=""):
        run = self.store.run(self.run_id)
        [task] = self.store.tasks(self.run_id)
        return implement.start(self.cfg, self.store, run, task, 1, profile, previous)

    def task(self):
        [task] = self.store.tasks(self.run_id)
        return task

    def steps(self):
        return self.store.steps(self.task_id)

    def moves(self):
        return [(t.dst, t.reason) for t in self.store.transitions(self.run_id)]

    def script(self, monkeypatch, *sessions):
        """Replace the sessions of the task: each does its action on the repository and ends
        with its outcome, recorded in the store like `step.run` does."""
        scripted = iter(sessions)

        def run(session, cfg, store, run_id, step_id):
            action, outcome = next(scripted)
            if action is not None:
                action(session.cwd)
            self.sessions.append(session)
            store.finish_step(step_id, outcome.status, {"reason": outcome.reason})
            return outcome

        monkeypatch.setattr(step, "run", run)


@pytest.fixture
def env(repo, tmp_path):
    (repo / "tests").mkdir()
    (repo / "tests" / "test_app.py").write_text(TEST_FILE)
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "tests"], cwd=repo, check=True)
    home = tmp_path / "home"
    db = Store(home / "loop.db")
    run_id = db.create_run(repo, "main")
    task_id = db.add_task(run_id, "Fix x", CARD)
    protected.save(repo, step.run_dir(home, run_id))
    yield Env(Config(home, repo, None, SETTINGS, {}), db, run_id, task_id, repo, [])
    db.close()


def test_done_work_that_passes_the_gate_waits_for_the_commit(env, monkeypatch):
    env.script(monkeypatch, (write("app.py", "x = 'fixed'\n"), answered()))

    assert env.start(previous="M app.py") is None

    task = env.task()
    assert task.status == "gating"
    assert task.tests == 3
    assert env.moves() == [("implementing", None), ("gating", None)]
    implementer, gate = env.steps()
    assert implementer.kind == "implement"
    assert gate.kind == "gate"
    assert gate.outcome == "completed"
    assert gate.detail["status"] == "pass"
    assert gate.checkpoint == implementer.checkpoint == git.head(env.repo)
    [session] = env.sessions
    assert implementer.session_id == session.session_id
    assert session.model == SETTINGS.model_implementer
    assert session.effort == SETTINGS.effort_implementer
    assert session.system_files == prompts.system_files("implementer", SETTINGS)
    assert session.schema == prompts.ANSWER_SCHEMA
    assert session.env == {"PORT": "5433"}
    assert session.cwd == env.repo
    assert "Make x fixed" in session.prompt
    assert "M app.py" in session.prompt


def test_fixer_gets_the_findings_and_the_work(env, monkeypatch):
    env.script(
        monkeypatch,
        (write("new.py", "y = 1\n"), answered()),
        (write("app.py", "x = 'fixed'\n"), answered()),
    )

    env.start()

    assert env.task().status == "gating"
    assert env.moves() == [
        ("implementing", None),
        ("gating", None),
        ("fixing", "test_failed"),
        ("gating", None),
    ]
    assert [s.kind for s in env.steps()] == ["implement", "gate", "fix", "gate"]
    first_gate = env.steps()[1]
    assert first_gate.detail["status"] == "fixing"
    fixer = env.sessions[1]
    assert fixer.system_files == prompts.system_files("fixer", SETTINGS)
    assert "test_failed" in fixer.prompt
    assert "x is not fixed" in fixer.prompt
    # The file the implementer has added is in the work the fixer gets.
    assert "+y = 1" in fixer.prompt
    assert len({s.checkpoint for s in env.steps()}) == 1


def test_secrets_stay_out_of_the_work_of_the_fixer(env, monkeypatch):
    env.script(
        monkeypatch,
        (write("config/.env.local", "TOKEN=abcdefghijk\n"), answered()),
        (write("app.py", "x = 'fixed'\n"), answered()),
    )

    env.start()

    assert "abcdefghijk" not in env.sessions[1].prompt


def test_task_goes_back_to_the_planner_when_the_fixes_fail(env, monkeypatch):
    env.script(monkeypatch, (None, answered()), (None, answered()), (None, answered()))

    env.start()

    assert env.task().status == "replan"
    assert env.task().reason == "fix_attempts"
    assert [s.kind for s in env.steps()].count("fix") == SETTINGS.fix_attempts


def test_tests_weakened_twice_go_to_a_human(env, monkeypatch):
    env.script(
        monkeypatch,
        (write("tests/test_app.py", "def test_x():\n    pass\n"), answered()),
        (write("app.py", "x = 'fixed'\n"), answered()),
    )

    env.start()

    assert env.moves()[-2:] == [("gating", None), ("needs_human", "weakened_tests")]


@pytest.mark.parametrize(
    ("outcome", "status", "reason"),
    [
        (answered("too_big", split=["a", "b"]), "replan", "too_big"),
        (answered("needs_human", question="which x?"), "needs_human", "question"),
        (answered("blocked"), "needs_human", "blocked"),
        (step.Outcome("completed", None, result(None), 0), "needs_human", "no_answer"),
        (step.Outcome("failed", step.PROJECT_SETTINGS, None, 0), "needs_human", "project_settings"),
        (step.Outcome("failed", "API error", result(None), 0), "needs_human", "session_failed"),
    ],
)
def test_task_without_done_work_skips_the_gate(env, monkeypatch, outcome, status, reason):
    env.script(monkeypatch, (None, outcome))

    assert env.start() is None

    assert (env.task().status, env.task().reason) == (status, reason)
    assert [s.kind for s in env.steps()] == ["implement"]


def test_paused_session_keeps_the_work_in_progress(env, monkeypatch):
    env.script(
        monkeypatch,
        (write("new.py", "y = 1\n"), step.Outcome("paused", "context", None, 130_000)),
    )

    assert env.start() is None

    assert (env.task().status, env.task().reason) == ("paused", "context")
    ref = git.wip_ref(env.run_id, 1)
    assert git.git(env.repo, "show", f"{ref}:new.py") == "y = 1\n"
    assert git.git(env.repo, "rev-parse", f"{ref}^").strip() == env.steps()[0].checkpoint


def test_interrupt_while_answering_stops_before_the_gate(env, monkeypatch):
    env.script(monkeypatch, (write("app.py", "x = 'fixed'\n"), answered(reason="interrupt")))

    assert env.start() == "interrupt"

    assert env.task().status == "gating"
    assert [s.kind for s in env.steps()] == ["implement"]


def test_moved_branch_pauses_the_task_at_the_gate(env, monkeypatch):
    def work_and_commit(repo):
        write("app.py", "x = 'fixed'\n")(repo)
        commit(repo)

    env.script(monkeypatch, (work_and_commit, answered()))

    env.start()

    assert (env.task().status, env.task().reason) == ("paused", "branch_changed")
    gate = env.steps()[-1]
    assert (gate.kind, gate.outcome, gate.detail["reason"]) == ("gate", "paused", "branch_changed")
