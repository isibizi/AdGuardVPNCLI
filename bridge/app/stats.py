"""Bandwidth accounting for the monitoring page.

Samples come from `wg show wg0 dump`, which reports cumulative byte counters per
peer. Deltas are stored at a fine resolution and rolled up as they age, so the
database stays small without losing the long-term picture:

    raw  (5 s)   kept for 1 hour
    min  (60 s)  kept for 24 hours
    hour (1 h)   kept for STATS_RETENTION_DAYS

Direction follows WireGuard's point of view: `rx` is what the bridge received
from the client (the client's upload), `tx` what it sent to the client (the
client's download).
"""

from __future__ import annotations

import ipaddress
import subprocess
import threading
import time

from app import db, wireguard
from app.config import SETTINGS

RAW, MINUTE, HOUR = "raw", "min", "hour"
RAW_INTERVAL = 5
RAW_MAX_AGE = 3600
MINUTE_MAX_AGE = 24 * 3600

TOTAL = 0  # peer_id used for "the bridge as a whole"

ONLINE_AFTER_HANDSHAKE = 180  # seconds; WireGuard has no session concept

_maintenance_lock = threading.Lock()
_last_maintenance = 0.0


def _delta(current: int, previous: int) -> int:
    """Counter delta that survives an interface being recreated."""
    if current < previous:
        return current
    return current - previous


def sample_once() -> None:
    """Take one sample of all peer counters and store the deltas."""
    live = wireguard.dump()
    if not live:
        return

    now = int(time.time())
    bucket = now - (now % RAW_INTERVAL)
    conn = db.connect()

    total_rx = total_tx = 0
    for peer in wireguard.list_peers():
        entry = live.get(peer.public_key)
        if entry is None:
            continue

        row = conn.execute(
            "SELECT rx, tx, last_rx, last_tx FROM peer_totals WHERE peer_id = ?",
            (peer.id,),
        ).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO peer_totals (peer_id, rx, tx, last_rx, last_tx, last_handshake, endpoint)"
                " VALUES (?, 0, 0, ?, ?, ?, ?)",
                (peer.id, entry["rx"], entry["tx"], entry["last_handshake"], entry["endpoint"]),
            )
            continue

        d_rx = _delta(entry["rx"], row["last_rx"])
        d_tx = _delta(entry["tx"], row["last_tx"])
        conn.execute(
            "UPDATE peer_totals SET rx = rx + ?, tx = tx + ?, last_rx = ?, last_tx = ?,"
            " last_handshake = ?, endpoint = ? WHERE peer_id = ?",
            (d_rx, d_tx, entry["rx"], entry["tx"], entry["last_handshake"],
             entry["endpoint"], peer.id),
        )

        if d_rx or d_tx:
            _add_sample(conn, bucket, peer.id, d_rx, d_tx)
        total_rx += d_rx
        total_tx += d_tx

    if total_rx or total_tx:
        _add_sample(conn, bucket, TOTAL, total_rx, total_tx)

    maintain()


def _add_sample(conn, ts: int, peer_id: int, rx: int, tx: int, resolution: str = RAW) -> None:
    conn.execute(
        "INSERT INTO samples (ts, resolution, peer_id, rx, tx) VALUES (?, ?, ?, ?, ?)"
        " ON CONFLICT(resolution, ts, peer_id) DO UPDATE SET rx = rx + excluded.rx,"
        " tx = tx + excluded.tx",
        (ts, resolution, peer_id, rx, tx),
    )


def maintain(force: bool = False) -> None:
    """Roll old samples up and drop what is past the retention window."""
    global _last_maintenance
    now = time.time()
    if not force and now - _last_maintenance < 60:
        return
    if not _maintenance_lock.acquire(blocking=False):
        return
    try:
        _last_maintenance = now
        conn = db.connect()
        _rollup(conn, RAW, MINUTE, 60, int(now) - RAW_MAX_AGE)
        _rollup(conn, MINUTE, HOUR, 3600, int(now) - MINUTE_MAX_AGE)
        cutoff = int(now) - SETTINGS.stats_retention_days * 86400
        conn.execute("DELETE FROM samples WHERE resolution = ? AND ts < ?", (HOUR, cutoff))
    finally:
        _maintenance_lock.release()


def _rollup(conn, source: str, target: str, bucket_seconds: int, before: int) -> None:
    rows = conn.execute(
        "SELECT (ts / ?) * ? AS bucket, peer_id, SUM(rx) AS rx, SUM(tx) AS tx"
        " FROM samples WHERE resolution = ? AND ts < ? GROUP BY bucket, peer_id",
        (bucket_seconds, bucket_seconds, source, before),
    ).fetchall()
    for row in rows:
        _add_sample(conn, row["bucket"], row["peer_id"], row["rx"], row["tx"], target)
    conn.execute("DELETE FROM samples WHERE resolution = ? AND ts < ?", (source, before))


