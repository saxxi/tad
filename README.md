# tado° local

A small local app to monitor and control a **classic tado° system** (Internet
Bridge, V2/V3/Smart AC — anything with HomeKit support) directly over your LAN.

Why local: tado's cloud API is rate-limited (100 requests/day without a
subscription since Sept 2025) and everything routes through their servers.
The bridge itself serves the same REST API on port 80 — this app talks to it
directly after a one-time login.

## How it works

1. **HomeKit pairing (fully local)** — current bridge firmware no longer serves the
   local REST API (port 80 is its HomeKit accessory server). The app pairs with the
   bridge as a HomeKit controller using the 8-digit code on the bridge sticker
   (`homekit_pin` in `data/config.json`, auto-pairs when the bridge is unpaired). Keys
   live in `data/homekit.json` (mode 600) — losing them leaves the bridge paired to
   nobody, and only the factory-reset hole on the bridge (hold 10+ s) frees it.
2. **Bridge address** — `bridge_ip`/`bridge_name` in `data/config.json`. The bridge is
   queried by unicast mDNS on that IP, which also works from Docker.
3. **Rooms and schedules are local** — `data/tado.db` (SQLite): rooms, which device is in
   which room, and a weekly schedule per room. A background job *enforces*
   the current point over HomeKit every 30 s (tado's own schedule still runs on the bridge
   and would drift the targets); a change made in the app holds until the room's next
   point ("Resume schedule" ends it early). Turning a valve by hand gets reverted.
   If the bridge stops answering, the app searches the /24 for it by its HomeKit name
   (which contains the serial) and moves the pairing to the new IP.
4. **Local-only mode** (`"local_only": true` in `data/config.json`) — the app never
   contacts tado.com. Without it, an optional tado account adds Home/Away, a device
   health check and a one-time import of the room list.

## Run

Runs in Docker: Docker Desktop has macOS Local Network access, a host Python started
by launchd does not (Local Network privacy → "No route to host").

```bash
docker compose up -d --build   # http://127.0.0.1:8787 (loopback only)
docker compose logs -f
```

`./run.sh` still works for development from a terminal that has Local Network access.

Remote access: put an authenticating reverse proxy in front. The dashboard uses relative
URLs, so it works under any path prefix, e.g. with Caddy running in Docker:

```caddyfile
redir /tad /tad/ permanent
handle_path /tad/* {
	basic_auth {
		someuser <hash from `caddy hash-password`>
	}
	reverse_proxy host.docker.internal:8787
}
```

"Sign out" in the header (shown when not opened on localhost) clears the browser's cached
basic-auth login. The container has `restart: unless-stopped`, so it comes back whenever
Docker Desktop starts (at login).

## Features

- **Dashboard** (home page, built for phones): house and room figures, temperature, humidity,
  heating time, a weekday × hour heating map and a room comparison, over 24 h to 1 year
- **Away mode** — a switch on the dashboard: every room holds one low temperature (or off),
  until a return date or until switched off
- **Night off** — heating off in a daily window per room, 22:00–08:00 by default
- **Settings** (menu): room controls, rename/add/delete rooms, move devices, each room's
  weekly schedule and night off, system status and pairing
- Set temperature, boost (max temperature), heating off — held until the next change
- Home/Away presence toggle for the tado account (cloud; hidden in local-only mode)
- Per-device **Blink** (HomeKit identify) to find which valve is which; **Check devices**
  asks tado (one cloud call) whether each device is online and its battery state
- Live HomeKit events plus a 60 s safety poll

## What decides a room's temperature

Strongest first: **manual change > away mode > night off > weekly schedule**.

A manual change lasts until the room's next change. During away mode or night off it lasts at
most an hour, so a forgotten boost can't heat all night. Rooms with no schedule are only touched
by night off and away mode, and nothing turns them back on afterwards. Unassigned devices follow
22:00–08:00 and away mode, and a manual change to one at night is reverted.

All of this runs in this app: if the container or the Mac is off, nothing enforces it and the
schedule stored on the bridge takes over.

## History

