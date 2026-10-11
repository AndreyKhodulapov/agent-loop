"""Commands of the repository profile: frozen for a run, run with timeouts, services up and down.

The profile is frozen when a run starts, in `runs/<run>/profile.json`: placeholders get their
values once, so `up`, every session and `down` see the same compose project and ports, and
`reap` can stop the services of a dead run without the profile it was started with.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import signal
import socket
import subprocess
from collections.abc import Iterator
from dataclasses import asdict
from pathlib import Path

from agent_loop import files
from agent_loop.config import Profile

logger = logging.getLogger(__name__)

PROFILE_FILE = "profile.json"
DEFAULT_TIMEOUT = 10 * 60
# The fixer gets the failures from it: a test report puts them at the end.
OUTPUT_TAIL_LINES = 200


class CommandError(Exception):
    pass


def freeze(profile: Profile, run_dir: Path, run_id: str) -> Profile:
    """Fill the placeholders and keep the result for the whole run; returns it.

    Called once, when the run starts; `loop resume` and `reap` read it with `frozen`. Each
    `{free_port}` in `[env]` gets its own port. `{changed_tests}` is filled per gate run.
    """
    with contextlib.ExitStack() as stack:
        # The sockets stay bound until every port is chosen, so two variables never get one port.
        ports = []
        for value in profile.env.values():
            for _ in range(value.count("{free_port}")):
                sock = stack.enter_context(socket.socket())
                sock.bind(("", 0))
                ports.append(str(sock.getsockname()[1]))
    env = {}
    for name, value in profile.env.items():
        while "{free_port}" in value:
            value = value.replace("{free_port}", ports.pop(), 1)
        env[name] = value.replace("{run_id}", run_id)
    commands = {
        name: command.replace("{run_id}", run_id) for name, command in profile.commands.items()
    }
    run_profile = Profile(
        commands, profile.timeouts, env, profile.deps_files, profile.secret_patterns
    )
    run_dir.mkdir(parents=True, exist_ok=True)
    files.write_atomic(run_dir / PROFILE_FILE, json.dumps(asdict(run_profile), indent=2))
    return run_profile


def frozen(run_dir: Path) -> Profile:
    raw = json.loads((run_dir / PROFILE_FILE).read_text(encoding="utf-8"))
    return Profile(
        raw["commands"],
        raw["timeouts"],
        raw["env"],
        tuple(raw["deps_files"]),
        tuple(raw["secret_patterns"]),
    )


def run(profile: Profile, name: str, repo: Path) -> str:
    """Run the command of the profile in the repository; returns its output.

    The command runs in its own process group: on timeout or on the way out of the loop the
    whole group is killed, `docker compose` and test workers included.
    """
    timeout = profile.timeouts.get(name, DEFAULT_TIMEOUT)
    proc = subprocess.Popen(
        profile.commands[name],
        shell=True,
        cwd=repo,
        env={**os.environ, **profile.env},
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        process_group=0,
    )
    try:
        output, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill(proc)
        output, _ = proc.communicate()
        raise CommandError(f"{name}: timed out after {timeout} s\n{_tail(output)}") from None
    finally:
        # Ctrl+C or SIGTERM of the loop while the command runs.
        if proc.poll() is None:
            _kill(proc)
            proc.wait()
    if proc.returncode != 0:
        raise CommandError(f"{name}: exit code {proc.returncode}\n{_tail(output)}")
    return output


@contextlib.contextmanager
def services(profile: Profile, repo: Path) -> Iterator[None]:
    """`up` of the profile before the block, `down` after it, however the block ends."""
    try:
        if "up" in profile.commands:
            logger.info("starting the services")
            run(profile, "up", repo)
        yield
    finally:
        down(profile, repo)


def down(profile: Profile, repo: Path) -> None:
    if "down" not in profile.commands:
        return
    logger.info("stopping the services")
    try:
        run(profile, "down", repo)
    except CommandError as exc:
        # Running services do not spoil the result of the run, the operator stops them.
        logger.error("cannot stop the services: %s", exc)


def _kill(proc: subprocess.Popen) -> None:
    # The shell may have exited while its children still run.
    with contextlib.suppress(ProcessLookupError):
        os.killpg(proc.pid, signal.SIGKILL)


def _tail(output: str) -> str:
    return "\n".join(output.splitlines()[-OUTPUT_TAIL_LINES:])
