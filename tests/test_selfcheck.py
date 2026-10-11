import json
from pathlib import Path

import pytest
from fakes import ASSISTANT, INIT, RATE_LIMIT, RESULT, read_calls

from agent_loop import agent, budget, guard, recovery, selfcheck

WORDS = {"project_word": "Maple.", "appended_word": "copper", "second_word": "willow"}
FIRST = [INIT, ASSISTANT, RATE_LIMIT, {**RESULT, "structured_output": WORDS}]
RESUMED = [INIT, ASSISTANT, RATE_LIMIT, {**RESULT, "result": "It is **copper**."}]
# The agent tries to read, is denied, writes the handoff and answers. The pause after init keeps
# the session alive while it is looked up in ps.
STOPPED = [
    INIT,
    "sleep:0.5",
    ASSISTANT,
    RATE_LIMIT,
    "tool:Read",
    "handoff",
    "tool:StructuredOutput",
    {**RESULT, "structured_output": {"summary": "stopped before reading"}},
]


def test_selfcheck_passes_and_records_version(tmp_path, fake_claude):
    calls_path = fake_claude(new=FIRST, resume=RESUMED, hooked=STOPPED)
    home = tmp_path / "home"

    assert selfcheck.run(home) == "2.1.296"

    assert json.loads((home / selfcheck.RESULT_FILE).read_text())["version"] == "2.1.296"
    first, resumed, stopped = read_calls(calls_path)
    session_id = first["argv"][first["argv"].index("--session-id") + 1]
    assert resumed["argv"][-2:] == ["--resume", session_id]
    assert "maple" in (Path(first["cwd"]) / "CLAUDE.md").read_text()
    assert "copper" in first["system"] and "willow" in first["system"]
    hooks = json.loads(Path(stopped["argv"][stopped["argv"].index("--settings") + 1]).read_text())
    assert "PreToolUse" in hooks["hooks"]
    denied, allowed = read_calls(calls_path.with_name("hooks.jsonl"))
    assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert allowed is None


def test_selfcheck_lists_every_problem(tmp_path, fake_claude, monkeypatch):
    fake_claude(
        new=[INIT, ASSISTANT, {**RESULT, "structured_output": {**WORDS, "second_word": "oak"}}],
        resume=[INIT, ASSISTANT, RATE_LIMIT, {**RESULT, "result": "I do not know."}],
        hooked=[INIT, ASSISTANT, RATE_LIMIT, RESULT],
    )
    monkeypatch.setattr(recovery, "session_groups", lambda session_id: set())
    home = tmp_path / "home"

    with pytest.raises(agent.AgentError) as exc:
        selfcheck.run(home)

    message = str(exc.value)
    assert "first: no rate_limit_event" in message
    assert "first: expected" in message and "'oak'" in message
    assert "resumed: resumed session lost the context" in message
    assert "guard: session" in message and "is not found in ps" in message
    assert "guard: expected the hook to stop on the stop request, got None" in message
    assert f"guard: the agent has not written {guard.HANDOFF}" in message
    assert "guard: no structured answer" in message
    assert not (home / selfcheck.RESULT_FILE).exists()


def test_selfcheck_ignores_the_real_limits(tmp_path, fake_claude):
    fake_claude(new=FIRST, resume=RESUMED, hooked=STOPPED)
    home = tmp_path / "home"
    home.mkdir()
    # Far over any cap: the guard would stop on the budget, not on the stop request.
    over = agent.Window(0.99, 4_000_000_000)
    budget.save_limits(home / budget.LIMITS_FILE, agent.Limits(over, over))

    assert selfcheck.run(home) == "2.1.296"


def test_selfcheck_without_claude(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path))

    with pytest.raises(agent.AgentError, match="claude is not found"):
        selfcheck.run(tmp_path / "home")
