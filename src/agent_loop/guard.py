"""`PreToolUse` hook that pauses a session from inside: the CLI asks it before every tool call.

The hook runs as a separate process for each call, so everything it knows comes from files in
the run directory: thresholds (`policy.json`), context fill that the loop copies from the stream
(`live.json`), a stop request of the loop (`stop`) and the shared limits file. What the hook has
decided goes to `guard.json`, which tells the loop why the session ended.

Soft stop: the call is denied with an instruction to write the handoff and end the turn; only
the handoff write and the structured answer pass. A read-only session, the planner's, has no
handoff to write. Near the context limit it is told to answer now with what it has found: a new
session would have to read the repository again from the start. On other stops it is told to
end the turn, and the loop resumes the conversation later.
Hard stop: `continue: false` ends the session after the current call, and the call itself is
denied unless it writes the handoff. A hook that fails blocks the call and records
`guard_error`: a session must not run without its caps.
"""

from __future__ import annotations

import json
import shlex
import sys
import time
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from agent_loop import budget
from agent_loop.agent import Limits
from agent_loop.config import Settings
from agent_loop.files import write_atomic

SETTINGS_FILE = "settings.json"
POLICY_FILE = "policy.json"
LIVE_FILE = "live.json"
STOP_FILE = "stop"
STATE_FILE = "guard.json"
HANDOFF = Path(".loop") / "handoff.md"
BASE_SETTINGS = Path(__file__).with_name("loop-settings.json")
WRITE_RULE = "Edit(./**)"
POLICY_FIELDS = (
    "ctx_soft_tokens",
    "ctx_hard_tokens",
    "start_allowance_5h",
    "reserve_5h",
    "reserve_7d",
)
# Denied calls after which an agent that keeps working is stopped hard.
SOFT_DENIALS = 3
LIMIT_HARD_OVERRUN = 0.02
# The answer of a step with a schema is a tool call too; denying it would lose the answer.
ALWAYS_ALLOWED = ("StructuredOutput",)
HANDOFF_TOOLS = ("Write", "Edit")
WHY = {
    "context": "the context of this session is close to its limit",
    "budget": "the subscription limits are close to their caps",
    "interrupt": "the operator asked to stop",
    "timeout": "the step has run out of time",
}
GUARD_ERROR = "guard_error"
# The exit code of a hook that blocks the call; its stderr goes to the agent.
BLOCK = 2
ERROR_INSTRUCTION = (
    "agent-loop guard failed, every tool call is blocked: {error}. Do not retry, end your turn."
)
INSTRUCTION = (
    "agent-loop is pausing this step: {why}. Do not continue the task and do not call other"
    " tools. Write {handoff} for the next session: what is done, what is left, which files are"
    " in progress and what you were about to do. Then end your turn."
)
READ_ONLY_INSTRUCTION = (
    "agent-loop is pausing this step: {why}. Do not continue and do not call other tools: end"
    " your turn. The loop resumes this conversation later."
)
ANSWER_INSTRUCTION = (
    "agent-loop is ending this step: {why}. Do not continue and do not call other tools: give"
    " your final answer now through the structured output tool, with what you have found so"
    " far, and say in it what you have not checked."
)


@dataclass(frozen=True)
class Stop:
    reason: str
    hard: bool


@dataclass(frozen=True)
class State:
    reason: str
    hard: bool
    denied: int
    # The agent is told to answer now: its answer is the answer of the step.
    answer_now: bool = False


def prepare(
    run_dir: Path, home: Path, settings: Settings, workdir: Path, read_only: bool = False
) -> Path:
    """Get the run directory ready for a new session; returns its `--settings` file.

    Called before every session, also a resumed one: the stop request, the context fill and the
    decisions of the previous session must not leak into it. A `read_only` session cannot write
    the working directory, with Bash neither.
    """
    run_dir.mkdir(parents=True, exist_ok=True)
    session_settings = json.loads(BASE_SETTINGS.read_text(encoding="utf-8"))
    if read_only:
        # The CLI adds an Edit rule in deny to the write denials of the sandbox of Bash.
        permissions = session_settings["permissions"]
        permissions["allow"].remove(WRITE_RULE)
        permissions["deny"].append(WRITE_RULE)
    # -I keeps the working directory out of sys.path: a module of the repository under work
    # must not shadow ours.
    hook = shlex.join([sys.executable, "-I", "-m", "agent_loop.guard", str(run_dir)])
    session_settings["hooks"] = {
        "PreToolUse": [{"matcher": "*", "hooks": [{"type": "command", "command": hook}]}]
    }
    (run_dir / SETTINGS_FILE).write_text(json.dumps(session_settings, indent=2), encoding="utf-8")
    policy = {
        "settings": {name: getattr(settings, name) for name in POLICY_FIELDS},
        "limits_file": str(home / budget.LIMITS_FILE),
        "handoff": None if read_only else str(workdir / HANDOFF),
    }
    (run_dir / POLICY_FILE).write_text(json.dumps(policy), encoding="utf-8")
    write_live(run_dir, 0)
    (run_dir / STOP_FILE).unlink(missing_ok=True)
    (run_dir / STATE_FILE).unlink(missing_ok=True)
    return run_dir / SETTINGS_FILE


