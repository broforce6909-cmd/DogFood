"""Outbound webhooks.

This is the most dangerous thing T4 adds, and it is worth being blunt about why: it
lets an *organizer* choose a URL that **our server** will then request. Our server
sits inside the Docker network, one hop from Postgres, and on a cloud host one hop
from the instance metadata service. Unguarded, a webhook form is a
server-side-request-forgery primitive with a friendly label on it.

So the rule is deny-by-default, and it is applied **twice**:

* at registration, so an organizer gets an immediate 422 rather than a silent
  never-delivering hook; and
* at delivery, because DNS can be re-pointed after registration. A name that
  resolved to a public address once can resolve to `127.0.0.1` later, and a
  check that only ran at registration would not notice.

Delivery is synchronous-but-bounded, inside a `BackgroundTasks` callback so it does
not sit in the caller's request. There is no job runner in this stack and adding one
for this would be a dependency for housekeeping; the cost is that a slow endpoint
delays nothing but its own delivery, and a dead one is disabled after
`MAX_FAILURES`.
"""

from __future__ import annotations

import ipaddress
import json
import socket
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy.orm import Session

from .models import (
    DeliveryStatus,
    Webhook,
    WebhookDelivery,
    WebhookEvent,
    utcnow,
)

# Consecutive *deliveries* (each of which may itself be several attempts, see
# below) before a hook is switched off. Low on purpose: an organizer's dead
# endpoint should not be retried all weekend, and re-enabling is one PATCH away
# once they have fixed it.
MAX_FAILURES = 5

# Per-attempt timeout. A hook is a courtesy to an integration, not a transaction.
TIMEOUT_SECONDS = 5

# Backoff between attempts *within* one delivery, riding out a receiver's brief
# blip (a deploy restart, a momentary 502) -- not a sustained outage, which
# `MAX_FAILURES` already exists to handle by disabling the hook entirely.
# Deliberately short: this all runs synchronously, either in a background task
# or, for the "Test" button, in the request itself, and an organizer waiting on
# a test ping should not wait the length of a real retry policy to find out
# their endpoint is down.
RETRY_BACKOFF_SECONDS = (0.5, 2.0)
MAX_DELIVERY_ATTEMPTS = len(RETRY_BACKOFF_SECONDS) + 1  # the first try, then two retries

# Bodies are truncated before being stored, so a chatty endpoint cannot fill the
# table with its own HTML error pages.
MAX_ERROR_CHARS = 500

# Hostnames refused on sight. Single-label names are caught by the dot rule; these
# are the dotted ones that are still never a legitimate webhook target.
_NEVER = {
    "localhost",
    "localhost.localdomain",
    # Cloud instance metadata, the highest-value SSRF target there is.
    "metadata.google.internal",
    "metadata.goog",
    "instance-data",
}


@dataclass(frozen=True)
class Rejected:
    reason: str


