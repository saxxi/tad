"""Local rooms and weekly schedules (SQLite in data/), plus the scheduler loop.

Replaces the tado cloud for naming/grouping and for the schedule: each room has
weekly change points ("Mon-Fri 06:30 -> 21 °C"). The scheduler *enforces* the
current point over HomeKit — tado's own schedule still runs on the bridge and
would otherwise drift the targets — except while a room is on hold: a change
made in this app holds until the room's next change point.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

log = logging.getLogger("tado-local.schedule")

TZ = ZoneInfo(os.environ.get("TZ", "Europe/Rome"))
TICK = 30.0  # seconds between scheduler checks

SCHEMA = """
CREATE TABLE IF NOT EXISTS rooms (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    last_applied TEXT,           -- unused (pre-enforcement scheduler)
    hold_until TEXT              -- ISO time: manual change from the app holds until then
);
CREATE TABLE IF NOT EXISTS devices (
    serial TEXT PRIMARY KEY,
    room_id INTEGER REFERENCES rooms(id) ON DELETE SET NULL
);
CREATE TABLE IF NOT EXISTS schedule (
    id INTEGER PRIMARY KEY,
    room_id INTEGER NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
    days TEXT NOT NULL,          -- ISO weekdays, e.g. "12345" = Mon-Fri
    time TEXT NOT NULL,          -- "HH:MM"
    celsius REAL                 -- NULL = heating off
);
"""


class LocalStore:
    def __init__(self, path: Path):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.executescript(SCHEMA)
        if "hold_until" not in {c["name"] for c in self.db.execute("PRAGMA table_info(rooms)")}:
            self.db.execute("ALTER TABLE rooms ADD COLUMN hold_until TEXT")

    # -- seeding ---------------------------------------------------------------
    def seed_from_cloud_cache(self, rooms: dict[str, dict]) -> None:
        """One-time import of the room list fetched earlier from tado (config["rooms"])."""
        if self.db.execute("SELECT 1 FROM rooms LIMIT 1").fetchone() or not rooms:
            return
        ids: dict[str, int] = {}
        for serial, room in sorted(rooms.items(), key=lambda kv: kv[1]["name"]):
            if room["name"] not in ids:
                ids[room["name"]] = self.db.execute("INSERT INTO rooms (name) VALUES (?)", (room["name"],)).lastrowid
            self.db.execute("INSERT OR IGNORE INTO devices (serial, room_id) VALUES (?, ?)", (serial, ids[room["name"]]))
        self.db.commit()
        log.info("imported %d rooms from the cached tado room list", len(ids))

    def register_devices(self, serials: list[str]) -> None:
        """Devices the bridge reports but we don't know yet show up as unassigned."""
        self.db.executemany("INSERT OR IGNORE INTO devices (serial) VALUES (?)", [(s,) for s in serials if s])
        self.db.commit()

    # -- rooms and devices -----------------------------------------------------
    def room_of(self) -> dict[str, int | None]:
        return {r["serial"]: r["room_id"] for r in self.db.execute("SELECT serial, room_id FROM devices")}

    def rooms(self) -> list[dict]:
        out = []
        now = datetime.now(TZ)
        for r in self.db.execute("SELECT id, name, hold_until FROM rooms ORDER BY name"):
            points = self.points(r["id"])
            held = r["hold_until"] and datetime.fromisoformat(r["hold_until"]) > now
            out.append(
                {
                    "id": r["id"],
                    "name": r["name"],
                    "devices": [d["serial"] for d in self.db.execute("SELECT serial FROM devices WHERE room_id = ?", (r["id"],))],
                    "schedule": points,
                    "next": _describe(_next_point(points, now)),
                    "held": bool(held),
                }
            )
        return out

    def add_room(self, name: str) -> int:
        rid = self.db.execute("INSERT INTO rooms (name) VALUES (?)", (name.strip() or "New room",)).lastrowid
        self.db.commit()
        return rid

    def rename_room(self, rid: int, name: str) -> None:
        self.db.execute("UPDATE rooms SET name = ? WHERE id = ?", (name.strip() or "Room", rid))
        self.db.commit()

    def delete_room(self, rid: int) -> None:
        self.db.execute("DELETE FROM rooms WHERE id = ?", (rid,))  # its devices become unassigned
        self.db.commit()

    def move_device(self, serial: str, rid: int | None) -> None:
        self.db.execute(
            "INSERT INTO devices (serial, room_id) VALUES (?, ?) ON CONFLICT(serial) DO UPDATE SET room_id = excluded.room_id",
            (serial, rid),
        )
        self.db.commit()

    # -- schedule --------------------------------------------------------------
    def points(self, rid: int) -> list[dict]:
        rows = self.db.execute("SELECT days, time, celsius FROM schedule WHERE room_id = ? ORDER BY time", (rid,))
        return [dict(r) for r in rows]

    def set_points(self, rid: int, points: list[dict]) -> None:
        self.db.execute("DELETE FROM schedule WHERE room_id = ?", (rid,))
        self.db.executemany(
            "INSERT INTO schedule (room_id, days, time, celsius) VALUES (?, ?, ?, ?)",
            [(rid, p["days"], p["time"], p.get("celsius")) for p in points],
        )
        self.db.execute("UPDATE rooms SET hold_until = NULL WHERE id = ?", (rid,))  # new schedule applies now
        self.db.commit()

    def hold(self, serial: str) -> None:
        """A manual change from the app: the device's room holds it until its next change point."""
        row = self.db.execute("SELECT room_id FROM devices WHERE serial = ?", (serial,)).fetchone()
        if not row or row["room_id"] is None:
            return
        nxt = _next_point(self.points(row["room_id"]), datetime.now(TZ))
        if nxt:
            self.db.execute("UPDATE rooms SET hold_until = ? WHERE id = ?", (nxt["at"], row["room_id"]))
            self.db.commit()

    def resume(self, rid: int) -> None:
        self.db.execute("UPDATE rooms SET hold_until = NULL WHERE id = ?", (rid,))
        self.db.commit()

    def targets(self, now: datetime) -> list[tuple[int, str, dict]]:
        """(room id, name, current point) for every scheduled room that isn't on hold."""
        out = []
        for r in self.db.execute("SELECT id, name, hold_until FROM rooms"):
            if r["hold_until"] and datetime.fromisoformat(r["hold_until"]) > now:
                continue
            if cur := _current_point(self.points(r["id"]), now):
                out.append((r["id"], r["name"], cur))
        return out


