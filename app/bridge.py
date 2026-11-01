"""tado Internet Bridge discovery (mDNS) and local HTTP client.

The classic bridge exposes the same REST API as my.tado.com on port 80, but
only answers when the request carries the cloud API Host header and a valid
bearer token. That gives full local control: no cloud round-trip, no rate
limits.
"""

from __future__ import annotations

import threading

import httpx
from zeroconf import ServiceBrowser, ServiceListener, Zeroconf

HAP_SERVICE_TYPE = "_hap._tcp.local."
CLOUD_HOST = "my.tado.com"


class _TadoListener(ServiceListener):
    def __init__(self) -> None:
        self.found: list[dict] = []
        self._event = threading.Event()

    def add_service(self, zc: Zeroconf, type_: str, name: str) -> None:
        if "tado" not in name.lower():
            return
        info = zc.get_service_info(type_, name, timeout=2500)
        if info and info.parsed_addresses():
            self.found.append(
                {
                    "name": name.removesuffix(f".{type_}"),
                    "ip": info.parsed_addresses()[0],
                }
            )
            self._event.set()

    def remove_service(self, zc: Zeroconf, type_: str, name: str) -> None:
        self.found = [f for f in self.found if f["name"] != name.removesuffix(f".{type_}")]

    def update_service(self, zc: Zeroconf, type_: str, name: str) -> None:
        pass


def discover_bridge(timeout: float = 5.0) -> dict | None:
    """Look for a tado bridge on the local network via mDNS/HomeKit."""
    zc = Zeroconf()
    listener = _TadoListener()
    browser = ServiceBrowser(zc, HAP_SERVICE_TYPE, listener)
    try:
        listener._event.wait(timeout)
    finally:
        browser.cancel()
        zc.close()
    return listener.found[0] if listener.found else None


class BridgeClient:
    """Talks to the bridge's local /api/v2 REST API."""

    def __init__(self, ip: str, http: httpx.AsyncClient):
        self.ip = ip
        self.http = http

    def _headers(self, token: str) -> dict:
        return {"Host": CLOUD_HOST, "Authorization": f"Bearer {token}"}

    async def request(
        self,
        method: str,
        path: str,
        token: str,
        *,
        json_body: dict | None = None,
    ) -> httpx.Response:
        """Return the raw response so callers can inspect status codes."""
        return await self.http.request(
            method,
            f"http://{self.ip}/api/v2{path}",
            headers=self._headers(token),
            json=json_body,
        )
