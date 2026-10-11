import json
import os

import pytest

from agent_loop import commands
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

    with commands.services(services_profile(), tmp_path):
        assert log.read_text() == "up\n"

    assert log.read_text() == "up\ndown\n"


def test_services_go_down_when_the_loop_is_terminated(tmp_path):
    # SIGTERM and SIGHUP of the loop become SystemExit, see `cli`.
    with pytest.raises(SystemExit), commands.services(services_profile(), tmp_path):
        raise SystemExit(128 + 15)

    assert (tmp_path / "services.log").read_text() == "up\ndown\n"


def test_services_go_down_when_up_fails(tmp_path):
    profile = services_profile(up="echo up >> services.log; exit 1")

    with (
        pytest.raises(commands.CommandError, match="up: exit code 1"),
        commands.services(profile, tmp_path),
    ):
        pytest.fail("the block must not run")

    assert (tmp_path / "services.log").read_text() == "up\ndown\n"


def test_failed_down_is_logged_and_does_not_hide_the_result(tmp_path, caplog):
    profile = services_profile(down="echo compose is gone; exit 1")

    with commands.services(profile, tmp_path):
        pass

    assert "cannot stop the services: down: exit code 1\ncompose is gone" in caplog.text
