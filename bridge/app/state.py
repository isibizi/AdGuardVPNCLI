"""Shared runtime state between the watchdog and the panel.

Written by the watchdog, read by the web process. A small JSON file is enough
and keeps the two processes decoupled - if the watchdog dies, the panel still
renders and simply reports stale data.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any

from app.config import SETTINGS

DEFAULT: dict[str, Any] = {
    "vpn_connected": False,
    "vpn_location": "",
    "vpn_protocol": "",
    "login_required": False,
    "exit_ip": "",
    "vps_ip": "",
    "gate": "closed",
    "connected_since": 0,
    "reconnects": 0,
    "last_check": 0,
    "last_error": "",
    "wg_backend": "",
    "busy": "",
}


def read() -> dict[str, Any]:
    state = dict(DEFAULT)
    try:
        with SETTINGS.state_file.open(encoding="utf-8") as handle:
            state.update(json.load(handle))
    except (OSError, ValueError):
        pass
    return state


def write(**changes: Any) -> dict[str, Any]:
    """Merge changes into the state file atomically."""
    state = read()
    state.update(changes)
    state["last_check"] = int(time.time())
    tmp = SETTINGS.state_file.with_suffix(".json.tmp")
    try:
        with tmp.open("w", encoding="utf-8") as handle:
            json.dump(state, handle)
        os.replace(tmp, SETTINGS.state_file)
    except OSError:  # pragma: no cover
        pass
    return state
