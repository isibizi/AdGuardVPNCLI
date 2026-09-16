"""Small command line entry points used by the shell scripts."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from app import auth, db, events, wireguard
from app.config import SETTINGS


def cmd_init() -> int:
    """Prepare persistent state on container start."""
    db.connect()
    wireguard.server_keys()
    auth.secret_key()

    # A password given in .env wins on first start; afterwards the stored one is
    # left alone so changing it in the panel is not undone by a restart.
    if SETTINGS.panel_password and not auth.password_is_set():
        try:
            auth.set_password(SETTINGS.panel_password)
        except ValueError as exc:
            print(f"[bridge] PANEL_PASSWORD abgelehnt: {exc}", file=sys.stderr)

    if not auth.password_is_set():
        print("[bridge] Kein Panel-Passwort gesetzt - der Setup-Assistent fragt beim ersten Aufruf danach.",
              file=sys.stderr)

    events.record(events.SYSTEM, "Bridge gestartet")
    return 0


def cmd_render_wg(config_path: Path, routes_path: Path) -> int:
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(wireguard.render_server_config(), encoding="utf-8")
    config_path.chmod(0o600)
    routes_path.write_text("\n".join(wireguard.collect_routes()) + "\n", encoding="utf-8")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="app.cli")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="create database, keys and secrets")

    render = sub.add_parser("render-wg", help="render the wg0 configuration")
    render.add_argument("--config", type=Path, default=SETTINGS.wg_dir / "wg0.conf")
    render.add_argument("--routes", type=Path, default=SETTINGS.wg_dir / "routes.txt")

    args = parser.parse_args(argv)
    if args.command == "init":
        return cmd_init()
    if args.command == "render-wg":
        return cmd_render_wg(args.config, args.routes)
    return 1


if __name__ == "__main__":
    sys.exit(main())
