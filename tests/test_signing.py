"""Signed records: canonicalisation, Ed25519, rotation, and tamper detection.

Written before `app.signing` existed, then rewritten in Phase 5 when HMAC was
replaced with Ed25519. The property that matters is narrow and worth stating
precisely:

    A third party can check a record without an account, without trusting the page
    it is displayed on, and without trusting our verdict -- only the published
    public key.

That rules out a few tempting shortcuts, and the tests below are mostly about those:

* **Signing a re-serialised object is not signing the record.** If verification
  rebuilds the JSON from a dict, the signature depends on key order and on every
  future change to the response model. So the exact bytes that were signed are
  stored, and verification is over bytes.
* **Asymmetric, not a shared secret.** A verifier needs only the public half of the
  active or a retired key, never anything held only by the server.
* **Rotation must not invalidate history.** A record signed under a key that is
  later retired must keep verifying under that key's `key_id`.
* **An unrecognised `key_id` fails closed.** A verifier -- and we -- cannot check
  what there is no public key for.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1] / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)

from app.signing import (  # noqa: E402
    canonical,
    new_code,
    public_keys,
    sign,
    sign_payload,
    verify,
)

PAYLOAD = {
    "kind": "judging",
    "event": "raptors-winter",
    "subject": "Priya Rivera",
    "reviews": 6,
    "issued_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
}


def _generate_keypair() -> tuple[str, str]:
    """A fresh Ed25519 keypair as (private_pem, public_pem), for rotation tests."""
    key = Ed25519PrivateKey.generate()
    private_pem = key.private_bytes(
        encoding=Encoding.PEM,
        format=PrivateFormat.PKCS8,
        encryption_algorithm=NoEncryption(),
    ).decode()
    public_pem = key.public_key().public_bytes(
        encoding=Encoding.PEM, format=PublicFormat.SubjectPublicKeyInfo
    ).decode()
    return private_pem, public_pem


# --------------------------------------------------------------------------- #
# Canonical form
# --------------------------------------------------------------------------- #


def test_canonical_form_is_stable_under_key_order() -> None:
    """The same facts in a different dict order must sign identically, or a
    verifier that rebuilt the object would reject a genuine record."""
    a = canonical({"b": 2, "a": 1})
    b = canonical({"a": 1, "b": 2})
    assert a == b


def test_canonical_form_is_bytes_and_compact() -> None:
    out = canonical(PAYLOAD)
    assert isinstance(out, bytes)
    # No incidental whitespace: a pretty-printer in the middle of a signing path is
    # a signature that breaks when somebody reformats.
    assert b", " not in out and b": " not in out


def test_canonical_form_is_deterministic_across_runs() -> None:
    assert canonical(PAYLOAD) == canonical(json.loads(json.dumps(PAYLOAD)))


def test_canonical_form_rejects_nan() -> None:
    """`NaN` is not JSON, and `json.dumps` emits it anyway unless told not to.
    A record nobody else can parse is not verifiable."""
    with pytest.raises(ValueError):
        canonical({"score": float("nan")})


# --------------------------------------------------------------------------- #
# Signing and verification
# --------------------------------------------------------------------------- #


def test_a_signature_verifies() -> None:
    body, signature, key_id = sign_payload(PAYLOAD)
    assert verify(body, signature, key_id) is True


def test_a_tampered_payload_does_not_verify() -> None:
    """The whole point. Change one number in a record somebody was handed and the
    signature stops matching."""
    body, signature, key_id = sign_payload(PAYLOAD)
    tampered = body.replace(b'"reviews":6', b'"reviews":60')
    assert tampered != body
    assert verify(tampered, signature, key_id) is False


def test_a_tampered_signature_does_not_verify() -> None:
    body, signature, key_id = sign_payload(PAYLOAD)
    flipped = ("0" if signature[0] != "0" else "1") + signature[1:]
    assert verify(body, flipped, key_id) is False


def test_a_signature_is_not_a_plain_hash() -> None:
    """If the signature were `sha256(body)`, anybody could mint a record.

    Asserted by showing the signature is not the digest -- the private key is
    what makes it evidence rather than a checksum.
    """
    import hashlib

    body, signature, _ = sign_payload(PAYLOAD)
    assert signature != hashlib.sha256(body).hexdigest()


def test_sign_payload_returns_the_active_key_id() -> None:
    body, signature, key_id = sign_payload(PAYLOAD)
    active = {entry["kid"] for entry in public_keys() if entry["status"] == "active"}
    assert key_id in active
    assert sign(body) == (signature, key_id)


def test_signing_is_independent_of_the_session_secret(monkeypatch) -> None:
    """Rotating `SESSION_SECRET` logs everybody out -- on purpose.

    It must NOT also invalidate every certificate ever issued, so signing keys and
    the session secret are entirely separate settings. This is the test that stops
    somebody 'simplifying' them into one.
    """
    body, signature, key_id = sign_payload(PAYLOAD)
    monkeypatch.setattr("app.signing.settings.session_secret", "rotated-session-secret")
    assert verify(body, signature, key_id) is True


def test_verification_is_constant_time_by_construction() -> None:
    """Ed25519 verification in `cryptography` is constant-time -- there is no
    hand-rolled byte comparison in `verify()` to time in the first place.

    Checked structurally: `verify()` must delegate to the library's key-object
    `.verify()` rather than compare a signature or digest with `==`.
    """
    import inspect

    import app.signing as signing

    source = inspect.getsource(signing.verify)
    assert "key.verify(" in source
    assert "==" not in source


def test_an_unknown_key_id_fails_closed() -> None:
    body, signature, _ = sign_payload(PAYLOAD)
    assert verify(body, signature, "no-such-kid") is False


# --------------------------------------------------------------------------- #
# Key rotation
# --------------------------------------------------------------------------- #


def test_rotation_keeps_old_records_verifiable(monkeypatch) -> None:
    """The property rotation exists for: signing key changes, old signatures don't
    stop verifying.

    Simulated end to end: sign under key A, "rotate" by making key B active and
    retiring A's public half, then confirm the record signed under A still
    verifies (by A's `key_id`) and a new record signs under B.
    """
    import app.signing as signing

    private_a, public_a = _generate_keypair()
    private_b, public_b = _generate_keypair()

    monkeypatch.setattr(signing.settings, "signing_active_kid", "key-a")
    monkeypatch.setattr(signing.settings, "signing_active_private_key_pem", private_a)
    monkeypatch.setattr(signing.settings, "signing_retired_keys", "[]")
    signing._active_key.cache_clear()
    signing._verification_keys.cache_clear()

    body_a, signature_a, kid_a = sign_payload(PAYLOAD)
    assert kid_a == "key-a"
    assert verify(body_a, signature_a, kid_a) is True

    retired = json.dumps([{"kid": "key-a", "public_key_pem": public_a}])
    monkeypatch.setattr(signing.settings, "signing_active_kid", "key-b")
    monkeypatch.setattr(signing.settings, "signing_active_private_key_pem", private_b)
    monkeypatch.setattr(signing.settings, "signing_retired_keys", retired)
    signing._active_key.cache_clear()
    signing._verification_keys.cache_clear()

    # The record signed under the now-retired key A still verifies.
    assert verify(body_a, signature_a, kid_a) is True

    # New records sign under the new active key, B.
    body_b, signature_b, kid_b = sign_payload(PAYLOAD)
    assert kid_b == "key-b"
    assert verify(body_b, signature_b, kid_b) is True

    # Key A's retired *public* half cannot verify a signature made with B's
    # private key under B's own kid -- they are different keys, full stop -- and
    # key A can no longer sign at all (only its public half is configured now).
    assert verify(body_b, signature_b, "key-a") is False

    signing._active_key.cache_clear()
    signing._verification_keys.cache_clear()


def test_public_keys_lists_active_and_retired_with_correct_status(monkeypatch) -> None:
    import app.signing as signing

    private_a, public_a = _generate_keypair()
    private_b, _public_b = _generate_keypair()

    retired = json.dumps([{"kid": "key-a", "public_key_pem": public_a}])
    monkeypatch.setattr(signing.settings, "signing_active_kid", "key-b")
    monkeypatch.setattr(signing.settings, "signing_active_private_key_pem", private_b)
    monkeypatch.setattr(signing.settings, "signing_retired_keys", retired)
    signing._active_key.cache_clear()
    signing._verification_keys.cache_clear()

    keys = {entry["kid"]: entry for entry in public_keys()}
    assert keys["key-b"]["status"] == "active"
    assert keys["key-a"]["status"] == "retired"
    # Only the public half is ever published -- no PEM header lies about that.
    assert "PRIVATE" not in keys["key-a"]["public_key_pem"]
    assert "PRIVATE" not in keys["key-b"]["public_key_pem"]

    signing._active_key.cache_clear()
    signing._verification_keys.cache_clear()


# --------------------------------------------------------------------------- #
# Verification codes
# --------------------------------------------------------------------------- #


def test_codes_are_unique_and_readable() -> None:
    """Short enough to read down a phone, random enough not to be enumerable."""
    codes = {new_code() for _ in range(2000)}
    assert len(codes) == 2000
    sample = next(iter(codes))
    assert 8 <= len(sample) <= 32
    # Unambiguous alphabet: no 0/O or 1/I/l to mis-hear or mis-type.
    assert not set(sample) & set("O0Il1")


def test_codes_are_uppercase_and_alphanumeric() -> None:
    code = new_code()
    assert code.isalnum()
    assert code == code.upper()
