"""`loop selfcheck`: short real sessions that catch a CLI update breaking the adapter or the guard.

The first session uses every flag of the adapter, the second one resumes it, the third one runs
under the guard hook with a stop request, the way a step is paused. The working directory has
project settings with hooks that none of the sessions may run. A gate command checks the
sandbox of the gate: `sandbox-exec` is deprecated and a macOS update may change it.
"""

from __future__ import annotations

import json
import shlex
from datetime import UTC, datetime
from pathlib import Path

from agent_loop import agent, commands, guard, recovery
from agent_loop.config import Profile, Settings

MODEL = "haiku"
DIR = "selfcheck"
RESULT_FILE = "selfcheck.json"
WORDS = {"project_word": "maple", "appended_word": "copper", "second_word": "willow"}
SETTINGS = {"autoMemoryEnabled": False, "permissions": {"defaultMode": "dontAsk"}}
GUARD_SCHEMA = {
    "type": "object",
    "properties": {"summary": {"type": "string"}},
    "required": ["summary"],
}


def run(home: Path) -> str:
    """Run the sessions and check the event shapes and the guard; all problems are reported in
    one `AgentError`. On success the CLI version is written to `<home>/selfcheck.json`; returns
    the version.
    """
    cli_version = agent.version()
    workdir = home / DIR
    workdir.mkdir(parents=True, exist_ok=True)
    (workdir / "CLAUDE.md").write_text(f"The project word is: {WORDS['project_word']}.\n")
    appended = workdir / "appended.md"
    appended.write_text(f"The appended word is: {WORDS['appended_word']}.\n")
    second = workdir / "second.md"
    second.write_text(f"The second word is: {WORDS['second_word']}.\n")
    settings = workdir / "settings.json"
    settings.write_text(json.dumps(SETTINGS))
    marker = workdir / "project-hook-ran"
    marker.unlink(missing_ok=True)
    (workdir / ".claude").mkdir(exist_ok=True)
    (workdir / ".claude" / "settings.json").write_text(json.dumps(_project_hooks(marker)))

    first = agent.Session(
        prompt="Report the project word, the appended word and the second word"
        " from your instructions.",
        model=MODEL,
        cwd=workdir,
        settings=settings,
        effort="low",
        system_files=(appended, second),
        schema={
            "type": "object",
            "properties": {name: {"type": "string"} for name in WORDS},
            "required": list(WORDS),
        },
    )
    result, problems = _check_session(first, workdir / "first.jsonl")
    if result is not None and _normalized(result.structured_output) != WORDS:
        problems.append(f"first: expected {WORDS}, got {result.structured_output}")

    resumed = agent.Session(
        prompt="Repeat the appended word.",
        model=MODEL,
        cwd=workdir,
        settings=settings,
        session_id=first.session_id,
        resume=True,
    )
    result, more = _check_session(resumed, workdir / "resumed.jsonl")
    problems += more
    if result is not None and WORDS["appended_word"] not in (result.text or "").lower():
        problems.append(f"resumed: resumed session lost the context: {result.text}")

    problems += _check_guard(workdir)
    problems += _check_gate_sandbox(workdir)
    if marker.exists():
        problems.append("a hook from the project settings ran: --restricted no longer ignores them")
    if problems:
        raise agent.AgentError("; ".join(problems))
    (home / RESULT_FILE).write_text(
        json.dumps(
            {"version": cli_version, "checked_at": datetime.now(UTC).isoformat(timespec="seconds")}
        )
    )
    return cli_version


def require(home: Path) -> None:
    """Raise `AgentError` unless the selfcheck has passed on the installed version of the CLI.

    A new version may change the flags or the events, so a run does not start on it unchecked.
    """
    installed = agent.version()
    result = home / RESULT_FILE
    checked = json.loads(result.read_text())["version"] if result.is_file() else None
    if checked != installed:
        raise agent.AgentError(
            f"claude {installed} has not passed the selfcheck, run `loop selfcheck` first"
        )


def _project_hooks(marker: Path) -> dict:
    hook = [{"hooks": [{"type": "command", "command": f"touch {shlex.quote(str(marker))}"}]}]
    return {"hooks": {"SessionStart": hook, "PreToolUse": hook}}


