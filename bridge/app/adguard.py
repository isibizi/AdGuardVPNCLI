"""Thin wrapper around the official `adguardvpn-cli` binary.

The CLI is closed source and its wording changes between releases, so every
parser here is deliberately tolerant: it looks for keywords rather than exact
sentences, and the raw output is always kept so the panel can show it verbatim
when parsing fails. Guessing wrong must never leave the user without
information.
"""

from __future__ import annotations

import json
import re
import shutil
import threading
import time
from dataclasses import dataclass

from app import runner
from app.config import SETTINGS
from app.runner import vpn_lock

BINARY = shutil.which("adguardvpn-cli") or "/opt/adguardvpn_cli/adguardvpn-cli"
SOCKS_PROXY = "socks5h://127.0.0.1:1080"

# Phrases that mean "the session is gone, reconnecting cannot help".
_AUTH_MARKERS = (
    "not logged in",
    "no active session",
    "unauthorized",
    "authorization failed",
    "authentication failed",
    "please log in",
    "log in first",
    "invalid token",
    "token expired",
    "session expired",
)

_URL_RE = re.compile(r"https?://[^\s'\"<>]+")
_CODE_RE = re.compile(r"user_code=([A-Za-z0-9\-]+)", re.IGNORECASE)
_STANDALONE_CODE_RE = re.compile(r"\b([A-Z0-9]{4}[- ][A-Z0-9]{4})\b")


@dataclass
class Status:
    connected: bool
    location: str
    raw: str
    auth_required: bool = False


def _cli(*args: str, timeout: int = 90) -> runner.Result:
    with vpn_lock():
        return runner.run([BINARY, *args], timeout=timeout)


def looks_like_auth_problem(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in _AUTH_MARKERS)


def status() -> Status:
    result = _cli("status", timeout=30)
    text = result.output
    lowered = text.lower()

    # "disconnected" contains "connected", so the negative forms are checked first.
    disconnected = any(word in lowered for word in ("disconnected", "not connected", "is not running"))
    connected = (not disconnected) and "connected" in lowered

    location = ""
    match = re.search(r"connected to ([^\n.,]+)", text, re.IGNORECASE)
    if match:
        location = match.group(1).strip()

    return Status(
        connected=connected,
        location=location,
        raw=text,
        auth_required=looks_like_auth_problem(text),
    )


def connect(location: str = "") -> runner.Result:
    """Connect, either to a named location or to the fastest one."""
    args = ["connect", "-y", "-4"]
    if location and location != "fastest":
        args += ["-l", location]
    else:
        args.append("-f")
    return _cli(*args, timeout=180)


def disconnect() -> runner.Result:
    return _cli("disconnect", timeout=60)


def logout() -> runner.Result:
    return _cli("logout", timeout=60)


def license_info() -> runner.Result:
    return _cli("license", timeout=30)


def config_show() -> runner.Result:
    return _cli("config", "show", timeout=30)


def set_protocol(protocol: str) -> runner.Result:
    return _cli("config", "set-protocol", protocol, timeout=30)


@dataclass
class Location:
    iso: str
    country: str
    city: str
    ping: str

    @property
    def value(self) -> str:
        """What to pass to `connect -l`. The city is the most specific option."""
        return self.city or self.country or self.iso

    @property
    def label(self) -> str:
        parts = [p for p in (self.city, self.country) if p]
        text = " – ".join(dict.fromkeys(parts))
        return f"{text} ({self.ping} ms)" if self.ping else text


def parse_locations(text: str) -> list[Location]:
    """Parse the `list-locations` table.

    Columns are separated by runs of whitespace. The header row and any
    decorative lines are skipped. Rows that do not look like data are ignored
    rather than raising - a changed column layout must not break the panel.
    """
    locations: list[Location] = []
    for line in text.splitlines():
        line = line.rstrip()
        if not line.strip() or set(line.strip()) <= set("-=+| "):
            continue
        columns = [c.strip() for c in re.split(r"\s{2,}|\t", line.strip()) if c.strip()]
        if len(columns) < 3:
            continue
        if columns[0].upper() in {"ISO", "CODE", "COUNTRY CODE"}:
            continue  # header

        iso, country, city = columns[0], columns[1], columns[2]
        ping = ""
        for candidate in reversed(columns[3:]):
            digits = re.sub(r"[^0-9]", "", candidate)
            if digits:
                ping = digits
                break
        if len(iso) > 4 or not iso.isalpha():
            continue
        locations.append(Location(iso=iso.upper(), country=country, city=city, ping=ping))
    return locations


LOCATIONS_CACHE = "locations_cache"
LOCATIONS_CACHED_AT = "locations_cached_at"
LOCATIONS_TTL = 12 * 3600


