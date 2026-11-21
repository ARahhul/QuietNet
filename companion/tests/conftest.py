import json
from ipaddress import IPv4Interface
from pathlib import Path

import pytest

from pahra_companion import netinfo

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


HOME_IFACES = [
    netinfo.LocalInterface("Wi-Fi", IPv4Interface("192.168.1.10/24")),
    netinfo.LocalInterface("vEthernet (WSL)", IPv4Interface("172.25.224.1/20")),
]


@pytest.fixture
def home_ifaces():
    return list(HOME_IFACES)


@pytest.fixture
def fake_lan(monkeypatch):
    """Pretend this machine sits on 192.168.1.10/24 behind 192.168.1.1."""
    monkeypatch.setattr(netinfo, "local_interfaces", lambda: list(HOME_IFACES))
    monkeypatch.setattr(netinfo, "default_gateway", lambda: {"ip": "192.168.1.1", "interface": "192.168.1.10"})
    return HOME_IFACES
