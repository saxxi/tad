"""tado local — FastAPI server.

Local-network control app for classic tado° heating systems:
  - preferred: HomeKit pairing with the bridge (fully local, no account needed)
  - fallback: one-time cloud login + the bridge's local REST mirror (old firmware)
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

import httpx
from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from aiohomekit.exceptions import HomeKitException
from pydantic import BaseModel, Field

from .air import Air, run_air
from .alerts import Alerts, post_ntfy, run_alerts
from .auth import AuthError
from .costs import Costs, run_weather
from .history import RANGES, History, run_sampler
from .homekit import MODE_OFF
from .local import DEFAULT_AWAY_CELSIUS, MAX_DIAL, TZ, run_scheduler
from .state import State, StateError

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("tado-local")

BASE_DIR = Path(__file__).parent
DATA_DIR = Path(os.environ.get("TADO_DATA_DIR") or BASE_DIR.parent / "data")

state = State(DATA_DIR)
history = History(DATA_DIR / "history.db")
costs = Costs(history, state.store)
alerts = Alerts(state.store)
air = Air(history)
scheduler_wake = asyncio.Event()  # set it to enforce now instead of on the next tick


@asynccontextmanager
async def lifespan(app: FastAPI):
    state.start_poller()
    try:
        await state.homekit.start()
        if state.homekit.is_paired():
            asyncio.create_task(state.load_room_names())
        elif state.config.get("homekit_pin"):
            asyncio.create_task(state.homekit.auto_pair(state.config["homekit_pin"]))
    except Exception as exc:  # noqa: BLE001
        log.warning("HomeKit controller failed to start: %s", exc)
    if not state.bridge_ip:
        asyncio.create_task(state.rescan_bridge())  # pre-fill bridge before login
    scheduler = asyncio.create_task(run_scheduler(state.store, state.homekit, scheduler_wake, alerts))
    sampler = asyncio.create_task(run_sampler(history, state.store, state.homekit))
    location = state.config.get("location")  # [latitude, longitude]; without it, no weather features
    weather = asyncio.create_task(run_weather(costs, location)) if location else None
    alerting = asyncio.create_task(run_alerts(alerts, state.homekit, air, state.store))
    air_task = asyncio.create_task(run_air(air, location)) if location else None
    yield
    alerting.cancel()
    if location:
        weather.cancel()
        air_task.cancel()
    scheduler.cancel()
    sampler.cancel()
    await state.close()


app = FastAPI(title="tado local", lifespan=lifespan, docs_url=None, redoc_url=None)


# -- helpers ----------------------------------------------------------------
def err(exc: Exception) -> JSONResponse:
    if isinstance(exc, StateError):
        return JSONResponse({"detail": str(exc)}, status_code=exc.status_code)
    if isinstance(exc, AuthError):
        return JSONResponse({"detail": str(exc)}, status_code=401)
    if isinstance(exc, HomeKitException):
        return JSONResponse({"detail": f"HomeKit: {exc or type(exc).__name__}"}, status_code=502)
    if isinstance(exc, httpx.HTTPError):
        return JSONResponse({"detail": f"network error: {exc}"}, status_code=502)
    log.exception("unexpected error")
    return JSONResponse({"detail": f"unexpected error: {exc}"}, status_code=500)


def bridge_call(method: str, path: str, payload: dict | None = None):
    """Run a bridge call and refresh the cache right after."""
    async def run():
        r = await state.bridge_request(method, path, json_body=payload)
        if r.status_code >= 400:
            raise StateError(f"bridge rejected the command: HTTP {r.status_code}: {r.text[:200]}")
        asyncio.create_task(state.poll_once())

    return run()


# -- routes -------------------------------------------------------------------
@app.get("/")
async def index():
    return FileResponse(BASE_DIR / "static" / "index.html")


@app.get("/api/status")
async def api_status():
    return state.status()


class SetTempBody(BaseModel):
    celsius: float | None = None
    termination: str = "next"  # next | timer | manual
    duration_minutes: int = 60


TERMINATION = {
    "next": {"type": "UNTIL_NEXT_TIME_BLOCK"},
    "timer": lambda b: {"type": "TIMER", "durationInSeconds": min(b.duration_minutes, 720) * 60},
    "manual": {"type": "MANUAL"},
}


@app.post("/api/auth/device")
async def api_auth_device():
    try:
        info = await state.auth.start_device_flow()
        return {"pending": info}
    except Exception as exc:  # noqa: BLE001
        return err(exc)


@app.post("/api/auth/activate")
async def api_auth_activate():
    try:
        completed = await state.auth.poll_device_flow()
        if completed:
            await state.after_login()
        return {"completed": completed, "status": state.status()}
    except Exception as exc:  # noqa: BLE001
        return err(exc)


@app.delete("/api/auth")
async def api_auth_logout():
    state.auth.logout()
    state.config.pop("home_id", None)
    state.config.pop("home_name", None)
    state.config.pop("local_token", None)
    state._save_config()
    state.zones = {}
    return {"ok": True}


@app.post("/api/bridge/rescan")
async def api_bridge_rescan():
    try:
        found = await state.rescan_bridge()
        return {"found": found, "status": state.status()}
    except Exception as exc:  # noqa: BLE001
        return err(exc)


class PairBody(BaseModel):
    pin: str


@app.post("/api/homekit/pair")
async def api_homekit_pair(body: PairBody):
    try:
        result = await state.homekit.pair(body.pin)
        asyncio.create_task(state.load_room_names())
        return result | {"status": state.status()}
    except Exception as exc:  # noqa: BLE001
        return err(exc)


@app.delete("/api/homekit")
async def api_homekit_unpair():
    try:
        await state.homekit.unpair()
        return {"ok": True}
    except Exception as exc:  # noqa: BLE001
        return err(exc)


# -- local rooms + schedule ----------------------------------------------------------
class RoomBody(BaseModel):
    name: str


class DeviceBody(BaseModel):
    room_id: int | None = None


class SchedulePoint(BaseModel):
    days: str  # ISO weekdays, "12345" = Mon-Fri
    time: str  # "HH:MM"
    celsius: float | None = None  # None = off


@app.get("/api/rooms")
async def api_rooms():
    return {"rooms": state.store.rooms(), "away": state.store.away(datetime.now(TZ)), "alerts": alerts.list(), "dial": state.store.dial()}


@app.post("/api/rooms")
async def api_room_add(body: RoomBody):
    return {"id": state.store.add_room(body.name)}


@app.patch("/api/rooms/{rid}")
async def api_room_rename(rid: int, body: RoomBody):
    state.store.rename_room(rid, body.name)
    return {"ok": True}


@app.delete("/api/rooms/{rid}")
async def api_room_delete(rid: int):
    state.store.delete_room(rid)
    return {"ok": True}


class OnDemandBody(BaseModel):
    celsius: float = Field(ge=5, le=25)
    hours: float = Field(ge=0.5, le=12)


@app.put("/api/rooms/{rid}/on-demand")
async def api_room_on_demand(rid: int, body: OnDemandBody | None = None):
    state.store.set_on_demand(rid, body.model_dump() if body else None)
    return {"ok": True}


class BoostBody(BaseModel):
    extend_hours: float | None = Field(None, ge=0.5, le=12)  # add to a running hold instead of starting one


@app.post("/api/rooms/{rid}/boost")
async def api_room_boost(rid: int, body: BoostBody):
    """The room's "I'm using this room" switch: warm it now, for a while."""
    config = state.store.on_demand().get(rid)
    if not config:
        return JSONResponse({"detail": "this room has no on-demand setting"}, status_code=422)
    return await _hold_room(rid, state.store.dialled(config["celsius"]), body.extend_hours or config["hours"], extend=body.extend_hours is not None)


