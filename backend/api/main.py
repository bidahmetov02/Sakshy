from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI

from api.realtime import Hub
from api.routes import (
    auth,
    checks,
    events,
    groups,
    health,
    incidents,
    maintenance,
    public,
    status_pages,
)
from api.security import check_secret
from core.db import create_engine
from core.log import configure_logging
from core.settings import Settings, get_settings


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    check_secret(settings)  # refuse to start without a usable JWT secret

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        configure_logging(settings.log_level)
        app.state.engine = create_engine(str(settings.database_url))
        app.state.hub = Hub(app.state.engine)
        await app.state.hub.start()
        yield
        await app.state.hub.stop()
        await app.state.engine.dispose()

    app = FastAPI(
        title="Sakshy",
        lifespan=lifespan,
        docs_url="/api/docs",
        redoc_url=None,
        openapi_url="/api/openapi.json",
    )
    app.state.settings = settings
    api = APIRouter(prefix="/api")
    modules = (auth, checks, groups, maintenance, incidents, status_pages, public, events, health)
    for module in modules:
        api.include_router(module.router)
    app.include_router(api)
    return app
