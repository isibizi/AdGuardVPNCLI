"""The probes that decide whether traffic really flows.

These exist because of a real outage: tun2socks was dead for its entire runtime,
every forwarded packet was dropped on tun0, and the watchdog reported the link
as healthy the whole time - because it probed the SOCKS proxy directly and so
never touched the path it was supposed to be proving.
"""

from __future__ import annotations

from app import adguard, watchdog
from app.runner import Result


def _capture(monkeypatch) -> list:
    seen: list = []

    def fake_run(args, timeout=60, env=None):
        seen.append(args)
        return Result(0, "203.0.113.9", "")

    monkeypatch.setattr(adguard.runner, "run", fake_run)
    return seen


def test_tunnel_probe_uses_the_forwarding_path(monkeypatch):
    seen = _capture(monkeypatch)
    adguard.probe_exit_ip(via=adguard.TUNNEL)
    args = seen[0]
    # Sourcing from the bridge address is what sends the packet through the
    # policy route into tun0. Without it the probe skips tun2socks entirely.
    assert "--interface" in args
    assert "10.8.0.1" in args
    assert "--proxy" not in args


def test_proxy_probe_talks_to_the_proxy_and_skips_the_tunnel(monkeypatch):
    seen = _capture(monkeypatch)
    adguard.probe_exit_ip(via=adguard.PROXY)
    args = seen[0]
    assert "--proxy" in args
    assert "--interface" not in args


def test_direct_probe_avoids_the_vpn_entirely(monkeypatch):
    seen = _capture(monkeypatch)
    adguard.probe_exit_ip(via=adguard.DIRECT)
    args = seen[0]
    assert "--noproxy" in args
    assert "--proxy" not in args
    assert "--interface" not in args


def test_non_ip_output_is_not_mistaken_for_success(monkeypatch):
    monkeypatch.setattr(adguard.runner, "run",
                        lambda *a, **k: Result(0, "<html>error page</html>", ""))
    assert adguard.probe_exit_ip(via=adguard.TUNNEL) == ""


def test_working_path_reports_no_fault(monkeypatch):
    monkeypatch.setattr(adguard, "probe_exit_ip",
                        lambda via=adguard.TUNNEL, timeout=10: "203.0.113.9")
    assert watchdog.probe() == ("203.0.113.9", "")


def test_dead_forwarding_is_told_apart_from_a_dead_vpn(monkeypatch):
    # AdGuard answers through the proxy, but nothing survives the tunnel.
    def only_proxy_works(via=adguard.TUNNEL, timeout=10):
        return "203.0.113.9" if via == adguard.PROXY else ""

    monkeypatch.setattr(adguard, "probe_exit_ip", only_proxy_works)
    assert watchdog.probe() == ("", "forwarding")


def test_both_paths_down_blames_adguard(monkeypatch):
    monkeypatch.setattr(adguard, "probe_exit_ip",
                        lambda via=adguard.TUNNEL, timeout=10: "")
    assert watchdog.probe() == ("", "adguard")


def test_status_live_asks_the_client_and_then_caches(monkeypatch):
    """The panel must not hammer the CLI on every page render."""
    adguard._status_cache.update(at=0.0, value=None)
    calls = []

    def fake_run(args, timeout=60, env=None):
        calls.append(args)
        return Result(0, "VPN is disconnected", "")

    monkeypatch.setattr(adguard.runner, "run", fake_run)
    first = adguard.status_live()
    second = adguard.status_live()

    assert first.connected is False
    assert first.auth_required is False
    assert second is first
    assert len(calls) == 1


def test_status_live_reports_a_logged_out_client(monkeypatch):
    adguard._status_cache.update(at=0.0, value=None)
    monkeypatch.setattr(adguard.runner, "run",
                        lambda *a, **k: Result(0, "You are not logged in", ""))
    assert adguard.status_live().auth_required is True


def test_status_live_never_waits_for_the_lock(monkeypatch):
    """A long connect must not freeze the panel - or the watchdog behind it."""
    from contextlib import contextmanager

    adguard._status_cache.update(at=0.0, value=None)

    @contextmanager
    def busy(blocking=True):
        yield False

    called = []
    monkeypatch.setattr(adguard, "vpn_lock", busy)
    monkeypatch.setattr(adguard.runner, "run",
                        lambda *a, **k: called.append(a) or Result(0, "", ""))

    assert adguard.status_live() is None   # nothing known yet, and nothing blocked
    assert called == []
