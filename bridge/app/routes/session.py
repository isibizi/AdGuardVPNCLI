"""Panel login and the first-run password step."""

from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse

from app import auth, events
from app.web import redirect, render

router = APIRouter()


def _client(request: Request) -> str:
    return request.client.host if request.client else "unbekannt"


@router.get("/setup")
def setup_form(request: Request):
    if auth.password_is_set():
        return RedirectResponse("/login", status_code=303)
    return render(request, "setup.html")


@router.post("/setup")
def setup_submit(request: Request, password: str = Form(...), confirm: str = Form(...)):
    if auth.password_is_set():
        return RedirectResponse("/login", status_code=303)
    if password != confirm:
        return render(request, "setup.html", error="Die beiden Passwörter stimmen nicht überein.")
    try:
        auth.set_password(password)
    except ValueError as exc:
        return render(request, "setup.html", error=str(exc))

    response = RedirectResponse("/", status_code=303)
    response.set_cookie(
        auth.SESSION_COOKIE, auth.issue_token(),
        httponly=True, samesite="lax", max_age=auth.SESSION_TTL,
    )
    return response


@router.get("/login")
def login_form(request: Request):
    if not auth.password_is_set():
        return RedirectResponse("/setup", status_code=303)
    return render(request, "login.html")


@router.post("/login")
def login_submit(request: Request, password: str = Form(...)):
    client = _client(request)

    wait = auth.throttled(client)
    if wait:
        return render(
            request, "login.html",
            error=f"Zu viele Fehlversuche. Bitte warte {wait} Sekunden.",
        )

    if not auth.check_password(password):
        auth.record_failure(client)
        return render(request, "login.html", error="Falsches Passwort.")

    auth.clear_failures(client)
    events.record(events.PANEL, f"Panel-Login von {client}")
    response = RedirectResponse("/", status_code=303)
    response.set_cookie(
        auth.SESSION_COOKIE, auth.issue_token(),
        httponly=True, samesite="lax", max_age=auth.SESSION_TTL,
    )
    return response


@router.get("/logout")
def logout():
    response = redirect("/login", msg="Abgemeldet.")
    response.delete_cookie(auth.SESSION_COOKIE)
    return response
