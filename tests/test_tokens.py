import base64

import pytest

from taskfence import Code, Denied, Fence, KeyRing
from taskfence.tokens import Claims, sign, verify


def _claims(kid="k1", exp=2_000_000_000):
    return Claims("g1", "user:42", "refund.issue", 1, "t1", kid, 1_000, exp)


def test_round_trip():
    keys = KeyRing.generate()
    assert verify(sign(_claims(), keys), keys, now=lambda: 1_500) == _claims()


def test_tampered_claims_fail_signature():
    keys = KeyRing.generate()
    prefix, body, mac = sign(_claims(), keys).split(".")
    raw = base64.urlsafe_b64decode(body + "==").replace(b'"epoch":1', b'"epoch":9')
    forged = base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
    with pytest.raises(Denied) as e:
        verify(f"{prefix}.{forged}.{mac}", keys, now=lambda: 1_500)
    assert e.value.code == Code.BAD_SIGNATURE


def test_other_keyring_fails():
    with pytest.raises(Denied) as e:
        verify(sign(_claims(), KeyRing.generate()), KeyRing.generate(), now=lambda: 1_500)
    assert e.value.code == Code.BAD_SIGNATURE


@pytest.mark.parametrize("token", ["", "tf1.x", "tf2.e30.AA", "tf1.!!!.AA", "tf1.e30.AA"])
def test_malformed(token):
    with pytest.raises(Denied) as e:
        verify(token, KeyRing.generate())
    assert e.value.code == Code.BAD_TOKEN


def test_expired_with_leeway():
    keys = KeyRing.generate()
    tok = sign(_claims(exp=1_000), keys)
    verify(tok, keys, now=lambda: 1_020, leeway_s=30)
    with pytest.raises(Denied) as e:
        verify(tok, keys, now=lambda: 1_031, leeway_s=30)
    assert e.value.code == Code.EXPIRED


def test_rotation_old_kid_still_verifies_until_removed():
    old = KeyRing.generate("k1")
    tok = sign(_claims("k1"), old)
    k1 = old.key("k1")
    rotated = KeyRing({"k2": b"n" * 32, "k1": k1}, "k2")
    verify(tok, rotated, now=lambda: 1_500)
    with pytest.raises(Denied) as e:
        verify(tok, KeyRing({"k2": b"n" * 32}, "k2"), now=lambda: 1_500)
    assert e.value.code == Code.UNKNOWN_KEY


def test_from_string_and_short_key():
    k = base64.urlsafe_b64encode(b"a" * 32).decode()
    ring = KeyRing.from_string(f"main:{k}, old:{k}")
    assert ring.signing_kid == "main"
    assert "aaaa" not in repr(ring)
    with pytest.raises(ValueError):
        KeyRing({"k": b"short"}, "k")


def test_mint_uses_injected_clock():
    f = Fence(KeyRing.generate(), clock=lambda: 10_000)
    c = f.verify(f.mint(grant_id="g", principal="p", operation="o", epoch=3, task_id="t", ttl_s=60))
    assert (c.iat, c.exp, c.epoch) == (10_000, 10_060, 3)
