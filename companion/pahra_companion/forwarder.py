"""LAN DNS forwarder ("laptop relay") for TVs that only take a plain IPv4 DNS server.

LG webOS and Samsung Tizen TVs cannot do DoT/DoH, and behind CGNAT QuietNet cannot tell a
home's plain-DNS traffic apart. The relay fixes both. It listens on this laptop's LAN IP,
UDP and TCP port 53. It looks up the query's source IP in a {tv_ip: client_id} map and
forwards the raw query over DoH (RFC 8484 POST) to https://<dns_hostname>/dns-query/<client_id>.
That way QuietNet (AdGuard Home) sees each TV under its own ClientID.

Rules:
  * Only mapped source IPs are answered. Everything else gets REFUSED and is counted.
  * No caching: QuietNet must see every query.
  * The upstream query is sent with id 0 (RFC 8484 §4.1); the original id is restored in the reply.
  * Any upstream failure (timeout, HTTP error, bad body) gives SERVFAIL.
  * UDP replies larger than the client accepts are cut to a TC=1 header so the client retries over TCP.

The relay lives in the companion process, so it stops when the backend stops. The backend
re-issues start_dns_forwarder on startup.
"""

from __future__ import annotations

import asyncio
import errno
import logging
import re
import struct
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import dns.exception
import dns.flags
import dns.message
import dns.rcode
import httpx

log = logging.getLogger(__name__)

CLIENT_ID_RE = re.compile(r"^[a-z0-9-]{1,63}$")
UPSTREAM_TIMEOUT = 4.0
MAX_MAPPINGS = 256
MAX_INFLIGHT = 128
TCP_IDLE_TIMEOUT = 15.0
DOH_CONTENT_TYPE = "application/dns-message"

RCODE_SERVFAIL = 2
RCODE_REFUSED = 5


class BindError(OSError):
    def __init__(self, message: str, hint: str):
        super().__init__(message)
        self.hint = hint


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def upstream_url(dns_hostname: str, client_id: str) -> str:
    return f"https://{dns_hostname}/dns-query/{client_id}"


def firewall_hint(port: int) -> str:
    return (
        f"Windows Firewall must allow inbound UDP and TCP {port} on the Private profile, and the home "
        "network must be set to Private. Run once in an administrator PowerShell: "
        f'New-NetFirewallRule -DisplayName "QuietNet DNS relay (UDP {port})" -Direction Inbound -Protocol UDP '
        f"-LocalPort {port} -Action Allow -Profile Private; "
        f'New-NetFirewallRule -DisplayName "QuietNet DNS relay (TCP {port})" -Direction Inbound -Protocol TCP '
        f"-LocalPort {port} -Action Allow -Profile Private"
    )


def bind_hint(ip: str, port: int, exc: OSError) -> str:
    winerr = getattr(exc, "winerror", None)
    if exc.errno in (errno.EADDRINUSE,) or winerr in (10048,):
        why = f"Something else is already using {ip}:{port}."
    elif exc.errno in (errno.EACCES, errno.EPERM) or winerr in (10013,):
        why = f"Access to {ip}:{port} was denied (another program holds it exclusively, or the port needs admin rights)."
    elif exc.errno == errno.EADDRNOTAVAIL or winerr in (10049,):
        why = f"{ip} is not an address of this computer any more (did the laptop change networks?)."
    else:
        why = f"Binding {ip}:{port} failed."
    return (
        why + " Usual culprits on Windows: Mobile Hotspot / Internet Connection Sharing (service SharedAccess), "
        "Docker Desktop or WSL DNS, or another local DNS server. Turn that off and try again. "
        "On macOS/Linux, port 53 needs root."
    )


# --------------------------------------------------------------------------- DNS wire helpers


def error_reply(query: bytes, rcode: int) -> bytes | None:
    """A reply to `query` with only the question and the given rcode. None if the query is not DNS."""
    if len(query) < 12:
        return None
    try:
        q = dns.message.from_wire(query)
        r = dns.message.make_response(q)
        r.set_rcode(rcode)
        return r.to_wire()
    except Exception:  # noqa: BLE001 - fall back to a bare header
        (qid, flags) = struct.unpack("!HH", query[:4])
        if flags & 0x8000:  # it is itself a response: do not answer
            return None
        opcode = flags & 0x7800
        rd = flags & 0x0100
        return struct.pack("!HHHHHH", qid, 0x8000 | opcode | rd | 0x0080 | (rcode & 0xF), 0, 0, 0, 0)


def udp_limit(query: bytes) -> int:
    """Largest UDP reply the client accepts: its EDNS payload size, else 512."""
    try:
        q = dns.message.from_wire(query)
    except Exception:  # noqa: BLE001
        return 512
    if q.edns >= 0:
        return max(512, min(int(q.payload), 65535))
    return 512


