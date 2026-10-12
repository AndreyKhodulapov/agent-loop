import subprocess
import time
from dataclasses import asdict, dataclass

import pytest

from agent_loop import agent, budget, gates, git, guard, implement, prompts, protected, step
from agent_loop.config import Config, Profile, Settings
from agent_loop.gates import Verdict
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


def both(*actions):
    def action(repo):
        for each in actions:
            each(repo)

    return action


def paused(reason, context_tokens=1000):
    return step.Outcome("paused", reason, None, context_tokens)


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
        return implement.start(self.cfg, self.store, run, self.task(), 1, profile, previous)

    def resume(self, previous=""):
        run = self.store.run(self.run_id)
        return implement.resume(self.cfg, self.store, run, self.task(), 1, PROFILE, previous)

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
            answer = outcome.result and outcome.result.structured_output
            detail = {"reason": outcome.reason, "context_tokens": outcome.context_tokens}
            store.finish_step(step_id, outcome.status, detail | {"answer": answer})
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
    run_id = db.create_run(repo, "main", git.head(repo))
    task_id = db.add_task(run_id, "Fix x", CARD)
    protected.save(repo, step.run_dir(home, run_id))
    yield Env(Config(home, repo, None, SETTINGS, {}), db, run_id, task_id, repo, [])
    db.close()


def test_done_work_that_passes_the_gate_is_committed(env, monkeypatch):
    checkpoint = git.head(env.repo)
    write(guard.HANDOFF, "stale")(env.repo)
    env.script(monkeypatch, (write("app.py", "x = 'fixed'\n"), answered()))

    assert env.start(previous="M app.py") is None

    task = env.task()
    assert task.status == "committed"
    assert task.tests == 3
    assert env.moves() == [("implementing", None), ("gating", None), ("committed", None)]
    implementer, gate, commit_step = env.steps()
    assert implementer.kind == "implement"
    assert gate.kind == "gate"
    assert gate.outcome == "completed"
    assert gate.detail["status"] == "pass"
    assert gate.checkpoint == implementer.checkpoint == commit_step.checkpoint == checkpoint
    head = git.head(env.repo)
    assert commit_step.kind == "commit"
    assert commit_step.outcome == "completed"
    assert commit_step.detail == {"commit": head, "message": "feat(app): x"}
    assert git.git(env.repo, "rev-parse", "HEAD^").strip() == checkpoint
    assert git.git(env.repo, "show", "HEAD:app.py") == "x = 'fixed'\n"
    assert git.git(env.repo, "status", "--porcelain") == ""
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
    assert not session.resume
    assert not (env.repo / guard.HANDOFF).exists()


def test_fixer_gets_the_findings_and_the_work(env, monkeypatch):
    env.script(
        monkeypatch,
        (write("new.py", "y = 1\n"), answered()),
        (write("app.py", "x = 'fixed'\n"), answered()),
    )

    env.start()

    assert env.task().status == "committed"
    assert env.moves() == [
        ("implementing", None),
        ("gating", None),
        ("fixing", "test_failed"),
        ("gating", None),
        ("committed", None),
    ]
    assert [s.kind for s in env.steps()] == ["implement", "gate", "fix", "gate", "commit"]
    first_gate = env.steps()[1]
    assert first_gate.detail["status"] == "fixing"
    fixer = env.sessions[1]
    assert fixer.system_files == prompts.system_files("fixer", SETTINGS)
    assert "test_failed" in fixer.prompt
    assert "x is not fixed" in fixer.prompt
    # The file the implementer has added is in the work the fixer gets and in the commit.
    assert "+y = 1" in fixer.prompt
    assert git.git(env.repo, "show", "HEAD:new.py") == "y = 1\n"
    assert len({s.checkpoint for s in env.steps()}) == 1


