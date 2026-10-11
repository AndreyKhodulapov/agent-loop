"""One agent session of a step under the loop's control.

While the session runs, the loop copies what the stream says into the files the guard reads
(`live.json`, the shared `limits.json`) and keeps limit snapshots in the store. It also watches
the time and the terminal: past the step timeout and on the first Ctrl+C it asks the agent to
wrap up through the guard; if the agent has not finished within the grace period, on the second
Ctrl+C or when the stream goes silent, it terminates the session.
"""

from __future__ import annotations

import json
import logging
import signal
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from agent_loop import agent, budget, guard
from agent_loop.config import Config
from agent_loop.store import Step, Store

logger = logging.getLogger(__name__)

RUNS_DIR = "runs"
# Time the agent has to write the handoff after a stop request.
STOP_GRACE = 5 * 60
# The stream is silent while a Bash command runs; longer than the longest command plus this
# margin means the CLI hangs.
IDLE_MARGIN = 2 * 60
# The context fill is logged each time it crosses another multiple of this.
CTX_LOG_STEP = 10_000
# A task that fills the context this many times does not fit one session.
CONTEXT_PAUSES_TO_REPLAN = 2


@dataclass(frozen=True)
class Outcome:
    # completed, paused or failed
    status: str
    # Why the step paused (context, budget, interrupt, timeout, idle, guard_error) or what
    # failed.
    reason: str | None
    # None when the session was terminated before its result.
    result: agent.Result | None
    context_tokens: int


@dataclass(frozen=True)
class Resume:
    # session: continue the paused session with --resume; handoff: a new session starts from
    # the handoff and the work in progress; replan: the task goes back to the planner.
    mode: str
    session_id: str | None = None


@dataclass
class Deadlines:
    """Timing of a session in monotonic seconds."""

    step_timeout: float
    idle_timeout: float
    started: float
    last_output: float
    stop_requested: float | None = None

    def due(self, now: float) -> str | None:
        """`timeout` to ask the agent to wrap up, `kill` when it has not done so in time,
        `idle` when the stream is silent too long."""
        if now - self.last_output >= self.idle_timeout:
            return "idle"
        if self.stop_requested is not None:
            return "kill" if now - self.stop_requested >= STOP_GRACE else None
        return "timeout" if now - self.started >= self.step_timeout else None

    def next_check(self) -> float:
        if self.stop_requested is None:
            next_stop = self.started + self.step_timeout
        else:
            next_stop = self.stop_requested + STOP_GRACE
        return min(self.last_output + self.idle_timeout, next_stop)


def run_dir(home: Path, run_id: str) -> Path:
    return home / RUNS_DIR / run_id


def run(session: agent.Session, cfg: Config, store: Store, run_id: str, step_id: int) -> Outcome:
    """Run the session of a step and record how it ended in the store.

    The caller starts the step in the store with the session id, gets the run directory ready
    with `guard.prepare` and passes the settings file it returns in the session. A CLI that
    exits without a result fails the step and raises.
    """
    directory = run_dir(cfg.home, run_id)
    log_path = directory / f"step-{step_id}.jsonl"
    started = time.monotonic()
    deadlines = Deadlines(
        cfg.settings.step_timeout_min * 60, _idle_timeout(session.settings), started, started
    )
    context_tokens = 0
    result = None
    logger.info("session %s, raw stream in %s", session.session_id, log_path)
    with agent.Process(session, log_path) as process:
        watch = _Watch(process, directory, deadlines)
        previous_handler = signal.signal(signal.SIGINT, watch.interrupt)
        watcher = threading.Thread(target=watch.run, name="step-watch", daemon=True)
        watcher.start()
        try:
            for event in process.events():
                if isinstance(event, agent.Usage):
                    crossed = event.context_tokens // CTX_LOG_STEP > context_tokens // CTX_LOG_STEP
                    context_tokens = event.context_tokens
                    guard.write_live(directory, context_tokens)
                    if crossed:
                        logger.info(_status(context_tokens, cfg))
                elif isinstance(event, agent.Limits):
                    budget.save_limits(cfg.home / budget.LIMITS_FILE, event)
                    store.record_limits(run_id, step_id, event)
                    logger.info(_status(context_tokens, cfg))
                elif isinstance(event, agent.Result):
                    result = event
        except agent.AgentError as exc:
            store.finish_step(step_id, "failed", {"error": str(exc)})
            raise
        finally:
            watch.close()
            watcher.join()
            signal.signal(signal.SIGINT, previous_handler)
    outcome = _outcome(result, guard.outcome(directory), watch.killed, context_tokens)
    store.finish_step(step_id, outcome.status, _detail(outcome))
    _log_end(outcome)
    return outcome