class HoldBody(BaseModel):
    celsius: float | None = Field(None, ge=5, le=25)  # None = heating off
    hours: float = Field(ge=0.25, le=24)


@app.post("/api/rooms/{rid}/hold")
async def api_room_hold(rid: int, body: HoldBody):
    """Warm a room or turn it off for a while, whatever its schedule says. Night off still ends it."""
    return await _hold_room(rid, body.celsius, body.hours)


async def _hold_room(rid: int, celsius: float | None, hours: float, extend: bool = False):
    room_of = state.store.room_of()
    aids = [aid for aid, z in state.homekit.zones.items() if room_of.get(z["serial"]) == rid]
    if not aids:
        return JSONResponse({"detail": "this room has no devices"}, status_code=422)
    # hold first, so the scheduler doesn't undo the change between the two steps
    state.store.boost(rid, hours, extend=extend)
    send = (lambda aid: state.homekit.set_mode(aid, MODE_OFF)) if celsius is None else (lambda aid: state.homekit.set_temperature(aid, celsius))
    results = await asyncio.gather(*(send(aid) for aid in aids), return_exceptions=True)
    if any(isinstance(r, Exception) for r in results):
        state.store.resume(rid)
        scheduler_wake.set()
        return JSONResponse({"detail": "the valve isn't responding, try again in a few minutes"}, status_code=502)
    return {"ok": True}


