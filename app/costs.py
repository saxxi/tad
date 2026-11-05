"""Heating cost so far and a forecast for the whole heating season.

Nothing here measures gas. HomeKit only says whether each valve is calling for heat, so:

    valve-hours  x  gas per valve-hour  x  price  =  euros

Gas per valve-hour starts as an assumption (kW per open valve, boiler efficiency) and is replaced
by a measured value once two gas meter readings at least a week apart exist.

The forecast assumes the house keeps heating as much per unit of cold as it has so far:

    valve-hours per degree day  x  degree days left in the season

Degree days come from the 16-day weather forecast, then from the average of past winters. The range
combines the mildest and coldest of those winters with how much the gas figure can be trusted.
Weather comes from Open-Meteo: only the coordinates ("location" in data/config.json) leave the house.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from datetime import date, datetime, timedelta

import httpx

from .history import History
from .local import TZ, LocalStore

log = logging.getLogger("tado-local.costs")

KWH_PER_SMC = 10.7
USAGE_CACHE_SECONDS = 300  # a season of samples takes a moment to scan
WEATHER_EVERY = 3 * 3600
WEATHER_RETRY = 15 * 60
MIN_METER_DAYS = 7
MIN_METER_VALVE_HOURS = 20
WHOLE_DAY_MINUTES = 1368  # 95% of a day
SETTLED_DAYS = 7  # below this the heating pattern hasn't even seen a full week

DEFAULTS = {
    "price": 1.40,  # EUR per Smc, all included
    "kw_per_valve": 1.0,  # average heat from a valve calling for heat
    "efficiency": 0.90,  # boiler
    "nonheating_smc_day": 0.4,  # hot water and cooking, taken out of meter readings
    "budget": None,  # EUR for the season
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS weather (hour INTEGER PRIMARY KEY, temp REAL NOT NULL);  -- outdoor, observed and forecast
CREATE TABLE IF NOT EXISTS climate (day TEXT PRIMARY KEY, temp REAL NOT NULL);      -- past winters, daily mean
CREATE TABLE IF NOT EXISTS meter (ts INTEGER PRIMARY KEY, smc REAL NOT NULL);       -- gas meter readings
"""


# -- pure calculations ------------------------------------------------------------
def season_for(day: date) -> tuple[date, date]:
    """Italian climate zone E: 15 October to 15 April. Between seasons, the next one."""
    year = day.year if (day.month, day.day) > (4, 15) else day.year - 1
    return date(year, 10, 15), date(year + 1, 4, 15)


def degree_days(mean_temp: float) -> float:
    """Heating degree days of one day, by the Eurostat rule: 18 - T, none above 15 °C."""
    return 18 - mean_temp if mean_temp <= 15 else 0.0


def _md(day: date) -> tuple[int, int]:
    return (2, 28) if (day.month, day.day) == (2, 29) else (day.month, day.day)


def climate_normals(climate: dict[date, float]) -> tuple[dict[tuple[int, int], float], float, float] | None:
    """(normal degree days by calendar day, mildest season / average, coldest season / average)."""
    totals: dict[int, list[float]] = {}
    by_day: dict[tuple[int, int], list[float]] = {}
    for day, temp in climate.items():
        start, end = season_for(day)
        if start <= day <= end:
            totals.setdefault(start.year, []).append(degree_days(temp))
            by_day.setdefault(_md(day), []).append(degree_days(temp))
    complete = [sum(v) for v in totals.values() if len(v) >= 180]
    if not complete:
        return None
    mean = sum(complete) / len(complete)
    days = [date(2001, 10, 15) + timedelta(days=i) for i in range(183)]  # any year without 29 Feb
    raw = [sum(by_day[_md(d)]) / len(by_day[_md(d)]) if _md(d) in by_day else 0.0 for d in days]
    smooth = {}
    for i, d in enumerate(days):  # a two-week average: one cold spell in one year isn't a normal
        window = raw[max(0, i - 7) : i + 8]
        smooth[_md(d)] = sum(window) / len(window)
    return smooth, min(complete) / mean, max(complete) / mean


def gas_per_valve_hour(settings: dict) -> float:
    return settings["kw_per_valve"] / (KWH_PER_SMC * settings["efficiency"])


