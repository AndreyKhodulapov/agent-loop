import json
import subprocess
from dataclasses import replace

import pytest

from agent_loop import budget, guard
from agent_loop.agent import Limits, Window
from agent_loop.config import Settings

# Defaults of the plan: soft 120k, hard 150k context tokens.
SETTINGS = Settings()
HOUR = 3600
FIVE_RESET = 1_791_734_400
FIVE_START = FIVE_RESET - budget.FIVE_HOURS
# Two hours into the 5-hour window the cap is 0.2 + 0.8 * 2 / 4 - 0.10 = 0.5.
NOW = FIVE_START + 2 * HOUR


def five(utilization):
    return Limits(Window(utilization, FIVE_RESET), None)


@pytest.mark.parametrize(
    ("context", "limits", "stop_reason", "expected"),
    [
        (119_999, five(0.49), None, None),
        (119_999, None, None, None),
        (120_000, None, None, guard.Stop("context", hard=False)),
        (0, five(0.51), None, guard.Stop("budget", hard=False)),
        (0, five(0.53), None, guard.Stop("budget", hard=True)),
        (0, None, "interrupt", guard.Stop("interrupt", hard=False)),
        (150_000, None, "interrupt", guard.Stop("context", hard=True)),
        (0, five(0.53), "timeout", guard.Stop("budget", hard=True)),
        (130_000, five(0.5), "timeout", guard.Stop("timeout", hard=False)),
        (0, Limits(Window(0.99, FIVE_START), None), None, None),
    ],
    ids=[
        "under both",
        "no limits",
        "soft context",
        "limit over cap",
        "limit more than 2% over cap",
        "stop request",
        "hard context beats a stop request",
        "hard limit beats a stop request",
        "stop request first among soft reasons",
        "reset window",
    ],
)
def test_pressure(context, limits, stop_reason, expected):
    assert guard.pressure(context, limits, stop_reason, NOW, SETTINGS) == expected


def test_pressure_thresholds_come_from_settings():
    settings = replace(SETTINGS, ctx_soft_tokens=1000, ctx_hard_tokens=2000)

    assert guard.pressure(2000, None, None, NOW, settings) == guard.Stop("context", hard=True)


SOFT = guard.Stop("context", hard=False)
HARD = guard.Stop("budget", hard=True)
HANDOFF = guard.HANDOFF


def test_soft_stop_denies_and_counts():
    output, state = guard.respond("Read", False, SOFT, 1, HANDOFF)

    decision = output["hookSpecificOutput"]
    assert decision["permissionDecision"] == "deny"
    assert "context of this session is close to its limit" in decision["permissionDecisionReason"]
    assert ".loop/handoff.md" in decision["permissionDecisionReason"]
    assert "continue" not in output
    assert state == guard.State("context", hard=False, denied=2)


@pytest.mark.parametrize(
    ("tool", "writes_handoff"), [("Write", True), ("Edit", True), ("StructuredOutput", False)]
)
def test_soft_stop_lets_the_handoff_and_the_answer_pass(tool, writes_handoff):
    output, state = guard.respond(tool, writes_handoff, SOFT, 0, HANDOFF)

    assert output is None
    assert state == guard.State("context", hard=False, denied=0)


