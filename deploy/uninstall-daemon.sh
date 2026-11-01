#!/bin/bash
# Remove the old com.tadolocal.server LaunchDaemon (run with sudo). macOS Local Network
# privacy blocked it from reaching the bridge; the app now runs in Docker (../compose.yml).
set -euo pipefail
launchctl bootout system/com.tadolocal.server 2>/dev/null || true
rm -f /Library/LaunchDaemons/com.tadolocal.server.plist
sleep 2
if lsof -nP -iTCP:8787 -sTCP:LISTEN >/dev/null; then echo "port 8787 still in use"; else echo "removed; port 8787 is free"; fi