def test_secrets_stay_out_of_the_prompts_and_stop_the_task(env, monkeypatch):
    checkpoint = git.head(env.repo)
    env.script(
        monkeypatch,
        (write("config/.env.local", "TOKEN=abcdefghijk\n"), paused("context", 130_000)),
    )
    env.start()
    env.script(monkeypatch, (write("app.py", "x = 'fixed'\n"), answered()))

    env.resume()

    assert "abcdefghijk" not in env.sessions[-1].prompt
    assert (env.task().status, env.task().reason) == ("needs_human", gates.SECRET_FILE)
    assert [s.kind for s in env.steps()] == ["implement", "implement", "gate"]
    assert git.head(env.repo) == checkpoint
    assert git.untracked(env.repo) == ["config/.env.local"]


def test_files_the_gate_commands_leave_are_not_the_work(env, monkeypatch):
    profile = Profile(commands={"test": "echo junit > report.xml; " + PROFILE.commands["test"]})
    env.script(
        monkeypatch,
        (None, answered()),
        (write("app.py", "x = 'fixed'\n"), answered()),
    )

    env.start(profile)

    assert env.task().status == "committed"
    assert env.steps()[1].detail["artifacts"] == ["report.xml"]
    assert "report.xml" not in env.sessions[1].prompt
    assert "report.xml" not in git.git(env.repo, "ls-tree", "-r", "--name-only", "HEAD")
    assert git.untracked(env.repo) == ["report.xml"]


def test_commit_message_that_breaks_the_rules_is_made_of_the_title(env, monkeypatch):
    message = "feat(app): x\n\nCo-Authored-By: Claude <noreply@anthropic.com>"
    env.script(monkeypatch, (write("app.py", "x = 'fixed'\n"), answered(commit_message=message)))

    env.start()

    assert git.git(env.repo, "log", "-1", "--format=%B").strip() == "feat: Fix x"


def test_done_without_changes_goes_to_a_human(env, monkeypatch):
    profile = Profile(commands={"collect": PROFILE.commands["collect"]})
    env.script(monkeypatch, (None, answered()))

    env.start(profile)

    assert (env.task().status, env.task().reason) == ("needs_human", "no_changes")
    assert [s.kind for s in env.steps()] == ["implement", "gate"]


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


def test_session_that_exits_without_a_result_goes_to_a_human(env, monkeypatch):
    def run(session, cfg, store, run_id, step_id):
        error = "claude exited with code 1 without a result: overloaded"
        store.finish_step(step_id, "failed", {"error": error})
        raise agent.AgentError(error)

    monkeypatch.setattr(step, "run", run)

    assert env.start() is None

    assert (env.task().status, env.task().reason) == ("needs_human", "session_failed")
    assert [(s.kind, s.outcome) for s in env.steps()] == [("implement", "failed")]


def test_paused_session_keeps_the_work_in_progress(env, monkeypatch):
    env.script(
        monkeypatch,
        (
            both(write("new.py", "y = 1\n"), write(guard.HANDOFF, "new.py is half done")),
            paused("context", 130_000),
        ),
    )

    assert env.start() is None

    assert (env.task().status, env.task().reason) == ("paused", "context")
    ref = git.wip_ref(env.run_id, 1)
    # The handoff is for the next session, not the work of the task.
    assert git.git(env.repo, "ls-tree", "-r", "--name-only", ref).split() == [
        "app.py",
        "new.py",
        "old.py",
        "tests/test_app.py",
    ]
    assert git.git(env.repo, "show", f"{ref}:new.py") == "y = 1\n"
    assert git.git(env.repo, "rev-parse", f"{ref}^").strip() == env.steps()[0].checkpoint


def test_interrupt_while_answering_stops_before_the_gate(env, monkeypatch):
    env.script(monkeypatch, (write("app.py", "x = 'fixed'\n"), answered(reason="interrupt")))

    assert env.start() == implement.Stop("interrupt")

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


