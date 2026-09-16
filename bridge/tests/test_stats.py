"""Bandwidth accounting."""

from __future__ import annotations

import time

from app import db, stats


def test_counter_reset_does_not_produce_a_negative_delta():
    # Interface recreated: the counter starts from zero again.
    assert stats._delta(50, 1000) == 50
    assert stats._delta(1500, 1000) == 500


def test_series_fills_gaps_with_zeroes():
    now = int(time.time())
    conn = db.connect()
    conn.execute(
        "INSERT INTO samples (ts, resolution, peer_id, rx, tx) VALUES (?, ?, 0, 100, 200)",
        (now - (now % 5), stats.RAW),
    )
    points = stats.series(peer_id=0, window="10m")
    assert len(points) > 100
    assert all("rx" in point and "tx" in point for point in points)
    assert sum(point["rx"] for point in points) == 100


def test_rollup_preserves_totals_and_frees_the_fine_grained_rows():
    conn = db.connect()
    old = int(time.time()) - 7200  # older than the raw retention window
    for offset in range(0, 120, 5):
        conn.execute(
            "INSERT INTO samples (ts, resolution, peer_id, rx, tx) VALUES (?, ?, 0, 10, 20)",
            (old + offset, stats.RAW),
        )
    before = conn.execute(
        "SELECT SUM(rx) AS rx FROM samples WHERE peer_id = 0"
    ).fetchone()["rx"]

    stats.maintain(force=True)

    after = conn.execute("SELECT SUM(rx) AS rx FROM samples WHERE peer_id = 0").fetchone()["rx"]
    remaining_raw = conn.execute(
        "SELECT COUNT(*) AS n FROM samples WHERE resolution = ? AND ts < ?",
        (stats.RAW, int(time.time()) - stats.RAW_MAX_AGE),
    ).fetchone()["n"]

    assert after == before      # no bytes lost in the rollup
    assert remaining_raw == 0   # and the fine-grained rows are gone
