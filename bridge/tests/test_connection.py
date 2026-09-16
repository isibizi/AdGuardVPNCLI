"""The connect sequence shared by the panel and the watchdog.

It exists because the panel used to only record the wish and leave the work to
the watchdog's next cycle, so a click appeared to do nothing for up to a minute
and people clicked again.
"""

from __future__ import annotations

from contextlib import contextmanager

from app import connection
from app.runner import Result


def _wire(monkeypatch, *, connect_ok=True, socks=True, exit_ip="203.0.113.9"):
    """Replace everything that touches the system, recording the order of calls."""
    order: list[str] = []

    monkeypatch.setattr(connection, "gate", lambda action: order.append(f"gate:{action}") or "")
    monkeypatch.setattr(connection.adguard, "disconnect",
                        lambda: order.append("disconnect") or Result(0, "", ""))
    monkeypatch.setattr(connection.adguard, "connect",
                        lambda location="": order.append(f"connect:{location}") or
                        Result(0 if connect_ok else 1, "" if connect_ok else "boom", ""))
    monkeypatch.setattr(connection, "wait_for_socks",
                        lambda timeout=45: order.append("wait_socks") or socks)
    monkeypatch.setattr(connection, "restart_tun2socks",
                        lambda: order.append("restart_tun2socks"))
    monkeypatch.setattr(connection, "probe",
                        lambda: order.append("probe") or (exit_ip, "" if exit_ip else "adguard"))
    monkeypatch.setattr(connection.adguard, "status",
                        lambda: connection.adguard.Status(True, "PRAGUE", "", False))
    monkeypatch.setattr(connection.time, "sleep", lambda s: None)
    monkeypatch.setattr(connection.state, "write", lambda **kw: kw)
    return order


def test_successful_connect_runs_the_steps_in_order(monkeypatch):
    order = _wire(monkeypatch)
    ok, message, exit_ip = connection.establish("Prague")

    assert ok is True
    assert exit_ip == "203.0.113.9"
    assert message == "PRAGUE"
    # The gate closes first and only reopens once traffic has been proven, and
    # tun2socks is restarted because the SOCKS listener is recreated each time.
    assert order == [
        "gate:close", "disconnect", "connect:Prague",
        "wait_socks", "restart_tun2socks", "probe", "gate:open",
    ]


def test_a_failed_connect_leaves_the_gate_shut(monkeypatch):
    order = _wire(monkeypatch, connect_ok=False)
    ok, message, exit_ip = connection.establish("Prague")

    assert ok is False
    assert "boom" in message
    assert exit_ip == ""
    assert "gate:open" not in order


def test_no_traffic_is_not_reported_as_success(monkeypatch):
    """The client claiming to be connected is not enough to open the gate."""
    order = _wire(monkeypatch, exit_ip="")
    ok, _, _ = connection.establish("Prague")

    assert ok is False
    assert "gate:open" not in order


def test_a_second_click_does_not_start_a_competing_attempt(monkeypatch):
    @contextmanager
    def busy(blocking=True):
        yield False

    order = _wire(monkeypatch)
    monkeypatch.setattr(connection, "vpn_lock", busy)

    ok, message, _ = connection.establish("Prague")
    assert ok is False
    assert "bereits" in message
    assert order == []   # nothing was touched
