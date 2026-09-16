"""WireGuard server state: peers, addressing and configuration rendering."""

from __future__ import annotations

import ipaddress
import re
import subprocess
import time
from dataclasses import dataclass

from app import crypto, db, events
from app.config import SETTINGS

_NAME_RE = re.compile(r"^[\w][\w .\-]{0,40}$", re.UNICODE)

SERVER_PRIVATE_KEY = "server_private_key"
SERVER_PUBLIC_KEY = "server_public_key"


class PeerError(ValueError):
    """Raised for input the user can correct, with a message meant to be shown."""


@dataclass
class Peer:
    id: int
    name: str
    private_key: str
    public_key: str
    preshared_key: str
    address: str
    behind_networks: str
    enabled: bool
    created_at: int

    @property
    def networks(self) -> list[str]:
        return [n for n in self.behind_networks.split(",") if n]

    @property
    def filename(self) -> str:
        safe = re.sub(r"[^A-Za-z0-9_-]+", "-", self.name).strip("-").lower()
        return f"{safe or 'peer'}.conf"


def server_keys() -> tuple[str, str]:
    """Return the server keypair, creating it on first use."""
    private = db.get_setting(SERVER_PRIVATE_KEY)
    if not private:
        private = crypto.generate_private_key()
        db.set_setting(SERVER_PRIVATE_KEY, private)
        db.set_setting(SERVER_PUBLIC_KEY, crypto.public_key(private))
        events.record(events.SYSTEM, "WireGuard-Serverschlüssel erzeugt")
    public = db.get_setting(SERVER_PUBLIC_KEY)
    if not public:
        public = crypto.public_key(private)
        db.set_setting(SERVER_PUBLIC_KEY, public)
    return private, public


def _row_to_peer(row) -> Peer:
    return Peer(
        id=row["id"],
        name=row["name"],
        private_key=row["private_key"],
        public_key=row["public_key"],
        preshared_key=row["preshared_key"],
        address=row["address"],
        behind_networks=row["behind_networks"],
        enabled=bool(row["enabled"]),
        created_at=row["created_at"],
    )


def list_peers() -> list[Peer]:
    rows = db.connect().execute("SELECT * FROM peers ORDER BY id").fetchall()
    return [_row_to_peer(row) for row in rows]


def get_peer(peer_id: int) -> Peer | None:
    row = db.connect().execute("SELECT * FROM peers WHERE id = ?", (peer_id,)).fetchone()
    return _row_to_peer(row) if row else None


def next_address() -> str:
    """Pick the lowest free host address, skipping the bridge's own .1."""
    taken = {row["address"] for row in db.connect().execute("SELECT address FROM peers")}
    bridge = str(SETTINGS.bridge_address)
    for host in SETTINGS.wg_subnet.hosts():
        candidate = str(host)
        if candidate == bridge or candidate in taken:
            continue
        return candidate
    raise PeerError(
        f"Im Netz {SETTINGS.wg_subnet} ist keine Adresse mehr frei. "
        "Lösche einen Peer oder vergrößere WG_SUBNET."
    )


def normalise_networks(raw: str) -> str:
    """Validate the optional 'network behind this peer' field."""
    if not raw or not raw.strip():
        return ""
    result: list[str] = []
    for chunk in re.split(r"[,\s]+", raw.strip()):
        if not chunk:
            continue
        try:
            net = ipaddress.ip_network(chunk, strict=False)
        except ValueError as exc:
            raise PeerError(f"'{chunk}' ist kein gültiges Netz (erwartet z. B. 192.168.1.0/24).") from exc
        if net.version != 4:
            raise PeerError("Nur IPv4-Netze werden unterstützt; IPv6 ist auf der Bridge abgeschaltet.")
        if net.overlaps(SETTINGS.wg_subnet):
            raise PeerError(
                f"{net} überschneidet sich mit dem Tunnelnetz {SETTINGS.wg_subnet}. "
                "Wähle im LAN ein anderes Netz oder passe WG_SUBNET an."
            )
        result.append(str(net))
    return ",".join(result)


def create_peer(name: str, behind_networks: str = "") -> Peer:
    name = (name or "").strip()
    if not _NAME_RE.match(name):
        raise PeerError("Der Name darf nur Buchstaben, Ziffern, Leerzeichen, Punkt, Minus und Unterstrich enthalten (max. 41 Zeichen).")

    networks = normalise_networks(behind_networks)
    private = crypto.generate_private_key()
    peer = Peer(
        id=0,
        name=name,
        private_key=private,
        public_key=crypto.public_key(private),
        preshared_key=crypto.generate_preshared_key(),
        address=next_address(),
        behind_networks=networks,
        enabled=True,
        created_at=int(time.time()),
    )

    conn = db.connect()
    try:
        cursor = conn.execute(
            "INSERT INTO peers (name, private_key, public_key, preshared_key, address,"
            " behind_networks, enabled, created_at) VALUES (?, ?, ?, ?, ?, ?, 1, ?)",
            (peer.name, peer.private_key, peer.public_key, peer.preshared_key,
             peer.address, peer.behind_networks, peer.created_at),
        )
    except Exception as exc:
        if "UNIQUE" in str(exc):
            raise PeerError(f"Ein Peer namens '{name}' existiert bereits.") from exc
        raise
    peer.id = int(cursor.lastrowid)
    events.record(events.PEER, f"Peer '{peer.name}' angelegt ({peer.address})")
    return peer


