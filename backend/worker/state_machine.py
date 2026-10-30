"""The check state machine (`design/state-machine.md`): pure functions, no I/O.

Pause, resume and delete are not handled here: they are API transactions that reset the
state directly. Whether a DOWN/UP email goes out also depends on maintenance windows and
the incident's `down_alert_queued`, which the completion transaction looks up.
"""

import enum
from dataclasses import dataclass
from datetime import datetime, timedelta

from core.models import MonitorState

FAIL_THRESHOLD = 3
RETRY_DELAY = timedelta(seconds=10)


class Event(enum.Enum):
    INCIDENT_OPENED = "incident_opened"
    INCIDENT_RECOVERED = "incident_recovered"


@dataclass(frozen=True)
class Streak:
    """The state-machine part of `check_state`."""

    state: MonitorState
    consecutive_failures: int = 0
    first_failure_at: datetime | None = None


@dataclass(frozen=True)
class Transition:
    streak: Streak
    event: Event | None
    retry: bool  # schedule a confirmation retry instead of the next normal run


def step(current: Streak, ok: bool, run_at: datetime) -> Transition:
    """Apply one run's outcome. `run_at` is when the run happened (the probe start)."""
    if ok:
        if current.state is MonitorState.DOWN:
            return Transition(Streak(MonitorState.UP), Event.INCIDENT_RECOVERED, retry=False)
        # Pending, Up, or a Failing blip: no incident, no email.
        return Transition(Streak(MonitorState.UP), None, retry=False)

    match current.state:
        case MonitorState.DOWN:
            return Transition(current, None, retry=False)
        case MonitorState.FAILING:
            failures = current.consecutive_failures + 1
            if failures >= FAIL_THRESHOLD:
                down = Streak(MonitorState.DOWN, failures, current.first_failure_at)
                return Transition(down, Event.INCIDENT_OPENED, retry=False)
            failing = Streak(MonitorState.FAILING, failures, current.first_failure_at)
            return Transition(failing, None, retry=True)
        case MonitorState.PENDING | MonitorState.UP:
            return Transition(Streak(MonitorState.FAILING, 1, run_at), None, retry=True)


def is_retry(current: Streak) -> bool:
    """A run is a confirmation retry if it was scheduled by a failure streak."""
    return current.state is MonitorState.FAILING


def next_run_at(
    scheduled_at: datetime, interval: timedelta, now: datetime, *, retry: bool, was_retry: bool
) -> datetime:
    """When to run next. All arguments come from the database clock.

    Normal runs stay anchored to the schedule (no drift) and skip slots already in the
    past, so runs missed while the monitor was down are never replayed. Once a retry
    streak ends, the schedule is re-anchored to now.
    """
    if retry:
        return now + min(RETRY_DELAY, interval)
    if was_retry:
        return now + interval
    missed = max(0, (now - scheduled_at) // interval)
    return scheduled_at + (missed + 1) * interval
