import pytest

from pahra_companion import adb as adbmod
from pahra_companion.adb import Adb, AdbCommandRejected, AdbError, AdbMissing

SERIAL = "192.168.1.34:5555"


class FakeAdb:
    """Mocked `adb` subprocess: records argv and emulates one TV's state."""

    def __init__(self, sdk="30", state="device", connect_out=None, settings=None):
        self.calls: list[list[str]] = []
        self.sdk = sdk
        self.state = state
        self.connect_out = connect_out or f"connected to {SERIAL}"
        self.settings = dict(settings or {"private_dns_mode": "opportunistic"})

    async def __call__(self, argv, timeout):
        assert argv[0] == "/fake/adb"
        args = argv[1:]
        self.calls.append(args)
        if args == ["start-server"]:
            return 0, "", ""
        if args == ["devices"]:
            rows = f"{SERIAL}\t{self.state}\n" if self.state else ""
            return 0, f"List of devices attached\n{rows}\n", ""
        if args[0] == "connect":
            return 0, self.connect_out + "\n", ""
        if args[0] == "pair":
            ok = args[2] == "123456"
            return (0, f"Successfully paired to {args[1]} [guid=adb-XYZ]\n", "") if ok else (1, "", "Failed: Wrong password\n")
        if args[:3] == ["-s", SERIAL, "shell"]:
            cmd = args[3:]
            if cmd[0] == "getprop":
                return 0, {"ro.build.version.sdk": self.sdk, "ro.product.manufacturer": "Amazon",
                           "ro.product.model": "AFTKA"}[cmd[1]] + "\r\n", ""
            if cmd[:3] == ["settings", "get", "global"]:
                return 0, self.settings.get(cmd[3], "null") + "\n", ""
            if cmd[:3] == ["settings", "put", "global"]:
                self.settings[cmd[3]] = cmd[4]
                return 0, "", ""
        raise AssertionError(f"unexpected adb call {args}")


def make(fake: FakeAdb) -> Adb:
    return Adb(adb_path="/fake/adb", runner=fake)


# ---------------------------------------------------------------- validation / allowlist


@pytest.mark.parametrize(
    "hostname",
    [
        "x; rm -rf /",
        "tv.dns.example.com; reboot",
        "$(reboot).dns.example.com",
        "`id`.example.com",
        "tv.dns.example.com && settings put global adb_enabled 0",
        "a|b.example.com",
        "tv dns.example.com",
        "tv.dns.example.com\nreboot",
        "-rf",
        "",
        "localhost",  # needs at least one dot
        "-tv.example.com",
        "tv-.example.com",
        "tv..example.com",
        "tv.example.com.",
        "x" * 64 + ".example.com",
        ("a" * 60 + ".") * 5 + "com",  # > 253 chars
        "tv_1.example.com",
        "тв.example.com",
    ],
)
def test_hostname_validation_rejects_injection_and_junk(hostname):
    with pytest.raises(AdbCommandRejected):
        adbmod.validate_hostname(hostname)


@pytest.mark.parametrize(
    "hostname,expected",
    [("tv-1a2b.dns.pahra.example", "tv-1a2b.dns.pahra.example"), ("TV-1.DNS.Example.COM", "tv-1.dns.example.com"), ("a.b", "a.b")],
)
def test_hostname_validation_accepts_dns_names(hostname, expected):
    assert adbmod.validate_hostname(hostname) == expected


@pytest.mark.parametrize(
    "args",
    [
        ["shell", "rm", "-rf", "/"],
        ["-s", SERIAL, "shell", "rm", "-rf", "/"],
        ["-s", SERIAL, "shell", "settings", "put", "global", "adb_enabled", "0"],
        ["-s", SERIAL, "shell", "settings", "put", "secure", "private_dns_mode", "off"],
        ["-s", SERIAL, "shell", "settings", "put", "global", "private_dns_mode", "hostname; reboot"],
        ["-s", SERIAL, "shell", "settings", "put", "global", "private_dns_specifier", "x; rm -rf /"],
        ["-s", SERIAL, "shell", "settings", "put", "global", "private_dns_specifier", "TV.Example.com"],
        ["-s", SERIAL, "shell", "getprop", "ro.serialno"],
        ["-s", SERIAL, "shell", "getprop"],
        ["-s", SERIAL, "shell", "settings", "get", "global", "private_dns_mode", "extra"],
        ["-s", "x; rm -rf /", "shell", "getprop", "ro.build.version.sdk"],
        ["-s", "192.168.1.34:5555;id", "get-state"],
        ["-s", "192.168.1.300:5555", "get-state"],
        ["-s", "192.168.1.34:70000", "get-state"],
        ["install", "evil.apk"],
        ["push", "a", "/sdcard/a"],
        ["root"],
        ["reboot"],
        ["tcpip", "5555"],
        ["connect", "evil.example.com:5555"],
        ["connect", "192.168.1.34:5555", "extra"],
        ["pair", SERIAL, "12345"],
        ["pair", SERIAL, "123456; id"],
        ["devices", "-l"],
        [],
    ],
)
def test_allowlist_rejects(args):
    with pytest.raises(AdbCommandRejected):
        adbmod.validate_argv(args)