@app.post("/api/rooms/{rid}/resume")
async def api_room_resume(rid: int):
    state.store.resume(rid)
    scheduler_wake.set()
    return {"ok": True}


@app.put("/api/devices/{serial}")
async def api_device_move(serial: str, body: DeviceBody):
    state.store.move_device(serial, body.room_id)
    return {"ok": True}


@app.put("/api/rooms/{rid}/schedule")
async def api_room_schedule(rid: int, points: list[SchedulePoint]):
    for p in points:
        hh, _, mm = p.time.partition(":")
        if not (hh.isdigit() and mm.isdigit() and int(hh) < 24 and int(mm) < 60 and p.days and set(p.days) <= set("1234567")):
            return JSONResponse({"detail": f"bad schedule entry: {p.days} {p.time}"}, status_code=422)
        if p.celsius is not None and not 5 <= p.celsius <= 25:
            return JSONResponse({"detail": "temperature must be 5-25 °C"}, status_code=422)
    norm = lambda t: "%02d:%02d" % tuple(map(int, t.split(":")))  # "6:30" -> "06:30"
    state.store.set_points(rid, [p.model_dump() | {"time": norm(p.time)} for p in points])
    scheduler_wake.set()
    return {"ok": True}


class QuietBody(BaseModel):
    start: str | None = None  # "HH:MM"; both None = no night off for this room
    end: str | None = None


def _hhmm(value: str) -> str | None:
    """'6:30' -> '06:30'; None if it isn't a time of day."""
    hh, _, mm = value.partition(":")
    if hh.isdigit() and mm.isdigit() and int(hh) < 24 and int(mm) < 60:
        return f"{int(hh):02d}:{int(mm):02d}"
    return None


@app.put("/api/rooms/{rid}/quiet")
async def api_room_quiet(rid: int, body: QuietBody):
    if body.start is None and body.end is None:
        state.store.set_quiet(rid, None, None)
    else:
        start, end = _hhmm(body.start or ""), _hhmm(body.end or "")
        if not start or not end or start == end:
            return JSONResponse({"detail": "night off needs a different start and end time"}, status_code=422)
        state.store.set_quiet(rid, start, end)
    scheduler_wake.set()
    return {"ok": True}


class DialBody(BaseModel):
    offset: float = Field(ge=-MAX_DIAL, le=MAX_DIAL)


@app.put("/api/house/dial")
async def api_house_dial(body: DialBody):
    if body.offset * 2 != int(body.offset * 2):
        return JSONResponse({"detail": "the dial moves in half degrees"}, status_code=422)
    state.store.set_dial(body.offset)
    scheduler_wake.set()
    return {"ok": True, "dial": body.offset}


class AwayBody(BaseModel):
    active: bool
    until: str | None = None  # ISO time; without an offset it's house time. None = until turned off
    celsius: float | None = DEFAULT_AWAY_CELSIUS  # None = heating off


@app.post("/api/house/away")
async def api_house_away(body: AwayBody):
    until = None
    if body.active and body.until:
        try:
            until = datetime.fromisoformat(body.until)
        except ValueError:
            return JSONResponse({"detail": f"bad date: {body.until}"}, status_code=422)
        until = until.replace(tzinfo=TZ) if until.tzinfo is None else until.astimezone(TZ)
        if until <= datetime.now(TZ):
            return JSONResponse({"detail": "the return time is in the past"}, status_code=422)
    if body.celsius is not None and not 5 <= body.celsius <= 25:
        return JSONResponse({"detail": "temperature must be 5-25 °C"}, status_code=422)
    celsius = body.celsius if body.active else state.store.away(datetime.now(TZ))["celsius"]  # remember it for next time
    state.store.set_away(body.active, until, celsius)
    scheduler_wake.set()
    return {"ok": True, "away": state.store.away(datetime.now(TZ))}


