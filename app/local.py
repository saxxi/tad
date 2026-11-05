"""Local rooms and weekly schedules (SQLite in data/), plus the scheduler loop.

Replaces the tado cloud for naming/grouping and for the schedule: each room has
weekly change points ("Mon-Fri 06:30 -> 21 °C"). The scheduler *enforces* the
current point over HomeKit — tado's own schedule still runs on the bridge and
would otherwise drift the targets.

Rules on top of the schedule, strongest first:
  manual hold > away mode > night off > weekly schedule
The house dial shifts every schedule and on-demand temperature up or down together, without changing them.
A change made in the app holds until the room's next change; during away or night off
that hold lasts at most MANUAL_NIGHT_HOLD, so a forgotten boost can't heat all night.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

log = logging.getLogger("tado-local.schedule")

TZ = ZoneInfo(os.environ.get("TZ", "Europe/Rome"))
TICK = 30.0  # seconds between scheduler checks
DEFAULT_QUIET = ("22:00", "08:00")  # night off for new rooms and unassigned devices
DEFAULT_AWAY_CELSIUS = 12.0
MANUAL_NIGHT_HOLD = timedelta(hours=1)
MAX_DIAL = 3.0  # °C either way
MIN_CELSIUS, MAX_CELSIUS = 5.0, 25.0

SCHEMA = """
CREATE TABLE IF NOT EXISTS rooms (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    last_applied TEXT,           -- unused (pre-enforcement scheduler)
    hold_until TEXT,             -- ISO time: manual change from the app holds until then
    quiet_start TEXT DEFAULT '22:00',  -- night off "HH:MM"; both NULL = no night off
    quiet_end TEXT DEFAULT '08:00'
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
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL          -- JSON
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
        if "quiet_start" not in {c["name"] for c in self.db.execute("PRAGMA table_info(rooms)")}:
            self.db.execute("ALTER TABLE rooms ADD COLUMN quiet_start TEXT DEFAULT '22:00'")
            self.db.execute("ALTER TABLE rooms ADD COLUMN quiet_end TEXT DEFAULT '08:00'")
            self.db.execute("UPDATE rooms SET hold_until = NULL")  # holds predate night off
            self.db.commit()

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
        away = self.away(now)
        on_demand = self.on_demand()
        dial = self.dial()
        for r in self.db.execute("SELECT * FROM rooms ORDER BY name").fetchall():
            points = self.points(r["id"])
            st = _room_state(_shift(points, dial), _quiet(r), away, r["hold_until"], now)
            out.append(
                {
                    "id": r["id"],
                    "name": r["name"],
                    "devices": [d["serial"] for d in self.db.execute("SELECT serial FROM devices WHERE room_id = ?", (r["id"],))],
                    "schedule": points,
                    "quiet": dict(zip(("start", "end"), _quiet(r))) if _quiet(r) else None,
                    "mode": st["mode"],
                    "until": st["until"],
                    "next": st["next"],
                    "held": st["mode"] == "manual",
                    "on_demand": on_demand[r["id"]] | {"effective": _clamp(on_demand[r["id"]]["celsius"] + dial)} if r["id"] in on_demand else None,
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

    def set_quiet(self, rid: int, start: str | None, end: str | None) -> None:
        self.db.execute("UPDATE rooms SET quiet_start = ?, quiet_end = ?, hold_until = NULL WHERE id = ?", (start, end, rid))
        self.db.commit()

    def hold(self, serial: str) -> None:
        """A manual change from the app: the device's room holds it until its next change,
        or for at most MANUAL_NIGHT_HOLD while away mode or night off is running."""
        row = self.db.execute(
            "SELECT r.id, r.quiet_start, r.quiet_end FROM devices d JOIN rooms r ON r.id = d.room_id WHERE d.serial = ?", (serial,)
        ).fetchone()
        if not row:
            return
        now = datetime.now(TZ)
        mode, _, until = _rule(self.points(row["id"]), _quiet(row), self.away(now), now)
        if mode in ("away", "night"):
            cap = now + MANUAL_NIGHT_HOLD
            until = min(until, cap) if until else cap
        if until:
            self.db.execute("UPDATE rooms SET hold_until = ? WHERE id = ?", (until.isoformat(), row["id"]))
            self.db.commit()

    def boost(self, rid: int, hours: float, extend: bool = False) -> None:
        """Hold a room for a set time: "I'm using this room". Night off still wins: it ends the
        hold when it starts, and during away mode or night off the hold lasts MANUAL_NIGHT_HOLD."""
        row = self.db.execute("SELECT quiet_start, quiet_end, hold_until FROM rooms WHERE id = ?", (rid,)).fetchone()
        now = datetime.now(TZ)
        held = datetime.fromisoformat(row["hold_until"]) if row["hold_until"] else None
        until = (held if extend and held and held > now else now) + timedelta(hours=hours)
        quiet = _quiet(row)
        mode = _rule(self.points(rid), quiet, self.away(now), now)[0]
        if mode in ("away", "night"):
            until = min(until, now + MANUAL_NIGHT_HOLD)
        elif quiet:
            until = min(until, _next_clock(quiet[0], now))
        self.db.execute("UPDATE rooms SET hold_until = ? WHERE id = ?", (until.isoformat(), rid))
        self.db.commit()

    def on_demand(self) -> dict[int, dict]:
        """Rooms with an "I'm using this room" switch: room id -> {celsius, hours}."""
        return {int(k): v for k, v in self.get_setting("on_demand", {}).items()}

    def set_on_demand(self, rid: int, config: dict | None) -> None:
        rooms = self.on_demand()
        rooms.pop(rid, None)
        if config:
            rooms[rid] = config
        self.set_setting("on_demand", rooms)

    def resume(self, rid: int) -> None:
        self.db.execute("UPDATE rooms SET hold_until = NULL WHERE id = ?", (rid,))
        self.db.commit()

    # -- settings ------------------------------------------------------------------
    def get_setting(self, key: str, default=None):
        row = self.db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    def set_setting(self, key: str, value) -> None:
        self.db.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, json.dumps(value)),
        )
        self.db.commit()

    # -- house dial ----------------------------------------------------------------
    def dial(self) -> float:
        return float(self.get_setting("dial", 0))

    def set_dial(self, offset: float) -> None:
        self.set_setting("dial", offset)

    def dialled(self, celsius: float) -> float:
        return _clamp(celsius + self.dial())

    # -- away mode ---------------------------------------------------------------
    def away(self, now: datetime) -> dict:
        row = self.db.execute("SELECT value FROM settings WHERE key = 'away'").fetchone()
        saved = json.loads(row["value"]) if row else {}
        until = saved.get("until")
        active = bool(saved.get("active")) and (not until or datetime.fromisoformat(until) > now)
        return {
            "active": active,
            "until": until if active else None,
            "since": saved.get("since") if active else None,
            "celsius": saved.get("celsius", DEFAULT_AWAY_CELSIUS),
        }

    def set_away(self, active: bool, until: datetime | None = None, celsius: float | None = DEFAULT_AWAY_CELSIUS) -> None:
        """Turning away mode on or off drops manual holds, so every room follows it at once."""
        value = {
            "active": active,
            "until": until.isoformat() if active and until else None,
            "since": datetime.now(TZ).isoformat() if active else None,
            "celsius": celsius,
        }
        self.db.execute(
            "INSERT INTO settings (key, value) VALUES ('away', ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (json.dumps(value),),
        )
        self.db.execute("UPDATE rooms SET hold_until = NULL")
        self.db.commit()

    # -- what each room should do now ----------------------------------------------
    def plan(self, now: datetime) -> dict[int | None, dict]:
        """Room id -> its current state; the None key is for unassigned devices, which
        have no schedule but still follow night off and away mode."""
        away = self.away(now)
        dial = self.dial()
        plan = {
            r["id"]: _room_state(_shift(self.points(r["id"]), dial), _quiet(r), away, r["hold_until"], now) | {"name": r["name"]}
            for r in self.db.execute("SELECT * FROM rooms").fetchall()
        }
        plan[None] = _room_state([], DEFAULT_QUIET, away, None, now) | {"name": "unassigned devices"}
        return plan


# -- schedule maths -------------------------------------------------------------
def _clamp(celsius: float) -> float:
    return max(MIN_CELSIUS, min(MAX_CELSIUS, celsius))


def _shift(points: list[dict], offset: float) -> list[dict]:
    """The schedule as the house dial makes it. "Off" stays off."""
    return [p | {"celsius": _clamp(p["celsius"] + offset)} if p.get("celsius") is not None else p for p in points]


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


def _quiet(row) -> tuple[str, str] | None:
    return (row["quiet_start"], row["quiet_end"]) if row["quiet_start"] and row["quiet_end"] else None


def _in_quiet(quiet: tuple[str, str], at: datetime) -> bool:
    start, end = quiet
    t = at.strftime("%H:%M")
    return start <= t < end if start < end else t >= start or t < end


def _next_clock(hhmm: str, after: datetime) -> datetime:
    """The next time the clock shows hhmm, strictly after `after`."""
    hh, mm = map(int, hhmm.split(":"))
    at = after.replace(hour=hh, minute=mm, second=0, microsecond=0)
    return at if at > after else at + timedelta(days=1)


def _rule(points: list[dict], quiet: tuple[str, str] | None, away: dict, at: datetime) -> tuple[str, float | None, datetime | None]:
    """(mode, wanted °C or None for off, when this stops applying), ignoring manual holds."""
    if away["active"] and (not away["until"] or datetime.fromisoformat(away["until"]) > at):
        return "away", away["celsius"], datetime.fromisoformat(away["until"]) if away["until"] else None
    if quiet and _in_quiet(quiet, at):
        return "night", None, _next_clock(quiet[1], at)
    night_starts = _next_clock(quiet[0], at) if quiet else None
    cur = _current_point(points, at)
    if not cur:  # no schedule: nothing to enforce until night off starts, nothing restores it after
        return "free", None, night_starts
    change = datetime.fromisoformat(_next_point(points, at)["at"])
    return "schedule", cur.get("celsius"), min(change, night_starts) if night_starts else change


def _room_state(points: list[dict], quiet: tuple[str, str] | None, away: dict, hold_until: str | None, now: datetime) -> dict:
    mode, want, until = _rule(points, quiet, away, now)
    if hold_until and datetime.fromisoformat(hold_until) > now:
        mode, until = "manual", datetime.fromisoformat(hold_until)
    return {
        "mode": mode,
        "want": want,
        "enforce": mode in ("away", "night", "schedule"),
        "until": until.isoformat() if until else None,
        "next": _describe(until, *_rule(points, quiet, away, until)[:2], now) if until else None,
    }


def _describe(at: datetime, mode: str, want: float | None, now: datetime) -> str:
    day = "today" if at.date() == now.date() else at.strftime("%a")
    if mode == "free":
        return f"{day} {at:%H:%M} → night off ends"
    what = "off" if want is None else f"{want:g}°"
    return f"{day} {at:%H:%M} → {what}"


# -- scheduler loop ---------------------------------------------------------------
async def enforce(store: LocalStore, homekit, alerts=None) -> None:
    """Put every device where its room's rule says. One device failing doesn't stop the
    others: a valve that's offline must not keep the rest of the house heating all night."""
    from .homekit import MODE_OFF  # local import: homekit imports nothing from here

    plan = store.plan(datetime.now(TZ))
    room_of = store.room_of()
    fixed: dict[int | None, int] = {}
    for aid, z in list(homekit.zones.items()):
        rid = room_of.get(z["serial"])
        room = plan.get(rid) or plan[None]
        key = "valve:" + str(z["serial"])
        if not room["enforce"]:
            if alerts:
                alerts.clear(key)
            continue
        want = room["want"]
        st = homekit.state.get(aid, {})
        try:
            if want is None:
                if st.get("target_hc") != MODE_OFF:
                    await homekit.set_mode(aid, MODE_OFF)
                    fixed[rid] = fixed.get(rid, 0) + 1
            elif st.get("target_hc") == MODE_OFF or abs((st.get("target_temp") or 0) - want) > 0.05:
                await homekit.set_temperature(aid, want)
                fixed[rid] = fixed.get(rid, 0) + 1
            if alerts:
                alerts.clear(key)
        except Exception as exc:  # noqa: BLE001 - retried next tick
            log.warning("could not set %s (%s): %s", z["serial"], room["name"], exc)
            if alerts:
                goal = "off" if want is None else f"at {want:g}°"
                alerts.fail(key, f'{room["name"]}: valve not responding', f"It should be {goal} but isn't taking commands, so it may keep heating. Check its batteries. Trying again every 30 seconds.", room_id=rid)
    for rid, n in fixed.items():
        room = plan.get(rid) or plan[None]
        log.info("%s: %s -> %s (%d devices set)", room["mode"], room["name"], "off" if room["want"] is None else f'{room["want"]:g}°', n)


async def run_scheduler(store: LocalStore, homekit, wake: asyncio.Event, alerts=None) -> None:
    """Enforce every TICK seconds, or at once when something sets `wake`."""
    while True:
        try:
            await asyncio.wait_for(wake.wait(), TICK)
        except TimeoutError:
            pass
        wake.clear()
        if not homekit.is_paired() or not homekit.connected:
            continue
        try:
            await enforce(store, homekit, alerts)
        except Exception as exc:  # noqa: BLE001 - keep the loop alive; retried next tick
            log.warning("scheduler tick failed: %s", exc)
