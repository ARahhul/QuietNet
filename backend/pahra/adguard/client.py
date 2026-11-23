"""Thin async client for the AdGuard Home admin API (reached via the SSH tunnel)."""

from typing import Any

import httpx


class AdGuardError(RuntimeError):
    pass


class AdGuardClient:
    def __init__(self, base_url: str, user: str, password: str, transport: httpx.AsyncBaseTransport | None = None):
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            auth=(user, password),
            timeout=httpx.Timeout(10.0, connect=5.0),
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _get(self, path: str, **params: Any) -> Any:
        try:
            r = await self._http.get(path, params={k: v for k, v in params.items() if v is not None})
        except httpx.HTTPError as e:
            raise AdGuardError(f"AdGuard unreachable ({e.__class__.__name__}): is the SSH tunnel up?") from e
        if r.status_code >= 400:
            raise AdGuardError(f"GET {path} -> {r.status_code}: {r.text[:200]}")
        return r.json() if r.content else None

    async def _post(self, path: str, payload: Any, timeout: float | None = None) -> Any:
        try:
            r = await self._http.post(path, json=payload, **({"timeout": timeout} if timeout else {}))
        except httpx.HTTPError as e:
            raise AdGuardError(f"AdGuard unreachable ({e.__class__.__name__}): is the SSH tunnel up?") from e
        if r.status_code >= 400:
            raise AdGuardError(f"POST {path} -> {r.status_code}: {r.text[:200]}")
        if not r.content:
            return None
        try:
            return r.json()
        except ValueError:
            return r.text

    # --- status ---------------------------------------------------------
    async def status(self) -> dict:
        return await self._get("/control/status")

    # --- query log ------------------------------------------------------
    async def querylog(self, older_than: str | None = None, limit: int = 500, search: str | None = None) -> dict:
        return await self._get("/control/querylog", older_than=older_than, limit=limit, search=search)

    # --- user rules -----------------------------------------------------
    async def user_rules(self) -> list[str]:
        st = await self._get("/control/filtering/status")
        return list(st.get("user_rules") or [])

    async def set_rules(self, rules: list[str]) -> None:
        await self._post("/control/filtering/set_rules", {"rules": rules})

    # --- filter lists -----------------------------------------------------
    async def filters(self) -> list[dict]:
        st = await self._get("/control/filtering/status")
        return list(st.get("filters") or [])

    async def add_filter(self, name: str, url: str) -> None:
        """`url` may be a path on AdGuard's own disk (it must match its `safe_fs_patterns`). AdGuard reads it now."""
        await self._post("/control/filtering/add_url", {"name": name, "url": url, "whitelist": False}, timeout=120.0)

    async def set_filter(self, url: str, name: str, enabled: bool = True) -> None:
        await self._post(
            "/control/filtering/set_url",
            {"url": url, "whitelist": False, "data": {"name": name, "url": url, "enabled": enabled}},
            timeout=120.0,
        )

    async def remove_filter(self, url: str) -> None:
        await self._post("/control/filtering/remove_url", {"url": url, "whitelist": False})

    async def refresh_filters(self) -> None:
        """Re-read every block list now (AdGuard only reloads the ones whose content changed)."""
        await self._post("/control/filtering/refresh", {"whitelist": False}, timeout=120.0)

    async def check_host(self, name: str, client: str | None = None) -> dict:
        return await self._get("/control/filtering/check_host", name=name, client=client)

    # --- persistent clients ---------------------------------------------
    async def clients(self) -> list[dict]:
        data = await self._get("/control/clients")
        return list((data or {}).get("clients") or [])

    @staticmethod
    def _client_body(name: str, ids: list[str], parental: dict | None = None) -> dict:
        """`parental` (pahra.parental.client_settings): adult-site filter and SafeSearch for this client only. Without
        it the client follows AdGuard's global settings, as every client did before parental controls."""
        body = {
            "name": name,
            "ids": ids,
            "use_global_settings": True,
            "filtering_enabled": True,
            "parental_enabled": False,
            "safebrowsing_enabled": False,
            "use_global_blocked_services": True,
            "blocked_services": [],
            "upstreams": [],
            "tags": [],
        }
        if parental:
            body |= {
                "use_global_settings": False,
                "parental_enabled": bool(parental["parental_enabled"]),
                "safe_search": dict(parental["safe_search"]),
            }
        return body

    async def add_client(self, name: str, ids: list[str], parental: dict | None = None) -> None:
        await self._post("/control/clients/add", self._client_body(name, ids, parental))

    async def update_client(self, name: str, ids: list[str], parental: dict | None = None) -> None:
        await self._post("/control/clients/update", {"name": name, "data": self._client_body(name, ids, parental)})

    @staticmethod
    def client_matches(cur: dict, ids: list[str], parental: dict | None) -> bool:
        """Does AdGuard's client `cur` already have these ids and parental settings?"""
        if sorted(cur.get("ids") or []) != sorted(ids):
            return False
        if parental is None:
            return cur.get("use_global_settings", True) is not False
        if cur.get("use_global_settings") is not False or bool(cur.get("parental_enabled")) != parental["parental_enabled"]:
            return False
        ss = cur.get("safe_search") or {}
        return all(bool(ss.get(k)) == v for k, v in parental["safe_search"].items())

    # --- blocked services (the app catalog parental controls pick from) ---
    async def blocked_services_all(self) -> list[dict]:
        """[{id, name, icon_svg (base64), rules: ["||tiktok.com^", ...], group_id}] (group_id: AdGuard ≥ 0.107.44)."""
        data = await self._get("/control/blocked_services/all")
        return list((data or {}).get("blocked_services") or [])

    async def delete_client(self, name: str) -> None:
        await self._post("/control/clients/delete", {"name": name})

    # --- access list (who may use the resolver) -------------------------
    async def access_list(self) -> dict:
        return await self._get("/control/access/list")

    async def set_access(self, allowed: list[str], disallowed: list[str], blocked_hosts: list[str]) -> None:
        await self._post(
            "/control/access/set",
            {"allowed_clients": allowed, "disallowed_clients": disallowed, "blocked_hosts": blocked_hosts},
        )

    # --- DNS settings (demo TTLs) ---------------------------------------
    async def dns_info(self) -> dict:
        return await self._get("/control/dns_info")

    async def set_dns_config(self, **values: Any) -> None:
        await self._post("/control/dns_config", values)
