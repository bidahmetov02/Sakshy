from typing import cast

from fastapi import APIRouter, Request, Response, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine

from api.schemas import Health
from core.models import WorkerHeartbeat

router = APIRouter(tags=["health"])


@router.get("/health", responses={503: {"model": Health}})
async def health(request: Request, response: Response) -> Health:
    """Whether the API reaches the database, and whether monitoring itself is alive."""
    engine: AsyncEngine = request.app.state.engine
    age = func.extract("epoch", func.now() - func.max(WorkerHeartbeat.last_seen_at))
    try:
        async with engine.connect() as conn:
            # null when no worker has a heartbeat
            seconds = cast(float | None, (await conn.execute(select(age))).scalar_one())
    except Exception:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return Health(db="error", worker_last_seen_s=None)
    return Health(db="ok", worker_last_seen_s=None if seconds is None else int(seconds))