def truncated_reply(query: bytes, full_reply: bytes) -> bytes:
    """Header + question with TC=1 and the upstream rcode, so the client retries over TCP."""
    q = dns.message.from_wire(query)
    r = dns.message.make_response(q)
    try:
        r.set_rcode(dns.message.from_wire(full_reply).rcode())
    except Exception:  # noqa: BLE001
        pass
    r.flags |= dns.flags.TC
    return r.to_wire()


# --------------------------------------------------------------------------- forwarder


@dataclass
class _ClientStats:
    queries: int = 0
    last_ts: str | None = None


@dataclass
class _Stats:
    queries: int = 0
    forwarded: int = 0
    refused: int = 0
    errors: int = 0
    per_client: dict[str, _ClientStats] = field(default_factory=dict)

    def as_dict(self) -> dict[str, int]:
        return {"queries": self.queries, "forwarded": self.forwarded, "refused": self.refused, "errors": self.errors}


class _UDPProtocol(asyncio.DatagramProtocol):
    def __init__(self, fwd: "DnsForwarder"):
        self.fwd = fwd
        self.transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport) -> None:  # type: ignore[override]
        self.transport = transport

    def datagram_received(self, data: bytes, addr) -> None:  # type: ignore[override]
        self.fwd._spawn(self._answer(data, addr))

    async def _answer(self, data: bytes, addr) -> None:
        try:
            reply = await self.fwd.handle(data, addr[0], transport="udp")
            if reply is not None and self.transport is not None and not self.transport.is_closing():
                self.transport.sendto(reply, addr)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("dns relay failed to answer %s", addr[0])

    def error_received(self, exc: Exception) -> None:  # e.g. WSAECONNRESET after ICMP unreachable
        log.debug("dns relay udp error: %s", exc)


