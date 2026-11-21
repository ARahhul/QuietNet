"""A stand-in for AdGuard Home's admin API so Pahra runs end-to-end before the VM exists.

Implements the endpoints Pahra uses, evaluates `||domain^$client=...` / `@@` rules
per client exactly like the real server would for our rule shapes, and generates
scripted traffic for every persistent client (profile picked from the client name:
"lg"/"tv" -> LG webOS, "samsung", "fire"/"amazon", "google"/"android-tv", else phone).

Run:  python -m devtools.fake_adguard --port 3100 [--clients rahhul-phone,rahhul-lg-tv]
Then: ADGUARD_URL=http://127.0.0.1:3100
"""

import argparse
import asyncio
import random
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, Query
from pydantic import BaseModel

# (fqdn, period seconds) — periods are jittered ±15 % except beacons
PROFILES: dict[str, list[tuple[str, float]]] = {
    "lg": [
        ("alphonso.tv", 20), ("tkacr.alphonso.tv", 20),           # ACR beacon (Live Plus)
        ("us.info.lgsmartad.com", 45), ("us.ad.lgsmartad.com", 60),
        ("ngfts.lge.com", 90), ("snu.lge.com", 120), ("aic-ngfts.lge.com", 120),
        ("lgtvonline.lge.com", 150), ("us.lgtvsdp.com", 100), ("eic.lgtvsdp.com", 110),
        ("nflxvideo.net", 40), ("api-global.netflix.com", 60),
        ("youtube.com", 70), ("googlevideo.com", 50), ("doubleclick.net", 80),
        ("time.lge.com", 300),
    ],
    "samsung": [
        ("acr0.samsungcloudsolution.com", 20), ("log-config.samsungacr.com", 25),
        ("samsungads.com", 60), ("config.samsungads.com", 60), ("ad.samsungadhub.com", 90),
        ("gpm.samsungqbe.com", 120), ("lcprd1.samsungcloudsolution.net", 150),
        ("time.samsungcloudsolution.com", 300), ("osb.samsungqbe.com", 200),
        ("nflxvideo.net", 40), ("api-global.netflix.com", 60), ("primevideo.com", 90),
    ],
    "fire": [
        ("device-metrics-us.amazon.com", 25), ("device-metrics-us-2.amazon.com", 30),
        ("mads.amazon-adsystem.com", 60), ("aax-us-east.amazon-adsystem.com", 60),
        ("api.amazon.com", 90), ("atv-ext.amazon.com", 120), ("primevideo.com", 90),
        ("crashlytics.com", 150), ("spectrum.s3.amazonaws.com", 200), ("nflxvideo.net", 40),
    ],
    "google": [
        ("clients4.google.com", 60), ("play.googleapis.com", 90), ("android.clients.google.com", 120),
        ("googleads.g.doubleclick.net", 45), ("app-measurement.com", 40), ("googlevideo.com", 30),
        ("youtube.com", 60), ("connectivitycheck.gstatic.com", 120), ("time.android.com", 300),
    ],
    "phone": [
        ("googleads.g.doubleclick.net", 30), ("pagead2.googlesyndication.com", 40),
        ("app-measurement.com", 35), ("graph.facebook.com", 45), ("firebase-settings.crashlytics.com", 120),
        ("connect.facebook.net", 70), ("api.mixpanel.com", 90), ("in.appcenter.ms", 150),
        ("ads.mopub.com", 80), ("analytics.tiktok.com", 60), ("bat.bing.com", 120),
        ("metrics.icloud.com", 200), ("www.google.com", 60), ("whatsapp.net", 45),
        ("youtube.com", 50), ("googlevideo.com", 30), ("connectivitycheck.gstatic.com", 120),
        ("news.example-news.in", 90), ("xjq3k9zt7w2p.top", 400),
    ],
}


