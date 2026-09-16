"""Runtime configuration, read once from the environment."""

from __future__ import annotations

import ipaddress
import os
from dataclasses import dataclass
from pathlib import Path


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    try:
        return int(raw)
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    wg_subnet: ipaddress.IPv4Network
    wg_endpoint: str
    wg_port: int
    wg_mtu: int
    peer_dns: str
    panel_password: str
    adguard_location: str
    adguard_protocol: str
    fallback_to_fastest: bool
    stats_retention_days: int
    connection_log: bool

    @property
    def db_path(self) -> Path:
        return self.data_dir / "bridge.db"

    @property
    def wg_dir(self) -> Path:
        return self.data_dir / "wg"

    @property
    def bridge_address(self) -> ipaddress.IPv4Address:
        """The bridge's own address inside the tunnel, e.g. 10.8.0.1."""
        return next(self.wg_subnet.hosts())

    @property
    def events_log(self) -> Path:
        return self.data_dir / "events.log"

    @property
    def state_file(self) -> Path:
        return self.data_dir / "state.json"


def load() -> Settings:
    subnet = os.environ.get("WG_SUBNET", "10.8.0.0/24").strip() or "10.8.0.0/24"
    return Settings(
        data_dir=Path(os.environ.get("BRIDGE_DATA_DIR", "/data")),
        wg_subnet=ipaddress.ip_network(subnet, strict=False),
        wg_endpoint=os.environ.get("WG_ENDPOINT", "").strip(),
        wg_port=_int("WG_PORT", 51820),
        wg_mtu=_int("WG_MTU", 1380),
        peer_dns=os.environ.get("PEER_DNS", "94.140.14.14,94.140.15.15").strip(),
        panel_password=os.environ.get("PANEL_PASSWORD", ""),
        adguard_location=os.environ.get("ADGUARD_LOCATION", "fastest").strip() or "fastest",
        adguard_protocol=os.environ.get("ADGUARD_PROTOCOL", "auto").strip(),
        fallback_to_fastest=_bool("FALLBACK_TO_FASTEST", True),
        stats_retention_days=_int("STATS_RETENTION_DAYS", 90),
        connection_log=_bool("CONNECTION_LOG", False),
    )


SETTINGS = load()
