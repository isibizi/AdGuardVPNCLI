"""AdGuard account, location and connection control."""

from __future__ import annotations

import asyncio
import json
import time

from fastapi import APIRouter, Form, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import PlainTextResponse, Response, StreamingResponse

from app import adguard, db, events, state, watchdog
from app.web import redirect, render

router = APIRouter()

PAUSED_SETTING = watchdog.PAUSED_SETTING


@router.get("/adguard")
def adguard_page(request: Request, refresh: int = 0):
    locations, raw, fetched_at = adguard.list_locations(force=bool(refresh))
    cache_age = int(time.time()) - fetched_at if fetched_at else None
    return render(
        request, "adguard.html",
        locations=locations,
        locations_raw=raw,
        cache_age=cache_age,
        selected=watchdog.selected_location(),
        paused=db.get_setting(PAUSED_SETTING) == "1",
    )


@router.post("/adguard/location")
def set_location(location: str = Form(...)):
    location = location.strip()
    if not location:
        return redirect("/adguard", err="Bitte wähle einen Standort aus.")

    db.set_setting(watchdog.LOCATION_SETTING, location)
    db.set_setting(PAUSED_SETTING, "0")
    events.record(events.VPN, f"Standort gewechselt zu '{location}'")

    # Drop the current connection and let the watchdog rebuild it. Routing the
    # change through the same code path as an automatic reconnect means the kill
    # switch, the tun2socks restart and the event log all behave identically.
    adguard.disconnect()
    state.write(vpn_connected=False, busy="Standortwechsel")
    return redirect("/adguard", msg=f"Standort '{location}' gespeichert. Die Verbindung wird neu aufgebaut.")


@router.post("/adguard/connect")
def connect_now():
    db.set_setting(PAUSED_SETTING, "0")
    state.write(busy="Verbindungsaufbau")
    events.record(events.VPN, "Verbindung manuell angefordert")
    return redirect("/", msg="Verbindung wird aufgebaut.")


@router.post("/adguard/disconnect")
def disconnect_now():
    # Without the pause flag the watchdog would helpfully reconnect within
    # seconds, which is not what somebody pressing "disconnect" wants.
    db.set_setting(PAUSED_SETTING, "1")
    events.record(events.VPN, "Verbindung manuell getrennt (Bridge pausiert)")
    adguard.disconnect()
    state.write(vpn_connected=False, busy="")
    return redirect("/", msg="Getrennt. Der Kill-Switch blockiert den LAN-Traffic, bis du wieder verbindest.")


# --- device-code login ------------------------------------------------------

@router.get("/adguard/login")
def login_page(request: Request):
    return render(request, "adguard_login.html")


@router.post("/adguard/login/start")
async def login_start():
    await run_in_threadpool(adguard.LOGIN.start)
    return {"started": True}


@router.post("/adguard/login/speedup")
async def login_speedup():
    await run_in_threadpool(adguard.LOGIN.speed_up)
    return {"ok": True}


@router.post("/adguard/login/cancel")
async def login_cancel():
    await run_in_threadpool(adguard.LOGIN.cancel)
    return {"ok": True}


@router.get("/adguard/login/stream")
async def login_stream():
    """Server-sent events carrying the login link, the code and the result."""

    async def generate():
        last = ""
        for _ in range(200):  # ~5 minutes at 1.5s per step
            payload = await run_in_threadpool(adguard.LOGIN.poll)
            serialised = json.dumps(payload, ensure_ascii=False)
            if serialised != last:
                last = serialised
                yield f"data: {serialised}\n\n"
            if payload.get("state") == "done":
                if payload.get("success"):
                    events.record(events.VPN, "Bei AdGuard angemeldet")
                    await run_in_threadpool(state.write, login_required=False, last_error="")
                    await run_in_threadpool(db.set_setting, PAUSED_SETTING, "0")
                return
            await asyncio.sleep(1.5)

        yield 'data: {"state": "done", "success": false, "error": "Zeitüberschreitung."}\n\n'

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/adguard/login/qr")
def login_qr():
    """QR code for the current login link.

    The URL comes from the running login flow, never from a request parameter -
    there is nothing a caller could inject here.

    The QR matters more than it looks: when the whole LAN is routed through a
    bridge whose VPN leg is down, the browser on that LAN cannot reach
    auth.adguard.io at all. Scanning this with a phone on mobile data always
    works.
    """
    from app.routes.peers import _svg_qr

    url = adguard.LOGIN.url
    if not url:
        return PlainTextResponse("noch kein Login-Link", status_code=404)
    return Response(_svg_qr(url), media_type="image/svg+xml")


@router.post("/adguard/logout")
def adguard_logout():
    result = adguard.logout()
    events.record(events.VPN, "Von AdGuard abgemeldet")
    state.write(vpn_connected=False, login_required=True)
    if not result.ok:
        return redirect("/adguard/login", err=result.output[-300:])
    return redirect("/adguard/login", msg="Abgemeldet. Melde dich mit einem beliebigen AdGuard-Konto neu an.")
