import contextlib
import json
import os
import signal
import subprocess
import sys
import time

import pytest
from fakes import INIT, RESULT, read_calls

from agent_loop import agent, cli, config, git, implement, selfcheck, step
from agent_loop.store import Store


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
    card = {"goal": "Make x fixed", "criteria": [], "files": [], "tests": [], "decisions": []}
    (tmp_path / "tasks.json").write_text(json.dumps([{"title": "Fix x", **card}]))
    return repo


def test_run_commits_the_work_of_its_tasks(isolated, in_repo, tmp_path, monkeypatch):
    def session(session, cfg, store, run_id, step_id):
        (session.cwd / "app.py").write_text("x = 'fixed'\n")
        answer = {"status": "done", "summary": "", "commit_message": "fix(app): fix x"}
        detail = {"reason": None, "answer": answer | {"notes_for_next": ""}}
        store.finish_step(step_id, "completed", detail)
        return step.Outcome("completed", None, None, 1000)

    monkeypatch.setattr(step, "run", session)

    assert cli.main(["run", str(tmp_path / "tasks.json")]) == 0

    assert git.git(in_repo, "log", "-1", "--format=%s").strip() == "fix(app): fix x"
    with contextlib.closing(Store(isolated / "loop.db")) as store:
        [run] = store.runs("done")
        assert [task.status for task in store.tasks(run.id)] == ["committed"]


def test_run_stopped_by_an_error_is_paused_as_a_dead_one(
    isolated, in_repo, tmp_path, monkeypatch, capsys
):
    def broken(*args):
        raise config.ConfigError("collect: no number in the last line of the output: ''")

    monkeypatch.setattr(implement, "start", broken)

    assert cli.main(["run", str(tmp_path / "tasks.json")]) == 1

    assert "collect: no number" in capsys.readouterr().err
    with contextlib.closing(Store(isolated / "loop.db")) as store:
        [run] = store.runs("paused")
        assert run.reason == "died"


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
