"""Panel password and session tokens."""

from __future__ import annotations

import time

from app import auth


def test_password_roundtrip():
    auth.set_password("correct horse battery")
    assert auth.check_password("correct horse battery")
    assert not auth.check_password("wrong")


def test_short_passwords_are_rejected():
    try:
        auth.set_password("short")
    except ValueError as exc:
        assert "8 Zeichen" in str(exc)
    else:
        raise AssertionError("a five character password was accepted")


def test_changing_the_password_invalidates_existing_sessions():
    auth.set_password("first password")
    token = auth.issue_token()
    assert auth.token_valid(token)

    auth.set_password("second password")
    assert not auth.token_valid(token)


def test_tampered_and_expired_tokens_are_rejected():
    auth.set_password("a valid password")
    token = auth.issue_token()
    expires, signature = token.split(".", 1)

    assert not auth.token_valid(f"{expires}.{signature[:-2]}xx")
    assert not auth.token_valid(f"{int(time.time()) - 10}.{signature}")
    assert not auth.token_valid(None)
    assert not auth.token_valid("garbage")


def test_throttle_kicks_in_after_repeated_failures():
    client = "10.8.0.99"
    auth.clear_failures(client)
    for _ in range(8):
        assert auth.throttled(client) == 0
        auth.record_failure(client)
    assert auth.throttled(client) > 0
