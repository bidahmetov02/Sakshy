"""The transition table in design/state-machine.md, row by row."""

from datetime import UTC, datetime, timedelta

import pytest

from core.models import MonitorState
from worker.state_machine import (
    FAIL_THRESHOLD,
    RETRY_DELAY,
    Event,
    Streak,
    Transition,
    is_retry,
    next_run_at,
    step,
)

T0 = datetime(2026, 10, 30, 12, tzinfo=UTC)
T1 = T0 + timedelta(seconds=10)
PENDING, UP, FAILING, DOWN = (
    MonitorState.PENDING,
    MonitorState.UP,
    MonitorState.FAILING,
    MonitorState.DOWN,
)


@pytest.mark.parametrize("state", [PENDING, UP])
def test_first_failure_starts_a_streak_and_retries(state: MonitorState) -> None:
    assert step(Streak(state), ok=False, run_at=T0) == Transition(
        Streak(FAILING, 1, T0), None, retry=True
    )


def test_failure_below_threshold_retries_again() -> None:
    assert step(Streak(FAILING, 1, T0), ok=False, run_at=T1) == Transition(
        Streak(FAILING, 2, T0), None, retry=True
    )


def test_third_failure_opens_incident_backdated_to_first_failure() -> None:
    current = Streak(FAILING, FAIL_THRESHOLD - 1, T0)
    assert step(current, ok=False, run_at=T1) == Transition(
        Streak(DOWN, FAIL_THRESHOLD, T0), Event.INCIDENT_OPENED, retry=False
    )


def test_failure_while_down_changes_nothing() -> None:
    current = Streak(DOWN, FAIL_THRESHOLD, T0)
    assert step(current, ok=False, run_at=T1) == Transition(current, None, retry=False)


def test_success_while_failing_is_a_blip() -> None:
    assert step(Streak(FAILING, 2, T0), ok=True, run_at=T1) == Transition(
        Streak(UP), None, retry=False
    )


def test_success_while_down_recovers() -> None:
    assert step(Streak(DOWN, 5, T0), ok=True, run_at=T1) == Transition(
        Streak(UP), Event.INCIDENT_RECOVERED, retry=False
    )


@pytest.mark.parametrize("state", [PENDING, UP])
def test_success_from_pending_or_up_is_up(state: MonitorState) -> None:
    assert step(Streak(state), ok=True, run_at=T0) == Transition(Streak(UP), None, retry=False)


def test_only_failing_runs_are_retries() -> None:
    assert [is_retry(Streak(s)) for s in (PENDING, UP, FAILING, DOWN)] == [
        False,
        False,
        True,
        False,
    ]


# ---------- Scheduling ----------

MINUTE = timedelta(minutes=1)


def test_next_run_is_anchored_to_the_schedule_not_to_now() -> None:
    # The run finished 4 s late; the next slot doesn't drift.
    now = T0 + timedelta(seconds=4)
    assert next_run_at(T0, MINUTE, now, retry=False, was_retry=False) == T0 + MINUTE


def test_missed_slots_are_skipped_not_replayed() -> None:
    now = T0 + timedelta(minutes=5, seconds=30)
    assert next_run_at(T0, MINUTE, now, retry=False, was_retry=False) == T0 + 6 * MINUTE


def test_a_slot_equal_to_now_is_skipped() -> None:
    now = T0 + MINUTE
    assert next_run_at(T0, MINUTE, now, retry=False, was_retry=False) == T0 + 2 * MINUTE


def test_retry_runs_after_retry_delay() -> None:
    assert next_run_at(T0, MINUTE, T1, retry=True, was_retry=False) == T1 + RETRY_DELAY


def test_retry_delay_is_capped_by_the_interval() -> None:
    interval = timedelta(seconds=5)
    assert next_run_at(T0, interval, T1, retry=True, was_retry=False) == T1 + interval


def test_schedule_is_reanchored_to_now_after_a_retry_streak() -> None:
    assert next_run_at(T0, MINUTE, T1, retry=False, was_retry=True) == T1 + MINUTE
