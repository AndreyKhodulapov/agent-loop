"""Subscription limits: how much of the 5-hour and the weekly window the loop may have used by now.

Utilization comes from the server (`rate_limit_event`) and is shared by the whole account, so
manual sessions, other loops and the second machine are already counted in it. A window whose
reset time has passed is treated as absent: the server has opened a new one.
"""

from __future__ import annotations

import json
import logging
import math
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path

from agent_loop.agent import Limits, Window
from agent_loop.config import Settings
from agent_loop.files import write_atomic

logger = logging.getLogger(__name__)

LIMITS_FILE = "limits.json"
DAY = 24 * 3600
WEEK = 7 * DAY
FIVE_HOURS = 5 * 3600
# The cap reaches 1 after four hours, so the window can be used up only in its last hour.
RAMP = 4 * 3600


@dataclass(frozen=True)
class Verdict:
    allowed: bool
    # Unix seconds when the caps are expected to leave room for a step; None when allowed.
    resume_at: int | None
    # "5h 31%/48% · 7d 52%/57%": utilization and cap of each window.
    summary: str


def cap_5h(window: Window, now: float, settings: Settings) -> float:
    elapsed = now - (window.resets_at - FIVE_HOURS)
    allowance = settings.start_allowance_5h
    return min(1.0, allowance + (1 - allowance) * elapsed / RAMP) - settings.reserve_5h


def cap_7d(window: Window, now: float, settings: Settings) -> float:
    # The week starts at the reset time (Monday 17:00 on this account), not at midnight.
    day = math.floor((now - (window.resets_at - WEEK)) / DAY) + 1
    return _cap_on_day(day, settings)


def check(limits: Limits, now: float, settings: Settings) -> Verdict:
    """Decide whether a step may start: each window needs `step_margin` of room under its cap."""
    margin = settings.step_margin
    five_hour = _active(limits.five_hour, now)
    seven_day = _active(limits.seven_day, now)
    waits = []
    if five_hour and five_hour.utilization >= cap_5h(five_hour, now, settings) - margin:
        waits.append(_resume_5h(five_hour, settings))
    if seven_day and seven_day.utilization >= cap_7d(seven_day, now, settings) - margin:
        waits.append(_resume_7d(seven_day, settings))
    summary = (
        f"5h {_share(five_hour, cap_5h, now, settings)}"
        f" · 7d {_share(seven_day, cap_7d, now, settings)}"
    )
    return Verdict(not waits, max(waits) if waits else None, summary)


def no_room(home: Path, settings: Settings) -> int | None:
    """Unix seconds when the shared limits are expected to leave room for the next session; None
    when they leave room now or no session has reported them yet."""
    limits = load_limits(home / LIMITS_FILE)
    if limits is None:
        return None
    verdict = check(limits, time.time(), settings)
    if verdict.allowed:
        return None
    resume_at = time.strftime("%Y-%m-%d %H:%M", time.localtime(verdict.resume_at))
    logger.warning("no room for a session in the limits (%s) until %s", verdict.summary, resume_at)
    return verdict.resume_at


def overrun(limits: Limits, now: float, settings: Settings) -> float | None:
    """How far utilization is above its cap in the worse window, negative while under it;
    None when no window is active."""
    overruns = []
    five_hour = _active(limits.five_hour, now)
    if five_hour:
        overruns.append(five_hour.utilization - cap_5h(five_hour, now, settings))
    seven_day = _active(limits.seven_day, now)
    if seven_day:
        overruns.append(seven_day.utilization - cap_7d(seven_day, now, settings))
    return max(overruns, default=None)


def save_limits(path: Path, limits: Limits) -> None:
    """Write the latest limits for every loop on this machine; the file is replaced atomically."""
    previous = load_limits(path)
    if previous is not None:
        # An event without a window must not erase what is known about it.
        limits = Limits(
            limits.five_hour or previous.five_hour, limits.seven_day or previous.seven_day
        )
    write_atomic(path, json.dumps(asdict(limits)))


def load_limits(path: Path) -> Limits | None:
    if not path.is_file():
        return None
    windows = json.loads(path.read_text(encoding="utf-8"))
    return Limits(**{name: None if raw is None else Window(**raw) for name, raw in windows.items()})


def _active(window: Window | None, now: float) -> Window | None:
    if window is None or window.resets_at <= now:
        return None
    return window


def _share(
    window: Window | None,
    cap: Callable[[Window, float, Settings], float],
    now: float,
    settings: Settings,
) -> str:
    if window is None:
        return "-"
    return f"{window.utilization:.0%}/{cap(window, now, settings):.0%}"


def _cap_on_day(day: int, settings: Settings) -> float:
    return day / 7 - settings.reserve_7d


def _resume_5h(window: Window, settings: Settings) -> int:
    needed = window.utilization + settings.step_margin + settings.reserve_5h
    if needed >= 1:
        return window.resets_at
    allowance = settings.start_allowance_5h
    elapsed = (needed - allowance) * RAMP / (1 - allowance)
    # One second past the point where the cap equals the need: the check wants it strictly above.
    return math.floor(window.resets_at - FIVE_HOURS + elapsed) + 1


def _resume_7d(window: Window, settings: Settings) -> int:
    # The cap grows by day, so the first day that leaves room is in the future.
    for day in range(2, 8):
        if window.utilization < _cap_on_day(day, settings) - settings.step_margin:
            return window.resets_at - WEEK + (day - 1) * DAY
    return window.resets_at
