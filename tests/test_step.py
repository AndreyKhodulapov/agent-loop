import json
import logging
import signal
from dataclasses import dataclass, replace

import pytest
from fakes import ASSISTANT, INIT, RATE_LIMIT, RESULT, USER, read_calls

from agent_loop import agent, budget, guard, log, protected, step
from agent_loop.agent import Limits, Window
from agent_loop.config import Config, Settings
from agent_loop.store import Step, Store

SETTINGS = Settings()
CONTEXT = 4 + 5232 + 13060
FULL = {
    **ASSISTANT,
    "message": {
        "content": [],
        "usage": {
            "input_tokens": 1000,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": SETTINGS.ctx_hard_tokens,
            "output_tokens": 5,
        },
    },
}


@dataclass
class Env:
    cfg: Config
    store: Store
    run_id: str
    step_id: int
    session: agent.Session

    @property
    def run_dir(self):
        return step.run_dir(self.cfg.home, self.run_id)

    def run(self):
        return step.run(self.session, self.cfg, self.store, self.run_id, self.step_id)

    def step(self):
        [recorded] = self.store.steps(self.store.tasks(self.run_id)[0].id)
        return recorded


@pytest.fixture
def env(tmp_path):
    home = tmp_path / "home"
    workdir = tmp_path / "repo"
    workdir.mkdir()
    db = Store(home / "loop.db")
    run_id = db.create_run(workdir, "feature/x")
    task_id = db.add_task(run_id, "Add x", {"goal": "x"})
    cfg = Config(home, workdir, None, SETTINGS, {})
    protected.save(workdir, step.run_dir(home, run_id))
    settings_file = guard.prepare(step.run_dir(home, run_id), home, SETTINGS, workdir)
    session = agent.Session(prompt="do x", model="haiku", cwd=workdir, settings=settings_file)
    step_id = db.start_step(task_id, "implement", "abc123", [], session.session_id)
    yield Env(cfg, db, run_id, step_id, session)
    db.close()


@pytest.fixture
def stderr_lines(capsys):
    # capsys replaces sys.stderr only for the test body, so the handler is created there.
    log.setup()
    yield lambda: capsys.readouterr().err.splitlines()
    logging.getLogger(log.LOGGER_NAME).handlers.clear()


def test_events_feed_the_guard_and_the_store(env, fake_claude, stderr_lines):
    fake_claude([INIT, ASSISTANT, RATE_LIMIT, {**RESULT, "structured_output": {"status": "done"}}])

    outcome = env.run()

    assert outcome.status == "completed"
    assert outcome.reason is None
    assert outcome.context_tokens == CONTEXT
    assert json.loads((env.run_dir / guard.LIVE_FILE).read_text()) == {"context_tokens": CONTEXT}
    limits = Limits(Window(0.03, 1791734400), Window(0.64, 1791813600))
    assert budget.load_limits(env.cfg.home / budget.LIMITS_FILE) == limits
    [snapshot] = env.store.limit_snapshots(env.run_id)
    assert (snapshot.step_id, snapshot.five_hour) == (env.step_id, 0.03)
    recorded = env.step()
    assert recorded.outcome == "completed"
    assert recorded.session_id == env.session.session_id
    assert recorded.detail["answer"] == {"status": "done"}
    assert recorded.detail["num_turns"] == 3
    assert (env.run_dir / f"step-{env.step_id}.jsonl").is_file()
    assert any("ctx 18k/150k · 5h " in line for line in stderr_lines())


