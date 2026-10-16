import json
import os

import pytest
from fakes import ASSISTANT, INIT, RATE_LIMIT, RESULT, SUBAGENT, USER, read_calls

from agent_loop import agent, git


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
        session_id="s-1",
    )

    args = agent.command(new, tmp_path / "system.md")

    assert args[:2] == ["claude", "-p"]
    assert args[args.index("--settings") + 1] == str(tmp_path / "settings.json")
    assert "--restricted" in args
    assert args[args.index("--tools") + 1] == "Bash,Read,Edit,Write"
    assert args[args.index("--permission-prompts") + 1] == "none"
    assert "--strict-mcp-config" in args
    assert args[args.index("--effort") + 1] == "high"
    assert args[args.index("--append-system-prompt-file") + 1] == str(tmp_path / "system.md")
    assert args[args.index("--json-schema") + 1] == '{"type": "object"}'
    assert args[-2:] == ["--session-id", "s-1"]

    resumed = agent.command(session(tmp_path, session_id="s-1", resume=True))

    assert resumed[-2:] == ["--resume", "s-1"]
    for flag in ("--effort", "--append-system-prompt-file", "--json-schema"):
        assert flag not in resumed


def commit(repo, files):
    for name, text in files.items():
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text(text)
    git.git(repo, "add", *files)
    git.git(repo, "commit", "-q", "-m", "docs")


def test_project_instructions_follow_committed_imports(repo):
    commit(
        repo,
        {
            "docs/style.md": "Style. See @../CLAUDE.md and @api.md",
            "docs/api.md": "API.",
            "docs/example.md": "Example.",
        },
    )
    (repo / ".env").write_text("TOKEN=secret")
    (repo / "notes.md").write_text("Uncommitted notes.")
    (repo / "CLAUDE.md").write_text(
        "Rules: @docs/style.md and email me@docs/api.md\n"
        "Not committed: @.env @notes.md @~/.ssh/config @missing.md\n"
        "Inline `@docs/example.md` and a block:\n```\n@docs/example.md\n```\n"
    )
    (repo / ".claude").mkdir()
    (repo / ".claude" / "CLAUDE.md").write_text("More rules.")

    instructions = agent.project_instructions(repo)

    assert [line for line in instructions.splitlines() if line.startswith("Contents of")] == [
        "Contents of CLAUDE.md:",
        "Contents of docs/style.md:",
        "Contents of docs/api.md:",
        "Contents of .claude/CLAUDE.md:",
    ]
    assert "secret" not in instructions and "Uncommitted" not in instructions


def test_project_instructions_stop_at_the_import_depth(repo):
    depth = agent.IMPORT_DEPTH
    commit(repo, {f"f{i}.md": f"File {i}: @f{i + 1}.md" for i in range(1, depth + 2)})
    (repo / "CLAUDE.md").write_text("@f1.md")

    instructions = agent.project_instructions(repo)

    assert f"File {depth}:" in instructions
    assert f"File {depth + 1}:" not in instructions


def test_linked_claude_md_is_read_only_when_committed(repo):
    (repo / ".env").write_text("TOKEN=secret")
    (repo / "CLAUDE.md").symlink_to(repo / ".env")

    assert agent.project_instructions(repo) == ""

    commit(repo, {"AGENTS.md": "Shared rules."})
    (repo / "CLAUDE.md").unlink()
    (repo / "CLAUDE.md").symlink_to("AGENTS.md")

    assert agent.project_instructions(repo) == "Contents of AGENTS.md:\n\nShared rules."


def test_process_streams_events_and_keeps_raw_log(tmp_path, fake_claude, monkeypatch):
    calls_path = fake_claude([INIT, ASSISTANT, RATE_LIMIT, USER, RESULT])
    monkeypatch.setenv("ANTHROPIC_API_KEY", "secret")
    first, second = tmp_path / "AGENTS.md", tmp_path / "context.md"
    first.write_text("rule one")
    second.write_text("rule two")
    (tmp_path / "CLAUDE.md").write_text("rule three")
    log_path = tmp_path / "runs" / "r1" / "implement.jsonl"
    new = session(tmp_path, system_files=(first, second), env={"POSTGRES_PORT": "55432"})

    with agent.Process(new, log_path) as process:
        events = list(process.events())

    assert [type(e) for e in events] == [agent.Init, agent.Usage, agent.Limits, agent.Result]
    assert events[0].session_id == new.session_id
    assert len(log_path.read_text().splitlines()) == 5
    [call] = read_calls(calls_path)
    assert call["prompt"] == "-v reply with pong"
    assert call["system"] == "rule one\n\nrule two\n\nContents of CLAUDE.md:\n\nrule three"
    assert call["pgid"] == call["pid"]
    assert "ANTHROPIC_API_KEY" not in call["env"]
    assert call["env"]["PATH"] == os.environ["PATH"]
    assert call["env"]["POSTGRES_PORT"] == "55432"


def test_exit_without_result_reports_stderr(tmp_path, fake_claude):
    fake_claude([], stderr="error: unknown option '--session-id'", exit_code=1)

    with (
        agent.Process(session(tmp_path), tmp_path / "s.jsonl") as process,
        pytest.raises(agent.AgentError, match="code 1 without a result.*unknown option"),
    ):
        list(process.events())


@pytest.mark.parametrize(
    "scenario",
    [[INIT, "sleep", RESULT], ["ignore-term", INIT, "sleep", RESULT]],
    ids=["exits on SIGTERM", "ignores SIGTERM"],
)
def test_stopped_session_ends_without_error(tmp_path, fake_claude, monkeypatch, scenario):
    fake_claude(scenario)
    monkeypatch.setattr(agent, "ORPHAN_GRACE", 0.5)

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