def test_paused_session_goes_on_with_its_context(env, monkeypatch):
    env.script(monkeypatch, (write("new.py", "y = 1\n"), paused("interrupt")))
    env.start()
    env.script(monkeypatch, (write("app.py", "x = 'fixed'\n"), answered()))

    assert env.resume() is None

    assert env.task().status == "committed"
    assert env.moves() == [
        ("implementing", None),
        ("paused", "interrupt"),
        ("implementing", None),
        ("gating", None),
        ("committed", None),
    ]
    assert [s.kind for s in env.steps()] == ["implement", "implement", "gate", "commit"]
    stopped, resumed = env.sessions
    assert resumed.resume
    assert resumed.session_id == stopped.session_id == env.steps()[1].session_id
    assert resumed.prompt == prompts.RESUMED
    assert resumed.system_files == prompts.system_files("implementer", SETTINGS)


def test_session_with_a_full_context_goes_on_from_the_handoff(env, monkeypatch):
    env.script(
        monkeypatch,
        (
            both(write("new.py", "y = 1\n"), write(guard.HANDOFF, "new.py is half done")),
            paused("context", 130_000),
        ),
    )
    env.start()
    env.script(monkeypatch, (write("app.py", "x = 'fixed'\n"), answered()))

    env.resume(previous="M old.py")

    assert env.task().status == "committed"
    session = env.sessions[-1]
    assert not session.resume
    assert "Make x fixed" in session.prompt
    assert "M old.py" in session.prompt
    assert "new.py is half done" in session.prompt
    assert "+y = 1" in session.prompt
    assert not (env.repo / guard.HANDOFF).exists()


def test_fixer_with_a_full_context_goes_on_from_the_findings_and_the_handoff(env, monkeypatch):
    env.script(
        monkeypatch,
        (None, answered()),
        (write(guard.HANDOFF, "app.py is next"), paused("context", 130_000)),
    )
    env.start()
    env.script(monkeypatch, (write("app.py", "x = 'fixed'\n"), answered()))

    env.resume()

    assert env.task().status == "committed"
    assert [s.kind for s in env.steps()] == ["implement", "gate", "fix", "fix", "gate", "commit"]
    fixer = env.sessions[-1]
    assert fixer.system_files == prompts.system_files("fixer", SETTINGS)
    assert "x is not fixed" in fixer.prompt
    assert "app.py is next" in fixer.prompt


def test_gate_paused_on_a_moved_branch_runs_again(env, monkeypatch):
    def work_and_commit(repo):
        write("app.py", "x = 'fixed'\n")(repo)
        commit(repo)

    env.script(monkeypatch, (work_and_commit, answered()))
    env.start()
    # The operator takes the commit back, the work stays in the working tree.
    subprocess.run(["git", "reset", "-q", "--soft", "HEAD~1"], cwd=env.repo, check=True)

    env.resume()

    assert env.moves()[-3:] == [("paused", "branch_changed"), ("gating", None), ("committed", None)]
    assert [s.kind for s in env.steps()] == ["implement", "gate", "gate", "commit"]
    assert env.steps()[-2].detail["status"] == "pass"


def test_branch_moved_after_the_gate_pauses_the_commit(env, monkeypatch):
    env.script(monkeypatch, (write("app.py", "x = 'fixed'\n"), answered(reason="interrupt")))
    env.start()
    gate = implement._Task._gate

    def gate_then_operator_commits(task):
        verdict = gate(task)
        write("old.py", "y = 3\n")(env.repo)
        subprocess.run(["git", "commit", "-q", "-m", "by hand", "old.py"], cwd=env.repo, check=True)
        return verdict

    monkeypatch.setattr(implement._Task, "_gate", gate_then_operator_commits)

    env.resume()

    assert (env.task().status, env.task().reason) == ("paused", "branch_changed")
    commit_step = env.steps()[-1]
    assert (commit_step.kind, commit_step.outcome) == ("commit", "paused")
    assert git.git(env.repo, "log", "-1", "--format=%s").strip() == "by hand"
    monkeypatch.setattr(implement._Task, "_gate", gate)
    # The operator takes the commit back, its change stays in the working tree.
    subprocess.run(["git", "reset", "-q", "--soft", "HEAD~1"], cwd=env.repo, check=True)

    env.resume()

    assert env.task().status == "committed"
    assert [s.kind for s in env.steps()] == ["implement", "gate", "commit", "gate", "commit"]


