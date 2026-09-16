"""WireGuard key material.

X25519 is implemented here (RFC 7748) rather than shelling out to `wg genkey`
so that key handling is testable without a WireGuard installation and does not
depend on a binary being present at the right moment. The implementation is the
reference ladder from the RFC and is checked against its test vectors in
tests/test_crypto.py.
"""

from __future__ import annotations

import base64
import os

_P = 2**255 - 19
_A24 = 121665


def _cswap(swap: int, x2: int, x3: int) -> tuple[int, int]:
    dummy = swap * ((x2 - x3) % _P)
    return (x2 - dummy) % _P, (x3 + dummy) % _P


def _scalarmult(k: int, u: int) -> int:
    x1, x2, z2, x3, z3, swap = u, 1, 0, u, 1, 0
    for t in range(254, -1, -1):
        kt = (k >> t) & 1
        swap ^= kt
        x2, x3 = _cswap(swap, x2, x3)
        z2, z3 = _cswap(swap, z2, z3)
        swap = kt

        a = (x2 + z2) % _P
        aa = a * a % _P
        b = (x2 - z2) % _P
        bb = b * b % _P
        e = (aa - bb) % _P
        c = (x3 + z3) % _P
        d = (x3 - z3) % _P
        da = d * a % _P
        cb = c * b % _P
        x3 = pow(da + cb, 2, _P)
        z3 = x1 * pow(da - cb, 2, _P) % _P
        x2 = aa * bb % _P
        z2 = e * ((aa + _A24 * e) % _P) % _P

    x2, x3 = _cswap(swap, x2, x3)
    z2, z3 = _cswap(swap, z2, z3)
    return x2 * pow(z2, _P - 2, _P) % _P


def _clamp(raw: bytes) -> bytes:
    key = bytearray(raw)
    key[0] &= 248
    key[31] &= 127
    key[31] |= 64
    return bytes(key)


def generate_private_key() -> str:
    """Return a new, clamped private key in WireGuard's base64 encoding."""
    return base64.b64encode(_clamp(os.urandom(32))).decode()


def public_key(private_key: str) -> str:
    """Derive the public key belonging to a base64 private key."""
    raw = _clamp(base64.b64decode(private_key))
    result = _scalarmult(int.from_bytes(raw, "little"), 9)
    return base64.b64encode(result.to_bytes(32, "little")).decode()


def generate_preshared_key() -> str:
    """Return a fresh symmetric key for the extra layer WireGuard supports."""
    return base64.b64encode(os.urandom(32)).decode()


def is_valid_key(value: str) -> bool:
    try:
        return len(base64.b64decode(value, validate=True)) == 32
    except Exception:
        return False
