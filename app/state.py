"""Shared application state: auth + bridge + cached zone data."""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from pathlib import Path

import httpx

from .auth import AuthError, TadoAuth
from .bridge import BridgeClient, discover_bridge
from .homekit import HomeKitManager
from .local import LocalStore

log = logging.getLogger("tado-local")

CLOUD_API = "https://my.tado.com/api/v2"
POLL_INTERVAL = 15.0  # seconds, jittered +-20%
RESCAN_AFTER_FAILURES = 20  # ~5 min of failed polls before re-running mDNS


class StateError(Exception):
    def __init__(self, message: str, status_code: int = 502):
        super().__init__(message)
        self.status_code = status_code


class State:
    def __init__(self, data_dir: str | Path):
        self.data_dir = Path(data_dir)
        self.auth = TadoAuth(self.data_dir / "auth.json")
        self.config = self._load_config()
        self.homekit = HomeKitManager(
            self.data_dir / "homekit.json", self.config.get("bridge_ip"), self.config.get("bridge_name")
        )
        self.store = LocalStore(self.data_dir / "tado.db")
        self.store.seed_from_cloud_cache(self.config.get("rooms") or {})
        self.http = httpx.AsyncClient(timeout=12)
        self.zones: dict[int, dict] = {}
        self.capabilities: dict[int, dict] = {}
        self.bridge_ok = False
        self.bridge_error: str | None = None
        self.last_poll: float | None = None
        self.presence: str | None = None
        self.zone_ids: dict[str, int] = {}  # device serial -> cloud zone id (for HomeKit zones)
        for serial, room in (self.config.get("rooms") or {}).items():  # cached: no cloud call at start
            self.homekit.names[serial] = room["name"]
            self.zone_ids[serial] = room["zone_id"]
        self._failures = 0
        self._poller_task: asyncio.Task | None = None

    # -- config -----------------------------------------------------------
    def _load_config(self) -> dict:
        path = self.data_dir / "config.json"
        if path.exists():
            try:
                return json.loads(path.read_text())
            except (json.JSONDecodeError, OSError):
                pass
        return {}

    def _save_config(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        (self.data_dir / "config.json").write_text(json.dumps(self.config, indent=2))

    @property
    def home_id(self) -> int | None:
        return self.config.get("home_id")

    @property
    def bridge_ip(self) -> str | None:
        return self.config.get("bridge_ip")

    # -- HTTP helpers -------------------------------------------------------
    @property
    def local_only(self) -> bool:
        return bool(self.config.get("local_only"))

    async def cloud_request(self, method: str, path: str, json_body: dict | None = None) -> httpx.Response:
        if self.local_only:
            raise StateError("local-only mode: this app doesn't contact tado.com", 409)
        token = await self.auth.ensure_access_token()
        return await self.http.request(
            method,
            f"{CLOUD_API}{path}",
            headers={"Authorization": f"Bearer {token}"},
            json=json_body,
        )

    async def bridge_request(self, method: str, path: str, json_body: dict | None = None) -> httpx.Response:
        """Call the bridge's local API.

        Local-first: reuse the cached token even when close to expiry (the
        bridge validates it on-device); only contact the tado cloud to refresh
        it after a 401. If a fresh cloud token is rejected too, upgrade once
        to the dedicated long-lived local token.
        """
        if not self.bridge_ip:
            raise StateError("bridge not discovered yet", 409)
        if not self.auth.is_authenticated():
            raise StateError("connect your tado account first", 401)
        token = self.config.get("local_token") or self.auth.access_token
        if not token:
            token = await self.auth.ensure_access_token()
        client = BridgeClient(self.bridge_ip, self.http)
        try:
            r = await client.request(method, path, token, json_body=json_body)
        except (httpx.ConnectError, httpx.TimeoutException, httpx.WriteError) as exc:
            self.bridge_ok = False
            self.bridge_error = str(exc)
            raise StateError(f"bridge unreachable: {exc}") from exc
        if r.status_code == 401:
            try:
                fresh = await self.auth.ensure_access_token()
            except AuthError:
                fresh = None
            if fresh and fresh != token:
                r = await client.request(method, path, fresh, json_body=json_body)
            if r.status_code == 401 and not self.config.get("local_token"):
                local = await self._fetch_local_token()
                if local:
                    r = await client.request(method, path, local, json_body=json_body)
        self.bridge_ok = r.status_code < 400
        self.bridge_error = None if self.bridge_ok else f"HTTP {r.status_code}: {r.text[:200]}"
        return r

    async def _fetch_local_token(self) -> str | None:
        """Some firmwares reject the cloud token; tado hands out a local one."""
        try:
            r = await self.cloud_request("GET", f"/homes/{self.home_id}/token")
            if r.status_code == 200:
                token = r.json().get("token")
                if token:
                    self.config["local_token"] = token
                    self._save_config()
                    log.info("upgraded to dedicated local bridge token")
                    return token
        except Exception as exc:  # noqa: BLE001 - best effort upgrade
            log.warning("could not fetch local token: %s", exc)
        return None

    # -- setup flow ---------------------------------------------------------
    async def after_login(self) -> None:
        r = await self.cloud_request("GET", "/me")
        if r.status_code >= 400:
            raise StateError(f"cloud /me failed: HTTP {r.status_code}")
        me = r.json()
        homes = me.get("homes") or []
        if not homes:
            raise StateError("no tado home on this account")
        self.config["home_id"] = homes[0]["id"]
        self.config["home_name"] = homes[0].get("name")
        self._save_config()
        if not self.bridge_ip:
            await self.rescan_bridge()
        await self.load_room_names()

    async def rescan_bridge(self) -> dict | None:
        found = await asyncio.to_thread(discover_bridge, 5.0)
        if found:
            self.config["bridge_ip"] = found["ip"]
            self.config["bridge_name"] = found["name"]
            self.homekit.bridge_ip, self.homekit.bridge_name = found["ip"], found["name"]
            self._save_config()
        return found

    # -- data ---------------------------------------------------------------
    async def load_room_names(self) -> None:
        """One cloud call to map device serials to tado rooms; cached in config.json."""
        if not self.auth.is_authenticated() or not self.home_id or self.config.get("rooms"):
            return
        try:
            r = await self.cloud_request("GET", f"/homes/{self.home_id}/zones")
            if r.status_code != 200:
                return
            for zone in r.json():
                for dev in zone.get("devices") or []:
                    if dev.get("serialNo"):
                        self.homekit.names[dev["serialNo"]] = zone.get("name")
                        self.zone_ids[dev["serialNo"]] = zone["id"]
            self.config["rooms"] = {
                serial: {"name": self.homekit.names[serial], "zone_id": zid} for serial, zid in self.zone_ids.items()
            }
            self._save_config()
        except Exception as exc:  # noqa: BLE001 - names are cosmetic
            log.warning("could not load room names: %s", exc)

    async def device_health(self) -> dict[str, dict]:
        """One cloud call: tado's view of each device (online, battery, last seen)."""
        if not self.auth.is_authenticated() or not self.home_id:
            raise StateError("checking devices needs the tado account: click Connect tado account", 409)
        r = await self.cloud_request("GET", f"/homes/{self.home_id}/zones")
        if r.status_code != 200:
            raise StateError(f"tado cloud zones failed: HTTP {r.status_code}")
        health = {}
        for zone in r.json():
            for dev in zone.get("devices") or []:
                conn = dev.get("connectionState") or {}
                health[dev.get("serialNo")] = {
                    "online": conn.get("value"),
                    "last_seen": conn.get("timestamp"),
                    "battery": dev.get("batteryState"),
                    "firmware": dev.get("currentFwVersion"),
                }
        return health

    async def poll_once(self) -> None:
        if self.homekit.is_paired():
            return  # HomeKit pushes its own updates
        if not self.auth.is_authenticated() or not self.home_id or not self.bridge_ip:
            return
        home = self.home_id
        r = await self.bridge_request("GET", f"/homes/{home}/zones")
        if r.status_code >= 400:
            raise StateError(f"bridge zones failed: HTTP {r.status_code}: {r.text[:200]}")
        zones = r.json()
        merged: dict[int, dict] = {}
        for zone in zones:
            zid = zone["id"]
            entry = {"zone": zone}
            try:
                rs = await self.bridge_request("GET", f"/homes/{home}/zones/{zid}/state")
                if rs.status_code == 200:
                    entry["state"] = rs.json()
            except StateError as exc:
                log.warning("zone %s state failed: %s", zid, exc)
            if zid not in self.capabilities:
                try:
                    rc = await self.bridge_request("GET", f"/homes/{home}/zones/{zid}/capabilities")
                    if rc.status_code == 200:
                        self.capabilities[zid] = rc.json()
                except StateError:
                    pass
            entry["capabilities"] = self.capabilities.get(zid)
            merged[zid] = entry
        self.zones = merged
        self.last_poll = time.time()
        self._failures = 0

    async def poller(self) -> None:
        while True:
            try:
                await self.poll_once()
            except (StateError, AuthError, httpx.HTTPError) as exc:
                self._failures += 1
                log.warning("poll failed (%s): %s", self._failures, exc)
                if self._failures == RESCAN_AFTER_FAILURES and self.auth.is_authenticated():
                    log.info("re-scanning for bridge (mDNS)...")
                    try:
                        await self.rescan_bridge()
                    except Exception as exc2:  # noqa: BLE001
                        log.warning("bridge rescan failed: %s", exc2)
                    self._failures = 0
            await asyncio.sleep(POLL_INTERVAL * (1 + random.uniform(-0.2, 0.2)))

    def start_poller(self) -> None:
        if self._poller_task is None or self._poller_task.done():
            self._poller_task = asyncio.create_task(self.poller())

    # -- presentation ---------------------------------------------------------
    def status(self) -> dict:
        return {
            "local_only": self.local_only,
            "authenticated": self.auth.is_authenticated(),
            "pending_device": self.auth.pending_info(),
            "home": {"id": self.home_id, "name": self.config.get("home_name")}
            if self.home_id
            else None,
            "bridge": {
                "ip": self.bridge_ip,
                "name": self.config.get("bridge_name"),
                "ok": self.homekit.connected if self.homekit.is_paired() else self.bridge_ok,
                "error": self.homekit.error if self.homekit.is_paired() else self.bridge_error,
            },
            "homekit": self.homekit.status(),
            "zones": len(self.homekit.zones) if self.homekit.is_paired() else len(self.zones),
            "last_poll": self.last_poll,
        }

    def zones_payload(self) -> list[dict]:
        if self.homekit.is_paired():
            rows = self.homekit.zones_payload()
            room_of = self.store.room_of()
            if unknown := [r["serial"] for r in rows if r["serial"] not in room_of]:
                self.store.register_devices(unknown)
            names = {r["id"]: r["name"] for r in self.store.rooms()}
            for row in rows:
                row["room_id"] = room_of.get(row["serial"])
                row["room"] = names.get(row["room_id"])
            return rows
        return [entry | {"id": zid} for zid, entry in sorted(self.zones.items())]

    async def close(self) -> None:
        await self.homekit.stop()
        await self.http.aclose()