def test_agent_that_keeps_working_is_stopped_hard():
    output, state = guard.respond("Bash", False, SOFT, guard.SOFT_DENIALS, HANDOFF)

    assert output["continue"] is False
    assert output["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert state == guard.State("context", hard=True, denied=guard.SOFT_DENIALS)


def test_hard_stop_denies_the_call():
    output, state = guard.respond("Bash", False, HARD, 0, HANDOFF)

    assert output["continue"] is False
    assert output["stopReason"] == "agent-loop: budget"
    assert output["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert state == guard.State("budget", hard=True, denied=0)


def test_hard_stop_lets_the_handoff_write_finish():
    output, _ = guard.respond("Write", True, HARD, 0, HANDOFF)

    assert output == {"continue": False, "stopReason": "agent-loop: budget"}


def test_hard_stop_lets_the_answer_pass():
    output, state = guard.respond("StructuredOutput", False, HARD, 0, HANDOFF)

    assert output is None
    assert state.hard


@pytest.fixture
def run_dir(tmp_path):
    workdir = tmp_path / "repo"
    workdir.mkdir()
    run_dir = tmp_path / "home" / "runs" / "r1"
    settings_path = guard.prepare(run_dir, tmp_path / "home", SETTINGS, workdir)
    assert settings_path == run_dir / guard.SETTINGS_FILE
    return run_dir


def run_hook(run_dir, tool="Read", tool_input=None, cwd=None):
    """Run the hook the way the CLI does: a new process with the call on stdin."""
    workdir = run_dir.parents[2] / "repo"
    call = {
        "session_id": "s-1",
        "hook_event_name": "PreToolUse",
        "tool_name": tool,
        "tool_input": tool_input or {"file_path": str(workdir / "app.py")},
        "cwd": str(cwd or workdir),
    }
    [entry] = json.loads((run_dir / guard.SETTINGS_FILE).read_text())["hooks"]["PreToolUse"]
    return subprocess.run(
        entry["hooks"][0]["command"],
        shell=True,
        input=json.dumps(call),
        capture_output=True,
        text=True,
        cwd=workdir,
    )


def hook(run_dir, tool="Read", tool_input=None, cwd=None):
    """The output of a hook that has exited normally."""
    proc = run_hook(run_dir, tool, tool_input, cwd)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout) if proc.stdout else None


def test_prepared_settings_keep_the_base_and_add_the_hook(run_dir):
    settings = json.loads((run_dir / guard.SETTINGS_FILE).read_text())
    base = json.loads(guard.BASE_SETTINGS.read_text())

    assert {k: v for k, v in settings.items() if k != "hooks"} == base
    assert settings["autoMemoryEnabled"] is False
    [entry] = settings["hooks"]["PreToolUse"]
    assert entry["matcher"] == "*"


def test_hook_lets_calls_pass_without_pressure(run_dir):
    assert hook(run_dir) is None
    assert guard.outcome(run_dir) is None


def test_hook_reads_context_and_counts_denials_across_calls(run_dir):
    guard.write_live(run_dir, 125_000)

    outputs = [hook(run_dir) for _ in range(guard.SOFT_DENIALS + 1)]

    assert [o.get("continue", True) for o in outputs] == [True] * guard.SOFT_DENIALS + [False]
    assert guard.outcome(run_dir) == guard.State("context", hard=True, denied=guard.SOFT_DENIALS)


def test_hook_lets_the_handoff_through_by_relative_path(run_dir):
    guard.request_stop(run_dir, "interrupt")

    output = hook(run_dir, "Write", {"file_path": ".loop/handoff.md", "content": "done: x"})

    assert output is None
    assert guard.outcome(run_dir) == guard.State("interrupt", hard=False, denied=0)
    assert hook(run_dir, "Write", {"file_path": "other.md", "content": "x"}) is not None


def test_hook_reads_the_shared_limits(run_dir, tmp_path):
    budget.save_limits(tmp_path / "home" / budget.LIMITS_FILE, Limits(None, Window(0.99, 2**40)))

    output = hook(run_dir, "Bash", {"command": "pytest"})

    assert output["continue"] is False
    assert guard.outcome(run_dir).reason == "budget"


def test_prepare_clears_the_previous_session(run_dir, tmp_path):
    guard.write_live(run_dir, 140_000)
    guard.request_stop(run_dir, "timeout")
    hook(run_dir)

    guard.prepare(run_dir, tmp_path / "home", SETTINGS, tmp_path / "repo")

    assert hook(run_dir) is None
    assert guard.outcome(run_dir) is None
    assert sorted(p.name for p in run_dir.iterdir()) == sorted(
        [guard.SETTINGS_FILE, guard.POLICY_FILE, guard.LIVE_FILE]
    )


def test_read_only_session_is_told_only_to_end_its_turn(run_dir, tmp_path):
    workdir = tmp_path / "repo"
    guard.prepare(run_dir, tmp_path / "home", SETTINGS, workdir, read_only=True)
    guard.request_stop(run_dir, "interrupt")

    output = hook(run_dir, "Write", {"file_path": str(workdir / guard.HANDOFF)})

    reason = output["hookSpecificOutput"]["permissionDecisionReason"]
    assert reason == guard.READ_ONLY_INSTRUCTION.format(why=guard.WHY["interrupt"])


def test_hook_does_not_import_from_the_working_directory(run_dir):
    # A module of the repository under work named like a stdlib one must not be picked up.
    workdir = run_dir.parents[2] / "repo"
    (workdir / "json.py").write_text("raise SystemExit('shadowed')\n")

    assert hook(run_dir) is None


def test_failing_hook_blocks_the_call_and_pauses_the_step(run_dir):
    (run_dir / guard.LIVE_FILE).unlink()

    proc = run_hook(run_dir)

    assert proc.returncode == guard.BLOCK
    assert "guard failed, every tool call is blocked" in proc.stderr
    assert guard.LIVE_FILE in proc.stderr
    assert guard.outcome(run_dir) == guard.State(guard.GUARD_ERROR, hard=True, denied=0)
