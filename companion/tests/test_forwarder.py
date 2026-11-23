"""Laptop DNS relay: real loopback sockets, mocked DoH upstream (no real network)."""

import asyncio
import socket
import struct

import dns.flags
import dns.message
import dns.rcode
import dns.rrset
import httpx
import pytest
from mcp import Client

from pahra_companion import forwarder as fwdmod
from pahra_companion import server
from pahra_companion.forwarder import DnsForwarder

HOST = "dns.example.com"


class Upstream:
    """httpx mock of the QuietNet DoH endpoint. Records requests; can fail or answer big."""

    def __init__(self, mode: str = "ok", answers: int = 1):
        self.mode = mode
        self.answers = answers
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.mode == "connect_error":
            raise httpx.ConnectError("boom", request=request)
        if self.mode == "http_500":
            return httpx.Response(500, text="oops")
        if self.mode == "wrong_type":
            return httpx.Response(200, content=b"<html>", headers={"content-type": "text/html"})
        q = dns.message.from_wire(request.content)
        r = dns.message.make_response(q)
        name = q.question[0].name
        ips = [f"10.0.{i // 250}.{i % 250 + 1}" for i in range(self.answers)]
        r.answer.append(dns.rrset.from_text_list(name, 60, "IN", "A", ips))
        return httpx.Response(200, content=r.to_wire(), headers={"content-type": "application/dns-message"})

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


def query(name: str = "example.org", qid: int = 0x1234, edns: bool = False) -> bytes:
    q = dns.message.make_query(name, "A", use_edns=0 if edns else False)
    q.id = qid
    return q.to_wire()


def udp_ask(port: int, data: bytes, timeout: float = 3.0) -> bytes:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.settimeout(timeout)
        s.sendto(data, ("127.0.0.1", port))
        return s.recvfrom(65535)[0]


def tcp_ask(port: int, data: bytes, timeout: float = 3.0) -> bytes:
    with socket.create_connection(("127.0.0.1", port), timeout=timeout) as s:
        s.sendall(struct.pack("!H", len(data)) + data)
        (n,) = struct.unpack("!H", s.recv(2))
        buf = b""
        while len(buf) < n:
            buf += s.recv(n - len(buf))
        return buf


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
async def relay():
    up = Upstream()
    fwd = DnsForwarder(transport=up.transport)
    await fwd.start("127.0.0.1", 0, {"127.0.0.1": "tv-living"}, HOST)
    yield fwd, up
    await fwd.stop()


# ---------------------------------------------------------------- forwarding over real sockets


async def test_udp_mapped_source_is_forwarded_with_id_restored(relay):
    fwd, up = relay
    raw = await asyncio.to_thread(udp_ask, fwd.port, query(qid=0x1234))
    reply = dns.message.from_wire(raw)
    assert reply.id == 0x1234
    assert reply.rcode() == dns.rcode.NOERROR
    assert reply.answer[0][0].address == "10.0.0.1"
    req = up.requests[0]
    assert str(req.url) == "https://dns.example.com/dns-query/tv-living"
    assert req.method == "POST"
    assert req.headers["content-type"] == "application/dns-message"
    assert req.content[:2] == b"\x00\x00"  # upstream id 0
    assert req.content[2:] == query(qid=0x1234)[2:]


async def test_tcp_path(relay):
    fwd, up = relay
    raw = await asyncio.to_thread(tcp_ask, fwd.port, query(qid=0xBEEF, name="tcp.example.org"))
    reply = dns.message.from_wire(raw)
    assert reply.id == 0xBEEF and reply.answer
    assert str(up.requests[0].url).endswith("/dns-query/tv-living")


async def test_no_caching_every_query_goes_upstream(relay):
    fwd, up = relay
    for _ in range(3):
        await asyncio.to_thread(udp_ask, fwd.port, query(qid=7))
    assert len(up.requests) == 3
    assert fwd.status()["stats"] == {"queries": 3, "forwarded": 3, "refused": 0, "errors": 0}


