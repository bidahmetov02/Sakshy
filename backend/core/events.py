"""Check events: `pg_notify` on commit, delivered to the API's LISTEN connection."""

import json

from sqlalchemy import Select, func, select

from core.models import MonitorState

NOTIFY_CHANNEL = "check_events"


def notify_check(
    check_id: int, owner_id: int, state: MonitorState, rt_ms: int | None
) -> Select[tuple[str]]:
    """The statement to run inside the transaction: listeners hear it only if it commits.
    Carries `owner_id` so the listener can route it without a query."""
    payload = {"check_id": check_id, "owner_id": owner_id, "state": state.value, "rt_ms": rt_ms}
    return select(func.pg_notify(NOTIFY_CHANNEL, json.dumps(payload)))
