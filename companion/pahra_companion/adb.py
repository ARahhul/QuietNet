"""Thin wrapper over Google platform-tools `adb` with a fixed command allowlist.

There is deliberately no way to run an arbitrary command. Every argv passes
`validate_argv` before it reaches the subprocess, and every argument that comes
from a caller (serial, pairing code, hostname) is checked against a strict regex.
That matters because `adb shell a b c` joins its arguments into one string that
the TV's /system/bin/sh interprets, so a hostname like "x; reboot" would
otherwise run on the device.
"""

from __future__ import annotations

import asyncio
import glob
import logging
import os
import re
import shutil
import sys
from typing import Any, Awaitable, Callable

log = logging.getLogger(__name__)

INSTALL_HINT = (
    "Install Google platform-tools: `winget install Google.PlatformTools` (Windows), "
    "`brew install android-platform-tools` (macOS) or your distro's android-tools package, "
    "or set ADB_PATH to the full path of adb."
)
MIN_PRIVATE_DNS_SDK = 28  # Android 9 introduced Private DNS

READ_PROPS = frozenset({"ro.build.version.sdk", "ro.product.manufacturer", "ro.product.model"})
DNS_KEYS = frozenset({"private_dns_mode", "private_dns_specifier"})
DNS_MODES = frozenset({"hostname", "opportunistic", "off"})

_SERIAL = re.compile(r"^(?:\d{1,3}\.){3}\d{1,3}:(\d{1,5})$")
_CODE = re.compile(r"^\d{6}$")
_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
_HOSTNAME = re.compile(rf"^(?={_LABEL}\.)(?!.{{254}}){_LABEL}(?:\.{_LABEL})+$")


class AdbError(Exception):
    def __init__(self, message: str, hint: str | None = None):
        super().__init__(message)
        self.hint = hint


class AdbMissing(AdbError):
    pass


class AdbCommandRejected(AdbError):
    pass


# --------------------------------------------------------------------------- validation


def validate_hostname(hostname: str) -> str:
    """Return the lower-cased hostname if it is a plain DNS name (labels of [a-z0-9-], at least one dot)."""
    if not isinstance(hostname, str):
        raise AdbCommandRejected("hostname must be a string")
    h = hostname.strip().lower()
    if not h or len(h) > 253 or not _HOSTNAME.match(h):
        raise AdbCommandRejected(
            f"invalid hostname {hostname!r}",
            "Use the QuietNet DNS hostname exactly, e.g. tv-1a2b.dns.example.com (letters, digits, '-' and '.').",
        )
    return h


def validate_serial(serial: str) -> str:
    m = _SERIAL.match(serial or "")
    if not m or not 1 <= int(m.group(1)) <= 65535:
        raise AdbCommandRejected(f"invalid device address {serial!r}", "Expected ip:port, e.g. 192.168.1.20:5555.")
    octets = serial.split(":")[0].split(".")
    if any(int(o) > 255 for o in octets):
        raise AdbCommandRejected(f"invalid device address {serial!r}")
    return serial


def validate_code(code: str) -> str:
    code = str(code).strip()
    if not _CODE.match(code):
        raise AdbCommandRejected("pairing code must be the 6 digits shown on the TV")
    return code


def validate_argv(args: list[str]) -> None:
    """Raise AdbCommandRejected unless `args` (without the adb binary) is on the allowlist.

    Allowed shapes:
      devices | start-server
      connect <ip:port> | disconnect <ip:port> | pair <ip:port> <6 digits>
      -s <ip:port> get-state
      -s <ip:port> shell getprop <ro.build.version.sdk|ro.product.manufacturer|ro.product.model>
      -s <ip:port> shell settings get global <private_dns_mode|private_dns_specifier>
      -s <ip:port> shell settings put global private_dns_mode <hostname|opportunistic|off>
      -s <ip:port> shell settings put global private_dns_specifier <hostname>
    """
    a = list(args)
    if not all(isinstance(x, str) for x in a):
        raise AdbCommandRejected("arguments must be strings")
    if a in (["devices"], ["start-server"]):
        return
    if len(a) == 2 and a[0] in ("connect", "disconnect"):
        validate_serial(a[1])
        return
    if len(a) == 3 and a[0] == "pair":
        validate_serial(a[1])
        validate_code(a[2])
        return
    if len(a) >= 3 and a[0] == "-s":
        validate_serial(a[1])
        rest = a[2:]
        if rest == ["get-state"]:
            return
        if len(rest) == 3 and rest[:2] == ["shell", "getprop"] and rest[2] in READ_PROPS:
            return
        if len(rest) == 5 and rest[:4] == ["shell", "settings", "get", "global"] and rest[4] in DNS_KEYS:
            return
        if len(rest) == 6 and rest[:4] == ["shell", "settings", "put", "global"]:
            key, value = rest[4], rest[5]
            if key == "private_dns_mode" and value in DNS_MODES:
                return
            if key == "private_dns_specifier" and validate_hostname(value) == value:
                return
    raise AdbCommandRejected(f"adb command not allowed: {' '.join(a)!r}", "Only fixed Private-DNS commands are allowed.")