`data/history.db` (SQLite, separate from `tado.db`): every device every minute for a year
(about 200 MB), plus per-room hourly figures kept forever (a few MB a year). Nothing is recorded
while the bridge is unreachable, so outages show as gaps. HomeKit reports whether a device is
calling for heat, not how hard, so "heating time" is time spent calling for heat, not energy.

To back it up, stop the container first or use `sqlite3 data/history.db ".backup copy.db"` from
inside it. Retention is `RAW_DAYS` in `app/history.py`.

Tests: `.venv/bin/python -m unittest tests.test_rules`

## API (for scripts/automation)

```
GET    /api/status
GET    /api/zones
POST   /api/zones/{id}/set       {"celsius": 21.5, "termination": "next"|"timer"|"manual", "duration_minutes": 60}
POST   /api/zones/{id}/boost
POST   /api/zones/{id}/identify     (blink)
GET    /api/rooms                   (rooms, devices, schedule, next change)
POST   /api/rooms {"name"} · PATCH /api/rooms/{id} {"name"} · DELETE /api/rooms/{id}
PUT    /api/devices/{serial}        {"room_id": 3 | null}
PUT    /api/rooms/{id}/schedule     [{"days": "12345", "time": "06:30", "celsius": 21}, {"days": "1234567", "time": "23:00", "celsius": null}]
PUT    /api/rooms/{id}/quiet        {"start": "22:00", "end": "08:00"} (both null = no night off)
POST   /api/house/away              {"active": true, "until": "2026-11-08T18:00" | null, "celsius": 12 | null}
GET    /api/history?range=24h       (24h, 7d, 30d, 90d, 1y)
GET    /api/devices/health          (online/battery per device, one cloud call)
POST   /api/zones/{id}/off
DELETE /api/zones/{id}/overlay   (resume schedule)
POST   /api/home/presence        {"mode": "HOME"|"AWAY"}
POST   /api/bridge/rescan
```

## Notes & limitations

- Works with the classic Internet Bridge. **tado X** (Matter/Thread) uses a
  completely different stack and is not supported by this app.
- If the bridge rejects the cloud token (some firmwares), the app
  automatically upgrades to a dedicated local token via
  `GET https://my.tado.com/api/v2/homes/{id}/token`.
- The bridge mDNS hostname looks like `tadoBridge<serial>.local`; reserve its
  IP in your router if you want discovery to stay stable.
- Reverse-engineered/unofficial: tado can change the API at any time.

## Costs and season forecast

The **Costs** tab estimates heating spend for the season (15 October to 15 April, Italian climate zone E).
Nothing measures gas directly: valve-hours x gas per valve-hour x price. Gas per valve-hour is assumed
(`kW per valve / (10.7 kWh per Smc x efficiency)`) until two gas meter readings at least a week apart,
with at least 20 valve-hours between them, replace it. The forecast scales valve-hours per degree day (Eurostat rule) by the
cold left in the season: a 16-day forecast, then the average of past winters. Weather comes from Open-Meteo;
only the coordinates leave the house. Set them in `data/config.json` (not in git): `"location": [latitude, longitude]`.

API: `GET /api/costs`, `PUT /api/costs/settings`, `POST /api/costs/meter`, `DELETE /api/costs/meter/{ts}`.

## On-demand rooms and alerts

A room can be **on demand**: it sits on its schedule, and a dashboard switch warms it for a set time
(`PUT /api/rooms/{id}/on-demand`, `POST /api/rooms/{id}/boost`). Night off still ends it.

A valve that rejects commands or a bridge that disappears shows on the dashboard at once. With **phone
alerts** on (Settings) a notification goes out after 5 minutes through ntfy.sh, and again when it's fixed.
Only the room name and the problem are sent, to a random private topic.

## Fresh air and issues

The home screen rates each waking hour for opening windows, from outdoor temperature and humidity, rain and
European air quality index (`GET /api/air`, from Open-Meteo). Issues appear on the same screen: a valve rejecting
commands, a room calling for heat for 2 hours without warming, and a suspected open window. A window
is only inferred from a fast fall in temperature, ignoring the 90 minutes after heating stops.

## House dial

`PUT /api/house/dial` shifts every schedule and on-demand temperature by up to 3 °C either way, in half degrees,
without changing them. Night off and away mode aren't affected, and a manual temperature stays as set.