class DnsForwarder:
    """One relay per process. `transport` lets tests inject an httpx mock transport."""

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None, timeout: float = UPSTREAM_TIMEOUT):
        self._transport = transport
        self._timeout = timeout
        self._udp: asyncio.DatagramTransport | None = None
        self._tcp: asyncio.base_events.Server | None = None
        self._http: httpx.AsyncClient | None = None
        self._tasks: set[asyncio.Task] = set()
        self._sem: asyncio.Semaphore | None = None
        self.listen_ip: str | None = None
        self.port: int | None = None
        self.dns_hostname: str | None = None
        self.mappings: dict[str, str] = {}
        self.stats = _Stats()
        self.started_at: str | None = None

    # ----- lifecycle

    @property
    def running(self) -> bool:
        return self._udp is not None

    async def start(self, listen_ip: str, port: int, mappings: dict[str, str], dns_hostname: str) -> dict[str, Any]:
        """Start, or replace the mapping set. Rebinds only if the address or port changes."""
        if self.running and (listen_ip, port) == (self.listen_ip, self.port):
            self.mappings = dict(mappings)
            self.dns_hostname = dns_hostname
            log.info("dns relay mappings replaced: %d clients", len(self.mappings))
            return {"rebound": False}
        if self.running:
            await self._close_sockets()
        else:
            self.stats = _Stats()
        loop = asyncio.get_running_loop()
        try:
            udp, _ = await loop.create_datagram_endpoint(lambda: _UDPProtocol(self), local_addr=(listen_ip, port))
        except OSError as exc:
            raise BindError(f"could not bind UDP {listen_ip}:{port}: {exc}", bind_hint(listen_ip, port, exc)) from exc
        bound_port = udp.get_extra_info("sockname")[1]
        try:
            tcp = await asyncio.start_server(self._handle_tcp, host=listen_ip, port=bound_port)
        except OSError as exc:
            udp.close()
            raise BindError(f"could not bind TCP {listen_ip}:{bound_port}: {exc}", bind_hint(listen_ip, bound_port, exc)) from exc
        self._udp, self._tcp = udp, tcp
        if self._http is None:
            self._http = httpx.AsyncClient(
                timeout=self._timeout,
                transport=self._transport,
                headers={"accept": DOH_CONTENT_TYPE, "content-type": DOH_CONTENT_TYPE},
                follow_redirects=False,
            )
        self._sem = asyncio.Semaphore(MAX_INFLIGHT)
        self.listen_ip, self.port = listen_ip, bound_port
        self.mappings = dict(mappings)
        self.dns_hostname = dns_hostname
        self.started_at = now_iso()
        log.info("dns relay listening on %s:%d for %d clients", listen_ip, bound_port, len(self.mappings))
        return {"rebound": True}

    async def _close_sockets(self) -> None:
        if self._udp is not None:
            self._udp.close()
            self._udp = None
        if self._tcp is not None:
            self._tcp.close()
            try:
                await asyncio.wait_for(self._tcp.wait_closed(), timeout=2)
            except asyncio.TimeoutError:
                pass
            self._tcp = None

    async def stop(self) -> None:
        await self._close_sockets()
        for t in list(self._tasks):
            t.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        if self._http is not None:
            await self._http.aclose()
            self._http = None
        log.info("dns relay stopped")

    def status(self) -> dict[str, Any]:
        return {
            "running": self.running,
            "listen_ip": self.listen_ip if self.running else None,
            "port": self.port if self.running else None,
            "mappings": [{"ip": ip, "client_id": cid} for ip, cid in sorted(self.mappings.items())] if self.running else [],
            "stats": self.stats.as_dict(),
            "per_client": {cid: {"queries": s.queries, "last_ts": s.last_ts} for cid, s in sorted(self.stats.per_client.items())},
        }

    def _spawn(self, coro) -> None:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # ----- query handling

    async def handle(self, query: bytes, src_ip: str, transport: str = "udp") -> bytes | None:
        if len(query) < 12 or query[2] & 0x80:  # too short, or a response rather than a query
            return None
        self.stats.queries += 1
        client_id = self.mappings.get(src_ip)
        if client_id is None:
            self.stats.refused += 1
            return error_reply(query, RCODE_REFUSED)
        cs = self.stats.per_client.setdefault(client_id, _ClientStats())
        cs.queries += 1
        cs.last_ts = now_iso()
        try:
            reply = await self._forward(query, client_id)
        except Exception as exc:  # noqa: BLE001 - any upstream failure is SERVFAIL
            self.stats.errors += 1
            log.info("dns relay upstream error for %s: %s", client_id, exc)
            return error_reply(query, RCODE_SERVFAIL)
        self.stats.forwarded += 1
        if transport == "udp" and len(reply) > udp_limit(query):
            try:
                return truncated_reply(query, reply)
            except Exception:  # noqa: BLE001
                return error_reply(query, RCODE_SERVFAIL)
        return reply

    async def _forward(self, query: bytes, client_id: str) -> bytes:
        if self._http is None or self.dns_hostname is None:
            raise RuntimeError("relay is not running")
        upstream_query = b"\x00\x00" + query[2:]
        sem = self._sem or asyncio.Semaphore(MAX_INFLIGHT)
        async with sem:
            resp = await self._http.post(upstream_url(self.dns_hostname, client_id), content=upstream_query)
        if resp.status_code != 200:
            raise RuntimeError(f"upstream HTTP {resp.status_code}")
        ctype = resp.headers.get("content-type", "").split(";")[0].strip().lower()
        if ctype != DOH_CONTENT_TYPE:
            raise RuntimeError(f"upstream content-type {ctype or 'missing'}")
        body = resp.content
        if len(body) < 12 or not body[2] & 0x80:
            raise RuntimeError("upstream body is not a DNS response")
        return query[:2] + body[2:]

    async def _handle_tcp(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        src_ip = peer[0] if peer else ""
        try:
            while True:
                try:
                    header = await asyncio.wait_for(reader.readexactly(2), timeout=TCP_IDLE_TIMEOUT)
                    (length,) = struct.unpack("!H", header)
                    query = await asyncio.wait_for(reader.readexactly(length), timeout=TCP_IDLE_TIMEOUT)
                except (asyncio.IncompleteReadError, asyncio.TimeoutError, ConnectionError):
                    break
                reply = await self.handle(query, src_ip, transport="tcp")
                if reply is None:
                    break
                writer.write(struct.pack("!H", len(reply)) + reply)
                await writer.drain()
        except (ConnectionError, OSError):
            pass
        finally:
            writer.close()
            try:
                await asyncio.wait_for(writer.wait_closed(), timeout=1)
            except (asyncio.TimeoutError, ConnectionError, OSError):
                pass


def validate_mappings(mappings: Any) -> dict[str, str]:
    """[{ip, client_id}] → {ip: client_id}. IPs are checked by the caller (subnet guard)."""
    if not isinstance(mappings, list) or not mappings:
        raise ValueError("mappings must be a non-empty list of {ip, client_id}")
    if len(mappings) > MAX_MAPPINGS:
        raise ValueError(f"at most {MAX_MAPPINGS} mappings")
    out: dict[str, str] = {}
    for m in mappings:
        if not isinstance(m, dict) or set(m) - {"ip", "client_id"} or "ip" not in m or "client_id" not in m:
            raise ValueError(f"each mapping must be exactly {{ip, client_id}}, got {m!r}")
        ip, cid = str(m["ip"]).strip(), m["client_id"]
        if not isinstance(cid, str) or not CLIENT_ID_RE.match(cid):
            raise ValueError(f"invalid client_id {cid!r}: use 1-63 characters of a-z, 0-9 and '-'")
        if ip in out and out[ip] != cid:
            raise ValueError(f"{ip} is mapped to two client_ids ({out[ip]}, {cid})")
        out[ip] = cid
    return out