def write_live(run_dir: Path, context_tokens: int) -> None:
    write_atomic(run_dir / LIVE_FILE, json.dumps({"context_tokens": context_tokens}))


def request_stop(run_dir: Path, reason: str) -> None:
    """Ask the running session to wrap up: `interrupt` or `timeout`."""
    write_atomic(run_dir / STOP_FILE, reason)


def outcome(run_dir: Path) -> State | None:
    """What the guard did in the last session; None if it let every call through."""
    path = run_dir / STATE_FILE
    if not path.is_file():
        return None
    return State(**json.loads(path.read_text(encoding="utf-8")))


def pressure(
    context_tokens: int,
    limits: Limits | None,
    stop_reason: str | None,
    now: float,
    settings: Settings,
) -> Stop | None:
    """Why the session has to stop, hard reasons first; None if it may go on."""
    overrun = None if limits is None else budget.overrun(limits, now, settings)
    if context_tokens >= settings.ctx_hard_tokens:
        return Stop("context", hard=True)
    if overrun is not None and overrun >= LIMIT_HARD_OVERRUN:
        return Stop("budget", hard=True)
    if stop_reason is not None:
        return Stop(stop_reason, hard=False)
    if context_tokens >= settings.ctx_soft_tokens:
        return Stop("context", hard=False)
    if overrun is not None and overrun >= 0:
        return Stop("budget", hard=False)
    return None


def respond(
    tool_name: str, writes_handoff: bool, stop: Stop, denied: int, handoff: Path | None
) -> tuple[dict | None, State]:
    """Hook output for one call under a stop and the new guard state; None output lets it pass.

    `handoff` is None for a read-only session.
    """
    hard = stop.hard or denied >= SOFT_DENIALS
    answer_now = handoff is None and stop.reason == "context"
    state = State(stop.reason, hard, denied, answer_now)
    if tool_name in ALWAYS_ALLOWED or (writes_handoff and not hard):
        return None, state
    if answer_now:
        instruction = ANSWER_INSTRUCTION.format(why=WHY[stop.reason])
    elif handoff is None:
        instruction = READ_ONLY_INSTRUCTION.format(why=WHY[stop.reason])
    else:
        instruction = INSTRUCTION.format(why=WHY[stop.reason], handoff=handoff)
    if hard:
        output: dict = {"continue": False, "stopReason": f"agent-loop: {stop.reason}"}
        # The handoff write still goes through: continue: false ends the session after it.
        if not writes_handoff:
            output["hookSpecificOutput"] = _deny(instruction)
        return output, state
    return {"hookSpecificOutput": _deny(instruction)}, replace(state, denied=denied + 1)


def main(argv: list[str]) -> int:
    run_dir = Path(argv[0])
    try:
        return _check(run_dir)
    except Exception as exc:
        # The CLI runs the call after any exit code but 2: the session would go on without the
        # context and budget caps, and the loop would not know.
        error = f"{type(exc).__name__}: {exc}"
        print(ERROR_INSTRUCTION.format(error=error), file=sys.stderr)
        state = State(GUARD_ERROR, hard=True, denied=0)
        write_atomic(run_dir / STATE_FILE, json.dumps(asdict(state)))
        return BLOCK


def _check(run_dir: Path) -> int:
    call = json.load(sys.stdin)
    policy = json.loads((run_dir / POLICY_FILE).read_text(encoding="utf-8"))
    settings = Settings(**policy["settings"])
    live = json.loads((run_dir / LIVE_FILE).read_text(encoding="utf-8"))
    stop_file = run_dir / STOP_FILE
    stop_reason = stop_file.read_text(encoding="utf-8").strip() if stop_file.is_file() else None
    limits = budget.load_limits(Path(policy["limits_file"]))
    stop = pressure(live["context_tokens"], limits, stop_reason, time.time(), settings)
    if stop is None:
        return 0
    previous = outcome(run_dir)
    handoff = None if policy["handoff"] is None else Path(policy["handoff"])
    output, state = respond(
        call["tool_name"],
        _writes(call, handoff),
        stop,
        0 if previous is None else previous.denied,
        handoff,
    )
    write_atomic(run_dir / STATE_FILE, json.dumps(asdict(state)))
    if output is not None:
        print(json.dumps(output))
    return 0


def _writes(call: dict, path: Path | None) -> bool:
    if path is None or call["tool_name"] not in HANDOFF_TOOLS:
        return False
    target = Path(call["tool_input"]["file_path"])
    return (Path(call["cwd"]) / target).resolve() == path.resolve()


def _deny(reason: str) -> dict:
    return {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": reason,
    }


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
