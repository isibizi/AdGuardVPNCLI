"""Bringing the AdGuard leg up, in one place.

The panel and the watchdog are separate processes, so the panel cannot call the
watchdog's code. Before this module existed it did not try: pressing "connect"
only set a flag and left the actual work to the watchdog's next cycle, which is
fifteen seconds away at best and a minute away during backoff. From the user's
side nothing happened after the click, so they clicked again.

Both now call the same sequence directly. The cross-process lock keeps them from
running it at once: whoever gets there first does the work, the other is told so
instead of queueing up a second attempt behind it.
"""

from __future__ import annotations

import socket
import subprocess
import time

from app import adguard, events, state
from app.runner import vpn_lock

SUPERVISOR_CONF = "/etc/supervisor/supervisord.conf"


def socks_ready(timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", 1080), timeout=timeout):
            return True
    except OSError:
        return False


def wait_for_socks(timeout: int = 45) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if socks_ready():
            return True
        time.sleep(1)
    return False


def gate(action: str) -> str:
    """Open or close the only path forwarded traffic may take."""
    try:
        result = subprocess.run(
            ["/usr/local/bin/bridge-net.sh", "gate", action],
            capture_output=True, text=True, timeout=30, check=False,
        )
        return result.stdout.strip()
    except (OSError, subprocess.SubprocessError) as exc:
        events.record(events.SYSTEM, f"Kill-Switch konnte nicht geschaltet werden: {exc}", events.ERROR)
        return ""


def restart_tun2socks() -> None:
    """tun2socks holds a connection to the SOCKS listener, which is recreated on
    every reconnect, so it has to be restarted alongside."""
    subprocess.run(
        ["supervisorctl", "-c", SUPERVISOR_CONF, "restart", "tun2socks"],
        capture_output=True, text=True, timeout=60, check=False,
    )


def probe() -> tuple[str, str]:
    """Check the real data path and classify a failure.

    Returns (exit_ip, fault). fault is empty when traffic flows, "forwarding"
    when AdGuard is reachable but nothing gets through the tunnel, and "adguard"
    when neither works. The distinction matters because the two are fixed in
    opposite ways: reconnecting the VPN does nothing for a dead tun2socks, and
    restarting tun2socks does nothing for an expired AdGuard session.
    """
    exit_ip = adguard.probe_exit_ip(via=adguard.TUNNEL)
    if exit_ip:
        return exit_ip, ""
    if adguard.probe_exit_ip(via=adguard.PROXY, timeout=8):
        return "", "forwarding"
    return "", "adguard"


def establish(location: str) -> tuple[bool, str, str]:
    """Rebuild the AdGuard connection and open the gate once traffic flows.

    Returns (ok, message, exit_ip). The caller decides what a failure means -
    the watchdog counts it towards its backoff, the panel shows it.
    """
    with vpn_lock(blocking=False) as acquired:
        if not acquired:
            return False, "Es läuft bereits eine andere Aktion.", ""

        gate("close")
        state.write(gate="closed", vpn_connected=False, busy="Verbindungsaufbau")

        # Disconnect first even when it claims to be disconnected: it clears
        # half-dead states the client otherwise keeps reporting as connected.
        adguard.disconnect()
        result = adguard.connect(location)
        if not result.ok:
            message = result.output.strip()[-400:] or "unbekannter Fehler"
            state.write(last_error=message, busy="")
            return False, message, ""

        if not wait_for_socks():
            message = "Der SOCKS-Proxy kam nach dem Verbinden nicht hoch."
            state.write(last_error=message, busy="")
            return False, message, ""

        restart_tun2socks()
        time.sleep(3)

        exit_ip, _ = probe()
        if not exit_ip:
            message = "Verbindung steht laut Client, aber es geht kein Traffic durch."
            state.write(last_error=message, busy="")
            return False, message, ""

        gate("open")
        status = adguard.status()
        state.write(
            vpn_connected=True, gate="open", exit_ip=exit_ip, login_required=False,
            vpn_location=status.location or location, last_error="", busy="",
        )
        return True, status.location or location, exit_ip
