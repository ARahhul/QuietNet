"""MCP-level tests: tool list and annotations, guard errors as {ok:false}, and an ADB flow
with a mocked adb binary, all through an in-process MCP client."""

import sys

import pytest
from mcp import Client, StdioServerParameters

from pahra_companion import discovery, server
from pahra_companion.adb import Adb
from test_adb import SERIAL, FakeAdb

EXPECTED_TOOLS = {
    "get_network_context": True,
    "scan_network": True,
    "ping_device": True,
    "probe_device": True,
    "adb_status": True,
    "get_setup_options": True,
    "adb_connect": False,
    "adb_pair": False,
    "set_private_dns": False,
    "clear_private_dns": False,
    "start_dns_forwarder": False,
    "stop_dns_forwarder": False,
    "dns_forwarder_status": True,
}
DESTRUCTIVE = {"set_private_dns", "clear_private_dns"}


def structured(result) -> dict:
    sc = getattr(result, "structured_content", None)
    if sc is None:
        sc = getattr(result, "structuredContent", None)
    assert sc is not None, result
    return sc


@pytest.fixture
def fake_adb(monkeypatch, fake_lan):
    fake = FakeAdb(state="device", sdk="30")
    monkeypatch.setattr(server, "_adb", Adb(adb_path="/fake/adb", runner=fake))

    async def tcp_open(ip, port, timeout=0.5):
        return ip == "192.168.1.34" and port == 5555

    monkeypatch.setattr(discovery, "tcp_open", tcp_open)
    return fake


async def test_lists_all_tools_with_annotations():
    async with Client(server.create_server()) as c:
        tools = {t.name: t for t in (await c.list_tools()).tools}
        assert set(tools) == set(EXPECTED_TOOLS)
        for name, read_only in EXPECTED_TOOLS.items():
            ann = tools[name].annotations.model_dump(by_alias=True)
            assert ann["readOnlyHint"] is read_only, name
            assert ann["destructiveHint"] is (name in DESTRUCTIVE), name
            assert tools[name].description
        assert server.READ_ONLY_TOOLS == {n for n, ro in EXPECTED_TOOLS.items() if ro}
        prompts = {p.name for p in (await c.list_prompts()).prompts}
        assert prompts == {"connect_device"}


async def test_connect_device_prompt_mentions_playbook():
    async with Client(server.create_server()) as c:
        r = await c.get_prompt("connect_device", {"description": "the LG in the living room"})
        text = r.messages[0].content.text
        assert "the LG in the living room" in text
        for word in ("get_network_context", "scan_network", "probe_device", "adb_connect", "set_private_dns",
                     "get_setup_options", "CGNAT", "impossible", "start_dns_forwarder", "listen_ip"):
            assert word in text


@pytest.mark.parametrize(
    "tool,args",
    [
        ("ping_device", {"ip": "8.8.8.8"}),
        ("probe_device", {"ip": "192.168.50.5"}),
        ("adb_status", {"ip": "1.1.1.1"}),
        ("adb_connect", {"ip": "100.64.0.1"}),
        ("set_private_dns", {"ip": "8.8.8.8", "hostname": "tv.dns.example.com"}),
        ("clear_private_dns", {"ip": "203.0.113.9"}),
        ("adb_pair", {"ip": "8.8.4.4", "port": 37000, "code": "123456"}),
        ("scan_network", {"subnet": "10.0.0.0/8"}),
        ("scan_network", {"subnet": "8.8.8.0/24"}),
    ],
)
async def test_guard_errors_are_ok_false(tool, args, fake_lan):
    async with Client(server.create_server()) as c:
        r = structured(await c.call_tool(tool, args))
        assert r["ok"] is False and r["code"] == "guard" and r["error"]


