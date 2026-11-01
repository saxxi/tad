"""tado local — FastAPI server.

Local-network control app for classic tado° heating systems:
  - preferred: HomeKit pairing with the bridge (fully local, no account needed)
  - fallback: one-time cloud login + the bridge's local REST mirror (old firmware)
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from aiohomekit.exceptions import HomeKitException
from pydantic import BaseModel

from .auth import AuthError
from .homekit import MODE_OFF
from .state import State, StateError

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("tado-local")

BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR.parent / "data"

state = State(DATA_DIR)


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
    yield
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
        # HomeKit has no termination types: this is a manual overlay until "Schedule"
        try:
            await state.homekit.set_temperature(zid, body.celsius)
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
        # no timers over HomeKit: heat to max until "Schedule" is pressed
        try:
            await state.homekit.set_temperature(zid, state.homekit.zones.get(zid, {}).get("max_temp", 25))
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
