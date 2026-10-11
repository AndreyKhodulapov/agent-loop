import pytest

from agent_loop import agent, cli


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
    monkeypatch.setattr(agent, "selfcheck", lambda home: "2.1.296")
    assert cli.main(["selfcheck"]) == 0
    assert "ok, claude 2.1.296" in capsys.readouterr().err

    def broken(home):
        raise agent.AgentError("first: no rate_limit_event with windows")

    monkeypatch.setattr(agent, "selfcheck", broken)
    assert cli.main(["selfcheck"]) == 1
    assert "failed: first: no rate_limit_event" in capsys.readouterr().err