@app.get("/api/history")
async def api_history(range: str = "24h"):
    if range not in RANGES:
        return JSONResponse({"detail": f"range must be one of {', '.join(RANGES)}"}, status_code=422)
    rooms = [{"id": r["id"], "name": r["name"]} for r in state.store.rooms()]
    return await asyncio.to_thread(history.query, range, rooms)


@app.get("/api/air")
async def api_air():
    names = {r["id"]: r["name"] for r in state.store.rooms()}
    return await asyncio.to_thread(air.report, names, int(time.time()))


@app.get("/api/alerts/phone")
async def api_alerts_phone():
    return alerts.phone()


class PhoneBody(BaseModel):
    enabled: bool


@app.put("/api/alerts/phone")
async def api_alerts_phone_set(body: PhoneBody):
    return alerts.set_phone(body.enabled)


@app.post("/api/alerts/test")
async def api_alerts_test():
    phone = alerts.phone()
    if not phone["enabled"]:
        return JSONResponse({"detail": "turn phone alerts on first"}, status_code=422)
    async with httpx.AsyncClient(timeout=15) as client:
        try:
            await post_ntfy(client, phone["topic"], "tado local", "Test: phone alerts are working.", "default")
        except httpx.HTTPError as exc:
            return err(exc)
    return {"ok": True}


@app.get("/api/costs")
async def api_costs():
    return await asyncio.to_thread(costs.report)


class CostSettingsBody(BaseModel):
    price: float | None = Field(None, ge=0.1, le=10)
    kw_per_valve: float | None = Field(None, ge=0.1, le=10)
    efficiency: float | None = Field(None, ge=0.5, le=1.1)
    nonheating_smc_day: float | None = Field(None, ge=0, le=10)
    budget: float | None = Field(None, gt=0)  # sent as null to clear it


@app.put("/api/costs/settings")
async def api_costs_settings(body: CostSettingsBody):
    changes = body.model_dump(exclude_unset=True)
    if any(v is None for k, v in changes.items() if k != "budget"):
        return JSONResponse({"detail": "only the budget can be left empty"}, status_code=422)
    return costs.update_settings(changes)


class MeterBody(BaseModel):
    smc: float = Field(ge=0)


@app.post("/api/costs/meter")
async def api_costs_meter(body: MeterBody):
    readings = await asyncio.to_thread(costs.readings)
    if readings and body.smc < readings[-1][1]:
        return JSONResponse({"detail": f"that's below the last reading ({readings[-1][1]:g})"}, status_code=422)
    await asyncio.to_thread(costs.add_reading, body.smc)
    return {"ok": True}


@app.delete("/api/costs/meter/{ts}")
async def api_costs_meter_delete(ts: int):
    await asyncio.to_thread(costs.delete_reading, ts)
    return {"ok": True}


@app.get("/api/zones")
async def api_zones():
    return {"zones": state.zones_payload(), "status": state.status()}


@app.post("/api/zones/{zid}/set")
async def api_zone_set(zid: int, body: SetTempBody):
    term = TERMINATION.get(body.termination)
    if callable(term):
        term = term(body)
    if term is None:
        return JSONResponse({"detail": f"unknown termination '{body.termination}'"}, status_code=422)
    if state.homekit.is_paired():
        # HomeKit has no termination types: the local schedule holds off until its next point
        try:
            await state.homekit.set_temperature(zid, body.celsius)
            state.hold(zid)
            return {"ok": True}
        except Exception as exc:  # noqa: BLE001
            return err(exc)
    setting = {"type": "HEATING", "power": "ON", "temperature": {"celsius": body.celsius}}
    try:
        await bridge_call("PUT", f"/homes/{state.home_id}/zones/{zid}/overlay", {"setting": setting, "termination": term})
        return {"ok": True}
    except Exception as exc:  # noqa: BLE001
        return err(exc)


