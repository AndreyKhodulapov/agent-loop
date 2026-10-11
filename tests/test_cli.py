import os
import signal
import subprocess
import sys
import time

import pytest
from fakes import INIT, RESULT, read_calls

from agent_loop import agent, cli, selfcheck


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