def calibrate(readings: list[tuple[int, float]], usage: list[tuple[int, int, float]], nonheating_smc_day: float) -> float | None:
    """Gas per valve-hour measured between the first and last meter reading, if they're usable."""
    if len(readings) < 2:
        return None
    (t0, smc0), (t1, smc1) = readings[0], readings[-1]
    days = (t1 - t0) / 86400
    hours = sum(h for hour, _, h in usage if t0 <= hour < t1)
    heating = smc1 - smc0 - nonheating_smc_day * days
    if days < MIN_METER_DAYS or hours < MIN_METER_VALVE_HOURS or heating <= 0:
        return None
    return heating / hours


def build_forecast(
    today: date,
    usage: dict[date, dict[int, float]],
    minutes: dict[date, int],
    weather: dict[date, float],
    normals: tuple[dict[tuple[int, int], float], float, float],
    gas: float,
    measured: bool,
    price: float,
    budget: float | None,
    room_names: dict[int, str],
) -> dict | None:
    """None until there is heating to scale from. usage: day -> room -> valve-hours."""
    start, end = season_for(today)
    normal, mild, cold = normals
    days = [start + timedelta(days=i) for i in range((end - start).days + 1)]
    cold_of = {d: degree_days(weather[d]) if d in weather else normal[_md(d)] for d in days}

    observed_days = [d for d in days if minutes.get(d)]
    hours = sum(sum(usage.get(d, {}).values()) for d in observed_days)
    # A part day says little: a morning alone holds the whole reheat and none of the quiet hours.
    basis = [d for d in observed_days if d < today and minutes[d] >= WHOLE_DAY_MINUTES] or observed_days
    basis_hours = sum(sum(usage.get(d, {}).values()) for d in basis)
    basis_cold = sum(cold_of[d] * min(1, minutes[d] / 1440) for d in basis)
    if hours <= 0 or basis_hours <= 0 or basis_cold <= 0:
        return None
    rate = basis_hours / basis_cold  # valve-hours per degree day

    observed_for = sum(minutes[d] for d in observed_days) / 1440
    uncertainty = (0.15 if measured else 0.40) + (0.25 if observed_for < SETTLED_DAYS else 0.0)

    recorded = estimated = upcoming = climate_part = 0.0  # valve-hours
    series, months = [], {}
    cold_total = sum(cold_of.values())
    cold_so_far = 0.0
    recorded_today = 0.0
    for d in days:
        rec = sum(usage.get(d, {}).values())
        share = min(1, minutes.get(d, 0) / 1440)
        missing = (1 - share) * cold_of[d] * rate
        if d < today:  # recording gaps, including everything before recording began
            recorded += rec
            estimated += missing
        elif d == today:
            recorded += rec
            recorded_today = rec
            upcoming += missing
        elif d in weather:
            upcoming += missing
        else:
            climate_part += missing
        cold_so_far += cold_of[d]
        spent_part = recorded + estimated
        cost = lambda h: h * gas * price  # noqa: E731
        series.append(
            {
                "d": d.isoformat(),
                "eur": round(cost(spent_part + upcoming + climate_part), 2),
                "low": round(cost(spent_part + upcoming + climate_part * mild) * (1 - uncertainty), 2),
                "high": round(cost(spent_part + upcoming + climate_part * cold) * (1 + uncertainty), 2),
                "aim": round(budget * cold_so_far / cold_total, 2) if budget else None,
            }
        )
        m = months.setdefault(d.strftime("%Y-%m"), {"month": d.strftime("%Y-%m"), "eur": 0.0, "recorded_eur": 0.0, "degree_days": 0.0})
        m["eur"] += cost(rec + missing)
        m["recorded_eur"] += cost(rec)
        m["degree_days"] += cold_of[d]

    room_hours: dict[int, float] = {}
    for d in observed_days:
        for rid, h in usage.get(d, {}).items():
            room_hours[rid] = room_hours.get(rid, 0.0) + h
    total_eur = series[-1]["eur"]
    pos = next((i for i, d in enumerate(days) if d == today), None)
    spent_eur = (recorded + estimated) * gas * price
    return {
        "spent": {"eur": round(spent_eur, 2), "recorded_eur": round(recorded * gas * price, 2), "estimated_eur": round(estimated * gas * price, 2)},
        "today_eur": round(recorded_today * gas * price, 2),
        "forecast": {
            "eur": total_eur,
            "low": series[-1]["low"],
            "high": series[-1]["high"],
            "smc": round((recorded + estimated + upcoming + climate_part) * gas, 1),
            "remaining_eur": round(total_eur - spent_eur, 2),
        },
        "budget": None if not budget else {
            "eur": budget,
            "aim_today_eur": series[pos]["aim"] if pos is not None else 0.0,
            "forecast_gap_eur": round(total_eur - budget, 2),
        },
        "confidence": {
            "level": "measured" if measured else "rough" if observed_for < SETTLED_DAYS else "estimate",
            "uncertainty": round(uncertainty, 2),
            "observed_days": round(observed_for, 2),
            "measured": measured,
            "weather_range": [round(mild, 2), round(cold, 2)],
        },
        "model": {
            "valve_hours_recorded": round(recorded, 1),
            "valve_hours_per_degree_day": round(rate, 3),
            "smc_per_valve_hour": round(gas, 4),
            "season_degree_days": round(cold_total),
        },
        "rooms": sorted(
            (
                {
                    "id": rid,
                    "name": room_names.get(rid, "Unassigned devices"),
                    "valve_hours": round(h, 1),
                    "share": round(h / hours, 3),
                    "season_eur": round(total_eur * h / hours, 2),
                }
                for rid, h in room_hours.items()
            ),
            key=lambda r: -r["season_eur"],
        ),
        "months": [m | {"eur": round(m["eur"], 2), "recorded_eur": round(m["recorded_eur"], 2), "degree_days": round(m["degree_days"])} for m in months.values()],
        "series": series,
    }


