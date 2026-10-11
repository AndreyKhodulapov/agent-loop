import json
import os

import pytest
from fakes import ASSISTANT, INIT, RATE_LIMIT, RESULT, SUBAGENT, USER, read_calls

from agent_loop import agent


def session(tmp_path, **kwargs):
    return agent.Session(
        prompt="-v reply with pong",
        model="haiku",
        cwd=tmp_path,
        settings=tmp_path / "settings.json",
        **kwargs,
    )


def line(event, session_id="s-1"):
    return json.dumps(event).replace("{session_id}", session_id)


def test_parse_main_chain_events():
    assert agent.parse_event(line(INIT)) == agent.Init(
        "s-1", "claude-haiku-5-5", "2.1.296", "dontAsk"
    )
    assert agent.parse_event(line(ASSISTANT)) == agent.Usage(4 + 5232 + 13060)
    assert agent.parse_event(line(RATE_LIMIT)) == agent.Limits(
        agent.Window(0.03, 1791734400), agent.Window(0.64, 1791813600)
    )
    assert agent.parse_event(line(RESULT)) == agent.Result(
        session_id="s-1",
        is_error=False,
        terminal_reason="completed",
        text="pong",
        structured_output=None,
        num_turns=3,
        cost_usd=0.0049,
        permission_denials=[],
    )


@pytest.mark.parametrize("event", [SUBAGENT, USER, {"type": "system", "subtype": "status"}])
def test_events_the_loop_does_not_use_are_skipped(event):
    assert agent.parse_event(line(event)) is None


def test_window_without_data_is_none():
    event = json.loads(line(RATE_LIMIT))
    del event["rate_limit_info"]["unifiedWindows"]["five_hour"]

    limits = agent.parse_event(json.dumps(event))

    assert limits == agent.Limits(None, agent.Window(0.64, 1791813600))


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ("not json", "not a JSON line"),
        (json.dumps({k: v for k, v in RESULT.items() if k != "terminal_reason"}), "result event"),
        (json.dumps({**ASSISTANT, "message": {"content": []}}), "assistant event"),
    ],
)
def test_unexpected_shape_is_a_protocol_error(raw, message):
    with pytest.raises(agent.ProtocolError, match=message):
        agent.parse_event(raw)


def test_command_for_new_and_resumed_session(tmp_path):
    new = session(
        tmp_path,
        effort="high",
        schema={"type": "object"},
        disallowed_tools=("Agent", "WebFetch"),
        session_id="s-1",
    )

    args = agent.command(new, tmp_path / "system.md")

    assert args[:2] == ["claude", "-p"]
    assert args[args.index("--settings") + 1] == str(tmp_path / "settings.json")
    assert args[args.index("--setting-sources") + 1] == "project"
    assert args[args.index("--permission-prompts") + 1] == "none"
    assert args[args.index("--effort") + 1] == "high"
    assert args[args.index("--append-system-prompt-file") + 1] == str(tmp_path / "system.md")
    assert args[args.index("--json-schema") + 1] == '{"type": "object"}'
    assert args[args.index("--disallowed-tools") + 1] == "Agent,WebFetch"
    assert args[-2:] == ["--session-id", "s-1"]

    resumed = agent.command(session(tmp_path, session_id="s-1", resume=True))

    assert resumed[-2:] == ["--resume", "s-1"]
    for flag in ("--effort", "--append-system-prompt-file", "--json-schema", "--disallowed-tools"):
        assert flag not in resumed


def test_process_streams_events_and_keeps_raw_log(tmp_path, fake_claude, monkeypatch):
    calls_path = fake_claude([INIT, ASSISTANT, RATE_LIMIT, USER, RESULT])
    monkeypatch.setenv("ANTHROPIC_API_KEY", "secret")
    first, second = tmp_path / "AGENTS.md", tmp_path / "CLAUDE.md"
    first.write_text("rule one")
    second.write_text("rule two")
    log_path = tmp_path / "runs" / "r1" / "implement.jsonl"
    new = session(tmp_path, system_files=(first, second))

    with agent.Process(new, log_path) as process:
        events = list(process.events())

    assert [type(e) for e in events] == [agent.Init, agent.Usage, agent.Limits, agent.Result]
    assert events[0].session_id == new.session_id
    assert len(log_path.read_text().splitlines()) == 5
    [call] = read_calls(calls_path)
    assert call["prompt"] == "-v reply with pong"
    assert call["system"] == "rule one\n\nrule two"
    assert call["pgid"] == call["pid"]
    assert "ANTHROPIC_API_KEY" not in call["env"]
    assert call["env"]["PATH"] == os.environ["PATH"]


def test_exit_without_result_reports_stderr(tmp_path, fake_claude):
    fake_claude([], stderr="error: unknown option '--session-id'", exit_code=1)

    with (
        agent.Process(session(tmp_path), tmp_path / "s.jsonl") as process,
        pytest.raises(agent.AgentError, match="code 1 without a result.*unknown option"),
    ):
        list(process.events())


def test_stopped_session_ends_without_error(tmp_path, fake_claude):
    fake_claude([INIT, "sleep", RESULT])

    with agent.Process(session(tmp_path), tmp_path / "s.jsonl") as process:
        events = []
        for event in process.events():
            events.append(event)
            process.stop()

    assert [type(e) for e in events] == [agent.Init]


def test_leaving_the_block_kills_the_session(tmp_path, fake_claude):
    calls_path = fake_claude([INIT, "sleep", RESULT])

    with agent.Process(session(tmp_path), tmp_path / "s.jsonl") as process:
        next(process.events())

    [call] = read_calls(calls_path)
    with pytest.raises(ProcessLookupError):
        os.kill(call["pid"], 0)