@app.post("/api/zones/{zid}/identify")
async def api_zone_identify(zid: int):
    try:
        await state.homekit.identify(zid)
        return {"ok": True}
    except Exception as exc:  # noqa: BLE001
        return err(exc)


@app.get("/api/devices/health")
async def api_devices_health():
    try:
        return {"devices": await state.device_health()}
    except Exception as exc:  # noqa: BLE001
        return err(exc)


@app.post("/api/zones/{zid}/off")
async def api_zone_off(zid: int):
    if state.homekit.is_paired():
        try:
            await state.homekit.set_mode(zid, MODE_OFF)
            state.hold(zid)
            return {"ok": True}
        except Exception as exc:  # noqa: BLE001
            return err(exc)
    overlay = {
        "setting": {"type": "HEATING", "power": "OFF"},
        "termination": {"type": "UNTIL_NEXT_TIME_BLOCK"},
    }
    try:
        await bridge_call("PUT", f"/homes/{state.home_id}/zones/{zid}/overlay", overlay)
        return {"ok": True}
    except Exception as exc:  # noqa: BLE001
        return err(exc)


@app.post("/api/zones/{zid}/boost")
async def api_zone_boost(zid: int):
    if state.homekit.is_paired():
        # no timers over HomeKit: heat to max until the room's next schedule point
        try:
            await state.homekit.set_temperature(zid, state.homekit.zones.get(zid, {}).get("max_temp", 25))
            state.hold(zid)
            return {"ok": True}
        except Exception as exc:  # noqa: BLE001
            return err(exc)
    caps = state.capabilities.get(zid) or {}
    max_temp = (caps.get("temperatures") or {}).get("celsius", {}).get("max", 25)
    overlay = {
        "setting": {"type": "HEATING", "power": "ON", "temperature": {"celsius": max_temp}},
        "termination": {"type": "TIMER", "durationInSeconds": 30 * 60},
    }
    try:
        await bridge_call("PUT", f"/homes/{state.home_id}/zones/{zid}/overlay", overlay)
        return {"ok": True}
    except Exception as exc:  # noqa: BLE001
        return err(exc)


@app.delete("/api/zones/{zid}/overlay")
async def api_zone_resume(zid: int):
    if state.homekit.is_paired():
        # the bridge's HomeKit has no "auto" mode: removing the overlay is one cloud call
        try:
            serial = (state.homekit.zones.get(zid) or {}).get("serial")
            if serial not in state.zone_ids:
                await state.load_room_names()
            if serial not in state.zone_ids:
                raise StateError("resuming the schedule needs the tado account: click Connect tado account", 409)
            r = await state.cloud_request("DELETE", f"/homes/{state.home_id}/zones/{state.zone_ids[serial]}/overlay")
            if r.status_code >= 400:
                raise StateError(f"tado cloud rejected schedule resume: HTTP {r.status_code}")
            await state.homekit.refresh()
            return {"ok": True}
        except Exception as exc:  # noqa: BLE001
            return err(exc)
    try:
        await bridge_call("DELETE", f"/homes/{state.home_id}/zones/{zid}/overlay")
        return {"ok": True}
    except Exception as exc:  # noqa: BLE001
        return err(exc)


class PresenceBody(BaseModel):
    mode: str  # HOME | AWAY


@app.post("/api/home/presence")
async def api_presence(body: PresenceBody):
    mode = body.mode.upper()
    if mode not in ("HOME", "AWAY"):
        return JSONResponse({"detail": "mode must be HOME or AWAY"}, status_code=422)
    payload = {"homePresence": mode}
    try:
        try:
            await bridge_call("PUT", f"/homes/{state.home_id}/presence", payload)
        except StateError:
            # older firmwares don't mirror presence locally; go through the cloud
            r = await state.cloud_request("PUT", f"/homes/{state.home_id}/presence", json_body=payload)
            if r.status_code >= 400:
                raise StateError(f"presence update failed: HTTP {r.status_code}")
        state.presence = mode
        return {"ok": True, "mode": mode}
    except Exception as exc:  # noqa: BLE001
        return err(exc)
