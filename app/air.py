"""When to open the windows, and when one looks left open.

Airing dries the house when the air outside holds less water than the air inside, which on a winter day it
nearly always does, and costs least in the warmest hours. Winter smog adds a catch, so the cleanest
warm hours beat the warmest ones. Weather and air quality come from Open-Meteo: only the coordinates
leave the house.

An open window is a guess from the temperature: nothing here can see one. The signature is a fast fall in
a room, ignoring the 90 minutes after heating stops, when a sensor on a hot valve cools quickly by itself.
"""

from __future__ import annotations

import asyncio
import logging
import math
from datetime import datetime

import httpx

from .history import ROOM_MINUTES, History
from .local import TZ

log = logging.getLogger("tado-local.air")

SCHEMA = """
CREATE TABLE IF NOT EXISTS air_hours (
    hour INTEGER PRIMARY KEY,
    temp REAL NOT NULL,
    humidity REAL NOT NULL,
    rain REAL NOT NULL,          -- mm
    aqi REAL                     -- European air quality index; NULL when unavailable
);
"""

FAST_DROP = 0.4  # °C
FAST_DROP_WINDOW = 15 * 60
AFTER_HEAT_IGNORE = 90 * 60
SETTLED_AFTER = 10 * 60  # no new low for this long: the fall is over
RECOVERED_BY = 0.2  # °C above the low
MERGE_WITHIN = 20 * 60
LEFT_OPEN_AFTER = 20 * 60
NO_EFFECT_AFTER = 2 * 3600
NO_EFFECT_RISE = 0.2  # °C

POOR_AIR = 60  # European AQI: 60-80 is "poor"
RAINY_MM = 0.2
MIN_DRYING = 0.5  # g/m³ the outside air must be drier by
AWAKE = range(8, 22)
HUMID = 60  # % indoors
BEST_BLOCK = 2  # hours


def absolute_humidity(temp: float, rh: float) -> float:
    """Grams of water per cubic metre of air (Magnus formula)."""
    return 6.112 * math.exp(17.67 * temp / (temp + 243.5)) * rh * 2.1674 / (273.15 + temp)


def airing_minutes(outside: float, inside: float = 20.0) -> int:
    """The colder it is outside, the faster the air changes: a short full airing beats a long crack."""
    gap = inside - outside
    return 5 if gap >= 15 else 10 if gap >= 8 else 15


def detect_windows(points: list[tuple[int, float, int]], now: int | None = None) -> list[dict]:
    """points: (ts, temp, heating) for one room, by time. Events run from the last high to when the
    temperature stops falling; `end` None means it is still going. With `now`, a room that stopped
    reporting isn't still going: there's nothing to say so."""
    events: list[dict] = []
    current = None
    last_heat = None
    settled_at = 0  # highs from before the last event ended don't count: that fall is already told
    for i, (ts, temp, heating) in enumerate(points):
        if current:
            current["heating"] = current["heating"] or bool(heating)
            if temp < current["low"]:
                current["low"], current["low_at"] = temp, ts
            elif temp >= current["low"] + RECOVERED_BY or ts - current["low_at"] >= SETTLED_AFTER:
                current["end"] = settled_at = ts
                if events and current["start"] - events[-1]["end"] <= MERGE_WITHIN:  # a window opened, shut, reopened
                    events[-1] |= {"end": ts, "low": min(events[-1]["low"], current["low"]), "heating": events[-1]["heating"] or current["heating"]}
                else:
                    events.append(current)
                current = None
        elif heating or last_heat is None or ts - last_heat >= AFTER_HEAT_IGNORE:
            earlier = [p for p in points[max(0, i - 40) : i] if ts - p[0] <= FAST_DROP_WINDOW and p[0] >= settled_at]
            peak = max(earlier, key=lambda p: (p[1], p[0]), default=None)
            if peak and peak[1] - temp >= FAST_DROP:
                current = {"start": peak[0], "end": None, "peak": peak[1], "low": temp, "low_at": ts, "heating": bool(heating)}
        if heating:
            last_heat = ts
    if current:
        if now is not None and now - points[-1][0] >= SETTLED_AFTER:
            current["end"] = points[-1][0]
        events.append(current)
    return [{"start": e["start"], "end": e["end"], "drop": round(e["peak"] - e["low"], 1), "heating": e["heating"]} for e in events]


def heating_without_effect(points: list[tuple[int, float, int]]) -> bool:
    """Calling for heat without a break for NO_EFFECT_AFTER, and no warmer for it."""
    if not points or not points[-1][2]:
        return False
    i = len(points) - 1
    while i > 0 and points[i - 1][2] and points[i][0] - points[i - 1][0] <= 300:
        i -= 1
    start = points[-1][0] - NO_EFFECT_AFTER
    if points[i][0] > start:
        return False
    baseline = next(temp for ts, temp, _ in points[i:] if ts >= start)
    return points[-1][1] - baseline < NO_EFFECT_RISE


