"""Replace IP and MAC addresses with stable handles before anything reaches the LLM.

host-1, host-2 … for IPv4/IPv6 and mac-1 … for MACs. Handles are mapped back when the
model calls a tool, so tools still receive real addresses.
"""

import ipaddress
import re
from typing import Any

IPV4 = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?:/\d{1,2})?(?![\d.])")
# candidate tokens with 2+ colons (covers "::" compression); validated with ipaddress below
IPV6 = re.compile(r"(?<![\w:])(?=[0-9a-fA-F:]*:[0-9a-fA-F:]*:)[0-9a-fA-F:]{2,39}(?:/\d{1,3})?(?![\w:])")
MAC = re.compile(r"(?<![0-9a-fA-F:-])(?:[0-9a-fA-F]{2}[:-]){5}[0-9a-fA-F]{2}(?![0-9a-fA-F:-])")
HANDLE = re.compile(r"\b(?:host|mac|net)-\d+\b")


class Redactor:
    def __init__(self) -> None:
        self.fwd: dict[str, str] = {}
        self.rev: dict[str, str] = {}
        self._n = {"host": 0, "mac": 0, "net": 0}
        self.secrets: dict[str, str] = {}  # literal value -> placeholder (one-way)

    def add_secret(self, value: str, placeholder: str) -> None:
        if value:
            self.secrets[value] = placeholder

    def _handle(self, value: str, kind: str) -> str:
        if value not in self.fwd:
            self._n[kind] += 1
            h = f"{kind}-{self._n[kind]}"
            self.fwd[value] = h
            self.rev[h] = value
        return self.fwd[value]

    def _sub(self, s: str) -> str:
        for value, placeholder in self.secrets.items():
            s = s.replace(value, placeholder)
        s = MAC.sub(lambda m: self._handle(m.group(0).lower(), "mac"), s)
        s = IPV4.sub(lambda m: self._handle(m.group(0), "net" if "/" in m.group(0) else "host"), s)
        s = IPV6.sub(self._v6, s)
        return s

    def _v6(self, m: re.Match) -> str:
        tok = m.group(0)
        try:
            ipaddress.ip_interface(tok) if "/" in tok else ipaddress.IPv6Address(tok)
        except ValueError:
            return tok  # e.g. a time like 12:30:45
        return self._handle(tok.lower(), "net" if "/" in tok else "host")

    def redact(self, obj: Any) -> Any:
        if isinstance(obj, str):
            return self._sub(obj)
        if isinstance(obj, dict):
            return {k: self.redact(v) for k, v in obj.items()}
        if isinstance(obj, (list, tuple)):
            return [self.redact(v) for v in obj]
        return obj

    def unredact(self, obj: Any) -> Any:
        if isinstance(obj, str):
            return HANDLE.sub(lambda m: self.rev.get(m.group(0), m.group(0)), obj)
        if isinstance(obj, dict):
            return {k: self.unredact(v) for k, v in obj.items()}
        if isinstance(obj, (list, tuple)):
            return [self.unredact(v) for v in obj]
        return obj

    def handle_for(self, value: str) -> str:
        return self._handle(value, "host")
