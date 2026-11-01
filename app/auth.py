"""tado OAuth2 device-flow authentication.

tado moved to the OAuth2 device authorization grant in March 2025
(username/password no longer works). Flow:
  1. POST /oauth2/device_authorize -> device_code + user_code + URL
  2. user opens URL, logs in, enters code
  3. poll POST /oauth2/token until access_token + refresh_token arrive
  4. refresh_token is persisted locally; access_token auto-refreshes
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path

import httpx

CLIENT_ID = "1bb50063-6b0c-4d11-bd99-387f4a91cc46"
DEVICE_AUTHORIZE_URL = "https://login.tado.com/oauth2/device_authorize"
TOKEN_URL = "https://login.tado.com/oauth2/token"
DEVICE_GRANT_TYPE = "urn:ietf:params:oauth:grant-type:device_code"
SCOPE = "offline_access"


class AuthError(Exception):
    pass


class TadoAuth:
    def __init__(self, storage_path: str | Path):
        self.storage = Path(storage_path)
        self.refresh_token: str | None = None
        self.access_token: str | None = None
        self.access_token_expires: float = 0.0
        self.pending_device: dict | None = None
        self._load()

    # -- persistence ------------------------------------------------------
    def _load(self) -> None:
        if not self.storage.exists():
            return
        try:
            data = json.loads(self.storage.read_text())
        except (json.JSONDecodeError, OSError):
            return
        self.refresh_token = data.get("refresh_token")
        self.access_token = data.get("access_token")
        self.access_token_expires = data.get("access_token_expires", 0.0)

    def _save(self) -> None:
        self.storage.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "refresh_token": self.refresh_token,
            "access_token": self.access_token,
            "access_token_expires": self.access_token_expires,
        }
        fd = os.open(self.storage, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump(payload, fh)

    # -- state ------------------------------------------------------------
    def is_authenticated(self) -> bool:
        return bool(self.refresh_token)

    def pending_info(self) -> dict | None:
        if not self.pending_device:
            return None
        if time.time() >= self.pending_device["expires_at"]:
            return None
        return {
            "user_code": self.pending_device["user_code"],
            "verification_uri_complete": self.pending_device["verification_uri_complete"],
            "expires_at": self.pending_device["expires_at"],
        }

    # -- device flow ------------------------------------------------------
    async def start_device_flow(self) -> dict:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(
                DEVICE_AUTHORIZE_URL,
                data={"client_id": CLIENT_ID, "scope": SCOPE},
            )
        if r.status_code >= 400:
            raise AuthError(f"device_authorize failed: HTTP {r.status_code}: {r.text[:200]}")
        data = r.json()
        self.pending_device = {
            "device_code": data["device_code"],
            "user_code": data["user_code"],
            "verification_uri_complete": data.get("verification_uri_complete") or data["verification_uri"],
            "expires_at": time.time() + int(data.get("expires_in", 300)),
            "interval": int(data.get("interval", 5)),
        }
        return self.pending_info()

    async def poll_device_flow(self) -> bool:
        """Poll once. Returns True when the flow completed."""
        if not self.pending_device or time.time() >= self.pending_device["expires_at"]:
            raise AuthError("no active device flow (start a new one)")
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(
                TOKEN_URL,
                data={
                    "client_id": CLIENT_ID,
                    "device_code": self.pending_device["device_code"],
                    "grant_type": DEVICE_GRANT_TYPE,
                },
            )
        if r.status_code == 200:
            self._store_tokens(r.json())
            self.pending_device = None
            return True
        error = (r.json() or {}).get("error") if r.headers.get("content-type", "").startswith("application/json") else None
        if error == "authorization_pending":
            return False
        if error == "slow_down":
            self.pending_device["interval"] += 5
            return False
        raise AuthError(f"device flow error: {error or r.status_code}")

    async def wait_for_activation(self, timeout: float = 300.0) -> None:
        """Block until the user completes the browser step or the flow expires."""
        deadline = min(time.time() + timeout, self.pending_device["expires_at"])
        while time.time() < deadline:
            if await self.poll_device_flow():
                return
            await asyncio.sleep(self.pending_device["interval"])
        raise AuthError("device flow timed out")

    # -- tokens -----------------------------------------------------------
    def _store_tokens(self, data: dict) -> None:
        self.access_token = data["access_token"]
        if data.get("refresh_token"):
            self.refresh_token = data["refresh_token"]
        self.access_token_expires = time.time() + int(data.get("expires_in", 600))
        self._save()

    async def ensure_access_token(self) -> str:
        if self.access_token and time.time() < self.access_token_expires - 60:
            return self.access_token
        if not self.refresh_token:
            raise AuthError("not authenticated")
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(
                TOKEN_URL,
                data={
                    "client_id": CLIENT_ID,
                    "grant_type": "refresh_token",
                    "refresh_token": self.refresh_token,
                },
            )
        if r.status_code in (400, 401):  # invalid_grant: the refresh token is dead, show "Connect" again
            self.logout()
        if r.status_code >= 400:
            raise AuthError(f"token refresh failed: HTTP {r.status_code} (re-login required)")
        self._store_tokens(r.json())
        return self.access_token

    def logout(self) -> None:
        self.refresh_token = None
        self.access_token = None
        self.access_token_expires = 0.0
        self.pending_device = None
        if self.storage.exists():
            self.storage.unlink()
