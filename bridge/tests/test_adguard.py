"""Parsers for the AdGuard CLI output.

The CLI is closed source and its wording shifts between releases, so these tests
pin the tolerant behaviour: recognise the shape, never depend on an exact
sentence, and never mistake "disconnected" for "connected".
"""

from __future__ import annotations

import pytest

from app import adguard


@pytest.mark.parametrize("text", [
    "VPN is disconnected",
    "AdGuard VPN is not connected",
    "The VPN service is not running",
])
def test_disconnected_is_not_read_as_connected(text, monkeypatch):
    monkeypatch.setattr(adguard, "_cli", lambda *a, **k: _result(text))
    assert adguard.status().connected is False


def test_connected_status_and_location(monkeypatch):
    monkeypatch.setattr(adguard, "_cli", lambda *a, **k: _result("VPN is connected to Frankfurt, Germany."))
    status = adguard.status()
    assert status.connected is True
    assert status.location == "Frankfurt"


def test_expired_session_is_detected(monkeypatch):
    monkeypatch.setattr(adguard, "_cli", lambda *a, **k: _result("Error: you are not logged in"))
    status = adguard.status()
    assert status.auth_required is True
    assert status.connected is False


def test_parse_locations():
    table = (
        "ISO   COUNTRY          CITY            PING\n"
        "DE    Germany          Frankfurt       12\n"
        "CH    Switzerland      Zurich          21\n"
        "US    United States    New York        94\n"
    )
    locations = adguard.parse_locations(table)
    assert [location.iso for location in locations] == ["DE", "CH", "US"]
    assert locations[0].value == "Frankfurt"
    assert "12 ms" in locations[0].label


def test_parse_locations_ignores_decoration_and_malformed_rows():
    table = (
        "ISO  COUNTRY  CITY  PING\n"
        "-----------------------\n"
        "\n"
        "garbage\n"
        "DE    Germany    Berlin    30\n"
    )
    locations = adguard.parse_locations(table)
    assert len(locations) == 1
    assert locations[0].city == "Berlin"


def test_connect_uses_fastest_when_no_location_selected(monkeypatch):
    seen = {}

    def fake(*args, **kwargs):
        seen["args"] = args
        return _result("ok")

    monkeypatch.setattr(adguard, "_cli", fake)
    adguard.connect("")
    assert "-f" in seen["args"]

    adguard.connect("Frankfurt")
    assert "-l" in seen["args"] and "Frankfurt" in seen["args"]


def _result(text: str):
    from app.runner import Result

    return Result(0, text, "")
