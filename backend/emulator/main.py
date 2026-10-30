"""Fake target sites for the worker to probe. Stage 1 stub: only a healthy endpoint."""

from fastapi import FastAPI
from fastapi.responses import PlainTextResponse

app = FastAPI(title="Sakshy emulator")


@app.get("/ok", response_class=PlainTextResponse)
async def ok() -> str:
    return "ok"
