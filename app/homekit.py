"""HomeKit-protocol control of the tado Internet Bridge (fully local).

The bridge's local HTTP mirror of the cloud API is closed on current
firmware (port 80 is the HomeKit accessory server), but pairing with it as
a HomeKit controller gives direct LAN control of every zone with real-time
updates, no cloud, no rate limits. This is the same approach Home
Assistant's homekit_controller uses.
"""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path

import aiohomekit
from aiohomekit.exceptions import HomeKitException
from aiohomekit.controller.abstract import TransportType
from aiohomekit.controller.ip.discovery import IpDiscovery
from aiohomekit.zeroconf import HomeKitService
from zeroconf.asyncio import AsyncServiceBrowser, AsyncServiceInfo, AsyncZeroconf

log = logging.getLogger("tado-local.homekit")

# HomeKit characteristic/service short IDs
SVC_ACCESSORY_INFO = 0x3E
SVC_THERMOSTAT = 0x4A
SVC_HUMIDITY = 0x82
CHAR_NAME = 0x23
CHAR_MODEL = 0x21
CHAR_SERIAL = 0x30
CHAR_IDENTIFY = 0x14
CHAR_CURRENT_HUMIDITY = 0x10
CHAR_CURRENT_HEATING_COOLING = 0x0F
CHAR_TARGET_HEATING_COOLING = 0x33
CHAR_CURRENT_TEMP = 0x11
CHAR_TARGET_TEMP = 0x35

MODE_OFF = 0
MODE_HEAT = 1
MODE_AUTO = 3  # standard HAP; tado's bridge only accepts 0-1, so schedule resume goes via the cloud

POLL_INTERVAL = 60.0  # events push changes; this is just a safety net
RELOCATE_AFTER = 3  # failed polls before searching the subnet for the bridge


async def locate_bridge(name: str, near_ip: str) -> str | None:
    """Find the bridge by its HomeKit instance name (it embeds the serial, stable across
    resets) by asking every address of near_ip's /24 directly: unicast mDNS, which works
    from Docker, unlike multicast. A fresh Zeroconf keeps old cached addresses out."""
    prefix = near_ip.rsplit(".", 1)[0]
    azc = AsyncZeroconf()
    try:
        async def ask(ip: str) -> str | None:
            info = AsyncServiceInfo("_hap._tcp.local.", f"{name}._hap._tcp.local.")
            if await info.async_request(azc.zeroconf, 2000, addr=ip):
                addrs = info.parsed_addresses()
                return addrs[0] if addrs else ip
            return None

        candidates = [f"{prefix}.{i}" for i in range(1, 255)]
        for k in range(0, len(candidates), 32):
            for found in await asyncio.gather(*(ask(ip) for ip in candidates[k : k + 32])):
                if found:
                    return found
        return None
    finally:
        await azc.async_close()


def _stype(uuid: str) -> int:
    return int(uuid.split("-")[0], 16)


def normalize_pin(pin: str) -> str:
    """Accept '12345678', '123 45 678', '123-45-678' -> '123-45-678'."""
    digits = re.sub(r"\D", "", pin or "")
    if len(digits) != 8:
        raise HomeKitException("HomeKit code must have 8 digits (XXX-XX-XXX)")
    return f"{digits[:3]}-{digits[3:5]}-{digits[5:]}"