def profile_for(name: str) -> str:
    n = name.lower()
    if "samsung" in n:
        return "samsung"
    if "fire" in n or "amazon" in n:
        return "fire"
    if "google" in n or "android-tv" in n or "sony" in n:
        return "google"
    if "lg" in n or "tv" in n:
        return "lg"
    return "phone"


def _ag_time(dt: datetime) -> str:
    # Real AdGuard emits RFC3339 with nanoseconds and a local offset.
    ist = timezone(timedelta(hours=5, minutes=30))
    s = dt.astimezone(ist).isoformat(timespec="microseconds")
    return s[:-6] + "123" + s[-6:]


@dataclass
class Rule:
    raw: str
    allow: bool
    domain: str
    clients: list[str]


def parse_rule(line: str) -> Rule | None:
    line = line.strip()
    if not line or line.startswith("!") or line.startswith("#"):
        return None
    allow = line.startswith("@@")
    body = line[2:] if allow else line
    if not body.startswith("||"):
        return None
    pattern, _, mods = body[2:].partition("$")
    clients: list[str] = []
    for m in mods.split(","):
        if m.startswith("client="):
            clients = [c.strip().strip("'\"") for c in m[len("client="):].split("|")]
    return Rule(line, allow, pattern.rstrip("^").lower(), clients)


@dataclass
class State:
    rules: list[str] = field(default_factory=list)
    clients: dict[str, dict] = field(default_factory=dict)
    access: dict = field(default_factory=lambda: {"allowed_clients": [], "disallowed_clients": [], "blocked_hosts": []})
    dns: dict = field(default_factory=lambda: {"blocked_response_ttl": 10, "cache_ttl_max": 0})
    log: list[dict] = field(default_factory=list)  # newest first
    next_due: dict[tuple[str, str], datetime] = field(default_factory=dict)

    def parsed(self) -> list[Rule]:
        return [r for r in (parse_rule(x) for x in self.rules) if r]

    def match(self, qname: str, client: str) -> tuple[bool, list[str]]:
        q = qname.lower().strip(".")
        hits_block, hits_allow = [], []
        for r in self.parsed():
            if not (q == r.domain or q.endswith("." + r.domain)):
                continue
            if r.clients and client not in r.clients:
                continue
            (hits_allow if r.allow else hits_block).append(r.raw)
        if hits_allow:
            return False, hits_allow
        return bool(hits_block), hits_block

    def record(self, client: str, qname: str, when: datetime | None = None) -> dict:
        when = when or datetime.now(timezone.utc)
        blocked, rules = self.match(qname, client)
        entry = {
            "answer": [] if blocked else [{"type": "A", "value": "203.0.113.%d" % random.randint(1, 254), "ttl": 30}],
            "cached": False,
            "client": "10.0.0.%d" % (abs(hash(client)) % 250 + 2),
            "client_id": client,
            "client_info": {"name": client, "whois": {}, "disallowed": False, "disallowed_rule": ""},
            "client_proto": "dot",
            "elapsedMs": "1.8",
            "question": {"class": "IN", "name": qname, "type": "A"},
            "reason": "FilteredBlackList" if blocked else "NotFilteredNotFound",
            "rules": [{"filter_list_id": 0, "text": r} for r in rules],
            "status": "NXDOMAIN" if blocked else "NOERROR",
            "time": _ag_time(when),
            "upstream": "" if blocked else "tls://1.1.1.1",
        }
        self.log.insert(0, entry)
        if len(self.log) > 1 and self.log[1]["time"] > entry["time"]:  # back-dated entry: keep newest-first order
            self.log.sort(key=lambda e: e["time"], reverse=True)
        del self.log[5000:]
        return entry

    def tick(self, now: datetime) -> None:
        for name in list(self.clients):
            prof = PROFILES[profile_for(name)]
            for fqdn, period in prof:
                key = (name, fqdn)
                due = self.next_due.get(key)
                if due is None:
                    due = now + timedelta(seconds=random.uniform(0, min(period, 15)))
                    self.next_due[key] = due
                if now >= due:
                    self.record(name, fqdn, now)
                    beacon = period <= 25
                    jitter = 0.03 if beacon else 0.15
                    self.next_due[key] = now + timedelta(seconds=period * random.uniform(1 - jitter, 1 + jitter))