# -- storage + report --------------------------------------------------------------
class Costs:
    def __init__(self, history: History, store: LocalStore):
        self.history = history
        self.store = store
        self._lock = threading.Lock()
        self._usage: tuple[float, list, dict] | None = None
        db = history._connect()
        db.executescript(SCHEMA)
        db.close()

    def settings(self) -> dict:
        return DEFAULTS | self.store.get_setting("costs", {})

    def update_settings(self, changes: dict) -> dict:
        self.store.set_setting("costs", self.settings() | changes)
        return self.settings()

    def _write(self, sql: str, rows: list[tuple]) -> None:
        with self._lock:
            db = self.history._connect()
            try:
                with db:
                    db.executemany(sql, rows)
            finally:
                db.close()

    def save_weather(self, rows: list[tuple[int, float]]) -> None:
        self._write("INSERT OR REPLACE INTO weather (hour, temp) VALUES (?, ?)", rows)

    def save_climate(self, rows: list[tuple[str, float]]) -> None:
        self._write("INSERT OR REPLACE INTO climate (day, temp) VALUES (?, ?)", rows)

    def has_climate(self) -> bool:
        db = self.history._connect()
        try:
            return db.execute("SELECT 1 FROM climate LIMIT 1").fetchone() is not None
        finally:
            db.close()

    def add_reading(self, smc: float, ts: int | None = None) -> None:
        ts = ts or int(time.time())
        self._write("INSERT OR REPLACE INTO meter (ts, smc) VALUES (?, ?)", [(ts, smc)])

    def delete_reading(self, ts: int) -> None:
        self._write("DELETE FROM meter WHERE ts = ?", [(ts,)])

    def readings(self) -> list[tuple[int, float]]:
        db = self.history._connect()
        try:
            return db.execute("SELECT ts, smc FROM meter ORDER BY ts").fetchall()
        finally:
            db.close()

    def _load_usage(self, since: int, now: int) -> tuple[list[tuple[int, int, float]], dict[date, int]]:
        """Valve-hours per hour and room, and minutes recorded per day. The wall thermostat
        reports the same calling-for-heat as its room but has no radiator, so it doesn't count."""
        if self._usage and now - self._usage[0] < USAGE_CACHE_SECONDS:
            return self._usage[1], self._usage[2]
        db = self.history._connect()
        try:
            usage = db.execute(
                """SELECT ts - ts % 3600, room_id, SUM(heating) / 60.0 FROM samples
                   WHERE ts >= ? AND serial NOT LIKE 'RU%' GROUP BY ts - ts % 3600, room_id""",
                (since,),
            ).fetchall()
            per_hour = db.execute(
                "SELECT ts - ts % 3600, COUNT(DISTINCT ts) FROM samples WHERE ts >= ? GROUP BY ts - ts % 3600", (since,)
            ).fetchall()
        finally:
            db.close()
        minutes: dict[date, int] = {}
        for hour, n in per_hour:
            day = datetime.fromtimestamp(hour, TZ).date()
            minutes[day] = minutes.get(day, 0) + n
        self._usage = (now, usage, minutes)
        return usage, minutes

    def report(self, now: int | None = None) -> dict:
        now = now or int(time.time())
        today = datetime.fromtimestamp(now, TZ).date()
        start, end = season_for(today)
        since = int(datetime(start.year, start.month, start.day, tzinfo=TZ).timestamp())
        settings = self.settings()
        usage_rows, minutes = self._load_usage(since, now)
        db = self.history._connect()
        try:
            temps: dict[date, list[float]] = {}
            for hour, temp in db.execute("SELECT hour, temp FROM weather WHERE hour >= ?", (since,)):
                temps.setdefault(datetime.fromtimestamp(hour, TZ).date(), []).append(temp)
            climate = {date.fromisoformat(d): t for d, t in db.execute("SELECT day, temp FROM climate")}
        finally:
            db.close()
        weather = {d: sum(v) / len(v) for d, v in temps.items()}
        readings = [r for r in self.readings() if r[0] >= since]
        out = {
            "ready": False,
            "settings": settings,
            "season": {"start": start.isoformat(), "end": end.isoformat()},
            "meter": [{"ts": t, "smc": s} for t, s in readings],
            "outdoor": _next_days(weather, today),
        }
        normals = climate_normals(climate)
        if not normals:
            return out | {"reason": "Waiting for past winters' weather. It needs \"location\" in data/config.json."}
        usage: dict[date, dict[int, float]] = {}
        for hour, rid, hours in usage_rows:
            room = usage.setdefault(datetime.fromtimestamp(hour, TZ).date(), {})
            room[rid] = room.get(rid, 0.0) + hours
        measured = calibrate(readings, usage_rows, settings["nonheating_smc_day"])
        gas = measured if measured is not None else gas_per_valve_hour(settings)
        names = {r["id"]: r["name"] for r in self.store.rooms()}
        result = build_forecast(today, usage, minutes, weather, normals, gas, measured is not None, settings["price"], settings["budget"], names)
        if not result:
            return out | {"reason": "Waiting for the first heating of the season."}
        return out | result | {"ready": True, "today": today.isoformat()}


