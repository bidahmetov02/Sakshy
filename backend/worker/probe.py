"""One HTTP probe and its evaluation: ok, or an `error_kind` with a message."""

import asyncio
import socket
import ssl
import time
from dataclasses import dataclass

import httpx2

from core.models import ErrorKind

MAX_REDIRECTS = 5
MAX_BODY_BYTES = 1024 * 1024
MAX_ERROR_MESSAGE = 500
USER_AGENT = "Sakshy uptime monitor"


@dataclass(frozen=True)
class ProbeResult:
    ok: bool
    response_ms: int | None = None  # time until the final response's headers arrived
    status_code: int | None = None
    error_kind: ErrorKind | None = None
    error_message: str | None = None


def create_client() -> httpx2.AsyncClient:
    """The worker's shared client: a fresh connection per probe, so every run pays the
    full DNS + TCP + TLS cost, as a real visitor would."""
    return httpx2.AsyncClient(
        follow_redirects=True,
        max_redirects=MAX_REDIRECTS,
        limits=httpx2.Limits(max_connections=None, max_keepalive_connections=0),
        headers={"User-Agent": USER_AGENT},
        trust_env=False,
    )


async def probe(
    client: httpx2.AsyncClient,
    url: str,
    timeout_s: float,
    expected_status: int,
    expected_body: str | None,
) -> ProbeResult:
    started = time.perf_counter()
    try:
        # The hard timeout bounds the whole run (redirects and body included), so it
        # always ends before the check's lease (timeout + 5 s) expires.
        async with (
            asyncio.timeout(timeout_s),
            client.stream("GET", url, timeout=timeout_s) as response,
        ):
            response_ms = round((time.perf_counter() - started) * 1000)
            status = response.status_code
            if status != expected_status:
                return _failure(
                    ErrorKind.STATUS_MISMATCH,
                    f"expected status {expected_status}, got {status}",
                    response_ms,
                    status,
                )
            if expected_body is None:
                return ProbeResult(ok=True, response_ms=response_ms, status_code=status)
            body = await _read_capped(response)
            if body is None:
                return _failure(
                    ErrorKind.TOO_LARGE,
                    f"body is larger than {MAX_BODY_BYTES} bytes",
                    response_ms,
                    status,
                )
            if expected_body not in body.decode(response.encoding or "utf-8", "replace"):
                return _failure(
                    ErrorKind.BODY_MISMATCH,
                    f"body does not contain {expected_body!r}",
                    response_ms,
                    status,
                )
            return ProbeResult(ok=True, response_ms=response_ms, status_code=status)
    except TimeoutError, httpx2.TimeoutException:
        return _failure(ErrorKind.TIMEOUT, f"no response within {timeout_s:g} s")
    except httpx2.HTTPError as exc:
        return _failure(classify(exc), str(exc) or type(exc).__name__)
    except httpx2.InvalidURL as exc:
        return _failure(ErrorKind.OTHER, f"invalid URL: {exc}")


async def _read_capped(response: httpx2.Response) -> bytes | None:
    """The body, or None if it is larger than MAX_BODY_BYTES."""
    body = bytearray()
    async for chunk in response.aiter_bytes():
        body += chunk
        if len(body) > MAX_BODY_BYTES:
            return None
    return bytes(body)


def classify(exc: httpx2.HTTPError) -> ErrorKind:
    """Map a transport error to an error kind by walking its cause chain."""
    if not isinstance(exc, httpx2.ConnectError):
        return ErrorKind.OTHER
    cause: BaseException | None = exc
    while cause is not None:
        if isinstance(cause, socket.gaierror):
            return ErrorKind.DNS
        if isinstance(cause, ssl.SSLError):
            return ErrorKind.TLS
        cause = cause.__cause__ or cause.__context__
    return ErrorKind.CONNECT


def _failure(
    kind: ErrorKind, message: str, response_ms: int | None = None, status: int | None = None
) -> ProbeResult:
    return ProbeResult(
        ok=False,
        response_ms=response_ms,
        status_code=status,
        error_kind=kind,
        error_message=message[:MAX_ERROR_MESSAGE],
    )
