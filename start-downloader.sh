#!/usr/bin/env bash
set -e
(cd /opt/bgutil/server && exec node --max-old-space-size=96 build/main.js --port 4416) >/tmp/bgutil.log 2>&1 &
exec python downloader_service.py
