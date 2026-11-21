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

    async def _post(self, path: str, payload: Any) -> Any:
        try:
            r = await self._http.post(path, json=payload)
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

    async def check_host(self, name: str, client: str | None = None) -> dict:
        return await self._get("/control/filtering/check_host", name=name, client=client)

    # --- persistent clients ---------------------------------------------
    async def clients(self) -> list[dict]:
        data = await self._get("/control/clients")
        return list((data or {}).get("clients") or [])

    @staticmethod
    def _client_body(name: str, ids: list[str]) -> dict:
        return {
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

    async def add_client(self, name: str, ids: list[str]) -> None:
        await self._post("/control/clients/add", self._client_body(name, ids))

    async def update_client(self, name: str, ids: list[str]) -> None:
        await self._post("/control/clients/update", {"name": name, "data": self._client_body(name, ids)})

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
