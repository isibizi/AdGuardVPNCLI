"""Keeps the AdGuard leg alive and controls the kill-switch gate.

This is the component that makes the bridge unattended: if the connection
between the VPS and AdGuard drops, it is rebuilt automatically, and while it is
down no packet from the home network is forwarded anywhere.

The loop never gives up. The only state in which it deliberately slows down is
an expired session, because no amount of reconnecting fixes a login.
"""

from __future__ import annotations

import sys
import time

from app import adguard, connection, db, events, state
# Re-exported so the rest of the code (and the tests) keep one obvious home
# for these, while the implementation lives with the connect sequence.
from app.connection import gate, probe, restart_tun2socks, socks_ready, wait_for_socks  # noqa: F401
from app.config import SETTINGS
from app.runner import vpn_lock

CYCLE_SECONDS = 15
PROBE_INTERVAL = 60
PROBE_FAILURES_BEFORE_RECONNECT = 3
FALLBACK_AFTER_FAILURES = 5
LOGIN_RETRY_SECONDS = 300

# 5s, 10s, 20s, 40s, then steady at 60s. Never longer: a bridge that is down is
# worth checking every minute, forever.
BACKOFF = (5, 10, 20, 40, 60)

LOCATION_SETTING = "adguard_location"
PAUSED_SETTING = "vpn_paused"
SUPERVISOR_CONF = "/etc/supervisor/supervisord.conf"


def selected_location() -> str:
    return db.get_setting(LOCATION_SETTING) or SETTINGS.adguard_location


def is_paused() -> bool:
    """True when the user pressed 'disconnect' in the panel.

    Without this the watchdog would faithfully undo a manual disconnect within
    fifteen seconds.
    """
    return db.get_setting(PAUSED_SETTING) == "1"


