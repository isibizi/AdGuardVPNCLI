"""The control panel.

Deliberately small: server-rendered pages, a handful of JSON endpoints for live
updates, no build step and no external assets. Everything it does is a thin
layer over adguardvpn-cli and wg.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from app import auth, db, events, stats, wireguard
from app.routes import adguard as adguard_routes
from app.routes import monitor as monitor_routes
from app.routes import peers as peers_routes
from app.routes import session as session_routes
from app.routes import status as status_routes

app = FastAPI(title="AdGuard VPN Bridge", docs_url=None, redoc_url=None, openapi_url=None)

_STATIC = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(_STATIC)), name="static")

# Paths reachable without a session.
_PUBLIC = ("/login", "/setup", "/static", "/healthz")


@app.middleware("http")
async def require_session(request: Request, call_next):
    path = request.url.path

    if path.startswith(_PUBLIC):
        return await call_next(request)

    # Before a password exists there is nothing to protect and no way in, so
    # every request is funnelled into the setup wizard.
    if not auth.password_is_set():
        return RedirectResponse("/setup", status_code=303)

    if not auth.token_valid(request.cookies.get(auth.SESSION_COOKIE)):
        if path.startswith("/api/"):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return RedirectResponse("/login", status_code=303)

    return await call_next(request)


@app.on_event("startup")
def on_startup() -> None:
    db.connect()
    wireguard.server_keys()
    auth.secret_key()
    stats.Sampler().start()
    events.record(events.PANEL, "Panel gestartet")


@app.get("/healthz")
def healthz() -> PlainTextResponse:
    return PlainTextResponse("ok")


app.include_router(session_routes.router)
app.include_router(status_routes.router)
app.include_router(adguard_routes.router)
app.include_router(peers_routes.router)
app.include_router(monitor_routes.router)
