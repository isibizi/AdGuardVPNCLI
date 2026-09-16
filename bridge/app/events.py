"""The event log shown on the monitoring page.

Events go to two places: the SQLite table (queryable, filterable) and a plain
text file on the volume (readable with `docker compose exec bridge cat
/data/events.log` when the panel itself is the thing that is broken).
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

from app import db
from app.config import SETTINGS

# Kinds are stable identifiers used by the UI filter.
VPN = "vpn"
PEER = "peer"
PANEL = "panel"
SYSTEM = "system"

INFO = "info"
WARNING = "warning"
ERROR = "error"

_MAX_EVENTS = 20000


def record(kind: str, message: str, level: str = INFO) -> None:
    ts = int(time.time())
    try:
        conn = db.connect()
        conn.execute(
            "INSERT INTO events (ts, kind, level, message) VALUES (?, ?, ?, ?)",
            (ts, kind, level, message),
        )
        # Keep the table bounded; the text file is rotated by size below.
        conn.execute(
            "DELETE FROM events WHERE id < (SELECT MAX(id) - ? FROM events)",
            (_MAX_EVENTS,),
        )
    except Exception:  # pragma: no cover - logging must never break the caller
        pass

    stamp = datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    line = f"{stamp} [{level}] {kind}: {message}\n"
    try:
        path = SETTINGS.events_log
        if path.exists() and path.stat().st_size > 5_000_000:
            path.replace(path.with_suffix(".log.1"))
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line)
    except OSError:  # pragma: no cover
        pass
    print(line, end="", flush=True)


def query(kind: str = "", search: str = "", limit: int = 300) -> list[dict]:
    sql = "SELECT ts, kind, level, message FROM events WHERE 1 = 1"
    params: list[object] = []
    if kind:
        sql += " AND kind = ?"
        params.append(kind)
    if search:
        sql += " AND message LIKE ?"
        params.append(f"%{search}%")
    sql += " ORDER BY ts DESC, id DESC LIMIT ?"
    params.append(limit)
    return [dict(row) for row in db.connect().execute(sql, params)]
