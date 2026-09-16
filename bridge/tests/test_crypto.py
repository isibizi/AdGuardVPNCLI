"""X25519 against the RFC 7748 test vectors."""

from __future__ import annotations

import base64

from app import crypto


def test_rfc7748_vector():
    private = bytes.fromhex("77076d0a7318a57d3c16c17251b26645df4c2f87ebc0992ab177fba51db92c2a")
    expected = bytes.fromhex("8520f0098930a754748b7ddcb43ef75a0dbf3a0d26381af4eba4a98eaa9b4e6a")
    assert base64.b64decode(crypto.public_key(base64.b64encode(private).decode())) == expected


def test_generated_keys_are_valid_and_unique():
    first = crypto.generate_private_key()
    second = crypto.generate_private_key()
    assert first != second
    for key in (first, second, crypto.public_key(first), crypto.generate_preshared_key()):
        assert crypto.is_valid_key(key)


def test_clamping_is_applied():
    raw = base64.b64decode(crypto.generate_private_key())
    assert raw[0] % 8 == 0
    assert raw[31] & 0b1100_0000 == 0b0100_0000
