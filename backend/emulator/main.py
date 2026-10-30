"""Fake target sites for the worker to probe.

Fixed endpoints always behave the same (tests and the load test use them). Named sites
under /sites/{name} behave as their current mode says, and reviewers flip them through
/control/sites/{name} to watch a check go down and recover live. Unknown sites are healthy.
State is in memory: run a single emulator process.
"""

import asyncio
import enum
from collections.abc import AsyncIterator

from fastapi import APIRouter, FastAPI, Path, Query, Response
from fastapi.responses import PlainTextResponse, RedirectResponse, StreamingResponse
from pydantic import BaseModel, Field

app = FastAPI(title="Sakshy emulator")

HANG_S = 3600


@app.get("/ok", response_class=PlainTextResponse)
async def ok() -> str:
    return "ok"


@app.get("/slow", response_class=PlainTextResponse)
async def slow(ms: int = Query(2000, ge=0, le=60_000)) -> str:
    await asyncio.sleep(ms / 1000)
    return "slow"


@app.get("/status/{code}")
async def status(code: int = Path(ge=200, le=599)) -> Response:
    return PlainTextResponse(f"status {code}", status_code=code)


@app.get("/hang")
async def hang() -> None:
    await asyncio.sleep(HANG_S)


@app.get("/body", response_class=PlainTextResponse)
async def body(text: str = "") -> str:
    return text


@app.get("/big")
async def big(kib: int = Query(2048, ge=1, le=64 * 1024)) -> StreamingResponse:
    async def chunks() -> AsyncIterator[bytes]:
        for _ in range(kib):
            yield b"x" * 1024

    return StreamingResponse(chunks(), media_type="text/plain")


@app.get("/redirect/{hops}")
async def redirect(hops: int = Path(ge=0, le=50)) -> RedirectResponse:
    """A chain of `hops` redirects that ends at /ok."""
    return RedirectResponse(f"/redirect/{hops - 1}" if hops > 1 else "/ok", status_code=302)


# ---------- Named sites --------------------------------------------------------


class Mode(enum.StrEnum):
    OK = "ok"
    SLOW = "slow"
    ERROR = "error"
    HANG = "hang"


class Site(BaseModel):
    mode: Mode = Mode.OK
    delay_ms: int = Field(default=3000, ge=0, le=60_000, description="used by `slow`")
    status: int = Field(default=500, ge=200, le=599, description="used by `error`")


sites: dict[str, Site] = {}


@app.get("/sites/{name}", response_class=PlainTextResponse)
async def site(name: str) -> Response:
    config = sites.get(name, Site())
    match config.mode:
        case Mode.OK:
            pass
        case Mode.SLOW:
            await asyncio.sleep(config.delay_ms / 1000)
        case Mode.ERROR:
            return PlainTextResponse(f"{name}: error", status_code=config.status)
        case Mode.HANG:
            await asyncio.sleep(HANG_S)
    return PlainTextResponse(f"{name}: ok")


control = APIRouter(prefix="/control/sites", tags=["control"])


@control.get("")
async def list_sites() -> dict[str, Site]:
    return sites


@control.put("/{name}")
async def set_site(name: str, config: Site) -> Site:
    sites[name] = config
    return config


@control.delete("/{name}", status_code=204)
async def reset_site(name: str) -> None:
    sites.pop(name, None)


app.include_router(control)
