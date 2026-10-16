"""Adapter for the `claude` CLI: the only module that knows its flags and the `stream-json` format.

A session runs `claude -p` in its own process group, so Ctrl+C in the terminal reaches only the
orchestrator, which then decides how to stop the session. The raw stream is appended to the log
file as it arrives; parsed events carry what the loop needs: session id, context fill, limits and
the final result.

Sessions run with `--restricted`: settings files of the user and of the project are ignored and
only `--settings` of the loop applies. The agent may write the settings of the project, and their
hooks would run outside the sandbox. The CLAUDE.md of the working directory is ignored too, so
the adapter puts it into the system prompt itself.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import signal
import subprocess
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

from agent_loop import git

CLAUDE = "claude"
# Hops of `@path` imports the CLI follows.
IMPORT_DEPTH = 5
# USER is needed to find the OAuth token in the keychain; tokens and keys from the shell are
# not passed on.
ENV_KEYS = ("PATH", "HOME", "USER", "LANG", "TMPDIR")
STDERR_TAIL_LINES = 20
# Time a session has to exit after SIGTERM before it is killed.
ORPHAN_GRACE = 10


class AgentError(Exception):
    pass


class ProtocolError(AgentError):
    """The CLI output does not have the shape the adapter expects, run `loop selfcheck`."""


@dataclass(frozen=True)
class Session:
    prompt: str
    model: str
    cwd: Path
    settings: Path
    effort: str | None = None
    # The CLI keeps only the last --append-system-prompt-file, so the files are joined into one,
    # followed by the instructions of the working directory.
    system_files: tuple[Path, ...] = ()
    schema: dict | None = None
    # `[env]` of the repository profile, on top of `ENV_KEYS`.
    env: dict[str, str] = field(default_factory=dict)
    # Known before the start, so the caller can store it before the process may die.
    session_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    resume: bool = False


@dataclass(frozen=True)
class Init:
    session_id: str
    model: str
    version: str
    permission_mode: str


@dataclass(frozen=True)
class Usage:
    context_tokens: int


@dataclass(frozen=True)
class Window:
    utilization: float
    resets_at: int


@dataclass(frozen=True)
class Limits:
    five_hour: Window | None
    seven_day: Window | None


@dataclass(frozen=True)
class Result:
    session_id: str
    is_error: bool
    terminal_reason: str
    text: str | None
    structured_output: dict | None
    num_turns: int
    # `total_cost_usd`: after --resume, the cost of the whole session so far.
    cost_usd: float
    permission_denials: list


type Event = Init | Usage | Limits | Result


def command(session: Session, system_file: Path | None = None) -> list[str]:
    args = [
        CLAUDE,
        "-p",
        "--output-format",
        "stream-json",
        "--verbose",
        "--restricted",
        # `StructuredOutput` is added by the CLI itself when there is a schema.
        "--tools",
        "Bash,Read,Edit,Write",
        "--settings",
        str(session.settings),
        "--permission-prompts",
        "none",
        # Only servers from --mcp-config, that is none: the `.mcp.json` of the repository is
        # the agent's to write, and its servers would run outside the sandbox.
        "--strict-mcp-config",
        "--model",
        session.model,
    ]
    if session.effort:
        args += ["--effort", session.effort]
    if system_file is not None:
        args += ["--append-system-prompt-file", str(system_file)]
    if session.schema is not None:
        args += ["--json-schema", json.dumps(session.schema)]
    args += ["--resume" if session.resume else "--session-id", session.session_id]
    return args


def project_instructions(cwd: Path) -> str:
    """The CLAUDE.md files of the working directory with the files they import by `@path`.

    An imported file is read only if it is committed: the agent may edit CLAUDE.md, and an
    import of `.env` or `~/.ssh/config` would put a file it is not allowed to read into the
    prompt of the next session. A CLAUDE.md that is a link is read under the same rule.
    """
    root = cwd.resolve()
    # Instruction files of the working directory the CLI would read without `--restricted`.
    instructions = ("CLAUDE.md", ".claude/CLAUDE.md")
    own = {root / name for name in instructions}
    imports = re.compile(r"(?:^|\s)@(\S+)")
    # Imports are not looked for in code, the way the CLI does it.
    code = re.compile(r"```.*?```|`[^`\n]*`", re.DOTALL)
    committed: set[Path] = set()
    texts: dict[Path, str] = {}

    def include(path: Path, depth: int) -> None:
        real = path.resolve()
        if real in texts or not real.is_file():
            return
        if real not in own:
            if not committed:
                names = git.git(root, "ls-tree", "-r", "-z", "--name-only", "HEAD")
                committed.update(root / name for name in names.split("\0")[:-1])
            if real not in committed:
                return
        texts[real] = real.read_text(encoding="utf-8")
        if depth < IMPORT_DEPTH:
            for name in imports.findall(code.sub("", texts[real])):
                include(path.parent / Path(name).expanduser(), depth + 1)

    for name in instructions:
        include(root / name, 0)
    return "\n\n".join(
        f"Contents of {path.relative_to(root).as_posix()}:\n\n{text}"
        for path, text in texts.items()
    )


def parse_event(line: str) -> Event | None:
    """Turn one `stream-json` line into an event; lines the loop does not use give None."""
    try:
        raw = json.loads(line)
    except json.JSONDecodeError as exc:
        raise ProtocolError(f"not a JSON line: {line[:200]!r}") from exc
    kind = raw.get("type")
    try:
        if kind == "system" and raw.get("subtype") == "init":
            return Init(
                raw["session_id"], raw["model"], raw["claude_code_version"], raw["permissionMode"]
            )
        # Messages of subagents carry their parent tool call and do not fill the main context.
        if kind == "assistant" and raw["parent_tool_use_id"] is None:
            usage = raw["message"]["usage"]
            return Usage(
                usage["input_tokens"]
                + usage["cache_read_input_tokens"]
                + usage["cache_creation_input_tokens"]
            )
        if kind == "rate_limit_event":
            windows = raw["rate_limit_info"]["unifiedWindows"]
            return Limits(_window(windows.get("five_hour")), _window(windows.get("seven_day")))
        if kind == "result":
            return Result(
                session_id=raw["session_id"],
                is_error=raw["is_error"],
                terminal_reason=raw["terminal_reason"],
                text=raw.get("result"),
                structured_output=raw.get("structured_output"),
                num_turns=raw["num_turns"],
                cost_usd=raw["total_cost_usd"],
                permission_denials=raw["permission_denials"],
            )
    except (KeyError, TypeError) as exc:
        raise ProtocolError(f"{kind} event without {exc}: {line[:200]!r}") from exc
    return None


def _window(raw: dict | None) -> Window | None:
    if raw is None:
        return None
    return Window(raw["utilization"], raw["resetsAt"])


class Process:
    """A running session; use as a context manager so the process never outlives the caller."""

    def __init__(self, session: Session, log_path: Path) -> None:
        self.log_path = log_path
        self.stderr_path = log_path.with_suffix(".stderr")
        self._stopped = False
        # Monotonic time of the last line of the stream, parsed or not: a tool result is a sign
        # of life too.
        self.last_output = time.monotonic()
        log_path.parent.mkdir(parents=True, exist_ok=True)
        rules = [path.read_text(encoding="utf-8") for path in session.system_files]
        instructions = project_instructions(session.cwd)
        if instructions:
            rules.append(instructions)
        system_file = None
        if rules:
            system_file = log_path.with_suffix(".system.md")
            system_file.write_text("\n\n".join(rules), encoding="utf-8")
        # The prompt goes through stdin: in argv it would be visible in `ps` and a leading dash
        # would be read as an option. The file stays next to the log for debugging.
        prompt_file = log_path.with_suffix(".prompt.md")
        prompt_file.write_text(session.prompt, encoding="utf-8")
        env = {key: os.environ[key] for key in ENV_KEYS if key in os.environ} | session.env
        with open(prompt_file, "rb") as stdin, open(self.stderr_path, "ab") as stderr:
            self._proc = subprocess.Popen(
                command(session, system_file),
                cwd=session.cwd,
                env=env,
                stdin=stdin,
                stdout=subprocess.PIPE,
                stderr=stderr,
                encoding="utf-8",
                process_group=0,
            )

    def events(self) -> Iterator[Event]:
        """Yield events until the stream ends; a stream without a result is an error unless
        `stop` was called."""
        has_result = False
        with open(self.log_path, "a", encoding="utf-8") as raw:
            for line in self._proc.stdout:
                self.last_output = time.monotonic()
                raw.write(line)
                raw.flush()
                event = parse_event(line)
                if isinstance(event, Result):
                    has_result = True
                if event is not None:
                    yield event
        code = self._proc.wait()
        if not has_result and not self._stopped:
            raise AgentError(f"claude exited with code {code} without a result: {self._tail()}")

    def stop(self) -> None:
        """Terminate the whole process group: the CLI and the commands it started.

        A group that has not exited within `ORPHAN_GRACE` is killed, otherwise the reader of the
        stream would wait for it forever.
        """
        self._signal(signal.SIGTERM)
        try:
            self._proc.wait(ORPHAN_GRACE)
        except subprocess.TimeoutExpired:
            self.kill()

    def kill(self) -> None:
        self._signal(signal.SIGKILL)

    def _signal(self, signum: int) -> None:
        self._stopped = True
        # The group is gone if the session has already exited.
        with contextlib.suppress(ProcessLookupError):
            os.killpg(self._proc.pid, signum)

    def __enter__(self) -> Process:
        return self

    def __exit__(self, *exc_info: object) -> None:
        if self._proc.poll() is None:
            self.stop()
            self._proc.wait()
        self._proc.stdout.close()

    def _tail(self) -> str:
        lines = self.stderr_path.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[-STDERR_TAIL_LINES:]) or "(stderr is empty)"


def version() -> str:
    try:
        proc = subprocess.run([CLAUDE, "--version"], capture_output=True, text=True, check=True)
    except FileNotFoundError as exc:
        raise AgentError(f"{CLAUDE} is not found in PATH") from exc
    # "2.1.296 (Claude Code)"
    return proc.stdout.split()[0]