def series(peer_id: int = TOTAL, window: str = "10m") -> list[dict]:
    """Return [{ts, rx, tx}] for a chart, picking a sensible resolution."""
    now = int(time.time())
    windows = {
        "10m": (now - 600, RAW, RAW_INTERVAL),
        "1h": (now - 3600, RAW, 60),
        "24h": (now - 86400, MINUTE, 600),
        "7d": (now - 7 * 86400, HOUR, 3600),
        "30d": (now - 30 * 86400, HOUR, 21600),
    }
    since, resolution, bucket = windows.get(window, windows["10m"])

    rows = db.connect().execute(
        "SELECT (ts / ?) * ? AS bucket, SUM(rx) AS rx, SUM(tx) AS tx FROM samples"
        " WHERE peer_id = ? AND ts >= ? AND resolution IN (?, ?, ?)"
        " GROUP BY bucket ORDER BY bucket",
        (bucket, bucket, peer_id, since, RAW, MINUTE, HOUR),
    ).fetchall()

    # Fill gaps with zeroes so the chart shows idle periods instead of
    # interpolating a straight line across them.
    by_bucket = {row["bucket"]: row for row in rows}
    result = []
    start = since - (since % bucket)
    for ts in range(start, now + bucket, bucket):
        row = by_bucket.get(ts)
        result.append({
            "ts": ts,
            "rx": int(row["rx"]) if row else 0,
            "tx": int(row["tx"]) if row else 0,
            "seconds": bucket,
        })
    return result


def volume(peer_id: int, since: int) -> tuple[int, int]:
    row = db.connect().execute(
        "SELECT COALESCE(SUM(rx), 0) AS rx, COALESCE(SUM(tx), 0) AS tx"
        " FROM samples WHERE peer_id = ? AND ts >= ?",
        (peer_id, since),
    ).fetchone()
    return int(row["rx"]), int(row["tx"])


def current_rate(peer_id: int) -> tuple[float, float]:
    """Bytes per second over the last three raw buckets."""
    since = int(time.time()) - 3 * RAW_INTERVAL
    row = db.connect().execute(
        "SELECT COALESCE(SUM(rx), 0) AS rx, COALESCE(SUM(tx), 0) AS tx FROM samples"
        " WHERE peer_id = ? AND resolution = ? AND ts >= ?",
        (peer_id, RAW, since),
    ).fetchone()
    span = 3 * RAW_INTERVAL
    return int(row["rx"]) / span, int(row["tx"]) / span


def peer_overview() -> list[dict]:
    """Everything the 'connected clients' table needs, per peer."""
    now = int(time.time())
    midnight = now - (now % 86400)
    month_start = now - 30 * 86400
    live = wireguard.dump()
    conn = db.connect()

    overview = []
    for peer in wireguard.list_peers():
        entry = live.get(peer.public_key, {})
        row = conn.execute(
            "SELECT rx, tx, last_handshake, endpoint FROM peer_totals WHERE peer_id = ?",
            (peer.id,),
        ).fetchone()

        handshake = int(entry.get("last_handshake") or (row["last_handshake"] if row else 0))
        rate_rx, rate_tx = current_rate(peer.id)
        today_rx, today_tx = volume(peer.id, midnight)
        month_rx, month_tx = volume(peer.id, month_start)

        overview.append({
            "id": peer.id,
            "name": peer.name,
            "address": peer.address,
            "enabled": peer.enabled,
            "networks": peer.networks,
            "endpoint": entry.get("endpoint") or (row["endpoint"] if row else ""),
            "last_handshake": handshake,
            "handshake_age": (now - handshake) if handshake else None,
            "online": bool(handshake) and (now - handshake) < ONLINE_AFTER_HANDSHAKE,
            "rate_up": rate_rx,
            "rate_down": rate_tx,
            "today": today_rx + today_tx,
            "month": month_rx + month_tx,
            "total": (row["rx"] + row["tx"]) if row else 0,
        })
    return overview


def active_sources() -> list[dict]:
    """Devices currently sending traffic, grouped by source address.

    Only meaningful when the router behind a peer forwards without NAT. With NAT
    (the UniFi default) every packet arrives as the peer's own tunnel address,
    so this correctly shows a single entry.
    """
    try:
        result = subprocess.run(
            ["conntrack", "-L", "-f", "ipv4"],
            capture_output=True, text=True, timeout=10, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if result.returncode != 0:
        return []

    counts: dict[str, int] = {}
    for line in result.stdout.splitlines():
        for field in line.split():
            if field.startswith("src="):
                source = field[4:]
                try:
                    if SETTINGS.wg_subnet.version == 4 and _in_scope(source):
                        counts[source] = counts.get(source, 0) + 1
                except ValueError:
                    pass
                break
    return [
        {"address": address, "connections": count}
        for address, count in sorted(counts.items(), key=lambda item: -item[1])
    ]


def _in_scope(address: str) -> bool:
    """True for addresses that belong to the tunnel or to a network behind a peer."""
    ip = ipaddress.ip_address(address)
    if ip in SETTINGS.wg_subnet:
        return True
    for peer in wireguard.list_peers():
        for network in peer.networks:
            if ip in ipaddress.ip_network(network):
                return True
    return False


class Sampler(threading.Thread):
    """Background sampler, started by the panel process."""

    daemon = True

    def run(self) -> None:
        while True:
            try:
                sample_once()
            except Exception:
                pass
            time.sleep(RAW_INTERVAL)
