from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI

from api.routes import auth, checks, health
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
        yield
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
    for module in (auth, checks, health):
        api.include_router(module.router)
    app.include_router(api)
    return app