async def test_unmapped_source_is_refused_over_socket():
    up = Upstream()
    fwd = DnsForwarder(transport=up.transport)
    await fwd.start("127.0.0.1", 0, {"192.168.1.31": "lg-tv"}, HOST)  # 127.0.0.1 is not mapped
    try:
        raw = await asyncio.to_thread(udp_ask, fwd.port, query(qid=42))
        reply = dns.message.from_wire(raw)
        assert reply.id == 42 and reply.rcode() == dns.rcode.REFUSED
        raw = await asyncio.to_thread(tcp_ask, fwd.port, query(qid=43))
        assert dns.message.from_wire(raw).rcode() == dns.rcode.REFUSED
        assert up.requests == []
        assert fwd.status()["stats"] == {"queries": 2, "forwarded": 0, "refused": 2, "errors": 0}
    finally:
        await fwd.stop()


@pytest.mark.parametrize("mode", ["connect_error", "http_500", "wrong_type"])
async def test_upstream_error_gives_servfail(mode):
    up = Upstream(mode=mode)
    fwd = DnsForwarder(transport=up.transport)
    await fwd.start("127.0.0.1", 0, {"127.0.0.1": "tv-living"}, HOST)
    try:
        raw = await asyncio.to_thread(udp_ask, fwd.port, query(qid=99))
        reply = dns.message.from_wire(raw)
        assert reply.id == 99 and reply.rcode() == dns.rcode.SERVFAIL
        assert fwd.status()["stats"] == {"queries": 1, "forwarded": 0, "refused": 0, "errors": 1}
    finally:
        await fwd.stop()


async def test_big_udp_answer_is_truncated_and_tcp_gets_it_all():
    up = Upstream(answers=60)  # ~1 KB answer, larger than 512 bytes
    fwd = DnsForwarder(transport=up.transport)
    await fwd.start("127.0.0.1", 0, {"127.0.0.1": "tv-living"}, HOST)
    try:
        small = dns.message.from_wire(await asyncio.to_thread(udp_ask, fwd.port, query(qid=5)))
        assert small.flags & dns.flags.TC and not small.answer and small.id == 5
        big = dns.message.from_wire(await asyncio.to_thread(tcp_ask, fwd.port, query(qid=6)))
        assert len(big.answer[0]) == 60 and not big.flags & dns.flags.TC
        edns = dns.message.from_wire(await asyncio.to_thread(udp_ask, fwd.port, query(qid=8, edns=True)))
        assert len(edns.answer[0]) == 60  # client advertised 1232+ bytes via EDNS
    finally:
        await fwd.stop()


async def test_restart_replaces_mappings_without_rebinding(relay):
    fwd, _ = relay
    port, udp = fwd.port, fwd._udp
    r = await fwd.start("127.0.0.1", port, {"127.0.0.1": "tv-bedroom", "192.168.1.40": "tizen-tv"}, HOST)
    assert r == {"rebound": False}
    assert fwd._udp is udp and fwd.port == port
    assert fwd.status()["mappings"] == [
        {"ip": "127.0.0.1", "client_id": "tv-bedroom"},
        {"ip": "192.168.1.40", "client_id": "tizen-tv"},
    ]
    await asyncio.to_thread(udp_ask, fwd.port, query())
    assert str(relay[1].requests[-1].url).endswith("/dns-query/tv-bedroom")


async def test_port_change_rebinds_and_keeps_stats(relay):
    fwd, _ = relay
    await asyncio.to_thread(udp_ask, fwd.port, query())
    new_port = free_port()
    r = await fwd.start("127.0.0.1", new_port, {"127.0.0.1": "tv-living"}, HOST)
    assert r == {"rebound": True} and fwd.port == new_port
    assert fwd.status()["stats"]["queries"] == 1
    reply = dns.message.from_wire(await asyncio.to_thread(udp_ask, new_port, query(qid=3)))
    assert reply.id == 3


