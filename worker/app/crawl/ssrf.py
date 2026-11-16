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

DNS rebinding (security review M2, 2026-11-16): the request hook above resolves the name, but the
connection used to resolve it AGAIN, so a hostile DNS server could answer "public" to the check and
"10.0.0.5" to the connection. Now the connection itself is pinned: PinnedBackend resolves once, checks
every address, and connects to exactly those checked IPs. TLS still verifies the certificate against the
real host name (httpcore passes it as server_hostname), so HTTPS keeps working.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Awaitable, Callable

import httpcore
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


class PinnedBackend(httpcore.AsyncNetworkBackend):
    """Opens TCP connections ONLY to addresses checked in the same step (no second DNS lookup)."""

    def __init__(self, resolve: HostResolve | None = None, inner: httpcore.AsyncNetworkBackend | None = None):
        self.resolve = resolve
        self.inner = inner or httpcore.AnyIOBackend()

    async def connect_tcp(self, host: str, port: int, timeout: float | None = None, local_address: str | None = None,
                          socket_options=None) -> httpcore.AsyncNetworkStream:
        name = host.strip("[]").lower()
        if port not in ALLOWED_PORTS:
            raise BlockedAddress(f"Port {port} is not crawled")
        try:
            ips = [str(ipaddress.ip_address(name))]
        except ValueError:
            try:
                ips = await (self.resolve or system_resolve)(name, port)
            except (OSError, UnicodeError) as exc:
                raise httpcore.ConnectError(f"Couldn't resolve {name}") from exc
        if not ips or not all(ip_is_public(ip) for ip in ips):
            raise BlockedAddress("Website points to a private/internal address; not crawled")
        last: Exception | None = None
        for ip in ips:  # all checked; try them in order like a normal client would
            try:
                return await self.inner.connect_tcp(ip, port, timeout=timeout, local_address=local_address,
                                                    socket_options=socket_options)
            except httpcore.ConnectError as exc:
                last = exc
        raise last or httpcore.ConnectError(f"Couldn't connect to {name}")

    async def connect_unix_socket(self, path: str, timeout: float | None = None, socket_options=None):
        raise BlockedAddress("Unix sockets are never used by the crawler")

    async def sleep(self, seconds: float) -> None:
        await self.inner.sleep(seconds)


def pinned_transport(limits: httpx.Limits, resolve: HostResolve | None = None) -> httpx.AsyncHTTPTransport:
    """httpx's normal transport, with the network layer swapped for PinnedBackend.

    httpx has no public option for this, so the pool's backend attribute is replaced. The test
    test_pinned_transport_is_wired fails loudly if a future httpx/httpcore renames it."""
    transport = httpx.AsyncHTTPTransport(limits=limits, trust_env=False)
    pool = transport._pool  # noqa: SLF001
    if not hasattr(pool, "_network_backend"):
        raise RuntimeError("httpcore changed: can't pin crawler connections (see ssrf.pinned_transport)")
    pool._network_backend = PinnedBackend(resolve)  # noqa: SLF001
    return transport
