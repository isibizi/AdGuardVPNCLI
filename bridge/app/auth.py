"""Password and session handling for the panel.

The panel is never exposed to the internet (see docker-compose.yml), but it is
reachable from every device in the tunnel, so it still needs a real password: a
guest on the WiFi behind the UniFi can reach it.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import time

from app import db, events

_ITERATIONS = 240_000
SESSION_COOKIE = "bridge_session"
SESSION_TTL = 7 * 24 * 3600

PASSWORD_HASH_SETTING = "panel_password_hash"
SECRET_KEY_SETTING = "secret_key"

# Simple in-memory throttle. Resets when the panel restarts, which is fine: it
# exists to stop online guessing, not to be an audit trail.
_FAILURES: dict[str, list[float]] = {}
_MAX_FAILURES = 8
_WINDOW = 300


def secret_key() -> bytes:
    value = db.get_setting(SECRET_KEY_SETTING)
    if not value:
        value = secrets.token_urlsafe(48)
        db.set_setting(SECRET_KEY_SETTING, value)
    return value.encode()


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _ITERATIONS)
    return f"pbkdf2${_ITERATIONS}${base64.b64encode(salt).decode()}${base64.b64encode(digest).decode()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algorithm, iterations, salt_b64, digest_b64 = stored.split("$")
        if algorithm != "pbkdf2":
            return False
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), base64.b64decode(salt_b64), int(iterations)
        )
        return hmac.compare_digest(digest, base64.b64decode(digest_b64))
    except (ValueError, TypeError):
        return False


def password_is_set() -> bool:
    return bool(db.get_setting(PASSWORD_HASH_SETTING))


def set_password(password: str) -> None:
    if len(password) < 8:
        raise ValueError("Das Passwort muss mindestens 8 Zeichen lang haben.")
    db.set_setting(PASSWORD_HASH_SETTING, hash_password(password))
    # Existing sessions must not survive a password change.
    db.set_setting(SECRET_KEY_SETTING, secrets.token_urlsafe(48))
    events.record(events.PANEL, "Panel-Passwort gesetzt")


def check_password(password: str) -> bool:
    stored = db.get_setting(PASSWORD_HASH_SETTING)
    return bool(stored) and verify_password(password, stored)


def throttled(client: str) -> int:
    """Seconds the client has to wait, or 0 if it may try again now."""
    now = time.time()
    attempts = [t for t in _FAILURES.get(client, []) if now - t < _WINDOW]
    _FAILURES[client] = attempts
    if len(attempts) < _MAX_FAILURES:
        return 0
    return int(_WINDOW - (now - attempts[0])) + 1


def record_failure(client: str) -> None:
    _FAILURES.setdefault(client, []).append(time.time())
    events.record(events.PANEL, f"Fehlgeschlagener Panel-Login von {client}", events.WARNING)


def clear_failures(client: str) -> None:
    _FAILURES.pop(client, None)


def issue_token() -> str:
    expires = int(time.time()) + SESSION_TTL
    payload = str(expires).encode()
    signature = hmac.new(secret_key(), payload, hashlib.sha256).digest()
    return f"{expires}.{base64.urlsafe_b64encode(signature).decode().rstrip('=')}"


def token_valid(token: str | None) -> bool:
    if not token or "." not in token:
        return False
    expires_raw, signature = token.split(".", 1)
    try:
        expires = int(expires_raw)
    except ValueError:
        return False
    if expires < time.time():
        return False
    expected = hmac.new(secret_key(), expires_raw.encode(), hashlib.sha256).digest()
    expected_b64 = base64.urlsafe_b64encode(expected).decode().rstrip("=")
    return hmac.compare_digest(signature, expected_b64)
