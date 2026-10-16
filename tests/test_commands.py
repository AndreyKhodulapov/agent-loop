import json
import os
import sys

import pytest

from agent_loop import commands, guard
from agent_loop.config import Profile


def test_freeze_fills_placeholders_once_for_the_run(tmp_path):
    profile = Profile(
        commands={
            "up": "docker compose -p {run_id} up -d --wait",
            "test": "pytest -q {changed_tests}",
        },
        timeouts={"up": 300},
        env={"POSTGRES_PORT": "{free_port}", "REDIS_URL": "redis://localhost:{free_port}/0"},
        deps_files=("uv.lock",),
        secret_patterns=("*.pem",),
    )
    run_dir = tmp_path / "runs" / "3f2a"

    run_profile = commands.freeze(profile, run_dir, "3f2a")

    assert run_profile.commands == {
        "up": "docker compose -p 3f2a up -d --wait",
        "test": "pytest -q {changed_tests}",
    }
    port = int(run_profile.env["POSTGRES_PORT"])
    redis_port = int(run_profile.env["REDIS_URL"].removeprefix("redis://localhost:")[:-2])
    assert port != redis_port
    assert (run_profile.timeouts, run_profile.deps_files) == ({"up": 300}, ("uv.lock",))
    assert commands.frozen(run_dir) == run_profile
    assert json.loads((run_dir / commands.PROFILE_FILE).read_text())["env"] == run_profile.env


def test_run_passes_the_profile_env_and_returns_output(tmp_path):
    profile = Profile(commands={"lint": "echo $POSTGRES_PORT; pwd"}, env={"POSTGRES_PORT": "1"})

    output = commands.run(profile, "lint", tmp_path)

    assert output.splitlines() == ["1", str(tmp_path.resolve())]


@pytest.fixture
def sandboxed(tmp_path, monkeypatch):
    """A repository with the operator's files and a home directory with keys; returns the
    repository."""
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    (home / ".ssh" / "config").write_text("Host *\n")
    monkeypatch.setenv("HOME", str(home))
    # A space in the path: the profile matches the repository by a regex.
    repo = tmp_path / "my repo"
    (repo / ".git" / "hooks").mkdir(parents=True)
    (repo / "deploy").mkdir()
    (repo / "deploy" / "server.pem").write_text("key\n")
    (repo / ".env").write_text("DB_PASSWORD=x\n")
    return repo


@pytest.mark.parametrize(
    "command",
    [
        "touch ../outside",
        "touch ~/outside",
        "cat ~/.ssh/config",
        "cat deploy/server.pem",
        "mv deploy/server.pem server.txt",
        "touch .git/hooks/pre-commit",
        "mkdir -p .CLAUDE && touch .CLAUDE/settings.local.json",
        "touch .Pre-Commit-Config.yaml",
        "mkdir .vscode",
        "mv .env env.txt",
        f"{sys.executable} -c 'import socket; socket.create_connection((\"192.0.2.1\", 80), 5)'",
    ],
)
def test_sandboxed_command_is_kept_within_the_repository(sandboxed, command):
    with pytest.raises(commands.CommandError, match="Operation not permitted"):
        commands.run(Profile(commands={"test": command}), "test", sandboxed)


def test_sandboxed_command_writes_the_repository_and_its_own_temporary_directory(
    sandboxed, monkeypatch
):
    monkeypatch.setenv("GITHUB_TOKEN", "1")
    server = "import socket; s = socket.create_server(('127.0.0.1', 0)); "
    server += "socket.create_connection(s.getsockname()).close()"
    profile = Profile(
        commands={
            "test": f'cat .env; touch built.txt $TMPDIR/cache; {sys.executable} -c "{server}";'
            " echo $TMPDIR; env"
        },
        env={"POSTGRES_PORT": "5433"},
    )

    output = commands.run(profile, "test", sandboxed).splitlines()

    assert output[0] == "DB_PASSWORD=x"
    assert (sandboxed / "built.txt").is_file()
    assert not os.path.exists(output[1])
    names = {line.split("=", 1)[0] for line in output[2:]}
    assert {"HOME", "TMPDIR", "POSTGRES_PORT"} <= names
    assert "GITHUB_TOKEN" not in names


def test_install_runs_outside_the_sandbox_with_the_whole_environment(sandboxed, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "1")
    profile = Profile(commands={"install": "touch ~/installed; echo $GITHUB_TOKEN"})

    assert commands.run(profile, "install", sandboxed) == "1\n"
    assert (sandboxed.parent / "home" / "installed").is_file()


def test_sandbox_keeps_the_home_paths_the_sessions_cannot_read():
    deny = json.loads(guard.BASE_SETTINGS.read_text())["permissions"]["deny"]
    home = {r[len("Read(~/") : -1].removesuffix("/**") for r in deny if r.startswith("Read(~/")}

    assert home <= set(commands.HOME_SECRETS)


def test_failed_command_reports_the_tail_of_its_output(tmp_path):
    profile = Profile(commands={"test": "seq 1 300; echo boom >&2; exit 3"})

    with pytest.raises(commands.CommandError) as exc:
        commands.run(profile, "test", tmp_path)

    message = str(exc.value)
    assert message.startswith("test: exit code 3\n")
    assert message.splitlines()[-1] == "boom"
    assert "\n50\n" not in message
    assert len(message.splitlines()) == commands.OUTPUT_TAIL_LINES + 1


def test_timeout_kills_the_whole_command(tmp_path):
    # The shell starts a child and waits: killing only the shell would leave the child running.
    child = tmp_path / "child.pid"
    profile = Profile(
        commands={
            "test_full": f"echo started; sleep 30 > /dev/null 2>&1 & echo $! > {child}; wait"
        },
        timeouts={"test_full": 1},
    )

    with pytest.raises(commands.CommandError, match="test_full: timed out after 1 s\nstarted"):
        commands.run(profile, "test_full", tmp_path)

    with pytest.raises(ProcessLookupError):
        os.kill(int(child.read_text()), 0)


def services_profile(up="echo up >> services.log", down="echo down >> services.log"):
    return Profile(commands={"up": up, "down": down})


def test_services_are_up_for_the_block_and_down_after_it(tmp_path):
    log = tmp_path / "services.log"

    with commands.services(services_profile(), tmp_path, tmp_path):
        assert log.read_text() == "up\n"
        assert (tmp_path / commands.SERVICES_FILE).is_file()

    assert log.read_text() == "up\ndown\n"
    assert not (tmp_path / commands.SERVICES_FILE).exists()


def test_services_go_down_when_the_loop_is_terminated(tmp_path):
    # SIGTERM and SIGHUP of the loop become SystemExit, see `cli`.
    with pytest.raises(SystemExit), commands.services(services_profile(), tmp_path, tmp_path):
        raise SystemExit(128 + 15)

    assert (tmp_path / "services.log").read_text() == "up\ndown\n"


def test_services_go_down_when_up_fails(tmp_path):
    profile = services_profile(up="echo up >> services.log; exit 1")

    with (
        pytest.raises(commands.CommandError, match="up: exit code 1"),
        commands.services(profile, tmp_path, tmp_path),
    ):
        pytest.fail("the block must not run")

    assert (tmp_path / "services.log").read_text() == "up\ndown\n"


def test_failed_down_is_logged_and_does_not_hide_the_result(tmp_path, caplog):
    profile = services_profile(down="echo compose is gone; exit 1")

    with commands.services(profile, tmp_path, tmp_path):
        pass

    assert "cannot stop the services: down: exit code 1\ncompose is gone" in caplog.text