def list_locations(force: bool = False) -> tuple[list[Location], str, int]:
    """Return the locations, their raw output, and when they were fetched.

    The CLI measures the ping to every location before it answers, which takes
    long enough to be felt on every page load - and it holds the VPN lock while
    it does, blocking the watchdog. Locations barely change, so the list is
    cached and only refreshed on demand or once the cache is half a day old.
    The ping values are a sorting hint, not a live measurement.
    """
    from app import db  # local import keeps this module usable without the DB

    if not force:
        cached = db.get_setting(LOCATIONS_CACHE)
        fetched_at = int(db.get_setting(LOCATIONS_CACHED_AT) or 0)
        if cached and time.time() - fetched_at < LOCATIONS_TTL:
            locations = [Location(**entry) for entry in json.loads(cached)]
            if locations:
                return locations, "", fetched_at

    result = _cli("list-locations", timeout=120)
    locations = parse_locations(result.output)

    if locations:
        now = int(time.time())
        db.set_setting(LOCATIONS_CACHE, json.dumps([vars(loc) for loc in locations]))
        db.set_setting(LOCATIONS_CACHED_AT, str(now))
        return locations, result.output, now

    # Nothing parsed - hand the raw output back so the panel can show it instead
    # of silently presenting an empty dropdown.
    return [], result.output, 0


TUNNEL = "tunnel"
PROXY = "proxy"
DIRECT = "direct"


def probe_exit_ip(via: str = TUNNEL, timeout: int = 10) -> str:
    """Ask an echo service what the world sees, over a chosen path.

    TUNNEL sources the request from the bridge's own address inside the tunnel,
    so the packet takes exactly the route a peer's traffic takes: policy route
    to table 100, out via tun0, through tun2socks into the SOCKS proxy. This is
    the only probe that proves the whole chain.

    PROXY talks to the SOCKS proxy directly and therefore skips tun0 entirely.
    On its own it is misleading - it reports success while tun2socks is dead -
    but as a second step it separates "AdGuard is down" from "forwarding is
    broken", which are fixed in completely different ways.

    DIRECT bypasses the VPN and returns the VPS's own address, for the
    comparison shown on the dashboard.
    """
    args = ["curl", "--silent", "--show-error", "--max-time", str(timeout), "--ipv4"]
    if via == PROXY:
        args += ["--proxy", SOCKS_PROXY]
    else:
        args += ["--noproxy", "*"]
        if via == TUNNEL:
            args += ["--interface", str(SETTINGS.bridge_address)]
    args.append("https://api.ipify.org")

    result = runner.run(args, timeout=timeout + 5)
    candidate = result.stdout.strip()
    if result.ok and re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}", candidate):
        return candidate
    return ""


class LoginFlow:
    """Drives the interactive device-code login on a pseudo-terminal.

    AdGuard removed username/password login in 1.5.10. `adguardvpn-cli login`
    now prints a URL with a one-time code and waits for you to open it. The
    panel extracts that URL, shows it as a link, as text and as a QR code, and
    keeps reading the process output until the CLI confirms the login.
    """

    TIMEOUT = 300

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._lock_handle = None
        self._reset()

    def _reset(self) -> None:
        self.session: runner.PtySession | None = None
        self.url = ""
        self.code = ""
        self.done = False
        self.success = False
        self.error = ""

    def start(self) -> None:
        with self._lock:
            if self.session and not self.session.finished:
                return  # a flow is already running
            self._reset()
            # Held for the whole flow so the watchdog does not connect or
            # disconnect underneath a login in progress.
            self._lock_handle = runner.acquire_lock_handle()
            self.session = runner.PtySession(args=[BINARY, "login"])
            self.session.start()

    def _release(self) -> None:
        runner.release_lock_handle(self._lock_handle)
        self._lock_handle = None

    def _extract(self, text: str) -> None:
        if not self.url:
            for url in _URL_RE.findall(text):
                cleaned = url.rstrip(".,);]")
                if "device" in cleaned.lower() or "adguard" in cleaned.lower():
                    self.url = cleaned
                    break
        if self.url and not self.code:
            match = _CODE_RE.search(self.url) or _CODE_RE.search(text)
            if match:
                self.code = match.group(1)
        if not self.code:
            match = _STANDALONE_CODE_RE.search(text)
            if match:
                self.code = match.group(1)

    def poll(self) -> dict:
        """Read new output and report the current state of the flow."""
        session = self.session
        if session is None:
            return {"state": "idle", "url": "", "code": "", "log": "", "error": ""}

        session.read_available()
        buffer = session.buffer
        self._extract(buffer)

        lowered = buffer.lower()
        if "successfully logged in" in lowered or "you are logged in" in lowered:
            self.done, self.success = True, True
            self._release()
        elif session.finished:
            self.done = True
            self._release()
            self.success = "error" not in lowered and session.returncode == 0
            if not self.success and not self.error:
                self.error = buffer.strip()[-500:] or "Der Login wurde beendet, ohne dass eine Anmeldung bestätigt wurde."
        elif time.time() - session.started_at > self.TIMEOUT:
            self.done = True
            self.error = (
                "Zeitüberschreitung: Die Anmeldung wurde nicht innerhalb von 5 Minuten "
                "abgeschlossen. Starte sie einfach neu."
            )
            session.stop()
            self._release()

        return {
            "state": "done" if self.done else "waiting",
            "success": self.success,
            "url": self.url,
            "code": self.code,
            "log": buffer[-4000:],
            "error": self.error,
        }

    def speed_up(self) -> None:
        """Answer the CLI's 's' menu entry to check the login state right away."""
        if self.session and not self.session.finished:
            self.session.send("s\n")

    def cancel(self) -> None:
        if self.session:
            self.session.stop()
        self.done = True
        self._release()


LOGIN = LoginFlow()