# --------------------------------------------------------------------------- binary discovery


def find_adb() -> str | None:
    env = os.environ.get("ADB_PATH")
    if env:
        if os.path.isdir(env):
            for name in ("adb.exe", "adb"):
                cand = os.path.join(env, name)
                if os.path.isfile(cand):
                    return cand
        elif os.path.isfile(env):
            return env
    found = shutil.which("adb")
    if found:
        return found
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA", "")
        candidates = [
            os.path.join(local, "Microsoft", "WinGet", "Links", "adb.exe"),
            os.path.join(local, "Android", "Sdk", "platform-tools", "adb.exe"),
            *glob.glob(os.path.join(local, "Microsoft", "WinGet", "Packages", "Google.PlatformTools*", "platform-tools", "adb.exe")),
        ]
        for c in candidates:
            if c and os.path.isfile(c):
                return c
    else:
        for c in (os.path.expanduser("~/Library/Android/sdk/platform-tools/adb"), os.path.expanduser("~/Android/Sdk/platform-tools/adb")):
            if os.path.isfile(c):
                return c
    return None


# --------------------------------------------------------------------------- runner

Runner = Callable[[list[str], float], Awaitable[tuple[int, str, str]]]


async def subprocess_runner(argv: list[str], timeout: float) -> tuple[int, str, str]:
    kwargs: dict[str, Any] = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
    # The adb server daemon outlives this call. Start it with no pipes attached, so it
    # cannot hold our stdout open (and, in stdio mode, never touches the MCP stream).
    pipe = asyncio.subprocess.DEVNULL if argv[1:] == ["start-server"] else asyncio.subprocess.PIPE
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=pipe,
        stderr=pipe,
        stdin=asyncio.subprocess.DEVNULL,
        **kwargs,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise AdbError(f"adb timed out after {timeout:.0f}s", "Check that the TV is on and on the same network.")
    return proc.returncode or 0, (out or b"").decode(errors="replace"), (err or b"").decode(errors="replace")


def parse_devices(text: str) -> dict[str, str]:
    """{serial: state} from `adb devices`."""
    states = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("*") or line.lower().startswith("list of devices"):
            continue
        parts = line.split()
        if len(parts) >= 2:
            states[parts[0]] = parts[1]
    return states


def _clean(value: str) -> str | None:
    v = value.strip()
    return None if v in ("", "null") else v


