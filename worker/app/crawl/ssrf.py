"""SSRF protection: the crawler may only talk to public internet hosts.

A business "website" comes from OpenStreetMap, i.e. from anyone. Without this check a URL like
http://169.254.169.254/ (cloud metadata), http://localhost:27017/ or a hostname that resolves to
10.x.x.x would make our worker probe its own private network.

Checked on EVERY request, including each redirect hop and robots.txt (httpx calls request event
hooks for redirects too):
  - scheme http/https only, no user:password@ in the URL
  - port 80/443 (or 8080/8443, which small business sites sometimes use)
  - every IP the hostname resolves to must be public (not private, loopback, link-local,
    reserved, multicast, unspecified, carrier-grade NAT, IPv4-mapped private...)

Limitation: DNS could change between our check and httpx's own lookup (DNS rebinding). The
window is milliseconds and the attacker would need to control a business's DNS; pinning the IP
would break TLS hostname checks, so we accept it.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Awaitable, Callable

import httpx

ALLOWED_PORTS = {80, 443, 8080, 8443}
HostResolve = Callable[[str, int], Awaitable[list[str]]]


class BlockedAddress(httpx.HTTPError):
    """Raised inside httpx so the fetcher treats it like any other failed request."""

    def __init__(self, message: str):
        super().__init__(message)


def ip_is_public(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip.split("%")[0])
    except ValueError:
        return False
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
        addr = addr.ipv4_mapped
    if addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_reserved or addr.is_multicast \
            or addr.is_unspecified:
        return False
    if isinstance(addr, ipaddress.IPv4Address) and addr in ipaddress.ip_network("100.64.0.0/10"):  # CGNAT
        return False
    return addr.is_global


async def system_resolve(host: str, port: int) -> list[str]:
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return list({info[4][0] for info in infos})


def make_guard(resolve: HostResolve | None = None) -> Callable[[httpx.Request], Awaitable[None]]:
    """resolve=None: the system resolver (looked up at call time, so tests can replace it)."""
    cache: dict[tuple[str, int], bool] = {}

    async def guard(request: httpx.Request) -> None:
        url = request.url
        if url.scheme not in ("http", "https"):
            raise BlockedAddress("Only http/https websites are crawled")
        if url.userinfo:
            raise BlockedAddress("URLs with a username/password are not crawled")
        port = url.port or (443 if url.scheme == "https" else 80)
        if port not in ALLOWED_PORTS:
            raise BlockedAddress(f"Port {port} is not crawled")
        host = (url.host or "").strip("[]").lower()
        if not host or host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
            raise BlockedAddress("Private host name")
        key = (host, port)
        if key not in cache:
            try:
                literal = ipaddress.ip_address(host)
                ips = [str(literal)]
            except ValueError:
                try:
                    ips = await (resolve or system_resolve)(host, port)
                except (OSError, UnicodeError) as exc:
                    raise httpx.ConnectError(f"Couldn't resolve {host}") from exc
            cache[key] = bool(ips) and all(ip_is_public(ip) for ip in ips)
        if not cache[key]:
            raise BlockedAddress("Website points to a private/internal address; not crawled")

    return guard
