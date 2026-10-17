import contextlib
import os
import signal
import subprocess
import sys
import time

import pytest
from fakes import INIT, RESULT, read_calls
from test_plan import PLAN
from test_runner import planned

from agent_loop import agent, cli, commands, config, gates, git, implement, selfcheck, step
from agent_loop.store import Store, StoreError


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("LOOP_HOME", str(home))
    monkeypatch.chdir(tmp_path)
    return home


@pytest.mark.parametrize("command", sorted(cli.STUBS))
def test_stub_reports_not_implemented(command, capsys):
    assert cli.main([command]) == 1
    assert f"{command}: not implemented yet" in capsys.readouterr().err


def test_config_shows_sources(capsys):
    assert cli.main(["--doc-lang", "ru", "--model-critic", "sonnet", "config"]) == 0

    output = capsys.readouterr().out
    assert "doc_lang            ru  (flag)" in output
    assert "model_critic        sonnet  (flag)" in output
    assert "fix_attempts        3  (default)" in output


def test_config_error_exits_with_2(isolated, capsys):
    (isolated / ".env").write_text("LOOP_DOC_LANG=de\n")

    assert cli.main(["config"]) == 2
    assert "config: doc_lang must be one of" in capsys.readouterr().err


def test_command_is_required(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main([])

    assert exc.value.code == 2


def test_selfcheck_reports_result(monkeypatch, capsys):
    monkeypatch.setattr(selfcheck, "run", lambda home: "2.1.296")
    assert cli.main(["selfcheck"]) == 0
    assert "ok, claude 2.1.296" in capsys.readouterr().err

    def broken(home):
        raise agent.AgentError("first: no rate_limit_event with windows")

    monkeypatch.setattr(selfcheck, "run", broken)
    assert cli.main(["selfcheck"]) == 1
    assert "failed: first: no rate_limit_event" in capsys.readouterr().err


@pytest.fixture
def in_repo(repo, tmp_path, monkeypatch):
    monkeypatch.chdir(repo)
    monkeypatch.setattr(selfcheck, "require", lambda home: None)
    return repo


def approved(home, repo, *titles):
    """Approve a plan of `repo` with a task under each of `titles`."""
    card = {"goal": "Make x fixed", "criteria": ["x"], "files": [], "tests": [], "decisions": []}
    planned(home, repo, tasks=[{"title": title, **card} for title in titles])
    assert cli.main(["approve"]) == 0


def test_run_through_all_its_tasks_exits_with_zero(isolated, in_repo, tmp_path, monkeypatch):
    def session(session, cfg, store, run_id, step_id):
        (session.cwd / "app.py").write_text("x = 'fixed'\n")
        answer = {"status": "done", "summary": "", "commit_message": "fix(app): fix x"}
        detail = {"reason": None, "answer": answer | {"notes_for_next": ""}}
        store.finish_step(step_id, "completed", detail)
        return step.Outcome("completed", None, None, 1000)

    monkeypatch.setattr(step, "run", session)
    approved(isolated, in_repo, "Fix x")

    assert cli.main(["run"]) == 0

    with contextlib.closing(Store(isolated / "loop.db")) as store:
        assert len(store.runs("done")) == 1


def test_run_stopped_inside_the_commit_goes_on_with_the_next_task(
    isolated, in_repo, tmp_path, monkeypatch
):
    approved(isolated, in_repo, "Add m1", "Add m2")
    modules = iter(["m1", "m2"])

    def session(session, cfg, store, run_id, step_id):
        module = next(modules)
        (session.cwd / f"{module}.py").write_text("x = 1\n")
        answer = {"status": "done", "summary": "", "commit_message": f"feat({module}): add it"}
        detail = {"reason": None, "answer": answer | {"notes_for_next": ""}}
        store.finish_step(step_id, "completed", detail)
        return step.Outcome("completed", None, None, 1000)

    monkeypatch.setattr(step, "run", session)
    real_git = git.git
    stops = iter([True])

    # Ctrl+C after the branch has moved to the commit of the first task.
    def stopped_before_the_reset(repo, *args, **kwargs):
        if args[0] == "reset" and next(stops, False):
            raise KeyboardInterrupt
        return real_git(repo, *args, **kwargs)

    monkeypatch.setattr(git, "git", stopped_before_the_reset)

    assert cli.main(["run"]) == 130
    assert cli.main(["resume"]) == 0

    log = git.git(in_repo, "log", "-3", "--format=%s").splitlines()
    assert log == ["feat(m2): add it", "feat(m1): add it", "init"]
    assert git.git(in_repo, "status", "--porcelain") == ""
    with contextlib.closing(Store(isolated / "loop.db")) as store:
        [run] = store.runs("done")
        assert [task.status for task in store.tasks(run.id)] == ["committed", "committed"]


@pytest.mark.parametrize("where", ["up", "gate", "answer"])
def test_operator_stop_pauses_the_run_with_the_exit_code_of_ctrl_c(
    isolated, in_repo, tmp_path, monkeypatch, capsys, where
):
    stops = [where]

    def stopped(name):
        if name not in stops:
            return False
        stops.remove(name)
        return True

    def session(session, cfg, store, run_id, step_id):
        (session.cwd / "app.py").write_text("x = 'fixed'\n")
        answer = {"status": "done", "summary": "", "commit_message": "fix(app): fix x"}
        reason = step.INTERRUPT if stopped("answer") else None
        detail = {"reason": reason, "answer": answer | {"notes_for_next": ""}}
        store.finish_step(step_id, "completed", detail)
        return step.Outcome("completed", reason, None, 1000)

    real_run, real_check = commands.run, gates.check

    def run(profile, name, repo):
        if stopped(name):
            raise KeyboardInterrupt
        return real_run(profile, name, repo)

    def check(*args):
        if stopped("gate"):
            raise KeyboardInterrupt
        return real_check(*args)

    monkeypatch.setattr(step, "run", session)
    monkeypatch.setattr(commands, "run", run)
    monkeypatch.setattr(gates, "check", check)
    profile = tmp_path / "loop.toml"
    profile.write_text('[commands]\nup = "true"\n')
    flags = ["--profile", str(profile)]

    approved(isolated, in_repo, "Fix x")

    assert cli.main([*flags, "run"]) == 130

    assert "is gone" not in capsys.readouterr().err
    with contextlib.closing(Store(isolated / "loop.db")) as store:
        [run] = store.runs("paused")
        assert run.reason == step.INTERRUPT
        [task] = store.tasks(run.id)
        paused = [s.detail["reason"] for s in store.steps(task.id) if s.outcome == "paused"]
        assert paused == ([step.INTERRUPT] if where == "gate" else [])
    assert cli.main([*flags, "resume"]) == 0
    assert git.git(in_repo, "log", "-1", "--format=%s").strip() == "fix(app): fix x"


@pytest.mark.parametrize(
    "error",
    [
        config.ConfigError("collect: no number in the last line of the output: ''"),
        git.GitError("git commit-tree: Author identity unknown"),
        StoreError("no run abc"),
    ],
)
def test_run_stopped_by_an_error_is_paused_as_a_dead_one(
    isolated, in_repo, tmp_path, monkeypatch, capsys, error
):
    def broken(*args):
        raise error

    monkeypatch.setattr(implement, "start", broken)
    services = tmp_path / "services.log"
    profile = tmp_path / "loop.toml"
    profile.write_text(
        f'[commands]\nup = "echo up >> {services}"\ndown = "echo down >> {services}"\n'
    )

    approved(isolated, in_repo, "Fix x")

    assert cli.main(["--profile", str(profile), "run"]) == 1

    assert str(error) in capsys.readouterr().err
    assert services.read_text() == "up\ndown\n"
    with contextlib.closing(Store(isolated / "loop.db")) as store:
        [run] = store.runs("paused")
        assert run.reason == "died"


def planner_sessions(monkeypatch, *outcomes):
    """Replace the planner sessions: each ends with its (status, reason, answer), recorded like
    `step.run` does."""
    scripted = iter(outcomes)

    def session(session, cfg, store, run_id, step_id):
        status, reason, output = next(scripted)
        detail = {"reason": reason, "context_tokens": 1000, "answer": output}
        store.finish_step(step_id, status, detail)
        return step.Outcome(status, reason, None, 1000)

    monkeypatch.setattr(step, "run", session)


def planning_runs(home):
    with contextlib.closing(Store(home / "loop.db")) as store:
        return store.runs("planning")


def test_plan_waits_for_the_answers_of_the_operator(isolated, in_repo, tmp_path, monkeypatch):
    planner_sessions(
        monkeypatch,
        ("completed", None, {"status": "questions", "questions": ["Who removes a dish?"]}),
        ("completed", None, {"status": "plan", "plan": PLAN}),
        ("completed", None, {"findings": []}),
    )
    request = tmp_path / "request.md"
    request.write_text("Add the admin of dishes")

    assert cli.main(["plan", "--from", str(request)]) == 1

    [run] = planning_runs(isolated)
    questions = isolated / "runs" / run.id / "QUESTIONS.md"
    assert cli.main(["answer"]) == 1
    questions.write_text(questions.read_text() + "The admin\n")
    assert cli.main(["answer", run.id]) == 0
    assert (isolated / "runs" / run.id / "plan.json").is_file()


def test_approve_takes_a_plan_once(isolated, in_repo, tmp_path, monkeypatch):
    planner_sessions(
        monkeypatch,
        ("completed", None, {"status": "plan", "plan": PLAN}),
        ("completed", None, {"findings": []}),
    )
    request = tmp_path / "request.md"
    request.write_text("Add the admin of dishes")
    assert cli.main(["plan", "--from", str(request)]) == 0

    assert cli.main(["approve"]) == 0
    assert cli.main(["approve"]) == 1


def test_plan_goes_on_with_the_same_request_only(isolated, in_repo, tmp_path, monkeypatch):
    planner_sessions(
        monkeypatch,
        ("paused", "interrupt", None),
        ("completed", None, {"status": "plan", "plan": PLAN}),
        ("completed", None, {"findings": []}),
        ("completed", None, {"status": "plan", "plan": PLAN}),
        ("completed", None, {"findings": []}),
    )
    request = tmp_path / "request.md"
    request.write_text("Add the admin of dishes")

    assert cli.main(["plan", "--from", str(request)]) == 130
    assert cli.main(["plan", "--from", str(request)]) == 0
    [planned] = planning_runs(isolated)

    request.write_text("Remove the admin of dishes")
    assert cli.main(["plan", "--from", str(request)]) == 0
    assert [run.id for run in planning_runs(isolated)][0] == planned.id
    assert len(planning_runs(isolated)) == 2


def test_plan_without_a_request_goes_on_with_the_conversation(isolated, in_repo, monkeypatch):
    talks = []
    monkeypatch.setattr(agent, "talk", lambda session, system_file: talks.append(session) or 0)
    planner_sessions(
        monkeypatch,
        ("paused", "interrupt", None),
        ("completed", None, {"status": "plan", "plan": PLAN}),
        ("completed", None, {"findings": []}),
        ("completed", None, {"status": "plan", "plan": PLAN}),
        ("completed", None, {"findings": []}),
    )

    assert cli.main(["plan"]) == 130
    assert cli.main(["plan", "--new"]) == 0
    # The new conversation has ended with a plan, the first one goes on.
    assert cli.main(["plan"]) == 0

    first, new, reopened = talks
    assert (new.session_id == first.session_id, new.resume) == (False, False)
    assert (reopened.session_id, reopened.resume) == (first.session_id, True)
    assert len(planning_runs(isolated)) == 2


def test_gc_without_dead_runs(capsys):
    assert cli.main(["gc"]) == 0
    assert "no dead runs" in capsys.readouterr().err


def test_terminated_loop_stops_its_session(isolated, tmp_path, fake_claude):
    calls_path = fake_claude([INIT, "sleep", RESULT])
    raw_stream = isolated / "selfcheck" / "first.jsonl"
    loop = subprocess.Popen(
        [sys.executable, "-m", "agent_loop", "selfcheck"], cwd=tmp_path, stderr=subprocess.DEVNULL
    )
    # The loop has read the first event, so it is inside the session block.
    deadline = time.monotonic() + 10
    while not (raw_stream.is_file() and raw_stream.read_text()) and time.monotonic() < deadline:
        time.sleep(0.01)

    loop.send_signal(signal.SIGTERM)

    assert loop.wait(timeout=10) == 128 + signal.SIGTERM
    [call] = read_calls(calls_path)
    with pytest.raises(ProcessLookupError):
        os.kill(call["pid"], 0)