async def test_status_counters_and_per_client(relay):
    fwd, _ = relay
    assert fwd.status()["per_client"] == {}
    await asyncio.to_thread(udp_ask, fwd.port, query())
    await asyncio.to_thread(tcp_ask, fwd.port, query())
    await fwd.handle(query(), "192.168.1.77")  # unmapped
    st = fwd.status()
    assert st["running"] is True and st["listen_ip"] == "127.0.0.1"
    assert st["stats"] == {"queries": 3, "forwarded": 2, "refused": 1, "errors": 0}
    assert st["per_client"]["tv-living"]["queries"] == 2
    assert st["per_client"]["tv-living"]["last_ts"].endswith("+00:00")
    await fwd.stop()
    st = fwd.status()
    assert st["running"] is False and st["mappings"] == [] and st["stats"]["queries"] == 3


async def test_garbage_and_responses_are_dropped(relay):
    fwd, up = relay
    assert await fwd.handle(b"\x01\x02", "127.0.0.1") is None
    resp = dns.message.make_response(dns.message.from_wire(query())).to_wire()
    assert await fwd.handle(resp, "127.0.0.1") is None  # QR=1: never answer a response
    assert up.requests == [] and fwd.status()["stats"]["queries"] == 0


async def test_bind_conflict_raises_bind_error():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as busy:
        busy.bind(("127.0.0.1", 0))
        port = busy.getsockname()[1]
        fwd = DnsForwarder(transport=Upstream().transport)
        with pytest.raises(fwdmod.BindError) as exc:
            await fwd.start("127.0.0.1", port, {"127.0.0.1": "tv"}, HOST)
        assert "SharedAccess" in exc.value.hint
        assert fwd.running is False


# ---------------------------------------------------------------- validation


@pytest.mark.parametrize(
    "mappings",
    [
        [],
        "192.168.1.31=tv",
        [{"ip": "192.168.1.31"}],
        [{"ip": "192.168.1.31", "client_id": "TV-1"}],
        [{"ip": "192.168.1.31", "client_id": "tv_1"}],
        [{"ip": "192.168.1.31", "client_id": ""}],
        [{"ip": "192.168.1.31", "client_id": "a" * 64}],
        [{"ip": "192.168.1.31", "client_id": "tv; rm -rf /"}],
        [{"ip": "192.168.1.31", "client_id": "tv.lan"}],
        [{"ip": "192.168.1.31", "client_id": "tv", "extra": "x"}],
        [{"ip": "192.168.1.31", "client_id": "tv-a"}, {"ip": "192.168.1.31", "client_id": "tv-b"}],
    ],
)
def test_validate_mappings_rejects(mappings):
    with pytest.raises(ValueError):
        fwdmod.validate_mappings(mappings)


def test_validate_mappings_accepts():
    assert fwdmod.validate_mappings([{"ip": " 192.168.1.31 ", "client_id": "lg-tv-1"}]) == {"192.168.1.31": "lg-tv-1"}


def test_firewall_hint_is_exact_and_private():
    h = fwdmod.firewall_hint(53)
    assert h.count("New-NetFirewallRule") == 2
    assert "-Protocol UDP -LocalPort 53" in h and "-Protocol TCP -LocalPort 53" in h
    assert h.count("-Profile Private") == 2


# ---------------------------------------------------------------- MCP tools


@pytest.fixture
def mcp_relay(monkeypatch, fake_lan):
    up = Upstream()
    fwd = DnsForwarder(transport=up.transport)
    monkeypatch.setattr(server, "_forwarder", fwd)
    monkeypatch.setattr(server, "_forwarder_listen_ip", lambda: "127.0.0.1")
    yield fwd, up


def sc(result) -> dict:
    return result.structured_content