def _next_days(weather: dict[date, float], today: date) -> list[dict]:
    return [{"d": (today + timedelta(days=i)).isoformat(), "temp": round(weather[today + timedelta(days=i)], 1)} for i in range(7) if today + timedelta(days=i) in weather]


# -- weather loop --------------------------------------------------------------------
async def fetch_weather(costs: Costs, client: httpx.AsyncClient, location: tuple[float, float]) -> None:
    where = {"latitude": location[0], "longitude": location[1], "timezone": "Europe/Rome"}
    r = await client.get(
        "https://api.open-meteo.com/v1/forecast",
        params=where | {"hourly": "temperature_2m", "past_days": 92, "forecast_days": 16, "timeformat": "unixtime"},
    )
    r.raise_for_status()
    hourly = r.json()["hourly"]
    rows = [(t, v) for t, v in zip(hourly["time"], hourly["temperature_2m"]) if v is not None]
    await asyncio.to_thread(costs.save_weather, rows)
    if not await asyncio.to_thread(costs.has_climate):
        today = date.today()
        r = await client.get(
            "https://archive-api.open-meteo.com/v1/archive",
            params=where | {"daily": "temperature_2m_mean", "start_date": f"{today.year - 11}-10-01", "end_date": (today - timedelta(days=7)).isoformat()},
        )
        r.raise_for_status()
        daily = r.json()["daily"]
        rows = [(d, v) for d, v in zip(daily["time"], daily["temperature_2m_mean"]) if v is not None]
        await asyncio.to_thread(costs.save_climate, rows)
        log.info("loaded %d days of past weather", len(rows))


async def run_weather(costs: Costs, location: tuple[float, float]) -> None:
    async with httpx.AsyncClient(timeout=30) as client:
        while True:
            try:
                await fetch_weather(costs, client, location)
                wait = WEATHER_EVERY
            except Exception as exc:  # noqa: BLE001 - costs fall back to what's stored
                log.warning("weather update failed: %s", exc)
                wait = WEATHER_RETRY
            await asyncio.sleep(wait)