def check_target(url: str) -> Rejected | None:
    """Is this a URL we are willing to have the server request? None means yes.

    Checks, in order:

    1. The scheme is http or https. `file://` would read our disk; `gopher://` and
       friends are request-smuggling shapes.
    2. There is a host.
    3. If the host is a literal address, it is global.
    4. If it is a name, it has a dot and is not on `_NEVER`. A single-label name is
       a container or a LAN host, never a public FQDN -- and this rule is checked
       before DNS precisely so the answer does not depend on DNS being available.
    5. **Every address the name resolves to** is global. Not the first one --
       `getaddrinfo` can return several, and a host that resolves to one public and
       one loopback address is an attack, not a misconfiguration.

    A *dotted* name that does not resolve is allowed through: an organizer may
    legitimately register a hook before their endpoint exists, and the delivery-time
    check catches it if it ever points somewhere private.
    """
    try:
        parts = urlsplit(url)
    except ValueError:
        return Rejected("that URL could not be parsed")

    if parts.scheme not in ("http", "https"):
        return Rejected("a webhook URL must start with http:// or https://")
    if not parts.hostname:
        return Rejected("that URL has no host")

    host = parts.hostname.lower()

    # Names that cannot be public, refused without asking DNS.
    #
    # A hostname with no dot is not a public FQDN -- it is a container name, a LAN
    # host, or a search-domain lookup. `db` and `api` are this stack's own service
    # names. Checking the shape rather than the resolution matters because DNS may
    # not answer where the hook is *registered* while resolving to something internal
    # where the server will *call* it, and that asymmetry is the attack.
    # Literal addresses first: an IPv6 literal has no dot, so the shape rule below
    # would reject a legitimate public one for the wrong reason.
    try:
        if not _is_public(ipaddress.ip_address(host.strip("[]"))):
            return Rejected(
                f"{host} is a private or internal address, and the server will not "
                "call it"
            )
        return None
    except ValueError:
        pass  # it is a name, not a literal

    if "." not in host or host in _NEVER:
        return Rejected(
            f"{host} is not a public hostname, and the server will not call internal "
            "or private addresses"
        )

    # Docker-network service names resolve to private addresses, so the resolution
    # check below catches `db` and `api` too -- but only if DNS is available. Named
    # explicitly so the refusal does not depend on that.
    try:
        infos = socket.getaddrinfo(host, parts.port or None, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        # Unresolvable. Allowed at registration; delivery will fail loudly.
        return None

    for info in infos:
        address = info[4][0]
        try:
            if not _is_public(ipaddress.ip_address(address)):
                return Rejected(
                    f"{host} resolves to {address}, a private or internal address, "
                    "and the server will not call it"
                )
        except ValueError:  # pragma: no cover - defensive
            return Rejected(f"{host} resolved to something unreadable: {address!r}")
    return None


def _is_public(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Everything reserved, private, loopback, link-local or multicast is out.

    `is_global` covers most of it; the rest are spelled out because being wrong here
    is the whole vulnerability, and an explicit list is easier to audit than trust in
    one property.
    """
    return (
        address.is_global
        and not address.is_private
        and not address.is_loopback
        and not address.is_link_local
        and not address.is_multicast
        and not address.is_reserved
        and not address.is_unspecified
    )


# --------------------------------------------------------------------------- #
# Signing a delivery
# --------------------------------------------------------------------------- #


def sign_body(body: bytes, secret: str, timestamp: int) -> str:
    """HMAC over `timestamp.body`, not over the body alone.

    Without the timestamp inside the signed material, a delivery captured once can be
    replayed against the receiver forever and still verify. With it, a receiver that
    rejects old timestamps has a replay window it controls.
    """
    import hmac
    from hashlib import sha256

    material = f"{timestamp}.".encode() + body
    return hmac.new(secret.encode("utf-8"), material, sha256).hexdigest()


def build_headers(body: bytes, *, secret: str, timestamp: int | None = None) -> dict[str, str]:
    """The headers a receiver verifies against. Documented in README."""
    stamp = int(time.time()) if timestamp is None else timestamp
    return {
        "content-type": "application/json",
        "user-agent": "dogfood-webhooks/1.0",
        "x-dogfood-timestamp": str(stamp),
        "x-dogfood-signature": sign_body(body, secret, stamp),
    }


# --------------------------------------------------------------------------- #
# Delivery
# --------------------------------------------------------------------------- #


def _is_retryable_status(code: int) -> bool:
    """5xx is the receiver's own server telling us to try again; 4xx is the
    receiver's own server telling us this request will never succeed -- an
    organizer's endpoint rejecting a malformed payload should not be retried
    three times before saying so."""
    return code >= 500


def deliver(
    db: Session,
    hook: Webhook,
    topic: WebhookEvent | str,
    data: dict[str, Any],
) -> WebhookDelivery:
    """Attempt a delivery, retrying transient failures with bounded backoff,
    and record the outcome. Never raises.

    A webhook failing must not fail the thing that triggered it -- a vote is cast
    whether or not somebody's Slack relay was up -- so every error path here ends in
    a stored `WebhookDelivery` rather than an exception.

    One `WebhookDelivery` row per call, not one per attempt: `attempts` on that
    row is how many times this delivery actually tried before landing on its
    final status, which is the number an organizer wants ("delivered on the
    2nd attempt"), not a separate row per retry cluttering the history for
    what was, from the outside, one notification. A target refused by
    `check_target` is never retried -- an SSRF refusal does not become less
    true on a second attempt -- and only `_is_retryable_status` failures and
    network-level exceptions (timeouts, connection errors, DNS failures) get
    another attempt; a 4xx response is the receiver's own answer and retrying
    it would not change it.
    """
    topic_value = topic.value if isinstance(topic, WebhookEvent) else str(topic)
    payload = {
        "topic": topic_value,
        "event": str(hook.event_id),
        "delivered_at": utcnow().isoformat(),
        "data": data,
    }
    body = json.dumps(payload, separators=(",", ":"), default=str).encode()

    delivery = WebhookDelivery(
        webhook_id=hook.id,
        event_id=hook.event_id,
        topic=topic_value,
        payload=body.decode("utf-8", "replace"),
        status=DeliveryStatus.PENDING,
        attempts=0,
    )
    db.add(delivery)

    rejected = check_target(hook.url)
    if rejected is not None:
        # Re-checked at delivery time, because DNS can be re-pointed after the hook
        # was registered. BLOCKED rather than FAILED: an organizer needs to tell
        # "we refused to call that" from "your endpoint said no". Never retried.
        delivery.attempts = 1
        delivery.status = DeliveryStatus.BLOCKED
        delivery.error = rejected.reason[:MAX_ERROR_CHARS]
        _record_failure(db, hook)
        db.commit()
        return delivery

    request = urllib.request.Request(
        hook.url, data=body, method="POST", headers=build_headers(body, secret=hook.secret)
    )
    for attempt in range(1, MAX_DELIVERY_ATTEMPTS + 1):
        delivery.attempts = attempt
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
                delivery.status = DeliveryStatus.DELIVERED
                delivery.response_code = response.status
                delivery.delivered_at = utcnow()
                hook.failure_count = 0
                hook.last_delivery_at = delivery.delivered_at
                db.commit()
                return delivery
        except urllib.error.HTTPError as exc:
            delivery.status = DeliveryStatus.FAILED
            delivery.response_code = exc.code
            delivery.error = f"HTTP {exc.code}"[:MAX_ERROR_CHARS]
            retryable = _is_retryable_status(exc.code)
        except Exception as exc:  # noqa: BLE001 - a hook must never break the caller
            delivery.status = DeliveryStatus.FAILED
            delivery.error = f"{type(exc).__name__}: {exc}"[:MAX_ERROR_CHARS]
            retryable = True  # timeouts, connection refused, DNS failures, etc.

        if retryable and attempt < MAX_DELIVERY_ATTEMPTS:
            time.sleep(RETRY_BACKOFF_SECONDS[attempt - 1])
            continue
        break

    _record_failure(db, hook)
    db.commit()
    return delivery


def _record_failure(db: Session, hook: Webhook) -> None:
    hook.failure_count = (hook.failure_count or 0) + 1
    hook.last_delivery_at = utcnow()
    if hook.failure_count >= MAX_FAILURES:
        hook.is_active = False


def emit(db: Session, event_id, topic: WebhookEvent, data: dict[str, Any]) -> int:
    """Fan a topic out to every active hook on an event that asked for it.

    Returns the number of deliveries attempted. Called from routes *after* their own
    commit, so a webhook can never be the reason a submission was not saved.
    """
    from sqlalchemy import select

    hooks = (
        db.execute(
            select(Webhook).where(
                Webhook.event_id == event_id, Webhook.is_active.is_(True)
            )
        )
        .scalars()
        .all()
    )
    sent = 0
    for hook in hooks:
        if not hook.wants(topic):
            continue
        deliver(db, hook, topic, data)
        sent += 1
    return sent


# --------------------------------------------------------------------------- #
# Emitting from a route
# --------------------------------------------------------------------------- #


def schedule(background, db: Session, event_id, topic: WebhookEvent, data: dict[str, Any]) -> int:
    """Fan a topic out *after* the response, never during it.

    Two rules, both learned from how this goes wrong:

    1. **The triggering action must not wait.** A dead endpoint costs
       `TIMEOUT_SECONDS` per hook, and a judge submitting a ballot should not pay for
       an organizer's broken Slack relay. So delivery runs in a background task.
    2. **The background task opens its own session.** The request's session is closed
       once the response is sent; reusing it here would be a use-after-close that only
       shows up under load.

    Returns the number of hooks scheduled, so a caller can assert on it. The
    existence check is a cheap synchronous query, which means an event with no hooks
    -- the overwhelmingly common case -- schedules nothing at all.
    """
    from sqlalchemy import select

    hook_ids = [
        row
        for row in db.execute(
            select(Webhook.id).where(
                Webhook.event_id == event_id, Webhook.is_active.is_(True)
            )
        ).scalars()
    ]
    if not hook_ids:
        return 0
    background.add_task(_deliver_later, [str(h) for h in hook_ids], topic.value, data)
    return len(hook_ids)


def _deliver_later(hook_ids: list[str], topic: str, data: dict[str, Any]) -> None:
    """Background worker. Swallows everything.

    A webhook is a courtesy to an integration. Nothing it does -- including failing in
    a way we did not anticipate -- may affect the request that triggered it, and by
    the time this runs that response has already been sent, so there is nobody to
    raise to.
    """
    from .db import SessionLocal

    db = SessionLocal()
    try:
        from sqlalchemy import select

        hooks = (
            db.execute(select(Webhook).where(Webhook.id.in_([uuid.UUID(h) for h in hook_ids])))
            .scalars()
            .all()
        )
        for hook in hooks:
            if not hook.is_active:
                continue
            try:
                if hook.wants(WebhookEvent(topic)) if topic in _TOPIC_VALUES else True:
                    deliver(db, hook, topic, data)
            except Exception:  # noqa: BLE001 - see the docstring
                continue
    except Exception:  # noqa: BLE001
        pass
    finally:
        db.close()


_TOPIC_VALUES = {t.value for t in WebhookEvent}
