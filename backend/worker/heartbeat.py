"""`worker_heartbeats`: one row per live worker, so the UI can tell when monitoring was off."""

from sqlalchemy import delete, func
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncEngine

from core.models import WorkerHeartbeat


async def mark_alive(engine: AsyncEngine, worker_id: str) -> None:
    """Register the worker on first call; afterwards only move `last_seen_at` forward."""
    stmt = pg_insert(WorkerHeartbeat).values(
        worker_id=worker_id, started_at=func.now(), last_seen_at=func.now()
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[WorkerHeartbeat.worker_id], set_={"last_seen_at": func.now()}
    )
    async with engine.begin() as conn:
        await conn.execute(stmt)


async def remove(engine: AsyncEngine, worker_id: str) -> None:
    """On graceful exit. A crashed worker's row goes stale and is pruned by housekeeping."""
    async with engine.begin() as conn:
        await conn.execute(delete(WorkerHeartbeat).where(WorkerHeartbeat.worker_id == worker_id))