class Adb:
    """All adb access goes through `Adb.run`, which enforces the allowlist."""

    def __init__(self, adb_path: str | None = None, runner: Runner | None = None):
        self._path = adb_path
        self._runner = runner or subprocess_runner
        self._server_started = False

    @property
    def path(self) -> str | None:
        if self._path is None:
            self._path = find_adb()
        return self._path

    async def run(self, args: list[str], timeout: float = 15.0) -> tuple[int, str, str]:
        validate_argv(args)
        path = self.path
        if not path:
            raise AdbMissing("adb (Android platform-tools) is not installed or not on PATH", INSTALL_HINT)
        try:
            if not self._server_started and args != ["start-server"]:
                await self._runner([path, "start-server"], 20.0)
                self._server_started = True
            log.info("adb %s", " ".join(args))
            return await self._runner([path, *args], timeout)
        except FileNotFoundError:
            raise AdbMissing(f"adb not found at {path}", INSTALL_HINT) from None

    # ----- state

    async def devices(self) -> dict[str, str]:
        _, out, _ = await self.run(["devices"])
        return parse_devices(out)

    async def state(self, serial: str) -> str | None:
        return (await self.devices()).get(validate_serial(serial))

    async def wait_settled(self, serial: str, seconds: float = 3.0) -> str | None:
        """Poll `adb devices` until the serial leaves the transient offline/connecting states."""
        state = None
        loop = asyncio.get_running_loop()
        deadline = loop.time() + seconds
        while True:
            state = await self.state(serial)
            if state in ("device", "unauthorized") or loop.time() >= deadline:
                return state
            await asyncio.sleep(0.5)

    # ----- connection

    async def connect(self, serial: str) -> dict[str, Any]:
        rc, out, err = await self.run(["connect", validate_serial(serial)], timeout=15)
        text = (out + err).strip()
        low = text.lower()
        if "connected to" in low and "cannot" not in low and "failed" not in low:
            state = await self.wait_settled(serial)
            return {"connected": state == "device", "state": state, "output": text}
        if "authenticate" in low or "unauthorized" in low:
            return {"connected": False, "state": "unauthorized", "output": text}
        return {"connected": False, "state": None, "output": text, "returncode": rc}

    async def disconnect(self, serial: str) -> str:
        _, out, err = await self.run(["disconnect", validate_serial(serial)])
        return (out + err).strip()

    async def pair(self, serial: str, code: str) -> dict[str, Any]:
        rc, out, err = await self.run(["pair", validate_serial(serial), validate_code(code)], timeout=30)
        text = (out + err).strip()
        ok = "successfully paired" in text.lower()
        return {"paired": ok, "output": text, "returncode": rc}

    # ----- reads

    async def getprop(self, serial: str, prop: str) -> str | None:
        rc, out, err = await self.run(["-s", validate_serial(serial), "shell", "getprop", prop])
        if rc != 0:
            raise AdbError(f"getprop {prop} failed: {(out + err).strip()}")
        return _clean(out)

    async def sdk(self, serial: str) -> int | None:
        v = await self.getprop(serial, "ro.build.version.sdk")
        try:
            return int(v) if v else None
        except ValueError:
            return None

    async def device_info(self, serial: str) -> dict[str, Any]:
        return {
            "sdk": await self.sdk(serial),
            "manufacturer": await self.getprop(serial, "ro.product.manufacturer"),
            "model": await self.getprop(serial, "ro.product.model"),
        }

    async def get_setting(self, serial: str, key: str) -> str | None:
        rc, out, err = await self.run(["-s", validate_serial(serial), "shell", "settings", "get", "global", key])
        if rc != 0:
            raise AdbError(f"settings get {key} failed: {(out + err).strip()}")
        return _clean(out)

    async def get_private_dns(self, serial: str) -> dict[str, str | None]:
        return {
            "mode": await self.get_setting(serial, "private_dns_mode"),
            "specifier": await self.get_setting(serial, "private_dns_specifier"),
        }

    # ----- writes

    async def put_setting(self, serial: str, key: str, value: str) -> None:
        rc, out, err = await self.run(["-s", validate_serial(serial), "shell", "settings", "put", "global", key, value])
        text = (out + err).strip()
        if rc != 0 or "exception" in text.lower() or "error" in text.lower():
            raise AdbError(f"settings put {key} failed: {text or rc}")

    async def set_private_dns(self, serial: str, hostname: str) -> dict[str, Any]:
        hostname = validate_hostname(hostname)
        sdk = await self.sdk(serial)
        if sdk is None or sdk < MIN_PRIVATE_DNS_SDK:
            raise AdbError(
                f"Private DNS needs Android 9 (SDK {MIN_PRIVATE_DNS_SDK}) or newer; this device reports SDK {sdk}",
                "Use the manual/router options from get_setup_options instead.",
            )
        before = await self.get_private_dns(serial)
        # Specifier first, so the device never runs mode=hostname with a stale or empty name.
        await self.put_setting(serial, "private_dns_specifier", hostname)
        await self.put_setting(serial, "private_dns_mode", "hostname")
        after = await self.get_private_dns(serial)
        return {
            "sdk": sdk,
            "before": before,
            "after": after,
            "verified": after == {"mode": "hostname", "specifier": hostname},
        }

    async def clear_private_dns(self, serial: str) -> dict[str, Any]:
        before = await self.get_private_dns(serial)
        await self.put_setting(serial, "private_dns_mode", "opportunistic")
        after = await self.get_private_dns(serial)
        return {"before": before, "after": after, "verified": after.get("mode") == "opportunistic"}
