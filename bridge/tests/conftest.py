"""Test fixtures.

The application reads its configuration once at import time, so the environment
has to be set before anything under `app` is imported.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_TMP = tempfile.mkdtemp(prefix="bridge-test-")
os.environ.setdefault("BRIDGE_DATA_DIR", _TMP)
os.environ.setdefault("WG_SUBNET", "10.8.0.0/24")
os.environ.setdefault("WG_ENDPOINT", "vpn.example.org")
os.environ.setdefault("WG_PORT", "51820")
os.environ.setdefault("WG_MTU", "1380")
os.environ.setdefault("PEER_DNS", "94.140.14.14,94.140.15.15")

import pytest  # noqa: E402

from app import db  # noqa: E402


@pytest.fixture(autouse=True)
def clean_database():
    conn = db.connect()
    conn.executescript(
        "DELETE FROM peers; DELETE FROM settings; DELETE FROM events;"
        " DELETE FROM samples; DELETE FROM peer_totals;"
    )
    yield
