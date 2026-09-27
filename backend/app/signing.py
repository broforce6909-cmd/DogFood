"""Signed, publicly verifiable records.

The requirement is *"signed, publicly verifiable judge participation records"*, and
the load-bearing word is **publicly**: a record is only useful if a third party can
check it without an account and without trusting the page that displayed it.

That rules out three tempting shortcuts, and this module exists to avoid them:

1. **Do not sign an object; sign bytes.** If verification rebuilds the JSON from a
   dict, the signature silently depends on key order, on float formatting, and on
   every future change to the response model. `canonical()` produces one
   deterministic encoding, and the exact bytes are stored alongside the signature so
   the check is always over what was actually signed.
2. **Asymmetric, not a shared secret (Phase 5).** The original version of this
   module used HMAC-SHA256, which meant "verified" was really "the server that
   holds the secret says so" -- a stranger checking a record had no way to
   confirm that *themselves*, only to trust our verdict. Ed25519 fixes that: a
   verifier needs only the **public** key, published at
   `GET /api/signing/public-keys`, and can recompute the signature check with
   nothing else from us. It also fixes the rotation problem an HMAC has no
   answer to -- see "Key rotation" below.
3. **Do not compare signatures with `==`.** A byte-by-byte comparison leaks the
   expected value to anybody who can measure the call. Ed25519 verification in
   `cryptography` is constant-time by construction, which is one more reason to
   prefer it over a hand-rolled comparison.

## Key rotation

`SIGNING_ACTIVE_KID` names the key that signs new records; its private half is
`SIGNING_ACTIVE_PRIVATE_KEY_PEM`. When that key is rotated, its **public** half
moves into `SIGNING_RETIRED_KEYS` (a JSON list of `{kid, public_key_pem}`) and a
new active key takes over. Every certificate already issued keeps its `key_id`,
so verification keeps working: `verify()` looks the key up by id in the combined
set of "active" and "retired" keys, never assumes there is only one. A key that
is retired rather than deleted is what lets rotation happen without invalidating
history -- the opposite of what an HMAC secret rotation does to every record
signed under the old one.
"""

from __future__ import annotations

import json
import secrets
from functools import lru_cache
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    PublicFormat,
    load_pem_private_key,
    load_pem_public_key,
)

from .config import settings

# Unambiguous alphabet: no O/0 or I/l/1, because these codes get read down a phone
# and typed off a printout. 12 characters of this is ~62 bits, which is far past
# enumerable.
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
CODE_LENGTH = 12


def canonical(payload: dict[str, Any]) -> bytes:
    """One deterministic JSON encoding of a record.

    Sorted keys, no incidental whitespace, UTF-8, and `allow_nan=False` -- `NaN`
    and `Infinity` are not JSON, and a record a standards-compliant parser
    rejects is not verifiable by a third party however well we signed it.

    Shared with `app.audit`'s hash chain as well as certificates -- both need the
    same property (one deterministic encoding of a dict) for the same reason.
    """
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


@lru_cache(maxsize=1)
def _active_key() -> tuple[str, Ed25519PrivateKey]:
    """The key that signs new records. Cached: parsing a PEM on every request
    that issues a certificate would be pure waste for a value that never
    changes while the process is running."""
    key = load_pem_private_key(settings.signing_active_private_key_pem.encode(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise TypeError("SIGNING_ACTIVE_PRIVATE_KEY_PEM must be an Ed25519 private key")
    return settings.signing_active_kid, key


@lru_cache(maxsize=1)
def _verification_keys() -> dict[str, Ed25519PublicKey]:
    """Every key a signature can be checked against: the active one, plus
    whatever has been retired. Retired keys carry only their public half --
    see the module docstring on why that is deliberate, not an oversight."""
    kid, private_key = _active_key()
    keys: dict[str, Ed25519PublicKey] = {kid: private_key.public_key()}

    retired = json.loads(settings.signing_retired_keys or "[]")
    for entry in retired:
        public_key = load_pem_public_key(entry["public_key_pem"].encode())
        if not isinstance(public_key, Ed25519PublicKey):
            raise TypeError(f"retired key {entry.get('kid')!r} is not Ed25519")
        keys[entry["kid"]] = public_key
    return keys


def public_keys() -> list[dict[str, str]]:
    """What `GET /api/signing/public-keys` publishes: every key a signature
    might be checked against, active or retired, public half only."""
    active_kid, _ = _active_key()
    out = []
    for kid, key in _verification_keys().items():
        pem = key.public_bytes(encoding=Encoding.PEM, format=PublicFormat.SubjectPublicKeyInfo)
        out.append(
            {
                "kid": kid,
                "public_key_pem": pem.decode(),
                "status": "active" if kid == active_kid else "retired",
            }
        )
    return out


def sign(body: bytes) -> tuple[str, str]:
    """Sign these exact bytes. Returns (signature_hex, key_id)."""
    kid, private_key = _active_key()
    signature = private_key.sign(body)
    return signature.hex(), kid


def sign_payload(payload: dict[str, Any]) -> tuple[bytes, str, str]:
    """Canonicalise and sign in one step. Returns (bytes_to_store, signature_hex, key_id).

    Callers store all three. Re-deriving the bytes later, or guessing which key
    signed them, is exactly the mistake this module is written to prevent.
    """
    body = canonical(payload)
    signature, kid = sign(body)
    return body, signature, kid


def verify(body: bytes, signature: str, key_id: str) -> bool:
    """Check a signature against the named key. False rather than raising: an
    invalid or unrecognised record is an answer a public endpoint should be
    able to return calmly, not a 500.

    An unrecognised `key_id` -- one that is neither active nor listed as
    retired -- fails closed. A verifier cannot check what it cannot find the
    public key for, and neither can we.
    """
    key = _verification_keys().get(key_id)
    if key is None:
        return False
    try:
        key.verify(bytes.fromhex(signature), body)
        return True
    except (InvalidSignature, ValueError):
        return False


def new_code() -> str:
    """A public verification handle."""
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))
