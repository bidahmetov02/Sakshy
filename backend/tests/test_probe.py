"""Probe + evaluate against the compose emulator, and error classification."""

import socket
import ssl
from collections.abc import AsyncGenerator

import httpx2
import pytest

from core.models import ErrorKind
from worker.probe import MAX_BODY_BYTES, ProbeResult, classify, create_client, probe


@pytest.fixture
async def client() -> AsyncGenerator[httpx2.AsyncClient]:
    async with create_client() as client:
        yield client


async def get(
    client: httpx2.AsyncClient,
    url: str,
    *,
    timeout_s: float = 5,
    expected_status: int = 200,
    expected_body: str | None = None,
) -> ProbeResult:
    return await probe(client, url, timeout_s, expected_status, expected_body)


async def test_healthy_site(client: httpx2.AsyncClient, emulator: str) -> None:
    result = await get(client, f"{emulator}/ok")
    assert result.ok
    assert result.status_code == 200
    assert result.response_ms is not None
    assert result.error_kind is None


async def test_response_time_is_measured(client: httpx2.AsyncClient, emulator: str) -> None:
    result = await get(client, f"{emulator}/slow?ms=300")
    assert result.ok
    assert result.response_ms is not None
    assert 300 <= result.response_ms < 2000


async def test_unexpected_status(client: httpx2.AsyncClient, emulator: str) -> None:
    result = await get(client, f"{emulator}/status/503")
    assert not result.ok
    assert result.error_kind is ErrorKind.STATUS_MISMATCH
    assert result.status_code == 503
    assert result.response_ms is not None
    assert result.error_message == "expected status 200, got 503"


async def test_expected_non_200_status(client: httpx2.AsyncClient, emulator: str) -> None:
    assert (await get(client, f"{emulator}/status/404", expected_status=404)).ok


async def test_body_contains_expected_text(client: httpx2.AsyncClient, emulator: str) -> None:
    result = await get(client, f"{emulator}/body?text=all+systems+go", expected_body="systems")
    assert result.ok


async def test_body_missing_expected_text(client: httpx2.AsyncClient, emulator: str) -> None:
    result = await get(client, f"{emulator}/body?text=maintenance", expected_body="systems")
    assert not result.ok
    assert result.error_kind is ErrorKind.BODY_MISMATCH
    assert result.status_code == 200


async def test_status_is_checked_before_body(client: httpx2.AsyncClient, emulator: str) -> None:
    result = await get(client, f"{emulator}/status/500", expected_body="anything")
    assert result.error_kind is ErrorKind.STATUS_MISMATCH


async def test_large_body_is_not_read_without_a_body_check(
    client: httpx2.AsyncClient, emulator: str
) -> None:
    assert (await get(client, f"{emulator}/big?kib=4096")).ok


async def test_body_over_the_cap_is_too_large(client: httpx2.AsyncClient, emulator: str) -> None:
    kib = MAX_BODY_BYTES // 1024 + 1
    result = await get(client, f"{emulator}/big?kib={kib}", expected_body="x")
    assert not result.ok
    assert result.error_kind is ErrorKind.TOO_LARGE


async def test_body_at_the_cap_is_read(client: httpx2.AsyncClient, emulator: str) -> None:
    kib = MAX_BODY_BYTES // 1024
    assert (await get(client, f"{emulator}/big?kib={kib}", expected_body="xxx")).ok


async def test_redirects_are_followed(client: httpx2.AsyncClient, emulator: str) -> None:
    result = await get(client, f"{emulator}/redirect/5")
    assert result.ok
    assert result.status_code == 200


async def test_too_many_redirects(client: httpx2.AsyncClient, emulator: str) -> None:
    result = await get(client, f"{emulator}/redirect/6")
    assert not result.ok
    assert result.error_kind is ErrorKind.OTHER


async def test_hanging_site_times_out(client: httpx2.AsyncClient, emulator: str) -> None:
    result = await get(client, f"{emulator}/hang", timeout_s=0.5)
    assert not result.ok
    assert result.error_kind is ErrorKind.TIMEOUT
    assert result.response_ms is None
    assert result.error_message == "no response within 0.5 s"


async def test_slow_body_is_bounded_by_the_hard_timeout(
    client: httpx2.AsyncClient, emulator: str
) -> None:
    # Each chunk arrives in time; only the overall asyncio.timeout catches it.
    result = await get(client, f"{emulator}/slow?ms=2000", timeout_s=0.5, expected_body="slow")
    assert result.error_kind is ErrorKind.TIMEOUT


async def test_dns_failure(client: httpx2.AsyncClient) -> None:
    result = await get(client, "http://does-not-exist.invalid/")
    assert not result.ok
    assert result.error_kind is ErrorKind.DNS


async def test_connection_refused(client: httpx2.AsyncClient) -> None:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    result = await get(client, f"http://127.0.0.1:{port}/")  # nothing listens there now
    assert not result.ok
    assert result.error_kind is ErrorKind.CONNECT


async def test_invalid_url(client: httpx2.AsyncClient) -> None:
    result = await get(client, "http://[::1/")
    assert result.error_kind is ErrorKind.OTHER


def test_error_message_is_truncated() -> None:
    from worker.probe import _failure  # pyright: ignore[reportPrivateUsage]

    message = _failure(ErrorKind.OTHER, "x" * 10_000).error_message
    assert message is not None
    assert len(message) == 500


def _connect_error(cause: BaseException) -> httpx2.ConnectError:
    try:
        try:
            raise cause
        except BaseException as inner:
            raise httpx2.ConnectError(str(inner)) from inner
    except httpx2.ConnectError as exc:
        return exc


@pytest.mark.parametrize(
    ("cause", "kind"),
    [
        (socket.gaierror(8, "nodename nor servname provided"), ErrorKind.DNS),
        (ssl.SSLCertVerificationError("certificate is not trusted"), ErrorKind.TLS),
        (ssl.SSLError("handshake failure"), ErrorKind.TLS),
        (ConnectionRefusedError(61, "refused"), ErrorKind.CONNECT),
    ],
)
def test_connect_errors_are_classified_by_cause(cause: BaseException, kind: ErrorKind) -> None:
    assert classify(_connect_error(cause)) is kind


def test_other_transport_errors_are_other() -> None:
    assert classify(httpx2.RemoteProtocolError("server hung up")) is ErrorKind.OTHER