def create_app(seed_clients: list[str] | None = None, generate: bool = True) -> FastAPI:
    state = State()
    for c in seed_clients or []:
        state.clients[c] = {"name": c, "ids": [c]}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        task = None
        if generate:
            async def loop():
                while True:
                    state.tick(datetime.now(timezone.utc))
                    await asyncio.sleep(1)
            task = asyncio.create_task(loop())
        yield
        if task:
            task.cancel()

    app = FastAPI(title="Fake AdGuard Home", lifespan=lifespan)
    app.state.ag = state

    @app.get("/control/status")
    async def status():
        return {"running": True, "version": "fake-0.107", "protection_enabled": True, "dns_port": 53}

    @app.get("/control/querylog")
    async def querylog(older_than: str | None = None, limit: int = 500, search: str | None = None):
        entries = state.log
        if older_than:
            entries = [e for e in entries if e["time"] < older_than]
        if search:
            s = search.lower()
            entries = [e for e in entries if s in e["client_id"] or s in e["question"]["name"]]
        page = entries[:limit]
        return {"data": page, "oldest": page[-1]["time"] if page else ""}

    @app.get("/control/filtering/status")
    async def filtering_status():
        return {"enabled": True, "interval": 24, "filters": [], "whitelist_filters": [], "user_rules": state.rules}

    class Rules(BaseModel):
        rules: list[str]

    @app.post("/control/filtering/set_rules")
    async def set_rules(body: Rules):
        state.rules = list(body.rules)
        return {}

    @app.get("/control/filtering/check_host")
    async def check_host(name: str, client: str = ""):
        blocked, rules = state.match(name, client)
        return {"reason": "FilteredBlackList" if blocked else "NotFilteredNotFound",
                "rules": [{"text": r, "filter_list_id": 0} for r in rules]}

    @app.get("/control/clients")
    async def clients():
        return {"clients": list(state.clients.values()), "auto_clients": [], "supported_tags": []}

    @app.post("/control/clients/add")
    async def add_client(body: dict):
        state.clients[body["name"]] = body
        return {}

    @app.post("/control/clients/update")
    async def update_client(body: dict):
        state.clients.pop(body["name"], None)
        state.clients[body["data"]["name"]] = body["data"]
        return {}

    @app.post("/control/clients/delete")
    async def delete_client(body: dict):
        state.clients.pop(body["name"], None)
        return {}

    @app.get("/control/access/list")
    async def access_list():
        return state.access

    @app.post("/control/access/set")
    async def access_set(body: dict):
        state.access = body
        return {}

    @app.get("/control/dns_info")
    async def dns_info():
        return state.dns

    @app.post("/control/dns_config")
    async def dns_config(body: dict):
        state.dns.update(body)
        return {}

    # --- test / demo helpers (not part of AdGuard) ----------------------
    class Inject(BaseModel):
        client: str
        domain: str

    @app.post("/fake/query")
    async def inject(body: Inject):
        e = state.record(body.client, body.domain)
        return {"blocked": e["reason"].startswith("Filtered"), "status": e["status"]}

    @app.get("/fake/resolve")
    async def resolve(name: str = Query(...), client: str = Query(...)):
        blocked, rules = state.match(name, client)
        return {"status": "NXDOMAIN" if blocked else "NOERROR", "rules": rules}

    return app


def main() -> None:
    import uvicorn

    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=3100)
    p.add_argument("--clients", default="", help="comma-separated ClientIDs that start talking immediately")
    a = p.parse_args()
    seed = [c for c in a.clients.split(",") if c]
    uvicorn.run(create_app(seed), host="127.0.0.1", port=a.port, log_level="warning")


if __name__ == "__main__":
    main()
