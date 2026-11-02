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
   which room, and a weekly schedule per room. A background job applies each change
   point over HomeKit; a manual change holds until the next point. Set tado's own
   "manual control" setting to *until ended manually* so tado's schedule doesn't fight it.
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

- Dashboard per zone: current temperature, target, humidity, heating activity
- Rooms grouped and named locally; "Edit rooms & schedules" to rename, add/delete rooms,
  move devices and edit each room's weekly schedule
- Set temperature, boost (max temperature), heating off — held until the next schedule change
- Home/Away presence toggle
- Per-device **Blink** (HomeKit identify) to find which valve is which; **Check devices**
  asks tado (one cloud call) whether each device is online and its battery state
- Live HomeKit events plus a 60 s safety poll; data stays in memory

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
