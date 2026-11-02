"""Check events: `pg_notify` on commit, delivered to the API's LISTEN connection.

An event only says "this check changed": `{"check_id", "owner_id"}`. The API reloads the
check with the same query as REST and pushes the result (`api/realtime.py`), so runs,
pauses, deletes, maintenance and group changes all look the same here.
"""

from sqlalchemy import ColumnElement, Select, Text, func, select

from core.models import Check

NOTIFY_CHANNEL = "check_events"


def notify_checks(*where: ColumnElement[bool]) -> Select[tuple[str]]:
    """The statement to run inside the transaction: one notification per matching check,
    which listeners hear only if it commits. Carries `owner_id`, so the listener can tell
    whether anyone is watching without a query."""
    payload = func.json_build_object("check_id", Check.id, "owner_id", Check.owner_id)
    return select(func.pg_notify(NOTIFY_CHANNEL, payload.cast(Text))).where(*where)