def _check_guard(workdir: Path) -> list[str]:
    """A session with the loop settings and the hook, asked to stop before it starts: the hook
    has to deny the first tool call, and the agent has to write the handoff and answer."""
    run_dir = workdir / "run"
    handoff = workdir / guard.HANDOFF
    handoff.unlink(missing_ok=True)
    (workdir / "notes.txt").write_text("The first line of the notes.\n")
    # The limits file of the selfcheck directory does not exist, so the real limits cannot
    # turn the stop request into a budget stop.
    session_settings = guard.prepare(run_dir, workdir, Settings(), workdir)
    guard.request_stop(run_dir, "interrupt")
    session = agent.Session(
        prompt="Read the file notes.txt and report its first line.",
        model=MODEL,
        cwd=workdir,
        settings=session_settings,
        effort="low",
        schema=GUARD_SCHEMA,
    )
    result, problems = _check_session(session, workdir / "guard.jsonl", find_process=True)
    state = guard.outcome(run_dir)
    if state is None or state.reason != "interrupt":
        problems.append(f"guard: expected the hook to stop on the stop request, got {state}")
    if not handoff.is_file():
        problems.append(f"guard: the agent has not written {guard.HANDOFF}")
    if result is not None and result.structured_output is None:
        problems.append(f"guard: no structured answer after the stop: {result.text}")
    return problems


def _check_gate_sandbox(workdir: Path) -> list[str]:
    """A sandboxed command has to write in its repository and fail to write next to it."""
    repo = workdir / "gate"
    repo.mkdir(exist_ok=True)
    inside = repo / "inside"
    outside = workdir / "outside"
    for path in (inside, outside):
        path.unlink(missing_ok=True)
    command = f"touch {shlex.quote(str(inside))} {shlex.quote(str(outside))}"
    try:
        commands.run(Profile(commands={"test": command}), "test", repo)
    except commands.CommandError as exc:
        if not inside.exists() or "Operation not permitted" not in str(exc):
            return [f"gate: the sandbox does not work: {exc}"]
    except OSError as exc:
        return [f"gate: sandbox-exec cannot run: {exc}"]
    if outside.exists():
        return ["gate: a sandboxed command wrote outside its repository"]
    return []


def _check_session(
    session: agent.Session, log_path: Path, find_process: bool = False
) -> tuple[agent.Result | None, list[str]]:
    """Run a session and check the events every session has; the result is None on an error.

    With `find_process` it also checks that the session is found by its id in the process list,
    the way `loop gc` finds an orphaned session.
    """
    # Logs are appended to, keep only the last check.
    log_path.unlink(missing_ok=True)
    log_path.with_suffix(".stderr").unlink(missing_ok=True)
    name = log_path.stem
    problems = []
    events = []
    with agent.Process(session, log_path) as process:
        for event in process.events():
            # Right after init the CLI is waiting for the model, so it is surely running.
            if (
                find_process
                and isinstance(event, agent.Init)
                and not recovery.session_groups(session.session_id)
            ):
                problems.append(f"{name}: session {session.session_id} is not found in ps")
            events.append(event)
    inits = [e for e in events if isinstance(e, agent.Init)]
    if [e.session_id for e in inits] != [session.session_id]:
        problems.append(f"{name}: expected one init of session {session.session_id}, got {inits}")
    if not any(isinstance(e, agent.Usage) and e.context_tokens > 0 for e in events):
        problems.append(f"{name}: no assistant message with usage")
    if not any(isinstance(e, agent.Limits) and (e.five_hour or e.seven_day) for e in events):
        problems.append(f"{name}: no rate_limit_event with windows")
    result = next(e for e in events if isinstance(e, agent.Result))
    if result.is_error:
        problems.append(f"{name}: error result: {result.text}")
        return None, problems
    return result, problems


def _normalized(words: dict | None) -> dict | None:
    # The model may capitalize a word or keep the period after it.
    if words is None:
        return None
    return {k: str(v).strip().rstrip(".").lower() for k, v in words.items()}