def test_task_stopped_after_the_answer_goes_to_the_gate(env, monkeypatch):
    env.script(monkeypatch, (write("app.py", "x = 'fixed'\n"), answered(reason="interrupt")))
    env.start()

    assert env.resume() is None

    assert env.task().status == "committed"
    assert [s.kind for s in env.steps()] == ["implement", "gate", "commit"]


def test_task_stopped_before_its_fixer_gets_one(env, monkeypatch):
    limits = budget.LIMITS_FILE
    resets_at = int(time.time()) + 3600

    def run_out_of_limits(repo):
        window = agent.Window(0.99, resets_at)
        budget.save_limits(env.cfg.home / limits, agent.Limits(window, None))

    env.script(monkeypatch, (run_out_of_limits, answered()))

    assert env.start() == implement.Stop("budget", resets_at)

    assert env.task().status == "fixing"
    assert [s.kind for s in env.steps()] == ["implement", "gate"]
    (env.cfg.home / limits).unlink()
    env.script(monkeypatch, (write("app.py", "x = 'fixed'\n"), answered()))

    env.resume()

    assert env.task().status == "committed"
    assert "x is not fixed" in env.sessions[-1].prompt


def test_no_room_in_the_limits_leaves_the_task_as_it_is(env, monkeypatch):
    resets_at = int(time.time()) + 3600
    window = agent.Window(0.99, resets_at)
    budget.save_limits(env.cfg.home / budget.LIMITS_FILE, agent.Limits(window, None))
    env.script(monkeypatch)

    assert env.start() == implement.Stop("budget", resets_at)

    assert env.task().status == "pending"
    assert env.steps() == []


@pytest.mark.parametrize(
    ("path", "kind", "outcome", "detail", "moves"),
    [
        (
            ["implementing"],
            "implement",
            "completed",
            {"reason": None, "answer": answered().result.structured_output},
            [("gating", None), ("committed", None)],
        ),
        (
            ["implementing"],
            "implement",
            "failed",
            {"error": "the CLI has exited without a result"},
            [("needs_human", "session_failed")],
        ),
        (
            ["implementing"],
            "implement",
            "paused",
            {"reason": "budget"},
            [("paused", "budget"), ("implementing", None), ("gating", None), ("committed", None)],
        ),
        (
            ["implementing", "gating"],
            "gate",
            "completed",
            asdict(Verdict("fixing", "test_failed", ("x is not fixed",))),
            [("fixing", "test_failed"), ("gating", None), ("committed", None)],
        ),
        (
            ["implementing", "gating"],
            "commit",
            "completed",
            {"commit": "c0ffee", "message": "feat(app): x"},
            [("committed", None)],
        ),
    ],
    ids=[
        "answer not taken",
        "error of the CLI",
        "pause not taken",
        "verdict not taken",
        "commit not taken",
    ],
)
def test_end_of_a_step_not_taken_is_taken_on_resume(
    env, monkeypatch, path, kind, outcome, detail, moves
):
    for status in path:
        env.store.move_task(env.task_id, status)
    step_id = env.store.start_step(env.task_id, kind, git.head(env.repo), [], "s-1")
    write("app.py", "x = 'fixed'\n")(env.repo)
    env.store.finish_step(step_id, outcome, detail)
    env.script(monkeypatch, (None, answered()))

    env.resume()

    assert env.moves()[len(path) :] == moves


def test_task_that_keeps_stalling_goes_back_to_the_planner(env, monkeypatch):
    env.script(monkeypatch, (None, paused("timeout")))
    env.start()
    for reason in ("idle", "guard_error"):
        env.script(monkeypatch, (None, paused(reason)))
        env.resume()

    assert env.resume() is None

    assert (env.task().status, env.task().reason) == ("replan", "stall_pauses")
    assert len(env.sessions) == 3
