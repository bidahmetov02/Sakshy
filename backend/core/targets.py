"""Which addresses a probe may reach. Probes run inside our network, so without this a check
could read internal services (SSRF): the worker refuses private and reserved addresses,
except on hosts listed in `probe_allowed_hosts` (dev: the emulator)."""

import ipaddress
from urllib.parse import urlsplit

BLOCKED_MESSAGE = "resolves to a private or reserved address"

type IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address


def is_public(ip: IPAddress) -> bool:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast


def private_url_host(url: str, allowed_hosts: frozenset[str]) -> str | None:
    """The URL's host if it is obviously private (localhost or a private IP literal), else
    None. A quick check when saving a check, without DNS: the worker checks every
    connection, which also covers names that resolve to private addresses and redirects."""
    host = (urlsplit(url).hostname or "").lower()
    if not host or host in allowed_hosts:
        return None
    if host == "localhost" or host.endswith(".localhost"):
        return host
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return None
    return None if is_public(ip) else host
