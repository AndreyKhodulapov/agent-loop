"""Commands of the repository profile: frozen for a run, run with timeouts, services up and down.

The profile is frozen when a run starts, in `runs/<run>/profile.json`: placeholders get their
values once, so `up`, every session and `down` see the same compose project and ports, and
`reap` can stop the services of a dead run without the profile it was started with.

The commands that check the work run the agent's code - tests, `conftest.py`, linter plugins -
so they run under `sandbox-exec` with rules close to the sandbox of the sessions. `install`,
`up` and `down` need package registries and docker and run without it.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import signal
import socket
import subprocess
import tempfile
from collections.abc import Iterator
from dataclasses import asdict
from pathlib import Path

from agent_loop import agent, files, protected
from agent_loop.config import Profile

logger = logging.getLogger(__name__)

PROFILE_FILE = "profile.json"
SERVICES_FILE = "services"
DEFAULT_TIMEOUT = 10 * 60
# The fixer gets the failures from it: a test report puts them at the end.
OUTPUT_TAIL_LINES = 200
SANDBOXED = ("fmt", "lint", "collect", "test", "test_full", "smoke")
# The `Read` denies of `loop-settings.json` under the home directory, plus the keychains.
HOME_SECRETS = (
    ".ssh",
    ".aws",
    ".config/gh",
    ".config/gcloud",
    ".kube",
    ".docker/config.json",
    ".gnupg",
    ".netrc",
    ".git-credentials",
    ".pypirc",
    ".npmrc",
    "Library/Keychains",
)
# Files of the operator in the repository that run code outside the sandbox: git hooks and
# config, the CLI settings, git hook managers, IDE and devcontainer settings.
OPERATOR_FILES = (
    ".git",
    *protected.PATHS,
    *protected.HOOK_CONFIGS,
    ".vscode",
    ".idea",
    ".devcontainer",
)
# Closed to reading as in the sessions. `.env*` is not: tests read their settings from it, the
# gate masks its values instead.
REPO_SECRETS = ("*.pem", "*.key")
# The base of the profile of `@anthropic-ai/sandbox-runtime` 0.0.79, the sandbox of the CLI
# (Apache-2.0): what a process needs to run, and nothing that reaches other processes -
# LaunchServices, Apple Events, preferences of other apps, the arguments of other processes.
SANDBOX_BASE = """(version 1)
(deny default)
(allow process-exec)
(allow process-fork)
(allow process-info* (target same-sandbox))
(allow signal (target same-sandbox))
(allow mach-priv-task-port (target same-sandbox))
(allow user-preference-read)
(allow mach-lookup
  (global-name "com.apple.audio.systemsoundserver")
  (global-name "com.apple.distributed_notifications@Uv3")
  (global-name "com.apple.FontObjectsServer")
  (global-name "com.apple.fonts")
  (global-name "com.apple.logd")
  (global-name "com.apple.lsd.mapdb")
  (global-name "com.apple.PowerManagement.control")
  (global-name "com.apple.system.logger")
  (global-name "com.apple.system.notification_center")
  (global-name "com.apple.system.opendirectoryd.libinfo")
  (global-name "com.apple.system.opendirectoryd.membership")
  (global-name "com.apple.bsd.dirhelper")
  (global-name "com.apple.securityd.xpc")
  (global-name "com.apple.coreservices.launchservicesd")
  (global-name "com.apple.SecurityServer"))
(allow ipc-posix-shm)
(allow ipc-posix-sem)
(allow iokit-open
  (iokit-registry-entry-class "IOSurfaceRootUserClient")
  (iokit-registry-entry-class "RootDomainUserClient")
  (iokit-user-client-class "IOSurfaceSendRight"))
(allow iokit-get-properties)
(allow system-socket (require-all (socket-domain AF_SYSTEM) (socket-protocol 2)))
(allow sysctl-read
  (sysctl-name "hw.activecpu")
  (sysctl-name "hw.busfrequency_compat")
  (sysctl-name "hw.byteorder")
  (sysctl-name "hw.cacheconfig")
  (sysctl-name "hw.cachelinesize_compat")
  (sysctl-name "hw.cpufamily")
  (sysctl-name "hw.cpufrequency")
  (sysctl-name "hw.cpufrequency_compat")
  (sysctl-name "hw.cputype")
  (sysctl-name "hw.l1dcachesize_compat")
  (sysctl-name "hw.l1icachesize_compat")
  (sysctl-name "hw.l2cachesize_compat")
  (sysctl-name "hw.l3cachesize_compat")
  (sysctl-name "hw.logicalcpu")
  (sysctl-name "hw.logicalcpu_max")
  (sysctl-name "hw.machine")
  (sysctl-name "hw.memsize")
  (sysctl-name "hw.ncpu")
  (sysctl-name "hw.nperflevels")
  (sysctl-name "hw.packages")
  (sysctl-name "hw.pagesize_compat")
  (sysctl-name "hw.pagesize")
  (sysctl-name "hw.physicalcpu")
  (sysctl-name "hw.physicalcpu_max")
  (sysctl-name "hw.tbfrequency_compat")
  (sysctl-name "hw.vectorunit")
  (sysctl-name "kern.argmax")
  (sysctl-name "kern.bootargs")
  (sysctl-name "kern.hostname")
  (sysctl-name "kern.maxfiles")
  (sysctl-name "kern.maxfilesperproc")
  (sysctl-name "kern.maxproc")
  (sysctl-name "kern.ngroups")
  (sysctl-name "kern.osproductversion")
  (sysctl-name "kern.osrelease")
  (sysctl-name "kern.ostype")
  (sysctl-name "kern.osvariant_status")
  (sysctl-name "kern.osversion")
  (sysctl-name "kern.secure_kernel")
  (sysctl-name "kern.tcsm_available")
  (sysctl-name "kern.tcsm_enable")
  (sysctl-name "kern.usrstack64")
  (sysctl-name "kern.version")
  (sysctl-name "kern.willshutdown")
  (sysctl-name "machdep.cpu.brand_string")
  (sysctl-name "machdep.ptrauth_enabled")
  (sysctl-name "security.mac.lockdown_mode_state")
  (sysctl-name "sysctl.proc_cputype")
  (sysctl-name "vm.loadavg")
  (sysctl-name-prefix "hw.optional.arm")
  (sysctl-name-prefix "hw.optional.arm.")
  (sysctl-name-prefix "hw.optional.armv8_")
  (sysctl-name-prefix "hw.perflevel")
  (sysctl-name-prefix "kern.proc.all")
  (sysctl-name-prefix "kern.proc.pgrp.")
  (sysctl-name-prefix "kern.proc.pid.")
  (sysctl-name-prefix "machdep.cpu.")
  (sysctl-name-prefix "net.routetable."))