class HomeKitManager:
    def __init__(self, storage: Path, bridge_ip: str | None = None, bridge_name: str | None = None):
        self.storage = storage
        self.bridge_ip = bridge_ip
        self.bridge_name = bridge_name
        self.azc: AsyncZeroconf | None = None
        self.controller: aiohomekit.Controller | None = None
        self.pairing = None
        self.alias = "tado"
        self.connected = False
        self.error: str | None = None
        self.zones: dict[int, dict] = {}  # aid -> meta + iids
        self.state: dict[int, dict] = {}  # aid -> live values
        self.names: dict[str, str] = {}  # serial -> cloud room name
        self._poll_task: asyncio.Task | None = None
        self.on_bridge_moved = None  # callback(new_ip), set by the app to persist it

    # -- lifecycle -----------------------------------------------------------
    async def start(self) -> None:
        self.azc = AsyncZeroconf()
        # aiohomekit piggybacks on browsers the host app owns (like Home Assistant)
        self._browser = AsyncServiceBrowser(
            self.azc.zeroconf, ["_hap._tcp.local.", "_hap._udp.local."], handlers=[lambda **_: None]
        )
        self.controller = aiohomekit.Controller(async_zeroconf_instance=self.azc)
        await self.controller.async_start()
        if self.storage.exists():
            try:
                self.controller.load_data(str(self.storage))
            except Exception as exc:  # noqa: BLE001
                log.warning("could not load HomeKit pairing data: %s", exc)
            if self.alias in self.controller.aliases:
                self.pairing = self.controller.aliases[self.alias]
                self._wire_events()
                asyncio.create_task(self._connect())

    def is_paired(self) -> bool:
        return self.pairing is not None

    async def stop(self) -> None:
        if self._poll_task:
            self._poll_task.cancel()
        if self.pairing:
            try:
                await self.pairing.close()
            except Exception:  # noqa: BLE001
                pass
        if self.controller:
            await self.controller.async_stop()
        if self.azc:
            await self._browser.async_cancel()
            await self.azc.async_close()

    # -- pairing -------------------------------------------------------------
    async def _unicast_discovery(self) -> IpDiscovery | None:
        """Ask the known bridge IP directly (unicast mDNS on 5353).

        Always fresh (multicast caches keep the pre-reset record around) and it
        works from a Docker container, where multicast mDNS doesn't reach the LAN.
        """
        if not (self.bridge_ip and self.bridge_name):
            return None
        info = AsyncServiceInfo("_hap._tcp.local.", f"{self.bridge_name}._hap._tcp.local.")
        if not await info.async_request(self.azc.zeroconf, 3000, addr=self.bridge_ip):
            return None
        desc = HomeKitService.from_service_info(info)
        return IpDiscovery(self.controller.transports[TransportType.IP], desc)

    async def _find_bridge(self, timeout: float = 6.0) -> IpDiscovery:
        if found := await self._unicast_discovery():
            return found
        candidates = []
        async for d in self.controller.async_discover(timeout):
            if "tado" in (d.description.name or "").lower():
                candidates.append(d)
        if not candidates:
            raise HomeKitException("no tado bridge found on the network")
        # a HomeKit reset gives the bridge a new id; the old (paired) record can linger in the cache
        return next((d for d in candidates if not d.paired), candidates[0])

    async def pair(self, pin: str) -> dict:
        pin = normalize_pin(pin)
        discovery = await self._find_bridge()
        name = discovery.description.name
        if discovery.paired:
            raise HomeKitException(
                f"{name} is still paired over HomeKit — hold the factory reset "
                "hole on the back of the bridge for 10+ s until all LEDs flash, then retry"
            )
        finish = await discovery.async_start_pairing(self.alias)
        self.pairing = await finish(pin)
        # finish() registers the pairing only on the IP sub-controller; save_data()
        # reads the top-level aliases. Register it there, then make sure the keys
        # are really on disk — losing them leaves the bridge paired to nobody.
        pairing_id = self.pairing.pairing_data["AccessoryPairingID"].lower()
        self.controller.pairings[pairing_id] = self.pairing
        self.controller.aliases[self.alias] = self.pairing
        self.controller.save_data(str(self.storage))
        self.storage.chmod(0o600)
        if "iOSDeviceLTSK" not in self.storage.read_text():
            raise HomeKitException(f"pairing keys were not saved to {self.storage} — do not restart the app")
        self._wire_events()
        await self._connect()
        return {"paired": True, "device": name, "zones": len(self.zones)}

    async def auto_pair(self, pin: str, interval: float = 120.0) -> None:
        """Keep trying to pair with the configured code until paired (e.g. after a bridge reset)."""
        while not self.is_paired():
            try:
                result = await self.pair(pin)
                log.info("auto-paired with %s", result["device"])
                return
            except Exception as exc:  # noqa: BLE001
                self.error = f"auto-pair: {exc or type(exc).__name__}"
                log.info("auto-pair not possible yet: %s", self.error)
            await asyncio.sleep(interval)

    async def unpair(self) -> None:
        if self._poll_task:
            self._poll_task.cancel()
        if self.controller and self.alias in self.controller.aliases:
            try:
                await self.controller.remove_pairing(self.alias)  # removes it on the bridge too
            except Exception as exc:  # noqa: BLE001
                log.warning("remove_pairing failed (forgetting locally anyway): %s", exc)
                self.controller.aliases.pop(self.alias, None)
        if self.storage.exists():
            self.storage.unlink()
        self.pairing = None
        self.zones = {}
        self.state = {}
        self.connected = False

    # -- data ------------------------------------------------------------------
    def _wire_events(self) -> None:
        p = self.pairing
        p.dispatcher_availability_changed(self._on_availability)
        p.dispatcher_connect(self._on_event)

    def _on_availability(self, available: bool) -> None:
        self.connected = bool(available)
        log.info("bridge availability: %s", available)

    def _on_event(self, event: dict) -> None:
        for (aid, iid), data in (event or {}).items():
            if "value" in (data or {}):
                self._apply_value(aid, iid, data["value"])

    def _apply_value(self, aid: int, iid: int, value) -> None:
        zone = self.zones.get(aid)
        if not zone:
            return
        key = zone["by_iid"].get(iid)
        if key:
            self.state.setdefault(aid, {})[key] = value

    async def _connect(self) -> None:
        try:
            self._build_zones(await self.pairing.list_accessories_and_characteristics())
            chars = self._all_chars()
            if chars:
                await self.pairing.subscribe(chars)
            await self.refresh()
            self.error = None
        except Exception as exc:  # noqa: BLE001
            self.connected = False
            self.error = str(exc) or type(exc).__name__
            log.warning("homekit connect failed: %s", self.error)
        if self._poll_task is None or self._poll_task.done():
            self._poll_task = asyncio.create_task(self._poller())

    async def _poller(self) -> None:
        failures = 0
        while self.pairing:
            await asyncio.sleep(POLL_INTERVAL)
            try:
                if not self.zones:
                    self._build_zones(await self.pairing.list_accessories_and_characteristics())
                await self.refresh()
                self.error = None
                failures = 0
            except Exception as exc:  # noqa: BLE001
                self.connected = False
                self.error = str(exc) or type(exc).__name__
                failures += 1
                log.warning("homekit poll failed (%d): %s", failures, self.error)
                if failures >= RELOCATE_AFTER:
                    failures = 0
                    await self._relocate()

    async def _relocate(self) -> None:
        """The bridge stopped answering: look for it by name and move the pairing to its new IP."""
        if not (self.bridge_name and self.bridge_ip):
            return
        log.info("looking for %s on the network...", self.bridge_name)
        try:
            ip = await locate_bridge(self.bridge_name, self.bridge_ip)
        except Exception as exc:  # noqa: BLE001
            log.warning("bridge search failed: %s", exc)
            return
        if not ip or ip == self.bridge_ip:
            log.info("bridge search: %s", "not found" if not ip else "same address, still not answering")
            return
        log.info("bridge moved: %s -> %s", self.bridge_ip, ip)
        data = dict(self.pairing.pairing_data) | {"AccessoryIP": ip, "AccessoryIPs": [ip]}
        try:
            await self.pairing.close()
        except Exception:  # noqa: BLE001
            pass
        self.controller.aliases.pop(self.alias, None)
        self.pairing = self.controller.load_pairing(self.alias, data)
        self.controller.save_data(str(self.storage))
        self.bridge_ip = ip
        if self.on_bridge_moved:
            self.on_bridge_moved(ip)
        self._wire_events()
        await self._connect()

    def _all_chars(self) -> list[tuple[int, int]]:
        return [(aid, iid) for aid, z in self.zones.items() for iid in z["iids"].values()]

    def _build_zones(self, accessories: list[dict]) -> None:
        self.zones = {}
        for acc in accessories:
            aid = acc["aid"]
            name = serial = model = identify_iid = None
            thermostat = humidity_svc = None
            for svc in acc.get("services", []):
                stype = _stype(svc["type"])
                if stype == SVC_ACCESSORY_INFO:
                    for ch in svc["characteristics"]:
                        ct = _stype(ch["type"])
                        if ct == CHAR_NAME:
                            name = ch.get("value")
                        elif ct == CHAR_SERIAL:
                            serial = ch.get("value")
                        elif ct == CHAR_MODEL:
                            model = ch.get("value")
                        elif ct == CHAR_IDENTIFY:
                            identify_iid = ch["iid"]
                elif stype == SVC_THERMOSTAT:
                    thermostat = svc
                elif stype == SVC_HUMIDITY:
                    humidity_svc = svc
            if not thermostat:
                log.info("accessory %s (%s, %s) has no thermostat service; services: %s", aid, name, model,
                         ["%X" % _stype(svc["type"]) for svc in acc.get("services", [])])
                continue
            chars = {_stype(c["type"]): c for c in thermostat["characteristics"]}
            wanted = {
                "current_hc": CHAR_CURRENT_HEATING_COOLING,
                "target_hc": CHAR_TARGET_HEATING_COOLING,
                "current_temp": CHAR_CURRENT_TEMP,
                "target_temp": CHAR_TARGET_TEMP,
            }
            iids = {key: chars[ct]["iid"] for key, ct in wanted.items() if ct in chars}
            if CHAR_CURRENT_HUMIDITY in chars:  # tado puts humidity inside the thermostat service
                iids["humidity"] = chars[CHAR_CURRENT_HUMIDITY]["iid"]
            elif humidity_svc:
                hc = {_stype(c["type"]): c for c in humidity_svc["characteristics"]}
                if CHAR_CURRENT_HUMIDITY in hc:
                    iids["humidity"] = hc[CHAR_CURRENT_HUMIDITY]["iid"]
            tgt = chars.get(CHAR_TARGET_TEMP, {})
            self.zones[aid] = {
                "aid": aid,
                "name": name or f"Zone {aid}",
                "serial": serial,
                "identify_iid": identify_iid,
                "model": model,
                "iids": iids,
                "by_iid": {v: k for k, v in iids.items()},
                "min_temp": tgt.get("minValue", 5),
                "max_temp": tgt.get("maxValue", 25),
                "step": tgt.get("minStep", 0.5),
            }
        log.info("discovered %d thermostat zone(s) on bridge", len(self.zones))

    async def refresh(self) -> None:
        if not self.pairing or not self.zones:
            return
        values = await asyncio.wait_for(self.pairing.get_characteristics(self._all_chars()), timeout=15)
        for (aid, iid), data in (values or {}).items():
            if "value" in (data or {}):
                self._apply_value(aid, iid, data["value"])
        self.connected = True

    # -- control -----------------------------------------------------------------
    async def _put(self, values: dict[tuple[int, int], object]) -> None:
        if not self.pairing:
            raise HomeKitException("bridge not paired")
        errors = await self.pairing.put_characteristics([(aid, iid, v) for (aid, iid), v in values.items()])
        if errors:
            raise HomeKitException(f"bridge rejected the command: {errors}")
        for (aid, iid), value in values.items():
            self._apply_value(aid, iid, value)

    def _zone(self, aid: int) -> dict:
        if aid not in self.zones:
            raise HomeKitException(f"unknown zone {aid}")
        return self.zones[aid]

    async def set_temperature(self, aid: int, temp: float) -> None:
        z = self._zone(aid)
        temp = max(z["min_temp"], min(z["max_temp"], temp))
        await self._put({(aid, z["iids"]["target_hc"]): MODE_HEAT, (aid, z["iids"]["target_temp"]): temp})

    async def identify(self, aid: int) -> None:
        """HomeKit Identify: the device shows itself (tado: blinks its display/LED)."""
        z = self._zone(aid)
        if not z["identify_iid"]:
            raise HomeKitException("this device has no identify function")
        errors = await self.pairing.put_characteristics([(aid, z["identify_iid"], True)])
        if errors:
            raise HomeKitException(f"bridge rejected identify: {errors}")

    async def set_mode(self, aid: int, mode: int) -> None:
        z = self._zone(aid)
        await self._put({(aid, z["iids"]["target_hc"]): mode})

    # -- presentation --------------------------------------------------------------
    def zones_payload(self) -> list[dict]:
        """Shape HomeKit data like the cloud API so the dashboard renders it as-is."""
        out = []
        for aid, z in sorted(self.zones.items()):
            st = self.state.get(aid, {})
            mode = st.get("target_hc")
            power = "OFF" if mode == MODE_OFF else "ON"
            # HomeKit can't tell a manual override from the schedule, so no overlay badge
            overlay = None
            out.append(
                {
                    "id": aid,
                    "serial": z["serial"],
                    "model": z["model"],
                    "room": self.names.get(z["serial"] or ""),
                    "zone": {"name": self.names.get(z["serial"] or "") or z["name"], "type": "HEATING"},
                    "state": {
                        "sensorDataPoints": {
                            "insideTemperature": {"celsius": st.get("current_temp")},
                            "humidity": {"percentage": st.get("humidity")},
                        },
                        "setting": {
                            "power": power,
                            "temperature": {"celsius": st.get("target_temp") if power == "ON" else None},
                        },
                        "activityDataPoints": {
                            "heatingPower": {"percentage": 100 if st.get("current_hc") == MODE_HEAT else 0}
                        },
                        "overlay": overlay,
                    },
                    "capabilities": {
                        "temperatures": {
                            "celsius": {"min": z["min_temp"], "max": z["max_temp"], "step": z["step"]}
                        }
                    },
                }
            )
        return out

    def status(self) -> dict:
        return {
            "paired": self.is_paired(),
            "connected": self.connected,
            "error": self.error,
            "zones": len(self.zones),
        }