class Watchdog:
    def __init__(self) -> None:
        self.failures = 0
        self.probe_failures = 0
        self.reconnects = 0
        self.last_probe = 0.0
        self.connected_since = 0
        self.fell_back = False

    # -- helpers ----------------------------------------------------------
    def publish(self, **changes) -> None:
        state.write(reconnects=self.reconnects, **changes)

    def backoff(self) -> int:
        index = min(self.failures, len(BACKOFF) - 1)
        return BACKOFF[index]

    # -- the reconnect itself ---------------------------------------------
    def reconnect(self, reason: str) -> bool:
        """Rebuild the AdGuard connection, counting failures towards the backoff."""
        events.record(events.VPN, f"Verbindung wird neu aufgebaut: {reason}", events.WARNING)

        location = selected_location()
        use_fallback = (
            SETTINGS.fallback_to_fastest
            and location != "fastest"
            and self.failures >= FALLBACK_AFTER_FAILURES
        )
        target = "fastest" if use_fallback else location

        ok, message, exit_ip = connection.establish(target)

        if not ok:
            self.failures += 1
            if adguard.looks_like_auth_problem(message):
                self.enter_login_required(message)
                return False
            events.record(
                events.VPN,
                f"Verbindungsaufbau fehlgeschlagen (Versuch {self.failures}): {message}",
                events.ERROR,
            )
            self.publish(busy="")
            return False

        if use_fallback and not self.fell_back:
            self.fell_back = True
            events.record(
                events.VPN,
                f"Standort '{location}' war nicht erreichbar, es wurde auf den schnellsten Standort ausgewichen.",
                events.WARNING,
            )

        self.failures = 0
        self.probe_failures = 0
        self.reconnects += 1
        self.connected_since = int(time.time())
        events.record(events.VPN, f"Verbindung steht (Standort: {message}, Exit-IP {exit_ip})")
        self.publish(connected_since=self.connected_since)
        return True

    def enter_login_required(self, detail: str) -> None:
        gate("close")
        self.publish(
            login_required=True, vpn_connected=False, gate="closed",
            last_error=detail[-400:], busy="",
        )
        events.record(
            events.VPN,
            "AdGuard verlangt eine neue Anmeldung. Öffne das Panel und melde dich neu an.",
            events.ERROR,
        )

    # -- one pass ----------------------------------------------------------
    def cycle(self) -> float:
        """Run one check. Returns how long to sleep afterwards."""
        with vpn_lock(blocking=False) as acquired:
            if not acquired:
                # The panel is mid-action (a login, a location change). Leave it be.
                return CYCLE_SECONDS

            if is_paused():
                if state.read().get("gate") != "closed":
                    gate("close")
                    self.publish(gate="closed", vpn_connected=False,
                                 connected_since=0, busy="")
                    self.connected_since = 0
                return CYCLE_SECONDS

            status = adguard.status()

            if status.auth_required:
                current = state.read()
                if not current.get("login_required"):
                    self.enter_login_required(status.raw)
                return LOGIN_RETRY_SECONDS

            if not status.connected:
                if self.connected_since:
                    events.record(events.VPN, "Verbindung zu AdGuard verloren.", events.WARNING)
                    self.connected_since = 0
                self.reconnect("Client meldet 'nicht verbunden'")
                return self.backoff()

            now = time.time()
            if now - self.last_probe < PROBE_INTERVAL:
                return CYCLE_SECONDS

            self.last_probe = now
            exit_ip, fault = probe()
            if exit_ip:
                self.probe_failures = 0
                if not self.connected_since:
                    self.connected_since = int(time.time())
                gate("open")
                self.publish(
                    vpn_connected=True, gate="open", exit_ip=exit_ip,
                    vpn_location=status.location, login_required=False,
                    connected_since=self.connected_since, last_error="", busy="",
                )
                return CYCLE_SECONDS

            self.probe_failures += 1

            if fault == "forwarding":
                # AdGuard answers, but the tunnel carries nothing. Rebuilding the
                # VPN connection would be the wrong repair; restart the part that
                # is actually broken and keep the gate shut meanwhile.
                events.record(
                    events.SYSTEM,
                    "AdGuard ist erreichbar, aber durch den Tunnel fließt nichts. "
                    "tun2socks wird neu gestartet.",
                    events.WARNING,
                )
                gate("close")
                self.publish(gate="closed", last_error="tun2socks leitet nicht weiter")
                restart_tun2socks()
                return CYCLE_SECONDS

            if self.probe_failures >= PROBE_FAILURES_BEFORE_RECONNECT:
                # The client claims to be connected but nothing gets through.
                # This zombie state is why status alone is not trusted.
                self.reconnect("kein Traffic trotz Status 'verbunden'")
                return self.backoff()

            events.record(
                events.VPN,
                f"Verbindungstest fehlgeschlagen ({self.probe_failures}/{PROBE_FAILURES_BEFORE_RECONNECT}).",
                events.WARNING,
            )
            return CYCLE_SECONDS

    def run(self) -> None:
        backend = ""
        backend_file = SETTINGS.wg_dir / "backend"
        if backend_file.exists():
            backend = backend_file.read_text().strip()

        vps_ip = adguard.probe_exit_ip(via=adguard.DIRECT)
        self.publish(wg_backend=backend, vps_ip=vps_ip, gate=gate("state") or "closed")
        events.record(events.SYSTEM, "Watchdog gestartet, Kill-Switch ist scharf.")

        if backend == "userspace":
            events.record(
                events.SYSTEM,
                "WireGuard läuft im Userspace-Modus (kein Kernelmodul auf diesem VPS). "
                "Funktioniert, ist aber langsamer als ein KVM-VPS mit Kernel-WireGuard.",
                events.WARNING,
            )

        while True:
            try:
                delay = self.cycle()
            except Exception as exc:  # keep the loop alive no matter what
                events.record(events.SYSTEM, f"Watchdog-Fehler: {exc}", events.ERROR)
                delay = CYCLE_SECONDS
            time.sleep(delay)


def main() -> int:
    Watchdog().run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
