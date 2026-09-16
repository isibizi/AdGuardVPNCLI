"""Shared helpers for the route modules."""

from __future__ import annotations

import subprocess
from pathlib import Path

from fastapi import Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from app import adguard, state
from app.config import SETTINGS

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def human_bytes(value: float) -> str:
    step = 1024.0
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(value) < step or unit == "TB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= step
    return f"{value:.1f} TB"


def human_rate(bytes_per_second: float) -> str:
    return f"{human_bytes(bytes_per_second)}/s"


def human_age(seconds: int | None) -> str:
    if seconds is None:
        return "nie"
    if seconds < 60:
        return f"vor {seconds} s"
    if seconds < 3600:
        return f"vor {seconds // 60} min"
    if seconds < 86400:
        return f"vor {seconds // 3600} h"
    return f"vor {seconds // 86400} Tagen"


TEMPLATES.env.filters["bytes"] = human_bytes
TEMPLATES.env.filters["rate"] = human_rate
TEMPLATES.env.filters["age"] = human_age


def render(request: Request, template: str, **context):
    current = state.read()
    live = adguard.status_live()
    context.setdefault("state", current)
    # Ask the client rather than trusting the stored flag: a stale "logged in"
    # is what hides the reason nothing works.
    context.setdefault(
        "login_required",
        live.auth_required if live is not None else bool(current.get("login_required")),
    )
    context.setdefault("settings", SETTINGS)
    context.setdefault("message", request.query_params.get("msg", ""))
    context.setdefault("error", request.query_params.get("err", ""))
    return TEMPLATES.TemplateResponse(request, template, context)


def redirect(path: str, msg: str = "", err: str = "") -> RedirectResponse:
    query = []
    if msg:
        query.append(f"msg={msg}")
    if err:
        query.append(f"err={err}")
    target = path + ("?" + "&".join(query) if query else "")
    return RedirectResponse(target, status_code=303)


def apply_wireguard_changes() -> str:
    """Re-render wg0 and apply it without dropping existing sessions.

    Returns an error message, or an empty string on success.
    """
    try:
        result = subprocess.run(
            ["/usr/local/bin/bridge-wg.sh", "sync"],
            capture_output=True, text=True, timeout=60, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return f"WireGuard-Konfiguration konnte nicht angewendet werden: {exc}"
    if result.returncode != 0:
        return (result.stderr or result.stdout).strip()[-300:]
    return ""


def endpoint_or_hint() -> tuple[str, str]:
    """The endpoint written into peer configs, plus a warning when unset."""
    if SETTINGS.wg_endpoint:
        return SETTINGS.wg_endpoint, ""
    return (
        "DEINE-VPS-IP",
        "WG_ENDPOINT ist nicht gesetzt. Trage die öffentliche IP oder den DNS-Namen "
        "deines VPS in die .env ein und starte den Container neu, sonst funktionieren "
        "die heruntergeladenen Configs nicht.",
    )
