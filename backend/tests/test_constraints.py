"""The database itself enforces the rules in design/data-model.md ("Rules the ERD can't show")."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import Executable, delete, func, insert, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from core.models import (
    AlertKind,
    Check,
    CheckGroup,
    CheckResult,
    CheckState,
    CheckStatsHourly,
    Incident,
    IncidentEndReason,
    MaintenanceWindow,
    Notification,
    StatusPage,
    StatusPageItem,
    User,
)

T0 = datetime(2026, 10, 30, 12, tzinfo=UTC)


async def insert_one(engine: AsyncEngine, stmt: Any) -> int:
    async with engine.begin() as conn:
        return (await conn.execute(stmt)).scalar_one()


async def assert_violates(engine: AsyncEngine, constraint: str, stmt: Executable) -> None:
    with pytest.raises(IntegrityError, match=f'"{constraint}"'):
        async with engine.begin() as conn:
            await conn.execute(stmt)


async def add_user(engine: AsyncEngine, email: str) -> int:
    return await insert_one(
        engine, insert(User).values(email=email, password_hash="x").returning(User.id)
    )


async def add_group(engine: AsyncEngine, owner_id: int, name: str = "g") -> int:
    stmt = insert(CheckGroup).values(owner_id=owner_id, name=name, alert_emails=["a@x.io"])
    return await insert_one(engine, stmt.returning(CheckGroup.id))


def check_values(owner_id: int, **overrides: Any) -> dict[str, Any]:
    return {
        "owner_id": owner_id,
        "name": "site",
        "url": "https://example.com",
        "interval_s": 60,
        "timeout_s": Decimal(10),
        "alert_emails": ["a@x.io"],
    } | overrides


async def add_check(engine: AsyncEngine, owner_id: int, **overrides: Any) -> int:
    stmt = insert(Check).values(check_values(owner_id, **overrides)).returning(Check.id)
    return await insert_one(engine, stmt)


async def add_incident(engine: AsyncEngine, check_id: int, **overrides: Any) -> int:
    values = {"check_id": check_id, "started_at": T0, "detected_at": T0} | overrides
    return await insert_one(engine, insert(Incident).values(values).returning(Incident.id))


async def add_status_page(engine: AsyncEngine, owner_id: int, slug: str = "s") -> int:
    stmt = insert(StatusPage).values(owner_id=owner_id, slug=slug, title="t")
    return await insert_one(engine, stmt.returning(StatusPage.id))


@pytest.fixture
async def alice(engine: AsyncEngine) -> int:
    return await add_user(engine, "alice@x.io")


@pytest.fixture
async def bob(engine: AsyncEngine) -> int:
    return await add_user(engine, "bob@x.io")


# ---------- Tenant isolation: composite FKs refuse cross-owner links ----------


async def test_check_cannot_join_another_owners_group(
    engine: AsyncEngine, alice: int, bob: int
) -> None:
    bobs_group = await add_group(engine, bob)
    await assert_violates(
        engine,
        "checks_group_id_owner_id_fkey",
        insert(Check).values(check_values(alice, group_id=bobs_group)),
    )


async def test_maintenance_cannot_target_another_owners_check(
    engine: AsyncEngine, alice: int, bob: int
) -> None:
    bobs_check = await add_check(engine, bob)
    await assert_violates(
        engine,
        "maintenance_windows_check_id_owner_id_fkey",
        insert(MaintenanceWindow).values(
            owner_id=alice, check_id=bobs_check, starts_at=T0, ends_at=T0 + timedelta(hours=1)
        ),
    )


async def test_maintenance_cannot_target_another_owners_group(
    engine: AsyncEngine, alice: int, bob: int
) -> None:
    bobs_group = await add_group(engine, bob)
    await assert_violates(
        engine,
        "maintenance_windows_group_id_owner_id_fkey",
        insert(MaintenanceWindow).values(
            owner_id=alice, group_id=bobs_group, starts_at=T0, ends_at=T0 + timedelta(hours=1)
        ),
    )


async def test_status_page_cannot_list_another_owners_check(
    engine: AsyncEngine, alice: int, bob: int
) -> None:
    page = await add_status_page(engine, alice)
    bobs_check = await add_check(engine, bob)
    await assert_violates(
        engine,
        "status_page_items_check_id_owner_id_fkey",
        insert(StatusPageItem).values(
            owner_id=alice, status_page_id=page, check_id=bobs_check, display_name="d"
        ),
    )


async def test_check_cannot_be_put_on_another_owners_status_page(
    engine: AsyncEngine, alice: int, bob: int
) -> None:
    bobs_page = await add_status_page(engine, bob)
    check = await add_check(engine, alice)
    await assert_violates(
        engine,
        "status_page_items_status_page_id_owner_id_fkey",
        insert(StatusPageItem).values(
            owner_id=alice, status_page_id=bobs_page, check_id=check, display_name="d"
        ),
    )


async def test_deleting_group_ungroups_checks_and_keeps_owner(
    engine: AsyncEngine, alice: int
) -> None:
    group = await add_group(engine, alice)
    check = await add_check(engine, alice, group_id=group)
    async with engine.begin() as conn:
        await conn.execute(delete(CheckGroup).where(CheckGroup.id == group))
        row = (
            await conn.execute(select(Check.group_id, Check.owner_id).where(Check.id == check))
        ).one()
    assert (row.group_id, row.owner_id) == (None, alice)


# ---------- Uniqueness that encodes a business rule ----------


async def test_email_is_unique_case_insensitively(engine: AsyncEngine, alice: int) -> None:
    await assert_violates(
        engine, "users_email_uq", insert(User).values(email="Alice@X.io", password_hash="x")
    )


async def test_group_name_is_unique_per_owner(engine: AsyncEngine, alice: int, bob: int) -> None:
    await add_group(engine, alice, "prod")
    await add_group(engine, bob, "prod")
    await assert_violates(
        engine,
        "check_groups_owner_id_name_key",
        insert(CheckGroup).values(owner_id=alice, name="prod", alert_emails=["a@x.io"]),
    )


async def test_at_most_one_open_incident_per_check(engine: AsyncEngine, alice: int) -> None:
    check = await add_check(engine, alice)
    closed = {"ended_at": T0, "end_reason": IncidentEndReason.RECOVERED}
    await add_incident(engine, check, **closed)
    await add_incident(engine, check, **closed)
    await add_incident(engine, check)
    await assert_violates(
        engine,
        "incidents_one_open_per_check",
        insert(Incident).values(check_id=check, started_at=T0, detected_at=T0),
    )


async def test_notification_rows_cannot_be_duplicated(engine: AsyncEngine, alice: int) -> None:
    incident = await add_incident(engine, await add_check(engine, alice))
    values = {"incident_id": incident, "kind": AlertKind.DOWN, "recipient": "a@x.io"}
    await insert_one(engine, insert(Notification).values(values).returning(Notification.id))
    await insert_one(
        engine,
        insert(Notification).values(values | {"kind": AlertKind.UP}).returning(Notification.id),
    )
    await assert_violates(
        engine,
        "notifications_incident_id_kind_recipient_key",
        insert(Notification).values(values),
    )


async def test_check_appears_once_per_status_page(engine: AsyncEngine, alice: int) -> None:
    page = await add_status_page(engine, alice)
    item = {"owner_id": alice, "status_page_id": page, "check_id": await add_check(engine, alice)}
    stmt = insert(StatusPageItem).values(item | {"display_name": "d"})
    await insert_one(engine, stmt.returning(StatusPageItem.id))
    await assert_violates(engine, "status_page_items_status_page_id_check_id_key", stmt)


async def test_status_page_slug_is_globally_unique(
    engine: AsyncEngine, alice: int, bob: int
) -> None:
    await add_status_page(engine, alice, "acme")
    await assert_violates(
        engine,
        "status_pages_slug_key",
        insert(StatusPage).values(owner_id=bob, slug="acme", title="t"),
    )


# ---------- Check constraints ----------


@pytest.mark.parametrize(
    ("overrides", "constraint"),
    [
        ({"url": "ftp://example.com"}, "checks_url_check"),
        ({"url": "example.com"}, "checks_url_check"),
        ({"interval_s": 29}, "checks_interval_s_check"),
        ({"interval_s": 3601}, "checks_interval_s_check"),
        ({"timeout_s": Decimal(0)}, "checks_timeout_s_check"),
        ({"interval_s": 30, "timeout_s": Decimal(30)}, "checks_check"),
        ({"expected_status": 99}, "checks_expected_status_check"),
        ({"expected_status": 600}, "checks_expected_status_check"),
        ({"alert_emails": []}, "checks_alert_emails_check"),
    ],
)
async def test_check_rejects_invalid_config(
    engine: AsyncEngine, alice: int, overrides: dict[str, Any], constraint: str
) -> None:
    await assert_violates(
        engine, constraint, insert(Check).values(check_values(alice, **overrides))
    )


@pytest.mark.parametrize(
    "overrides",
    [
        {"url": "HTTP://example.com"},
        {"interval_s": 30, "timeout_s": Decimal("29.99")},
        {"interval_s": 3600},
        {"expected_status": 100},
        {"expected_status": 599},
    ],
)
async def test_check_accepts_boundary_config(
    engine: AsyncEngine, alice: int, overrides: dict[str, Any]
) -> None:
    await add_check(engine, alice, **overrides)


async def test_group_needs_an_alert_email(engine: AsyncEngine, alice: int) -> None:
    await assert_violates(
        engine,
        "check_groups_alert_emails_check",
        insert(CheckGroup).values(owner_id=alice, name="g", alert_emails=[]),
    )


@pytest.mark.parametrize(
    ("overrides", "constraint"),
    [
        (
            {"ended_at": T0 - timedelta(seconds=1), "end_reason": IncidentEndReason.RECOVERED},
            "incidents_check",
        ),
        ({"ended_at": T0}, "incidents_check1"),
        ({"end_reason": IncidentEndReason.PAUSED}, "incidents_check1"),
    ],
)
async def test_incident_end_is_consistent(
    engine: AsyncEngine, alice: int, overrides: dict[str, Any], constraint: str
) -> None:
    check = await add_check(engine, alice)
    values = {"check_id": check, "started_at": T0, "detected_at": T0} | overrides
    await assert_violates(engine, constraint, insert(Incident).values(values))


@pytest.mark.parametrize(
    ("target", "ends_at", "constraint"),
    [
        ("both", T0 + timedelta(hours=1), "maintenance_windows_check"),
        ("neither", T0 + timedelta(hours=1), "maintenance_windows_check"),
        ("check", T0, "maintenance_windows_check1"),
    ],
)
async def test_maintenance_window_shape(
    engine: AsyncEngine, alice: int, target: str, ends_at: datetime, constraint: str
) -> None:
    check, group = await add_check(engine, alice), await add_group(engine, alice)
    targets = {
        "both": {"check_id": check, "group_id": group},
        "neither": {},
        "check": {"check_id": check},
    }[target]
    await assert_violates(
        engine,
        constraint,
        insert(MaintenanceWindow).values(owner_id=alice, starts_at=T0, ends_at=ends_at, **targets),
    )


# ---------- Physical design ----------


async def test_results_route_to_daily_partition_or_default(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "CREATE TABLE check_results_20261030 PARTITION OF check_results"
                " FOR VALUES FROM ('2026-10-30 00:00+00') TO ('2026-10-31 00:00+00')"
            )
        )
        # No FK to checks: results reference check ids only logically.
        await conn.execute(
            insert(CheckResult),
            [
                {"check_id": 999, "checked_at": T0, "ok": True},
                {"check_id": 999, "checked_at": T0 + timedelta(days=5), "ok": False},
            ],
        )
        placed = (
            await conn.execute(
                text("SELECT tableoid::regclass::text, checked_at FROM check_results")
            )
        ).all()
        await conn.execute(text("DROP TABLE check_results_20261030"))
    assert sorted(placed, key=lambda r: r[1]) == [
        ("check_results_20261030", T0),
        ("check_results_default", T0 + timedelta(days=5)),
    ]


DEPENDENTS = (
    CheckState,
    CheckStatsHourly,
    Incident,
    Notification,
    MaintenanceWindow,
    StatusPageItem,
)


async def test_hard_deleting_check_cascades_to_everything_it_owns(
    engine: AsyncEngine, alice: int
) -> None:
    check = await add_check(engine, alice)
    incident = await add_incident(engine, check)
    page = await add_status_page(engine, alice)
    async with engine.begin() as conn:
        await conn.execute(insert(CheckState).values(check_id=check))
        await conn.execute(
            insert(CheckStatsHourly).values(
                check_id=check, bucket=T0, total=1, ok=1, rt_count=0, rt_sum_ms=0
            )
        )
        await conn.execute(
            insert(Notification).values(
                incident_id=incident, kind=AlertKind.DOWN, recipient="a@x.io"
            )
        )
        await conn.execute(
            insert(MaintenanceWindow).values(
                owner_id=alice, check_id=check, starts_at=T0, ends_at=T0 + timedelta(hours=1)
            )
        )
        await conn.execute(
            insert(StatusPageItem).values(
                owner_id=alice, status_page_id=page, check_id=check, display_name="d"
            )
        )
        await conn.execute(delete(Check).where(Check.id == check))
        counts = {
            model.__tablename__: (await conn.scalar(select(func.count()).select_from(model)))
            for model in DEPENDENTS
        }
    assert counts == dict.fromkeys(counts, 0)
