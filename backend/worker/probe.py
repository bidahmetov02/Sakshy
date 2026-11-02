"""One HTTP probe and its evaluation: ok, or an `error_kind` with a message."""

import asyncio
import ipaddress
import socket
import ssl
import time
from collections.abc import Iterable
from dataclasses import dataclass

import anyio
import httpcore2
import httpx2
from httpcore2._backends.anyio import AnyIOBackend
from httpcore2._backends.base import SOCKET_OPTION, AsyncNetworkStream

from core.models import ErrorKind
from core.targets import BLOCKED_MESSAGE, is_public

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


class BlockedAddress(OSError):
    """The target resolves to an address probes may not reach."""


class GuardedBackend(AnyIOBackend):
    """Resolves each connection's host itself, refuses it if any address is private or
    reserved, then connects to a vetted IP. Checking at connect time covers every redirect
    hop, and connecting to the IP we checked leaves no room for DNS rebinding. TLS still
    verifies the hostname: httpcore takes SNI from the URL, not from this call."""

    def __init__(self, allowed_hosts: frozenset[str]) -> None:
        self.allowed_hosts = allowed_hosts

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,  # noqa: ASYNC109 (httpcore's interface)
        local_address: str | None = None,
        socket_options: Iterable[SOCKET_OPTION] | None = None,
    ) -> AsyncNetworkStream:
        if host.lower() in self.allowed_hosts:
            return await super().connect_tcp(host, port, timeout, local_address, socket_options)
        try:
            with anyio.fail_after(timeout):
                addresses = await _vetted_addresses(host, port)
        except TimeoutError as exc:
            raise httpcore2.ConnectTimeout(f"resolving {host} timed out") from exc
        except BlockedAddress as exc:
            raise httpcore2.ConnectError(str(exc)) from exc
        except OSError as exc:
            raise httpcore2.ConnectError(str(exc) or type(exc).__name__) from exc
        error = httpcore2.ConnectError(f"{host} has no addresses")
        for address in addresses:  # like a browser: fall back to the next address
            try:
                return await super().connect_tcp(
                    address, port, timeout, local_address, socket_options
                )
            except httpcore2.ConnectError as exc:
                error = exc
        raise error


async def _vetted_addresses(host: str, port: int) -> list[str]:
    infos = await anyio.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    ips = list(dict.fromkeys(ipaddress.ip_address(info[4][0].split("%")[0]) for info in infos))
    if not all(is_public(ip) for ip in ips):
        raise BlockedAddress(f"{host} {BLOCKED_MESSAGE}")
    return [str(ip) for ip in ips]


def create_client(allowed_hosts: frozenset[str] = frozenset()) -> httpx2.AsyncClient:
    """The worker's shared client: a fresh connection per probe, so every run pays the
    full DNS + TCP + TLS cost, as a real visitor would. Private addresses are refused,
    except on `allowed_hosts`."""
    transport = httpx2.AsyncHTTPTransport(
        limits=httpx2.Limits(max_connections=None, max_keepalive_connections=0), trust_env=False
    )
    # httpx doesn't expose the network backend: set it on the pool it built.
    # tests/test_probe.py fails loudly if a new httpx version changes this.
    transport._pool._network_backend = GuardedBackend(allowed_hosts)  # pyright: ignore[reportPrivateUsage]
    return httpx2.AsyncClient(
        transport=transport,
        follow_redirects=True,
        max_redirects=MAX_REDIRECTS,
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
        if isinstance(cause, BlockedAddress):
            return ErrorKind.BLOCKED
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
