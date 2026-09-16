#!/bin/sh
#
# Keeps the AdGuard leg alive: detects drops, reconnects automatically, and
# controls the kill-switch gate. The logic lives in Python so it can share the
# event log and database with the panel; this wrapper keeps a stable name and
# lets you run the watchdog by hand for debugging.
set -eu
exec /opt/panel-venv/bin/python3 -m app.watchdog "$@"
