"""Holds one MCP session to the pahra-companion server (stdio subprocess, or in-process in tests)."""

import json
import logging
import os
import shutil
from pathlib import Path
from typing import Any

from mcp import Client, StdioServerParameters

log = logging.getLogger("pahra.companion")

# Tools that change something on a device: the user must approve each call (also enforced if
# the server forgets its annotations).
KNOWN_MUTATING = {"adb_connect", "adb_pair", "set_private_dns", "clear_private_dns", "start_dns_forwarder", "stop_dns_forwarder"}


def resolve_companion_command(command: str, companion_dir: Path | None = None) -> StdioServerParameters | None:
    """`pahra-companion` on PATH, or run it from the sibling project's venv (companion/.venv, made by `poetry install`).

    The venv case runs `python -m pahra_companion` rather than the launcher script: pip writes a .exe, Poetry
    writes a .cmd shim, and the module form works the same for both (and on Linux).
    """
    parts = command.split()
    if not parts:
        return None
    if companion_dir is None:
        companion_dir = Path(__file__).resolve().parents[3] / "companion"
    cwd = str(companion_dir) if companion_dir.exists() else None
    exe = shutil.which(parts[0])
    if exe:
        return StdioServerParameters(command=exe, args=parts[1:], env=dict(os.environ), cwd=cwd)
    if parts[0] == "pahra-companion":
        for py in (companion_dir / ".venv" / "Scripts" / "python.exe", companion_dir / ".venv" / "bin" / "python"):
            if py.exists():
                return StdioServerParameters(
                    command=str(py), args=["-m", "pahra_companion", *parts[1:]], env=dict(os.environ), cwd=cwd
                )
    return None


class CompanionClient:
    def __init__(self, target: Any):
        self.target = target  # StdioServerParameters | MCPServer instance (tests)
        self._cm: Client | None = None
        self.client: Client | None = None
        self.tools: dict[str, Any] = {}
        self.error: str | None = None
        self.playbook: str | None = None

    async def start(self) -> None:
        if self.target is None:
            self.error = "pahra-companion not found: run `poetry install` in companion/ or set COMPANION_COMMAND"
            return
        try:
            self._cm = Client(self.target, read_timeout_seconds=120)
            self.client = await self._cm.__aenter__()
            listed = await self.client.list_tools()
            self.tools = {t.name: t for t in listed.tools}
            try:
                p = await self.client.get_prompt("connect_device", {"description": "{description}"})
                self.playbook = "\n".join(getattr(m.content, "text", "") for m in p.messages)
            except Exception:
                self.playbook = None
            self.error = None
            log.info("companion connected: %d tools", len(self.tools))
        except Exception as e:
            self.error = f"{e.__class__.__name__}: {e}"[:300]
            self.client = None
            log.warning("companion unavailable: %s", self.error)

    async def stop(self) -> None:
        if self._cm is not None:
            try:
                await self._cm.__aexit__(None, None, None)
            except Exception:
                pass
        self.client = None

    @property
    def connected(self) -> bool:
        return self.client is not None

    def status(self) -> dict:
        return {"enabled": True, "connected": self.connected, "error": self.error, "tools": sorted(self.tools)}

    def is_mutating(self, name: str) -> bool:
        if name in KNOWN_MUTATING:
            return True
        t = self.tools.get(name)
        ann = getattr(t, "annotations", None)
        return not (ann and ann.read_only_hint)  # unknown -> treat as mutating (ask first)

    async def call(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        if not self.client:
            return {"ok": False, "error": "companion not connected"}
        if name not in self.tools:
            return {"ok": False, "error": f"unknown tool {name}"}
        try:
            res = await self.client.call_tool(name, args)
        except Exception as e:
            return {"ok": False, "error": f"{e.__class__.__name__}: {e}"[:300]}
        data = getattr(res, "structured_content", None)
        if isinstance(data, dict) and set(data) == {"result"} and isinstance(data["result"], dict):
            data = data["result"]
        if not isinstance(data, dict):
            text = "".join(getattr(c, "text", "") for c in (getattr(res, "content", None) or []))
            try:
                data = json.loads(text)
            except ValueError:
                data = {"ok": not getattr(res, "is_error", False), "text": text}
        if getattr(res, "is_error", False):
            data.setdefault("ok", False)
        return data