@pytest.mark.parametrize(
    "args",
    [
        ["devices"],
        ["start-server"],
        ["connect", SERIAL],
        ["disconnect", SERIAL],
        ["pair", "192.168.1.33:41231", "123456"],
        ["-s", SERIAL, "get-state"],
        ["-s", SERIAL, "shell", "getprop", "ro.build.version.sdk"],
        ["-s", SERIAL, "shell", "getprop", "ro.product.model"],
        ["-s", SERIAL, "shell", "settings", "get", "global", "private_dns_specifier"],
        ["-s", SERIAL, "shell", "settings", "put", "global", "private_dns_mode", "opportunistic"],
        ["-s", SERIAL, "shell", "settings", "put", "global", "private_dns_specifier", "tv-1.dns.example.com"],
    ],
)
def test_allowlist_accepts(args):
    adbmod.validate_argv(args)


async def test_run_never_reaches_subprocess_for_rejected_commands():
    fake = FakeAdb()
    with pytest.raises(AdbCommandRejected):
        await make(fake).run(["-s", SERIAL, "shell", "reboot"])
    assert fake.calls == []


async def test_set_private_dns_rejects_bad_hostname_before_any_call():
    fake = FakeAdb()
    with pytest.raises(AdbCommandRejected):
        await make(fake).set_private_dns(SERIAL, "x; rm -rf /")
    assert fake.calls == []


# ---------------------------------------------------------------- mocked subprocess flows


async def test_missing_adb_is_reported_with_winget_hint(monkeypatch):
    monkeypatch.setattr(adbmod, "find_adb", lambda: None)
    with pytest.raises(AdbMissing) as exc:
        await Adb().devices()
    assert "winget install Google.PlatformTools" in exc.value.hint


def test_find_adb_honours_adb_path(monkeypatch, tmp_path):
    exe = tmp_path / "adb.exe"
    exe.write_text("")
    monkeypatch.setenv("ADB_PATH", str(tmp_path))
    assert adbmod.find_adb() == str(exe)
    monkeypatch.setenv("ADB_PATH", str(exe))
    assert adbmod.find_adb() == str(exe)


async def test_start_server_runs_once_first():
    fake = FakeAdb()
    a = make(fake)
    await a.devices()
    await a.devices()
    assert fake.calls == [["start-server"], ["devices"], ["devices"]]


async def test_connect_success_and_device_info():
    fake = FakeAdb()
    a = make(fake)
    r = await a.connect(SERIAL)
    assert r["connected"] and r["state"] == "device"
    info = await a.device_info(SERIAL)
    assert info == {"sdk": 30, "manufacturer": "Amazon", "model": "AFTKA"}


async def test_connect_unauthorized():
    fake = FakeAdb(state="unauthorized")
    r = await make(fake).connect(SERIAL)
    assert r == {"connected": False, "state": "unauthorized", "output": f"connected to {SERIAL}"}


async def test_connect_refused():
    fake = FakeAdb(state=None, connect_out=f"cannot connect to {SERIAL}: No connection could be made (10061)")
    r = await make(fake).connect(SERIAL)
    assert r["connected"] is False and r["state"] is None


async def test_pair():
    a = make(FakeAdb())
    assert (await a.pair("192.168.1.33:41231", "123456"))["paired"] is True
    assert (await a.pair("192.168.1.33:41231", "654321"))["paired"] is False


async def test_set_private_dns_puts_both_settings_and_reads_back():
    fake = FakeAdb(sdk="28")
    r = await make(fake).set_private_dns(SERIAL, "TV-1.DNS.Example.com")
    assert r == {
        "sdk": 28,
        "before": {"mode": "opportunistic", "specifier": None},
        "after": {"mode": "hostname", "specifier": "tv-1.dns.example.com"},
        "verified": True,
    }
    puts = [c[3:] for c in fake.calls if c[3:6] == ["settings", "put", "global"]]
    assert puts == [
        ["settings", "put", "global", "private_dns_specifier", "tv-1.dns.example.com"],
        ["settings", "put", "global", "private_dns_mode", "hostname"],
    ]


async def test_set_private_dns_refuses_old_android():
    fake = FakeAdb(sdk="25")  # Android 7.1, e.g. Fire OS 6
    with pytest.raises(AdbError, match="SDK 28"):
        await make(fake).set_private_dns(SERIAL, "tv.dns.example.com")
    assert not any("put" in c for c in fake.calls)


async def test_clear_private_dns():
    fake = FakeAdb(settings={"private_dns_mode": "hostname", "private_dns_specifier": "tv.dns.example.com"})
    r = await make(fake).clear_private_dns(SERIAL)
    assert r["before"]["mode"] == "hostname"
    assert r["after"] == {"mode": "opportunistic", "specifier": "tv.dns.example.com"}
    assert r["verified"] is True


def test_parse_devices():
    text = (
        "* daemon not running; starting now at tcp:5037\n* daemon started successfully\n"
        "List of devices attached\n192.168.1.34:5555\tdevice\n192.168.1.35:5555\tunauthorized\nemulator-5554\toffline\n\n"
    )
    assert adbmod.parse_devices(text) == {
        "192.168.1.34:5555": "device",
        "192.168.1.35:5555": "unauthorized",
        "emulator-5554": "offline",
    }