def rate_hours(hours: list[dict], indoor_water: float | None, now: int) -> tuple[list[dict], dict | None]:
    """Rate each waking hour from this one on, and find the best block of each day.
    hours: {hour, temp, humidity, rain, aqi}. Returns (rated hours, next best block)."""
    this_hour = now - now % 3600
    rated, by_day = [], {}
    for h in hours:
        at = datetime.fromtimestamp(h["hour"], TZ)
        if h["hour"] < this_hour or at.hour not in AWAKE:
            continue
        reason = None
        if h["rain"] >= RAINY_MM:
            reason = "rain"
        elif h["aqi"] is not None and h["aqi"] >= POOR_AIR:
            reason = "poor air"
        elif indoor_water is not None and indoor_water - absolute_humidity(h["temp"], h["humidity"]) < MIN_DRYING:
            reason = "not drier outside"
        row = h | {"rating": "avoid" if reason else "ok", "reason": reason, "minutes": airing_minutes(h["temp"])}
        rated.append(row)
        by_day.setdefault(at.date(), []).append(row)

    score = lambda r: r["temp"] - (r["aqi"] or 0) * 0.05  # noqa: E731 - 20 AQI points are worth a degree
    blocks = []
    for rows in by_day.values():
        runs = [rows[i : i + BEST_BLOCK] for i in range(len(rows) - BEST_BLOCK + 1)]
        runs = [r for r in runs if all(x["rating"] == "ok" for x in r) and r[-1]["hour"] - r[0]["hour"] == (BEST_BLOCK - 1) * 3600]
        if not runs:
            singles = [[x] for x in rows if x["rating"] == "ok"]
            runs = singles
        if runs:
            best = max(runs, key=lambda r: sum(score(x) for x in r) / len(r))
            for x in best:
                x["rating"] = "best"
            blocks.append(best)
    nxt = None
    if blocks:
        first = blocks[0]
        nxt = {
            "start": first[0]["hour"],
            "end": first[-1]["hour"] + 3600,
            "temp": round(sum(x["temp"] for x in first) / len(first), 1),
            "aqi": None if first[0]["aqi"] is None else round(first[0]["aqi"]),
            "minutes": first[0]["minutes"],
        }
    return rated, nxt


class Air:
    def __init__(self, history: History):
        self.history = history
        db = history._connect()
        db.executescript(SCHEMA)
        db.close()

    def save(self, rows: list[tuple]) -> None:
        db = self.history._connect()
        try:
            with db:
                db.executemany("INSERT OR REPLACE INTO air_hours (hour, temp, humidity, rain, aqi) VALUES (?, ?, ?, ?, ?)", rows)
        finally:
            db.close()

    def room_points(self, since: int, now: int) -> dict[int, list[tuple[int, float, int]]]:
        db = self.history._connect()
        try:
            rows = db.execute(f"SELECT room_id, ts, temp, heating FROM ({ROOM_MINUTES}) ORDER BY room_id, ts", (since, now + 1)).fetchall()
        finally:
            db.close()
        out: dict[int, list] = {}
        for rid, ts, temp, heating in rows:
            out.setdefault(rid, []).append((ts, temp, heating))
        return out

    def report(self, names: dict[int, str], now: int) -> dict:
        midnight = int(datetime.fromtimestamp(now, TZ).replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
        points = self.room_points(midnight - 7200, now)  # back far enough to know about heat before midnight
        events = []
        for rid, pts in points.items():
            for e in detect_windows(pts, now):
                if rid in names and (e["end"] is None or e["end"] >= midnight):
                    events.append(e | {"room": names[rid], "room_id": rid})
        db = self.history._connect()
        try:
            hours = [
                dict(zip(("hour", "temp", "humidity", "rain", "aqi"), r))
                for r in db.execute("SELECT hour, temp, humidity, rain, aqi FROM air_hours WHERE hour >= ? AND hour < ? ORDER BY hour", (now - 3600, now + 40 * 3600))
            ]
            indoor = db.execute(
                "SELECT room_id, AVG(temp), AVG(humidity) FROM samples WHERE ts >= ? AND humidity IS NOT NULL GROUP BY room_id", (now - 900,)
            ).fetchall()
        finally:
            db.close()
        waters = [absolute_humidity(t, h) for rid, t, h in indoor if rid in names]
        rated, best = rate_hours(hours, sum(waters) / len(waters) if waters else None, now)
        humid = [{"room": names[rid], "humidity": round(h)} for rid, _, h in indoor if rid in names and h >= HUMID]
        return {"hours": rated, "best": best, "events": sorted(events, key=lambda e: -e["start"]), "humid_rooms": humid, "now": now}


async def fetch_air(air: Air, client: httpx.AsyncClient, location: tuple[float, float]) -> None:
    where = {"latitude": location[0], "longitude": location[1], "timezone": "Europe/Rome", "timeformat": "unixtime"}
    r = await client.get(
        "https://api.open-meteo.com/v1/forecast",
        params=where | {"hourly": "temperature_2m,relative_humidity_2m,precipitation", "forecast_days": 3},
    )
    r.raise_for_status()
    weather = r.json()["hourly"]
    aqi: dict[int, float] = {}
    try:  # useful, not essential: the advice still works from the weather alone
        q = await client.get("https://air-quality-api.open-meteo.com/v1/air-quality", params=where | {"hourly": "european_aqi", "forecast_days": 3})
        q.raise_for_status()
        hourly = q.json()["hourly"]
        aqi = dict(zip(hourly["time"], hourly["european_aqi"]))
    except (httpx.HTTPError, KeyError, ValueError) as exc:
        log.warning("air quality unavailable: %s", exc)
    rows = [
        (t, temp, rh, rain or 0.0, aqi.get(t))
        for t, temp, rh, rain in zip(weather["time"], weather["temperature_2m"], weather["relative_humidity_2m"], weather["precipitation"])
        if temp is not None and rh is not None
    ]
    await asyncio.to_thread(air.save, rows)


async def run_air(air: Air, location: tuple[float, float]) -> None:
    async with httpx.AsyncClient(timeout=30) as client:
        while True:
            try:
                await fetch_air(air, client, location)
                wait = 3600
            except Exception as exc:  # noqa: BLE001 - the advice keeps using what's stored
                log.warning("air update failed: %s", exc)
                wait = 900
            await asyncio.sleep(wait)
