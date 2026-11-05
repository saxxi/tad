"""Long-term climate history in its own SQLite file (data/history.db).

SQLite because it's one writer, a dozen devices and one small container: no server to run,
one file to back up, and a range query over a week of minutes takes milliseconds. No WAL:
data/ is a Docker Desktop bind mount, and a plain journal keeps it safe to copy the file.

Two tiers, so old charts stay fast while nothing is thrown away:
  samples  every device, every minute, kept RAW_DAYS (~200 MB a year for 9 devices)
  hourly   per room, kept forever (a few MB a year)

HomeKit only says whether a device is calling for heat, not how hard, so "heating hours"
means time spent calling for heat. A room counts as heating when any of its devices is.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from .local import TZ, LocalStore

log = logging.getLogger("tado-local.history")

SAMPLE_EVERY = 60  # seconds
ROLLUP_EVERY = 300  # seconds
RAW_DAYS = 365
UNASSIGNED = 0  # room_id stored for devices without a room

SCHEMA = """
CREATE TABLE IF NOT EXISTS samples (
    ts INTEGER NOT NULL,         -- unix time, on the minute
    serial TEXT NOT NULL,
    room_id INTEGER NOT NULL,    -- the room the device was in at the time
    temp REAL NOT NULL,
    target REAL,                 -- NULL = heating off
    humidity REAL,
    heating INTEGER NOT NULL,    -- 1 = calling for heat
    PRIMARY KEY (ts, serial)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS hourly (
    hour INTEGER NOT NULL,       -- unix time, on the hour
    room_id INTEGER NOT NULL,
    temp REAL NOT NULL,
    temp_min REAL NOT NULL,
    temp_max REAL NOT NULL,
    target REAL,
    off_min INTEGER NOT NULL,    -- minutes with the whole room off
    humidity REAL,
    heat_min INTEGER NOT NULL,   -- minutes with the room calling for heat
    minutes INTEGER NOT NULL,    -- minutes recorded
    PRIMARY KEY (hour, room_id)
) WITHOUT ROWID;
"""

# one row per room per minute
ROOM_MINUTES = """
    SELECT ts, room_id, AVG(temp) temp, MIN(temp) temp_min, MAX(temp) temp_max, AVG(target) target,
           MIN(target IS NULL) off, AVG(humidity) humidity, MAX(heating) heating
    FROM samples WHERE ts >= ? AND ts < ? GROUP BY ts, room_id
"""

# range -> (seconds covered, chart bucket in seconds, heating bars grouped by)
RANGES = {
    "24h": (86400, 300, "hour"),
    "7d": (7 * 86400, 1800, "day"),
    "30d": (30 * 86400, 86400, "day"),  # daily: anything shorter just draws the day/night cycle
    "90d": (90 * 86400, 86400, "week"),
    "1y": (365 * 86400, 86400, "month"),
}
RAW_UP_TO = 7 * 86400  # longer ranges read the hourly table


def _group_start(ts: int, by: str) -> int:
    at = datetime.fromtimestamp(ts, TZ).replace(minute=0, second=0, microsecond=0)
    if by != "hour":
        at = at.replace(hour=0)
    if by == "week":
        at -= timedelta(days=at.weekday())
    elif by == "month":
        at = at.replace(day=1)
    return int(at.timestamp())


class History:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()  # writes come from worker threads
        self.db = self._connect()
        self.db.execute("PRAGMA synchronous = NORMAL")
        self.db.executescript(SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=15, check_same_thread=False)

    # -- writing ---------------------------------------------------------------
    def record(self, ts: int, rows: list[tuple]) -> None:
        """rows: (serial, room_id, temp, target, humidity, heating)"""
        with self._lock, self.db:
            self.db.executemany(
                "INSERT OR REPLACE INTO samples (ts, serial, room_id, temp, target, humidity, heating) VALUES (?, ?, ?, ?, ?, ?, ?)",
                [(ts, *r) for r in rows],
            )

    def rollup(self, now: int | None = None) -> None:
        """Refresh the hourly table from the last rolled-up hour on, the running hour included."""
        now = now or int(time.time())
        with self._lock, self.db:
            last = self.db.execute("SELECT MAX(hour) FROM hourly").fetchone()[0] or 0
            self.db.execute(
                f"""INSERT OR REPLACE INTO hourly
                    SELECT ts - ts % 3600, room_id, AVG(temp), MIN(temp_min), MAX(temp_max), AVG(target),
                           SUM(off), AVG(humidity), SUM(heating), COUNT(*)
                    FROM ({ROOM_MINUTES}) GROUP BY ts - ts % 3600, room_id""",
                (last, now + 1),
            )
            self.db.execute("DELETE FROM samples WHERE ts < ?", (now - RAW_DAYS * 86400,))

    # -- reading ---------------------------------------------------------------
    def query(self, range_: str, rooms: list[dict], now: int | None = None) -> dict:
        """Everything the dashboard shows for a range. Runs on a worker thread, on its own connection."""
        now = now or int(time.time())
        span, bucket, bars_by = RANGES[range_]
        start = now - span
        db = self._connect()
        try:
            if span <= RAW_UP_TO:
                source, args = f"({ROOM_MINUTES})", (start, now + 1)
                cols = "ts t, temp, temp_min, temp_max, target, off, humidity, heating, 1 minutes"
                weight = "1"
            else:
                source, args = "hourly", (start - start % 3600, now + 1)
                cols = "hour t, temp, temp_min, temp_max, target, off_min off, humidity, heat_min heating, minutes"
                weight = "minutes"
            where = "" if source != "hourly" else "WHERE hour >= ? AND hour < ?"
            buckets = db.execute(
                f"""SELECT room_id, t - t % {bucket} b, SUM(temp * {weight}) / SUM({weight}), MIN(temp_min), MAX(temp_max),
                           SUM(target * {weight}) / NULLIF(SUM(CASE WHEN target IS NULL THEN 0 ELSE {weight} END), 0),
                           SUM(off), SUM(humidity * {weight}) / NULLIF(SUM(CASE WHEN humidity IS NULL THEN 0 ELSE {weight} END), 0),
                           SUM(heating), SUM({weight})
                    FROM (SELECT room_id, {cols} FROM {source} {where})
                    GROUP BY room_id, b ORDER BY room_id, b""",
                args,
            ).fetchall()
            hourly = db.execute(
                "SELECT hour, room_id, heat_min, minutes FROM hourly WHERE hour >= ? AND hour < ?",
                (min(start, now - 7 * 86400), now + 1),
            ).fetchall()
            first = db.execute("SELECT MIN(hour) FROM hourly").fetchone()[0]
            today = self._today(db, now)
        finally:
            db.close()

        by_room: dict[int, list] = {}
        for row in buckets:
            by_room.setdefault(row[0], []).append(row[1:])
        names = [(r["id"], r["name"]) for r in rooms]
        if UNASSIGNED in by_room:
            names.append((UNASSIGNED, "Unassigned devices"))
        out_rooms = []
        for rid, name in names:
            rows = by_room.get(rid, [])
            minutes = sum(r[8] for r in rows)
            hum = [(r[6], r[8]) for r in rows if r[6] is not None]
            out_rooms.append(
                {
                    "id": rid,
                    "name": name,
                    # [time, temp, target (None when off most of the bucket), humidity, share of time heating, min, max]
                    "points": [
                        [r[0], round(r[1], 2), None if r[5] > r[8] / 2 or r[4] is None else round(r[4], 2),
                         None if r[6] is None else round(r[6], 1), round(r[7] / r[8], 3), r[2], r[3]]
                        for r in rows
                    ],
                    "stats": None if not rows else {
                        "avg": round(sum(r[1] * r[8] for r in rows) / minutes, 2),
                        "min": min(r[2] for r in rows),
                        "max": max(r[3] for r in rows),
                        "humidity": round(sum(h * m for h, m in hum) / sum(m for _, m in hum), 1) if hum else None,
                        "heat_hours": round(sum(r[7] for r in rows) / 60, 2),
                    },
                }
            )
        return {
            "range": range_,
            "from": start,
            "to": now,
            "bucket": bucket,
            "collecting_since": first,
            "db_bytes": self.path.stat().st_size,
            "rooms": out_rooms,
            "bars": self._bars(hourly, start, bars_by),
            "heatmap": self._heatmap(hourly, min(start, now - 7 * 86400)),
            "today": today,
        }

    @staticmethod
    def _today(db: sqlite3.Connection, now: int) -> dict:
        """Heating hours so far today, against yesterday up to the same time."""
        midnight = int(datetime.fromtimestamp(now, TZ).replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
        yesterday = int((datetime.fromtimestamp(midnight, TZ) - timedelta(days=1)).timestamp())

        def hours(a: int, b: int) -> tuple[float, int]:
            heat, n = db.execute(f"SELECT SUM(heating), COUNT(*) FROM ({ROOM_MINUTES})", (a, b)).fetchone()
            return (heat or 0) / 60, n

        heat, _ = hours(midnight, now + 1)
        prev, prev_n = hours(yesterday, yesterday + (now - midnight) + 1)
        return {"heat_hours": round(heat, 2), "yesterday_heat_hours": round(prev, 2) if prev_n else None}

    @staticmethod
    def _bars(hourly: list[tuple], start: int, by: str) -> dict:
        groups: dict[int, dict[int, float]] = {}
        for hour, rid, heat_min, _ in hourly:
            if hour >= start - start % 3600:
                g = groups.setdefault(_group_start(hour, by), {})
                g[rid] = g.get(rid, 0) + heat_min / 60
        return {
            "by": by,
            "rows": [{"t": t, "rooms": {str(k): round(v, 2) for k, v in g.items()}} for t, g in sorted(groups.items())],
        }

    @staticmethod
    def _heatmap(hourly: list[tuple], start: int) -> dict[str, list[list[float | None]]]:
        """Room id -> [weekday (Mon=0)][hour] -> average minutes per hour calling for heat."""
        total: dict[tuple, list[float]] = {}
        for hour, rid, heat_min, minutes in hourly:
            if hour >= start and minutes:
                at = datetime.fromtimestamp(hour, TZ)
                cell = total.setdefault((rid, at.weekday(), at.hour), [0.0, 0.0])
                cell[0] += heat_min
                cell[1] += minutes
        out: dict[str, list[list[float | None]]] = {}
        for (rid, wd, h), (heat, minutes) in total.items():
            grid = out.setdefault(str(rid), [[None] * 24 for _ in range(7)])
            grid[wd][h] = round(heat / minutes * 60, 1)
        return out


# -- sampling loop ---------------------------------------------------------------
def snapshot(store: LocalStore, homekit) -> list[tuple]:
    """Current reading of every device; devices that haven't reported a temperature are skipped."""
    from .homekit import MODE_HEAT, MODE_OFF  # local import, same as the scheduler

    room_of = store.room_of()
    rows = []
    for aid, z in homekit.zones.items():
        st = homekit.state.get(aid, {})
        if st.get("current_temp") is None or not z["serial"]:
            continue
        rows.append(
            (
                z["serial"],
                room_of.get(z["serial"]) or UNASSIGNED,
                st["current_temp"],
                None if st.get("target_hc") == MODE_OFF else st.get("target_temp"),
                st.get("humidity"),
                int(st.get("current_hc") == MODE_HEAT),
            )
        )
    return rows


async def run_sampler(history: History, store: LocalStore, homekit) -> None:
    """Records once a minute while the bridge is connected: an outage is a gap, not stale values."""
    last_rollup = 0.0
    while True:
        await asyncio.sleep(SAMPLE_EVERY - time.time() % SAMPLE_EVERY)
        try:
            if homekit.is_paired() and homekit.connected and (rows := snapshot(store, homekit)):
                ts = int(time.time() + 1) // SAMPLE_EVERY * SAMPLE_EVERY
                await asyncio.to_thread(history.record, ts, rows)
            if time.time() - last_rollup >= ROLLUP_EVERY:
                await asyncio.to_thread(history.rollup)
                last_rollup = time.time()
        except Exception as exc:  # noqa: BLE001 - history must never take the app down
            log.warning("history sample failed: %s", exc)
