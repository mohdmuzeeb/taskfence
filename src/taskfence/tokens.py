"""Signed task tokens: `tf1.<base64url(claims)>.<base64url(hmac-sha256)>`.

Standard library only. A `KeyRing` holds several keys by `kid`: one signs, all
verify, so keys rotate without invalidating tokens in flight. Removing a kid
makes its tokens fail with UNKNOWN_KEY.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from dataclasses import asdict, dataclass
from typing import Callable

from .outcomes import Code, Denied

PREFIX = "tf1"
MIN_KEY_BYTES = 32


def _b64e(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


@dataclass(frozen=True)
class Claims:
    grant_id: str
    principal: str
    operation: str
    epoch: int
    task_id: str
    kid: str
    iat: int
    exp: int


class KeyRing:
    def __init__(self, keys: dict[str, bytes], signing_kid: str):
        if signing_kid not in keys:
            raise ValueError("signing_kid must be one of the keys")
        for kid, k in keys.items():
            if len(k) < MIN_KEY_BYTES:
                raise ValueError(f"key {kid!r} is shorter than {MIN_KEY_BYTES} bytes")
        self._keys = dict(keys)
        self.signing_kid = signing_kid

    def __repr__(self) -> str:          # never print key material
        return f"KeyRing(kids={sorted(self._keys)}, signing_kid={self.signing_kid!r})"

    @classmethod
    def generate(cls, kid: str = "k1") -> "KeyRing":
        return cls({kid: os.urandom(32)}, kid)

    @classmethod
    def from_string(cls, spec: str) -> "KeyRing":
        """`kid:base64key[,kid:base64key...]`; the first entry signs."""
        keys: dict[str, bytes] = {}
        for part in filter(None, (p.strip() for p in spec.split(","))):
            kid, _, b64 = part.partition(":")
            if not kid or not b64:
                raise ValueError("expected kid:base64key")
            keys[kid] = _b64d(b64)
        if not keys:
            raise ValueError("no keys")
        return cls(keys, next(iter(keys)))

    @classmethod
    def from_env(cls, var: str = "TASKFENCE_KEYS") -> "KeyRing":
        spec = os.environ.get(var)
        if not spec:
            raise ValueError(f"environment variable {var} is not set")
        return cls.from_string(spec)

    def key(self, kid: str) -> bytes | None:
        return self._keys.get(kid)


def sign(claims: Claims, keys: KeyRing) -> str:
    key = keys.key(claims.kid)
    if key is None:
        raise ValueError(f"unknown kid {claims.kid!r}")
    body = _b64e(json.dumps(asdict(claims), separators=(",", ":"), sort_keys=True).encode())
    mac = hmac.new(key, f"{PREFIX}.{body}".encode(), hashlib.sha256).digest()
    return f"{PREFIX}.{body}.{_b64e(mac)}"


def verify(token: str, keys: KeyRing, *, now: Callable[[], float] = time.time,
           leeway_s: int = 30) -> Claims:
    """Return the claims or raise `Denied`. Checks format, key, MAC and expiry."""
    try:
        prefix, body, mac = token.split(".")
        if prefix != PREFIX:
            raise ValueError
        raw = json.loads(_b64d(body))
        claims = Claims(**raw)
        if not isinstance(claims.epoch, int) or not isinstance(claims.exp, int):
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise Denied(Code.BAD_TOKEN) from None
    key = keys.key(claims.kid)
    if key is None:
        raise Denied(Code.UNKNOWN_KEY, claims.kid)
    expected = hmac.new(key, f"{PREFIX}.{body}".encode(), hashlib.sha256).digest()
    try:
        given = _b64d(mac)
    except ValueError:
        raise Denied(Code.BAD_TOKEN) from None
    if not hmac.compare_digest(expected, given):
        raise Denied(Code.BAD_SIGNATURE)
    if now() > claims.exp + leeway_s:
        raise Denied(Code.EXPIRED)
    return claims
