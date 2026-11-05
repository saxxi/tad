"""Things that need a person: a valve that won't take commands, or the bridge going away.

Problems show on the dashboard as soon as they're seen. A phone notification waits NOTIFY_AFTER, since
a valve often misses one command and takes the next, and it only goes out once phone alerts are turned
on. It goes through ntfy.sh, a public relay: the random topic is what keeps it private, and the
message (room name and problem) is all that leaves the house.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import time

import httpx

from .air import LEFT_OPEN_AFTER, NO_EFFECT_AFTER, Air, detect_windows, heating_without_effect
from .local import LocalStore

log = logging.getLogger("tado-local.alerts")

NOTIFY_AFTER = 300  # seconds
CHECK_EVERY = 30  # seconds
NTFY_SERVER = "https://ntfy.sh"


class Alerts:
    def __init__(self, store: LocalStore):
        self.store = store
        self.active: dict[str, dict] = {}
        self.resolved: list[dict] = []  # notified problems that ended, waiting to say so

    def fail(self, key: str, title: str, message: str, now: float | None = None, kind: str = "valve", room_id: int | None = None, notify_after: int = NOTIFY_AFTER) -> None:
        if key in self.active:
            self.active[key] |= {"title": title, "message": message}
        else:
            self.active[key] = {
                "key": key, "kind": kind, "room_id": room_id, "title": title, "message": message,
                "since": int(now or time.time()), "notified": False, "notify_after": notify_after,
            }

    def clear(self, key: str) -> None:
        alert = self.active.pop(key, None)
        if alert and alert["notified"]:
            self.resolved.append(alert)

    def list(self) -> list[dict]:
        return sorted(({k: a[k] for k in ("key", "kind", "room_id", "title", "message", "since")} for a in self.active.values()), key=lambda a: a["since"])

    # -- phone settings ------------------------------------------------------------
    def phone(self) -> dict:
        return {"enabled": False, "topic": None, "server": NTFY_SERVER} | self.store.get_setting("alerts", {})

    def set_phone(self, enabled: bool) -> dict:
        saved = self.phone()
        topic = saved["topic"] or "tado-" + secrets.token_urlsafe(18)  # stays the same once subscribed
        self.store.set_setting("alerts", {"enabled": enabled, "topic": topic})
        return self.phone()

    # -- notifying -------------------------------------------------------------------
    def due(self, now: float) -> list[dict]:
        return [a for a in self.active.values() if not a["notified"] and now - a["since"] >= a["notify_after"]]

    async def notify(self, send, now: float | None = None) -> None:
        """A failed send is retried on the next check. With phone alerts off nothing is queued up:
        turning them on later shouldn't deliver old news about problems that already ended."""
        now = now or time.time()
        if not self.phone()["enabled"]:
            self.resolved.clear()
            return
        for alert in self.due(now):
            await send(alert["title"], alert["message"], "high")
            alert["notified"] = True
        while self.resolved:
            await send(self.resolved[0]["title"] + ": fixed", "It's working again.", "default")
            self.resolved.pop(0)


async def post_ntfy(client: httpx.AsyncClient, topic: str, title: str, message: str, priority: str) -> None:
    # JSON rather than ntfy's header form, which can't carry accents in a room name
    levels = {"default": 3, "high": 4}
    r = await client.post(NTFY_SERVER, json={"topic": topic, "title": title, "message": message, "priority": levels[priority]})
    r.raise_for_status()


def check_bridge(alerts: Alerts, homekit) -> None:
    if homekit.is_paired() and not homekit.connected:
        alerts.fail("bridge", "Bridge not responding", "Schedules and night off are paused, so valves stay as they are until it's back.")
    else:
        alerts.clear("bridge")


def check_rooms(alerts: Alerts, points: dict[int, list[tuple[int, float, int]]], names: dict[int, str], now: int | None = None) -> None:
    """Problems only the temperature gives away: a window left open, a radiator that isn't warming."""
    for rid, name in names.items():
        pts = points.get(rid, [])
        opened = next((e for e in detect_windows(pts, now) if e["end"] is None), None)
        if opened:
            during = "while the valves are heating, so heat is going straight outside" if opened["heating"] else "with the heating idle"
            alerts.fail(
                f"window:{rid}", f"{name}: window open?", f"Temperature is down {opened['drop']:g}° quickly, {during}.",
                now=opened["start"], kind="window", room_id=rid, notify_after=LEFT_OPEN_AFTER,
            )
        else:
            alerts.clear(f"window:{rid}")
        if not opened and heating_without_effect(pts):
            alerts.fail(
                f"noheat:{rid}", f"{name}: not warming up",
                f"It has called for heat for over {NO_EFFECT_AFTER // 3600} hours without getting warmer. Check the valve and radiator, and that the boiler is on.",
                kind="valve", room_id=rid,
            )
        else:
            alerts.clear(f"noheat:{rid}")


async def run_alerts(alerts: Alerts, homekit, air: Air, store: LocalStore) -> None:
    async with httpx.AsyncClient(timeout=15) as client:
        async def send(title: str, message: str, priority: str) -> None:
            await post_ntfy(client, alerts.phone()["topic"], title, message, priority)

        while True:
            await asyncio.sleep(CHECK_EVERY)
            try:
                check_bridge(alerts, homekit)
                now = int(time.time())
                points = await asyncio.to_thread(air.room_points, now - 3 * 3600, now)
                check_rooms(alerts, points, {r["id"]: r["name"] for r in store.rooms()}, now)
                await alerts.notify(send)
            except Exception as exc:  # noqa: BLE001 - tried again next check
                log.warning("phone alert failed: %s", exc)
