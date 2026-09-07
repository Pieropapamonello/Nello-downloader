#!/usr/bin/env bash
set -e
node --max-old-space-size=64 /opt/bgutil/server/build/main.js --port 4416 >/tmp/bgutil.log 2>&1 &
exec python downloader_service.py