(allow sysctl-write (sysctl-name "kern.tcsm_enable"))
(allow distributed-notification-post)
(allow file-ioctl
  (literal "/dev/null")
  (literal "/dev/zero")
  (literal "/dev/random")
  (literal "/dev/urandom")
  (literal "/dev/dtracehelper"))
; Test databases from `up` listen on localhost. A (local ip) filter on outbound connections
; would match any address: the local address of an unbound socket is the any-address.
(allow network-bind (local ip "*:*"))
(allow network-inbound (local ip "*:*"))
(allow network-outbound (remote ip "localhost:*"))
"""


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
    whole group is killed, `docker compose` and test workers included. A command of `SANDBOXED`
    runs under `sandbox-exec`, with the environment of a session and a temporary directory of
    its own.
    """
    timeout = profile.timeouts.get(name, DEFAULT_TIMEOUT)
    argv = ["/bin/sh", "-c", profile.commands[name]]
    env = {**os.environ, **profile.env}
    with contextlib.ExitStack() as stack:
        if name in SANDBOXED:
            # Removed after the command: the next session of the agent could read what is left.
            tmp = Path(stack.enter_context(tempfile.TemporaryDirectory())).resolve()
            argv = ["sandbox-exec", "-p", _sandbox(repo.resolve(), tmp), *argv]
            env = {key: os.environ[key] for key in agent.ENV_KEYS if key in os.environ}
            env |= {"TMPDIR": f"{tmp}/", **profile.env}
        proc = subprocess.Popen(
            argv,
            cwd=repo,
            env=env,
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
def services(profile: Profile, repo: Path, run_dir: Path) -> Iterator[None]:
    """`up` of the profile before the block, `down` after it, however the block ends.

    `runs/<run>/services` is there while the services may be up: `reap` stops them only for a
    run that has died without `down`.
    """
    marker = run_dir / SERVICES_FILE
    marker.touch()
    try:
        if "up" in profile.commands:
            logger.info("starting the services")
            run(profile, "up", repo)
        yield
    finally:
        down(profile, repo)
        marker.unlink()


def down(profile: Profile, repo: Path) -> None:
    if "down" not in profile.commands:
        return
    logger.info("stopping the services")
    try:
        run(profile, "down", repo)
    except CommandError as exc:
        # Running services do not spoil the result of the run, the operator stops them.
        logger.error("cannot stop the services: %s", exc)


def _sandbox(repo: Path, tmp: Path) -> str:
    """Seatbelt profile of a command: it reads all but the secrets, writes the repository and
    `tmp` but the operator's files, connects to localhost only.

    Seatbelt matches the real path, so `repo` and `tmp` come resolved.
    """
    root = re.escape(str(repo))

    def anywhere(names: tuple[str, ...]) -> str:
        return " ".join(f'(regex #"^{root}/(.*/)?{_pattern(name)}$")' for name in names)

    home = Path.home().resolve()
    home_secrets = " ".join(f"(subpath {_string(home / path)})" for path in HOME_SECRETS)
    operator = " ".join(f'(regex #"^{root}/{_pattern(path)}(/|$)")' for path in OPERATOR_FILES)
    return f"""{SANDBOX_BASE}(allow file-read*)
(deny file-read* {home_secrets} {anywhere(REPO_SECRETS)})
(allow file-write* (subpath {_string(repo)}) (subpath {_string(tmp)})
  (literal "/dev/null") (literal "/dev/stdout") (literal "/dev/stderr") (regex #"^/dev/fd/"))
; `.env*` belongs to the operator too. A secret renamed or linked under another name would be
; readable.
(deny file-write* {operator} {anywhere((*REPO_SECRETS, ".env*"))})
"""


def _string(path: Path) -> str:
    # Seatbelt reads the escapes of quotes and backslashes, not of other characters.
    return json.dumps(str(path), ensure_ascii=False)


def _pattern(name: str) -> str:
    # In any case: the file system ignores it, a new `.CLAUDE` is the `.claude` of the CLI.
    return "".join(
        "[^/]*" if c == "*" else f"[{c.upper()}{c.lower()}]" if c.isalpha() else re.escape(c)
        for c in name
    )


def _kill(proc: subprocess.Popen) -> None:
    # The shell may have exited while its children still run.
    with contextlib.suppress(ProcessLookupError):
        os.killpg(proc.pid, signal.SIGKILL)


def _tail(output: str) -> str:
    return "\n".join(output.splitlines()[-OUTPUT_TAIL_LINES:])