async def test_mcp_start_status_stop(mcp_relay):
    fwd, up = mcp_relay
    port = free_port()
    async with Client(server.create_server()) as c:
        r = sc(await c.call_tool("start_dns_forwarder", {
            "mappings": [{"ip": "192.168.1.31", "client_id": "lg-tv"}], "dns_hostname": "DNS.Example.com", "port": port}))
        assert r["ok"] and r["running"] and r["listen_ip"] == "127.0.0.1" and r["port"] == port
        assert r["mappings"] == [{"ip": "192.168.1.31", "client_id": "lg-tv"}]
        assert r["upstream_template"] == "https://dns.example.com/dns-query/<client_id>"
        assert "New-NetFirewallRule" in r["hint"] and "-Profile Private" in r["hint"]

        # 127.0.0.1 is not mapped through MCP (the guard only allows LAN IPs), so it is refused.
        raw = await asyncio.to_thread(udp_ask, port, query(qid=11))
        assert dns.message.from_wire(raw).rcode() == dns.rcode.REFUSED

        r2 = sc(await c.call_tool("start_dns_forwarder", {
            "mappings": [{"ip": "192.168.1.31", "client_id": "lg-tv"}, {"ip": "192.168.1.32", "client_id": "samsung-tv"}],
            "dns_hostname": "dns.example.com", "port": port}))
        assert r2["ok"] and r2["rebound"] is False and len(r2["mappings"]) == 2

        st = sc(await c.call_tool("dns_forwarder_status", {}))
        assert st["ok"] and st["running"] and st["port"] == port
        assert st["stats"] == {"queries": 1, "forwarded": 0, "refused": 1, "errors": 0}
        assert st["per_client"] == {}

        stop = sc(await c.call_tool("stop_dns_forwarder", {}))
        assert stop == {"ok": True, "running": False, "was_running": True,
                        "stats": {"queries": 1, "forwarded": 0, "refused": 1, "errors": 0}}
        st = sc(await c.call_tool("dns_forwarder_status", {}))
        assert st["running"] is False and st["listen_ip"] is None


@pytest.mark.parametrize(
    "args,code",
    [
        ({"mappings": [{"ip": "8.8.8.8", "client_id": "tv"}], "dns_hostname": HOST}, "guard"),
        ({"mappings": [{"ip": "192.168.50.9", "client_id": "tv"}], "dns_hostname": HOST}, "guard"),
        ({"mappings": [{"ip": "100.64.0.9", "client_id": "tv"}], "dns_hostname": HOST}, "guard"),
        ({"mappings": [{"ip": "192.168.1.31", "client_id": "Bad_ID"}], "dns_hostname": HOST}, "invalid_argument"),
        ({"mappings": [], "dns_hostname": HOST}, "invalid_argument"),
        ({"mappings": [{"ip": "192.168.1.31", "client_id": "tv"}], "dns_hostname": "x; rm -rf /"}, "invalid_argument"),
        ({"mappings": [{"ip": "192.168.1.31", "client_id": "tv"}], "dns_hostname": "https://dns.example.com"}, "invalid_argument"),
        ({"mappings": [{"ip": "192.168.1.31", "client_id": "tv"}], "dns_hostname": HOST, "port": 70000}, "invalid_argument"),
    ],
)
async def test_mcp_start_validation(mcp_relay, args, code):
    async with Client(server.create_server()) as c:
        r = sc(await c.call_tool("start_dns_forwarder", args))
        assert r["ok"] is False and r["code"] == code, r
    assert mcp_relay[0].running is False


async def test_mcp_bind_failed(mcp_relay):
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as busy:
        busy.bind(("127.0.0.1", 0))
        port = busy.getsockname()[1]
        async with Client(server.create_server()) as c:
            r = sc(await c.call_tool("start_dns_forwarder", {
                "mappings": [{"ip": "192.168.1.31", "client_id": "tv"}], "dns_hostname": HOST, "port": port}))
    assert r["ok"] is False and r["code"] == "bind_failed" and r["running"] is False
    assert "SharedAccess" in r["hint"]


async def test_listen_ip_is_primary_lan_ip(fake_lan):
    assert server._forwarder_listen_ip() == "192.168.1.10"