# -- schedule maths -------------------------------------------------------------
def _occurrences(points: list[dict], around: datetime):
    """Concrete datetimes of every point from a week before to a week after `around`."""
    monday = (around - timedelta(days=around.isoweekday() - 1)).replace(hour=0, minute=0, second=0, microsecond=0)
    for week in (-7, 0, 7):
        for p in points:
            hh, mm = map(int, p["time"].split(":"))
            for d in p["days"]:
                at = monday + timedelta(days=week + int(d) - 1, hours=hh, minutes=mm)
                yield at, p


def _current_point(points: list[dict], now: datetime) -> dict | None:
    past = [(at, p) for at, p in _occurrences(points, now) if at <= now]
    if not past:
        return None
    at, p = max(past, key=lambda x: x[0])
    return p | {"at": at.isoformat()}


def _next_point(points: list[dict], now: datetime) -> dict | None:
    future = [(at, p) for at, p in _occurrences(points, now) if at > now]
    if not future:
        return None
    at, p = min(future, key=lambda x: x[0])
    return p | {"at": at.isoformat()}


def _describe(point: dict | None) -> str | None:
    if not point:
        return None
    at = datetime.fromisoformat(point["at"])
    day = "today" if at.date() == datetime.now(TZ).date() else at.strftime("%a")
    what = "off" if point.get("celsius") is None else f'{point["celsius"]:g}°'
    return f"{day} {at:%H:%M} → {what}"


# -- scheduler loop ---------------------------------------------------------------
async def run_scheduler(store: LocalStore, homekit) -> None:
    from .homekit import MODE_OFF  # local import: homekit imports nothing from here

    while True:
        await asyncio.sleep(TICK)
        if not homekit.is_paired() or not homekit.connected:
            continue
        try:
            room_of = store.room_of()
            for rid, name, point in store.targets(datetime.now(TZ)):
                want = point.get("celsius")
                fixed = 0
                for aid, z in list(homekit.zones.items()):
                    if room_of.get(z["serial"]) != rid:
                        continue
                    st = homekit.state.get(aid, {})
                    if want is None:
                        if st.get("target_hc") != MODE_OFF:
                            await homekit.set_mode(aid, MODE_OFF)
                            fixed += 1
                    elif st.get("target_hc") == MODE_OFF or abs((st.get("target_temp") or 0) - want) > 0.05:
                        await homekit.set_temperature(aid, want)
                        fixed += 1
                if fixed:
                    log.info("schedule: %s -> %s (%d devices set)", name, "off" if want is None else f"{want:g}°", fixed)
        except Exception as exc:  # noqa: BLE001 - keep the loop alive; retried next tick
            log.warning("scheduler tick failed: %s", exc)
