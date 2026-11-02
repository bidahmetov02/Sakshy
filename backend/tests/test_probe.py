"""Probe + evaluate against the compose emulator, and error classification."""

import ipaddress
import socket
import ssl
from collections.abc import AsyncGenerator
from urllib.parse import urlsplit

import httpx2
import pytest

from core.models import ErrorKind
from core.targets import is_public, private_url_host
from tests.conftest import EMULATOR_HOST
from worker.probe import MAX_BODY_BYTES, ProbeResult, classify, create_client, probe


@pytest.fixture
async def client() -> AsyncGenerator[httpx2.AsyncClient]:
    async with create_client(frozenset({EMULATOR_HOST})) as client:
        yield client


@pytest.fixture
async def guarded() -> AsyncGenerator[httpx2.AsyncClient]:
    """No allowlist: what the worker does for every host but the emulator."""
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


# ---------- Private addresses (SSRF) ----------


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "[::1]", "0.0.0.0"])
async def test_private_targets_are_blocked(
    guarded: httpx2.AsyncClient, emulator: str, host: str
) -> None:
    port = urlsplit(emulator).port
    result = await get(guarded, f"http://{host}:{port}/ok")
    assert not result.ok
    assert result.error_kind is ErrorKind.BLOCKED
    assert result.error_message is not None
    assert result.error_message.endswith("resolves to a private or reserved address")


async def test_a_redirect_to_a_private_address_is_blocked(emulator: str) -> None:
    """The first hop is allowed; the redirect target is checked again when connecting."""
    private = f"http://127.0.0.1:{urlsplit(emulator).port}/ok"
    async with create_client(frozenset({"localhost"})) as client:
        port = urlsplit(emulator).port
        result = await get(client, f"http://localhost:{port}/redirect-to?url={private}")
    assert result.error_kind is ErrorKind.BLOCKED


@pytest.mark.parametrize(
    ("address", "public"),
    [
        ("8.8.8.8", True),
        ("2606:4700:4700::1111", True),
        ("127.0.0.1", False),
        ("10.1.2.3", False),
        ("172.18.0.2", False),  # docker networks
        ("192.168.1.1", False),
        ("169.254.169.254", False),  # cloud metadata
        ("100.64.0.1", False),  # carrier-grade NAT
        ("0.0.0.0", False),
        ("224.0.0.1", False),
        ("::1", False),
        ("fd00::1", False),
        ("fe80::1", False),
        ("::ffff:127.0.0.1", False),  # IPv4-mapped
    ],
)
def test_is_public(address: str, public: bool) -> None:
    assert is_public(ipaddress.ip_address(address)) is public


@pytest.mark.parametrize(
    ("url", "host"),
    [
        ("http://localhost:8000/", "localhost"),
        ("http://api.localhost/", "api.localhost"),
        ("http://10.0.0.5/", "10.0.0.5"),
        ("http://[::1]:8080/", "::1"),
        ("http://169.254.169.254/latest/meta-data/", "169.254.169.254"),
        ("https://example.com/", None),
        ("http://8.8.8.8/", None),
        ("http://emulator:9000/ok", None),  # names are checked by the worker, at connect time
        ("http://Allowed.local/", None),
    ],
)
def test_private_url_host(url: str, host: str | None) -> None:
    assert private_url_host(url, frozenset({"allowed.local"})) == host