@pytest.mark.parametrize(
    ("scenario", "reason", "hard"),
    [
        ([INIT, ASSISTANT, "interrupt", "tool:Bash", RESULT], "interrupt", False),
        ([INIT, FULL, "tool:Bash", {**RESULT, "terminal_reason": "hook_stopped"}], "context", True),
    ],
    ids=["ctrl+c once", "context over the hard limit"],
)
def test_guard_stop_pauses_the_step(env, fake_claude, scenario, reason, hard):
    calls_path = fake_claude(scenario)
    handler = signal.getsignal(signal.SIGINT)

    outcome = env.run()

    [output] = read_calls(calls_path.parent / "hooks.jsonl")
    assert output["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert ("continue" in output) == hard
    assert outcome.status == "paused"
    assert outcome.reason == reason
    assert outcome.result is not None
    assert env.step().outcome == "paused"
    assert env.step().detail["reason"] == reason
    assert signal.getsignal(signal.SIGINT) is handler


@pytest.mark.parametrize("ignore_term", [[], ["ignore-term"]], ids=["exits", "ignores SIGTERM"])
def test_second_ctrl_c_terminates_the_session(env, fake_claude, monkeypatch, ignore_term):
    fake_claude([INIT, ASSISTANT, *ignore_term, "interrupt", "interrupt", "sleep", RESULT])
    monkeypatch.setattr(agent, "ORPHAN_GRACE", 0.5)

    outcome = env.run()

    assert (env.run_dir / guard.STOP_FILE).read_text() == "interrupt"
    assert outcome == step.Outcome("paused", "interrupt", None, CONTEXT)
    assert env.step().outcome == "paused"


ANSWER = {**RESULT, "structured_output": {"status": "done"}}


@pytest.mark.parametrize(
    "scenario",
    [
        [INIT, ASSISTANT, "interrupt", "tool:StructuredOutput", ANSWER],
        [INIT, ASSISTANT, ANSWER, "interrupt", "interrupt", "sleep"],
    ],
    ids=["ctrl+c while answering", "terminated after the answer"],
)
def test_answer_completes_the_step_despite_a_stop(env, fake_claude, scenario):
    fake_claude(scenario)

    outcome = env.run()

    assert (outcome.status, outcome.reason) == ("completed", "interrupt")
    assert outcome.result.structured_output == {"status": "done"}
    assert env.step().outcome == "completed"


def set_idle_timeout(env, monkeypatch, seconds):
    settings = json.loads(env.session.settings.read_text())
    settings["env"]["BASH_MAX_TIMEOUT_MS"] = "0"
    env.session.settings.write_text(json.dumps(settings))
    monkeypatch.setattr(step, "IDLE_MARGIN", seconds)


def test_silent_stream_is_terminated(env, fake_claude, monkeypatch):
    fake_claude([INIT, "sleep", RESULT])
    set_idle_timeout(env, monkeypatch, 0.2)

    outcome = env.run()

    assert outcome == step.Outcome("paused", "idle", None, 0)


def test_tool_results_keep_the_stream_alive(env, fake_claude, monkeypatch):
    # Each pause is shorter than the idle timeout, all of them together are longer.
    fake_claude([INIT, "sleep:0.6", USER, "sleep:0.6", USER, "sleep:0.6", RESULT])
    set_idle_timeout(env, monkeypatch, 1.2)

    outcome = env.run()

    assert outcome.status == "completed"


def test_error_result_fails_the_step(env, fake_claude):
    fake_claude(
        [
            INIT,
            {
                **RESULT,
                "is_error": True,
                "terminal_reason": "api_error",
                "result": "model not found",
            },
        ]
    )

    outcome = env.run()

    assert (outcome.status, outcome.reason) == ("failed", "model not found")
    assert env.step().outcome == "failed"


def test_session_with_a_project_hook_does_not_start(env, fake_claude):
    calls_path = fake_claude([INIT, RESULT])
    project_settings = env.session.cwd / ".claude" / "settings.json"
    project_settings.parent.mkdir()
    hook = {"type": "command", "command": "touch /tmp/escaped"}
    project_settings.write_text(
        json.dumps({"hooks": {"PreToolUse": [{"matcher": "*", "hooks": [hook]}]}})
    )

    outcome = env.run()

    assert outcome == step.Outcome("failed", step.PROJECT_SETTINGS, None, 0)
    assert not calls_path.exists()
    assert not project_settings.exists()
    recorded = env.step()
    assert recorded.outcome == "failed"
    assert recorded.detail["restored"] == [".claude/settings.json"]


def test_exit_without_result_fails_the_step_and_raises(env, fake_claude):
    fake_claude([INIT], stderr="boom", exit_code=1)

    with pytest.raises(agent.AgentError, match="boom"):
        env.run()

    recorded = env.step()
    assert recorded.outcome == "failed"
    assert "boom" in recorded.detail["error"]


# A step of 60 minutes with 12 minutes of allowed silence, started at 0.
@pytest.mark.parametrize(
    ("last_output", "stop_requested", "now", "due", "next_check"),
    [
        (100, None, 600, None, 820),
        (3500, None, 3599, None, 3600),
        (3500, None, 3600, "timeout", 3600),
        (3500, 3600, 3899, None, 3900),
        (3700, 3600, 3900, "kill", 3900),
        (100, 3600, 3600, "idle", 820),
        (3000, 100, 3500, "kill", 400),
    ],
    ids=[
        "running",
        "just before the timeout",
        "timeout",
        "within the grace period",
        "grace period is over",
        "silent stream",
        "stop requested early",
    ],
)
def test_deadlines(last_output, stop_requested, now, due, next_check):
    deadlines = step.Deadlines(3600, 720, 0, last_output, stop_requested)

    assert deadlines.due(now) == due
    assert deadlines.next_check() == next_check


def ended(outcome, reason, session_id, context_tokens=50_000):
    return Step(
        id=1,
        task_id=1,
        kind="implement",
        checkpoint="abc123",
        session_id=session_id,
        outcome=outcome,
        detail={"reason": reason, "context_tokens": context_tokens},
        started_at="2026-10-11T10:00:00+00:00",
        finished_at="2026-10-11T10:30:00+00:00",
        untracked=[],
    )


@pytest.mark.parametrize(
    ("steps", "expected"),
    [
        ([ended("paused", "budget", "s-1")], step.Resume("session", "s-1")),
        (
            [ended("paused", "context", "s-1"), ended("paused", "interrupt", "s-2")],
            step.Resume("session", "s-2"),
        ),
        ([ended("paused", "context", "s-1")], step.Resume("handoff")),
        (
            [ended("completed", None, "s-1"), ended("paused", "context", "s-2")],
            step.Resume("handoff"),
        ),
        (
            [ended("paused", "context", "s-1"), ended("paused", "context", "s-2")],
            step.Resume("replan", reason="context_pauses"),
        ),
        ([ended("paused", "interrupt", "s-1", 140_000)], step.Resume("handoff")),
        (
            [ended("paused", "context", "s-1"), ended("paused", "timeout", "s-2", 120_000)],
            step.Resume("replan", reason="context_pauses"),
        ),
        (
            [ended("paused", "timeout", "s-1"), ended("paused", "idle", "s-1")],
            step.Resume("session", "s-1"),
        ),
        (
            [
                ended("paused", "timeout", "s-1"),
                ended("paused", "interrupt", "s-1"),
                ended("paused", "idle", "s-1"),
                ended("paused", "guard_error", "s-1"),
            ],
            step.Resume("replan", reason="stall_pauses"),
        ),
    ],
    ids=[
        "budget",
        "interrupt after a context pause",
        "first context pause",
        "context pause after a completed step",
        "second context pause",
        "interrupt past the soft context limit",
        "timeout past the soft context limit after a context pause",
        "second stall",
        "third stall",
    ],
)
def test_how_to_resume(steps, expected):
    assert step.how_to_resume(steps, SETTINGS.ctx_soft_tokens) == expected


def test_how_to_resume_takes_only_a_paused_step():
    failed = replace(ended("failed", None, "s-1"), detail={"error": "no result"})

    with pytest.raises(ValueError, match="has not paused, it is failed"):
        step.how_to_resume([failed], SETTINGS.ctx_soft_tokens)