def how_to_resume(steps: list[Step], ctx_soft_tokens: int) -> Resume:
    """How to go on with a task whose last step paused; `steps` are all steps of the task.

    The paused session keeps its context, so it is resumed, unless the context was full: then a
    new session starts from the handoff, and a task that has filled the context again is too big
    for one session.
    """
    last = steps[-1]
    if not _context_full(last, ctx_soft_tokens):
        return Resume("session", last.session_id)
    context_pauses = sum(
        1 for s in steps if s.outcome == "paused" and _context_full(s, ctx_soft_tokens)
    )
    if context_pauses >= CONTEXT_PAUSES_TO_REPLAN:
        return Resume("replan")
    return Resume("handoff")


def _context_full(paused: Step, ctx_soft_tokens: int) -> bool:
    # A step paused for another reason past the soft limit would be stopped for the context on
    # the first tool call of the resumed session. A step of a dead run has no context recorded.
    return (
        paused.detail["reason"] == "context"
        or paused.detail.get("context_tokens", 0) >= ctx_soft_tokens
    )


class _Watch:
    """Stops the session on timeouts (a thread) and on Ctrl+C (the SIGINT handler)."""

    def __init__(self, process: agent.Process, directory: Path, deadlines: Deadlines) -> None:
        self.killed: str | None = None
        self._process = process
        self._directory = directory
        self._deadlines = deadlines
        self._requested: str | None = None
        self._interrupts = 0
        self._closed = False
        # Set when a deadline moves closer or the session is over.
        self._wake = threading.Event()

    def run(self) -> None:
        while not self._closed:
            now = time.monotonic()
            self._deadlines.last_output = self._process.last_output
            due = self._deadlines.due(now)
            if due == "timeout":
                logger.warning("step timeout: asking the agent to write a handoff and stop")
                self._request_stop("timeout", now)
            elif due == "idle":
                logger.warning(
                    "no events for %.0f s: terminating the session", self._deadlines.idle_timeout
                )
                self._kill("idle")
                return
            elif due == "kill":
                logger.warning("the agent has not stopped in %d s: terminating it", STOP_GRACE)
                self._kill(self._requested)
                return
            self._wake.wait(max(0.0, self._deadlines.next_check() - now))
            self._wake.clear()

    def interrupt(self, signum: int, frame: object) -> None:
        self._interrupts += 1
        if self._interrupts == 1:
            logger.warning(
                "Ctrl+C: the agent writes a handoff and stops, press Ctrl+C again to stop it now"
            )
            self._request_stop("interrupt", time.monotonic())
        elif self._interrupts == 2:
            logger.warning("Ctrl+C again: terminating the session")
            self._kill("interrupt")
        else:
            # The second Ctrl+C is still waiting for the session to exit after SIGTERM.
            logger.warning("Ctrl+C once more: killing the session")
            self._process.kill()

    def close(self) -> None:
        self._closed = True
        self._wake.set()

    def _request_stop(self, reason: str, now: float) -> None:
        guard.request_stop(self._directory, reason)
        self._requested = reason
        # The grace period runs from the first request.
        if self._deadlines.stop_requested is None:
            self._deadlines.stop_requested = now
            self._wake.set()

    def _kill(self, reason: str) -> None:
        self.killed = reason
        self._process.stop()


def _outcome(
    result: agent.Result | None, state: guard.State | None, killed: str | None, context: int
) -> Outcome:
    # A guard stop wins over the way the session ended: after a context stop the step must
    # resume in a new session, whatever terminated the old one.
    if state is not None:
        return Outcome("paused", state.reason, result, context)
    if killed is not None:
        return Outcome("paused", killed, result, context)
    if result.is_error:
        return Outcome("failed", result.text or result.terminal_reason, result, context)
    return Outcome("completed", None, result, context)


def _detail(outcome: Outcome) -> dict:
    detail: dict = {"reason": outcome.reason, "context_tokens": outcome.context_tokens}
    result = outcome.result
    if result is not None:
        detail |= {
            "terminal_reason": result.terminal_reason,
            "num_turns": result.num_turns,
            "cost_usd": result.cost_usd,
            "answer": result.structured_output,
        }
    return detail


def _idle_timeout(settings_file: Path) -> float:
    session_settings = json.loads(settings_file.read_text(encoding="utf-8"))
    return int(session_settings["env"]["BASH_MAX_TIMEOUT_MS"]) / 1000 + IDLE_MARGIN


def _status(context_tokens: int, cfg: Config) -> str:
    line = f"ctx {context_tokens // 1000}k/{cfg.settings.ctx_hard_tokens // 1000}k"
    limits = budget.load_limits(cfg.home / budget.LIMITS_FILE)
    if limits is not None:
        line += " · " + budget.check(limits, time.time(), cfg.settings).summary
    return line


def _log_end(outcome: Outcome) -> None:
    if outcome.status == "paused":
        logger.warning("paused: %s", outcome.reason)
    elif outcome.status == "failed":
        logger.error("failed: %s", outcome.reason)
    else:
        result = outcome.result
        logger.info("completed in %d turns, $%.2f", result.num_turns, result.cost_usd)
