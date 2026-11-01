#!/bin/bash
# tado local — start the app
cd "$(dirname "$0")"
exec .venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port "${PORT:-8787}"