async def test_get_setup_options_over_mcp():
    async with Client(server.create_server()) as c:
        r = structured(await c.call_tool("get_setup_options", {"platform": "tizen", "cgnat": "not_cgnat"}))
        assert r["ok"] and r["options"][0]["kind"] == "router_dns"
        bad = structured(await c.call_tool("get_setup_options", {"platform": "toaster"}))
        assert bad["ok"] is False and bad["code"] == "invalid_argument"


async def test_adb_flow_with_mocked_adb(fake_adb):
    async with Client(server.create_server()) as c:
        st = structured(await c.call_tool("adb_status", {"ip": "192.168.1.34"}))
        assert st["status"] == "connected" and st["sdk"] == 30 and st["private_dns_supported"]

        closed = structured(await c.call_tool("adb_status", {"ip": "192.168.1.31"}))
        assert closed["status"] == "not_listening"

        con = structured(await c.call_tool("adb_connect", {"ip": "192.168.1.34"}))
        assert con["ok"] and con["status"] == "connected" and con["model"] == "AFTKA"

        bad = structured(await c.call_tool("set_private_dns", {"ip": "192.168.1.34", "hostname": "x; rm -rf /"}))
        assert bad["ok"] is False and bad["code"] == "rejected"

        r = structured(await c.call_tool("set_private_dns", {"ip": "192.168.1.34", "hostname": "tv-9f.dns.example.com"}))
        assert r["ok"] and r["verified"]
        assert r["before"] == {"mode": "opportunistic", "specifier": None}
        assert r["after"] == {"mode": "hostname", "specifier": "tv-9f.dns.example.com"}

        r = structured(await c.call_tool("clear_private_dns", {"ip": "192.168.1.34"}))
        assert r["ok"] and r["after"]["mode"] == "opportunistic"

    # Nothing outside the allowlist ever reached the "subprocess".
    for call in fake_adb.calls:
        from pahra_companion.adb import validate_argv

        validate_argv(call)
    assert not any(c[:1] == ["shell"] for c in fake_adb.calls)


async def test_adb_connect_unauthorized(monkeypatch, fake_adb):
    fake_adb.state = "unauthorized"
    async with Client(server.create_server()) as c:
        r = structured(await c.call_tool("adb_connect", {"ip": "192.168.1.34"}))
        assert r["ok"] is False and r["status"] == "unauthorized" and "Allow USB debugging" in r["hint"]
        r = structured(await c.call_tool("set_private_dns", {"ip": "192.168.1.34", "hostname": "tv.dns.example.com"}))
        assert r["ok"] is False and r["code"] == "not_connected"


async def test_adb_missing_reported(monkeypatch, fake_lan):
    monkeypatch.setattr(server, "_adb", Adb(adb_path=None))
    monkeypatch.setattr("pahra_companion.adb.find_adb", lambda: None)

    async def tcp_open(ip, port, timeout=0.5):
        return True

    monkeypatch.setattr(discovery, "tcp_open", tcp_open)
    async with Client(server.create_server()) as c:
        r = structured(await c.call_tool("adb_connect", {"ip": "192.168.1.34"}))
        assert r["ok"] is False and r["code"] == "adb_missing" and "winget install Google.PlatformTools" in r["hint"]
        st = structured(await c.call_tool("adb_status", {"ip": "192.168.1.34"}))
        assert st["status"] == "listening" and st["adb_available"] is False


def test_http_refuses_non_loopback():
    with pytest.raises(SystemExit):
        server._parse_hostport("0.0.0.0:8765")
    assert server._parse_hostport("127.0.0.1:8765") == ("127.0.0.1", 8765)


async def test_stdio_server_starts_and_lists_tools():
    """Spawn the real server process over stdio, as the QuietNet backend does."""
    params = StdioServerParameters(command=sys.executable, args=["-m", "pahra_companion"])
    async with Client(params, read_timeout_seconds=30) as c:
        names = {t.name for t in (await c.list_tools()).tools}
        assert names == set(EXPECTED_TOOLS)
        r = structured(await c.call_tool("get_setup_options", {"platform": "webos", "cgnat": "cgnat"}))
        assert r["ok"]