def update_peer(peer_id: int, *, name: str | None = None, behind_networks: str | None = None,
                enabled: bool | None = None) -> Peer:
    peer = get_peer(peer_id)
    if peer is None:
        raise PeerError("Dieser Peer existiert nicht (mehr).")

    if name is not None:
        name = name.strip()
        if not _NAME_RE.match(name):
            raise PeerError("Ungültiger Name.")
        peer.name = name
    if behind_networks is not None:
        peer.behind_networks = normalise_networks(behind_networks)
    if enabled is not None:
        peer.enabled = enabled

    db.connect().execute(
        "UPDATE peers SET name = ?, behind_networks = ?, enabled = ? WHERE id = ?",
        (peer.name, peer.behind_networks, int(peer.enabled), peer.id),
    )
    events.record(events.PEER, f"Peer '{peer.name}' geändert")
    return peer


def delete_peer(peer_id: int) -> None:
    peer = get_peer(peer_id)
    if peer is None:
        return
    conn = db.connect()
    conn.execute("DELETE FROM peers WHERE id = ?", (peer_id,))
    conn.execute("DELETE FROM peer_totals WHERE peer_id = ?", (peer_id,))
    conn.execute("DELETE FROM samples WHERE peer_id = ?", (peer_id,))
    events.record(events.PEER, f"Peer '{peer.name}' gelöscht")


def render_server_config() -> str:
    """Render wg0 in the format `wg setconf` expects.

    Address, MTU and DNS are intentionally absent: those are applied by
    bridge-wg.sh, because `wg setconf` rejects wg-quick's extra keys.
    """
    private, _ = server_keys()
    lines = ["[Interface]", f"PrivateKey = {private}", f"ListenPort = {SETTINGS.wg_port}", ""]
    for peer in list_peers():
        if not peer.enabled:
            continue
        allowed = [f"{peer.address}/32", *peer.networks]
        lines += [
            "[Peer]",
            f"# {peer.name}",
            f"PublicKey = {peer.public_key}",
            f"PresharedKey = {peer.preshared_key}",
            f"AllowedIPs = {', '.join(allowed)}",
            "",
        ]
    return "\n".join(lines)


def collect_routes() -> list[str]:
    """Networks that must be routed into wg0 on top of the tunnel subnet."""
    routes: list[str] = []
    for peer in list_peers():
        if peer.enabled:
            routes.extend(peer.networks)
    return sorted(set(routes))


def render_peer_config(peer: Peer, endpoint: str = "") -> str:
    """Render the .conf the user imports into UniFi or a WireGuard app."""
    _, server_public = server_keys()
    host = endpoint or SETTINGS.wg_endpoint or "DEINE-VPS-IP"
    dns = ", ".join(part.strip() for part in SETTINGS.peer_dns.split(",") if part.strip())

    lines = [
        "[Interface]",
        f"PrivateKey = {peer.private_key}",
        f"Address = {peer.address}/32",
    ]
    if dns:
        lines.append(f"DNS = {dns}")
    lines += [
        f"MTU = {SETTINGS.wg_mtu}",
        "",
        "[Peer]",
        f"PublicKey = {server_public}",
        f"PresharedKey = {peer.preshared_key}",
        # Full tunnel: everything the client sends goes through the bridge.
        # IPv6 (::/0) is deliberately omitted - the bridge does not carry it,
        # and claiming otherwise would black-hole IPv6 traffic.
        "AllowedIPs = 0.0.0.0/0",
        f"Endpoint = {host}:{SETTINGS.wg_port}",
        "PersistentKeepalive = 25",
        "",
    ]
    return "\n".join(lines)


def parse_wg_dump(text: str) -> dict[str, dict]:
    """Parse `wg show wg0 dump` into {public_key: {...}}.

    The first line describes the interface itself and is skipped; peer lines are
    public-key, preshared-key, endpoint, allowed-ips, latest-handshake, rx, tx,
    persistent-keepalive.
    """
    peers: dict[str, dict] = {}
    for index, line in enumerate(text.splitlines()):
        if index == 0 or not line.strip():
            continue
        fields = line.split("\t")
        if len(fields) < 7:
            continue
        endpoint = fields[2]
        peers[fields[0]] = {
            "endpoint": "" if endpoint == "(none)" else endpoint,
            "allowed_ips": fields[3],
            "last_handshake": int(fields[4] or 0),
            "rx": int(fields[5] or 0),
            "tx": int(fields[6] or 0),
        }
    return peers


def dump() -> dict[str, dict]:
    """Read live peer statistics from the kernel, or {} if wg0 is not up."""
    try:
        result = subprocess.run(
            ["wg", "show", "wg0", "dump"],
            capture_output=True, text=True, timeout=10, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return {}
    if result.returncode != 0:
        return {}
    return parse_wg_dump(result.stdout)
