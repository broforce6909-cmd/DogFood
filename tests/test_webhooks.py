"""Webhooks: the new attack surface, and the rules that contain it.

Written before the feature. Outbound HTTP that an *organizer* controls the target
of is the most dangerous thing added in T4, because it turns the API into a
request-forging proxy sitting inside the Docker network — one hop from Postgres and
from any other service on the host.

So the first and longest section here is SSRF, and it is deny-by-default: the URL
is resolved and checked against private ranges before a request is ever made.
An organizer is trusted to run the event, not to aim the server at `169.254.169.254`.

The second property is integrity in the other direction: a receiver must be able to
tell a genuine delivery from anything else, which means a signature over the body
and a timestamp that makes replay detectable.
"""

from __future__ import annotations

import json
import time

import pytest
from fastapi.testclient import TestClient

from app.models import Role, SubmissionStatus


@pytest.fixture()
def hooked(db, make_event, make_user):
    def _build():
        event = make_event()
        return event, make_user(Role.ORGANIZER)

    return _build


def register(client: TestClient, slug: str, headers, url: str, **extra):
    return client.post(
        f"/api/events/{slug}/webhooks",
        headers=headers,
        json={"url": url, **extra},
    )


# --------------------------------------------------------------------------- #
# SSRF: the addresses we refuse to call
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8000/hook",
        "http://localhost:8000/hook",
        "http://[::1]:8000/hook",
        "http://10.0.0.5/hook",
        "http://192.168.1.10/hook",
        "http://172.16.0.9/hook",
        # Link-local, which on a cloud host is the instance metadata service and the
        # single most valuable target for an SSRF.
        "http://169.254.169.254/latest/meta-data/",
        "http://0.0.0.0/hook",
        # The Docker-network names this very stack uses. An organizer pointing a hook
        # at `db` would make the API talk to Postgres for them.
        "http://db:5432/hook",
        "http://api:8000/hook",
    ],
)
def test_a_hook_cannot_target_a_private_address(
    client: TestClient, auth, hooked, url: str
):
    event, organizer = hooked()
    r = register(client, event.slug, auth(organizer), url)
    assert r.status_code == 422, f"{url} was accepted: {r.status_code} {r.text[:120]}"
    assert "private" in r.text.lower() or "internal" in r.text.lower()


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "gopher://example.com/hook",
        "ftp://example.com/hook",
        "ws://example.com/hook",
        # No scheme at all.
        "example.com/hook",
    ],
)
def test_a_hook_must_be_http_or_https(client: TestClient, auth, hooked, url: str):
    """Anything but HTTP(S) is either not a request we can make or not one we should."""
    event, organizer = hooked()
    r = register(client, event.slug, auth(organizer), url)
    assert r.status_code == 422, f"{url} was accepted"


def test_a_public_https_hook_is_accepted(client: TestClient, auth, hooked):
    """The control: the rule rejects private targets, not all targets."""
    event, organizer = hooked()
    r = register(
        client,
        event.slug,
        auth(organizer),
        "https://hooks.example.com/dogfood",
        description="Slack relay",
    )
    assert r.status_code == 201, r.text
    assert r.json()["url"] == "https://hooks.example.com/dogfood"


def test_the_secret_is_shown_once_on_creation(client: TestClient, auth, hooked):
    """A receiver needs it to verify signatures, so it is returned on create --
    and not again on list, where it would be an unnecessary second exposure."""
    event, organizer = hooked()
    headers = auth(organizer)
    created = register(client, event.slug, headers, "https://hooks.example.com/a")
    assert created.json()["secret"]

    listed = client.get(f"/api/events/{event.slug}/webhooks", headers=headers).json()
    assert listed[0].get("secret") in (None, "")


# --------------------------------------------------------------------------- #
# Authorization
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("role", [Role.PARTICIPANT, Role.JUDGE])
def test_only_staff_manage_webhooks(client: TestClient, auth, make_user, hooked, role):
    """A hook is an outbound request with our network position. Staff only."""
    event, _organizer = hooked()
    headers = auth(make_user(role))
    assert register(client, event.slug, headers, "https://hooks.example.com/x").status_code == 403
    assert client.get(f"/api/events/{event.slug}/webhooks", headers=headers).status_code == 403


def test_an_anonymous_caller_cannot_list_webhooks(client: TestClient, hooked):
    event, _organizer = hooked()
    assert client.get(f"/api/events/{event.slug}/webhooks").status_code == 403


def test_a_duplicate_url_on_one_event_is_rejected(client: TestClient, auth, hooked):
    event, organizer = hooked()
    headers = auth(organizer)
    url = "https://hooks.example.com/dup"
    assert register(client, event.slug, headers, url).status_code == 201
    assert register(client, event.slug, headers, url).status_code == 409


def test_unknown_topics_are_rejected(client: TestClient, auth, hooked):
    """A typo'd topic is a subscription that silently never fires, so it is a 422
    rather than a quietly accepted no-op."""
    event, organizer = hooked()
    r = register(
        client,
        event.slug,
        auth(organizer),
        "https://hooks.example.com/t",
        topics=["submission.submited"],
    )
    assert r.status_code == 422
    assert "submited" in r.text or "topic" in r.text.lower()


# --------------------------------------------------------------------------- #
# Signing the delivery
# --------------------------------------------------------------------------- #


def test_a_delivery_is_signed_over_the_exact_body(client: TestClient, auth, hooked):
    """A receiver has to be able to tell a genuine delivery from a forged one.

    The headers are asserted here by inspecting what the sender *builds*, because
    the test suite has no public HTTP endpoint to receive a real delivery. The
    delivery attempt itself is covered by `test_a_blocked_target_is_recorded`.
    """
    from app.hooks import build_headers, sign_body

    body = json.dumps({"topic": "vote.cast", "data": {"n": 1}}).encode()
    stamp = int(time.time())
    headers = build_headers(body, secret="s3cret", timestamp=stamp)

    assert headers["content-type"] == "application/json"
    assert headers["x-dogfood-timestamp"] == str(stamp)
    # Signed over timestamp + body, not the body alone: without the timestamp in the
    # signed material, a captured delivery can be replayed forever.
    assert headers["x-dogfood-signature"] == sign_body(body, "s3cret", stamp)
    assert headers["x-dogfood-signature"] != sign_body(body, "s3cret", stamp + 1)


def test_a_different_secret_gives_a_different_signature() -> None:
    from app.hooks import sign_body

    body = b'{"a":1}'
    assert sign_body(body, "one", 1) != sign_body(body, "two", 1)


# --------------------------------------------------------------------------- #
# Delivery records
# --------------------------------------------------------------------------- #


def test_a_blocked_target_is_recorded_rather_than_attempted(
    client: TestClient, auth, db, hooked
):
    """A hook whose DNS resolves to a private address at *delivery* time must be
    refused then too -- registration-time validation is not enough, because DNS can
    be re-pointed afterwards. The attempt is recorded as BLOCKED, which an organizer
    needs to see as distinct from "your endpoint said no".
    """
    from app.hooks import deliver
    from app.models import DeliveryStatus, Webhook, WebhookEvent

    event, organizer = hooked()
    hook = Webhook(
        event_id=event.id,
        url="http://127.0.0.1:9/blackhole",  # bypasses the route's validation
        secret="s",
        topics=[],
    )
    db.add(hook)
    db.commit()

    delivery = deliver(db, hook, WebhookEvent.VOTE_CAST, {"hello": "world"})
    assert delivery.status is DeliveryStatus.BLOCKED
    assert "private" in (delivery.error or "").lower()
    assert delivery.response_code is None


def test_deliveries_are_listed_for_the_organizer(client: TestClient, auth, db, hooked):
    event, organizer = hooked()
    headers = auth(organizer)
    created = register(client, event.slug, headers, "https://hooks.example.com/d")
    hook_id = created.json()["id"]

    r = client.get(
        f"/api/events/{event.slug}/webhooks/{hook_id}/deliveries", headers=headers
    )
    assert r.status_code == 200, r.text
    assert isinstance(r.json()["items"], list)


def test_a_test_ping_is_recorded(client: TestClient, auth, hooked):
    """An organizer should be able to check their endpoint without waiting for a
    real event, and see the result."""
    event, organizer = hooked()
    headers = auth(organizer)
    created = register(client, event.slug, headers, "https://hooks.invalid/unreachable")
    hook_id = created.json()["id"]

    ping = client.post(
        f"/api/events/{event.slug}/webhooks/{hook_id}/test", headers=headers
    )
    assert ping.status_code == 200, ping.text
    # Unreachable host, so this records a failure rather than a success -- the point
    # is that it is recorded and reported, not that it succeeded.
    assert ping.json()["status"] in ("failed", "blocked", "delivered")

    deliveries = client.get(
        f"/api/events/{event.slug}/webhooks/{hook_id}/deliveries", headers=headers
    ).json()["items"]
    assert len(deliveries) == 1
    assert deliveries[0]["topic"] == "ping"


def test_a_dead_hook_is_eventually_disabled(client: TestClient, auth, db, hooked):
    """An organizer's dead endpoint must not slow every request for the rest of the
    weekend, so consecutive failures disable it."""
    from app.hooks import MAX_FAILURES, deliver
    from app.models import Webhook, WebhookEvent

    event, _organizer = hooked()
    hook = Webhook(event_id=event.id, url="http://10.1.1.1/dead", secret="s", topics=[])
    db.add(hook)
    db.commit()

    for _ in range(MAX_FAILURES):
        deliver(db, hook, WebhookEvent.VOTE_CAST, {})
    db.refresh(hook)
    assert hook.is_active is False
    assert hook.failure_count >= MAX_FAILURES


def test_a_blocked_target_is_never_retried(client: TestClient, auth, db, hooked):
    """Retrying an SSRF refusal would not make the target any less private --
    `attempts` must stay at 1 for a `BLOCKED` delivery."""
    from app.hooks import deliver
    from app.models import DeliveryStatus, Webhook, WebhookEvent

    event, _organizer = hooked()
    hook = Webhook(event_id=event.id, url="http://127.0.0.1:9/blackhole", secret="s", topics=[])
    db.add(hook)
    db.commit()

    delivery = deliver(db, hook, WebhookEvent.VOTE_CAST, {"hello": "world"})
    assert delivery.status is DeliveryStatus.BLOCKED
    assert delivery.attempts == 1


# --------------------------------------------------------------------------- #
# Retries with bounded backoff
# --------------------------------------------------------------------------- #


def test_a_5xx_response_is_retried_then_recorded_as_failed(
    client: TestClient, auth, db, hooked, monkeypatch
):
    """A receiver's own server error reads as transient: retried up to
    `MAX_DELIVERY_ATTEMPTS` times with backoff before this delivery gives up."""
    import urllib.error

    import app.hooks as hooks
    from app.models import DeliveryStatus, Webhook, WebhookEvent

    monkeypatch.setattr(hooks.time, "sleep", lambda _seconds: None)
    calls: list[int] = []

    def fake_urlopen(request, timeout):
        calls.append(1)
        raise urllib.error.HTTPError(request.full_url, 503, "Service Unavailable", {}, None)

    monkeypatch.setattr(hooks.urllib.request, "urlopen", fake_urlopen)

    event, _organizer = hooked()
    hook = Webhook(event_id=event.id, url="https://hooks.example.com/retry-5xx", secret="s", topics=[])
    db.add(hook)
    db.commit()

    delivery = hooks.deliver(db, hook, WebhookEvent.VOTE_CAST, {})
    assert delivery.status is DeliveryStatus.FAILED
    assert delivery.response_code == 503
    assert delivery.attempts == hooks.MAX_DELIVERY_ATTEMPTS
    assert len(calls) == hooks.MAX_DELIVERY_ATTEMPTS


def test_a_4xx_response_is_not_retried(client: TestClient, auth, db, hooked, monkeypatch):
    """The receiver already gave its answer -- retrying a 422 three times
    would not change it, so this delivery must stop after one attempt."""
    import urllib.error

    import app.hooks as hooks
    from app.models import DeliveryStatus, Webhook, WebhookEvent

    monkeypatch.setattr(hooks.time, "sleep", lambda _seconds: None)
    calls: list[int] = []

    def fake_urlopen(request, timeout):
        calls.append(1)
        raise urllib.error.HTTPError(request.full_url, 422, "Unprocessable", {}, None)

    monkeypatch.setattr(hooks.urllib.request, "urlopen", fake_urlopen)

    event, _organizer = hooked()
    hook = Webhook(event_id=event.id, url="https://hooks.example.com/retry-4xx", secret="s", topics=[])
    db.add(hook)
    db.commit()

    delivery = hooks.deliver(db, hook, WebhookEvent.VOTE_CAST, {})
    assert delivery.status is DeliveryStatus.FAILED
    assert delivery.response_code == 422
    assert delivery.attempts == 1
    assert len(calls) == 1


def test_a_receiver_back_up_on_retry_still_counts_as_delivered(
    client: TestClient, auth, db, hooked, monkeypatch
):
    """The whole point of retrying: a receiver that fails once and then
    succeeds must end this delivery as `DELIVERED`, with `attempts` showing
    it took two tries -- not silently recorded as if it worked the first time."""
    import urllib.error

    import app.hooks as hooks
    from app.models import DeliveryStatus, Webhook, WebhookEvent

    monkeypatch.setattr(hooks.time, "sleep", lambda _seconds: None)
    attempts = {"n": 0}

    class FakeResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

    def fake_urlopen(request, timeout):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise urllib.error.HTTPError(request.full_url, 503, "Service Unavailable", {}, None)
        return FakeResponse()

    monkeypatch.setattr(hooks.urllib.request, "urlopen", fake_urlopen)

    event, _organizer = hooked()
    hook = Webhook(
        event_id=event.id, url="https://hooks.example.com/retry-then-ok", secret="s", topics=[]
    )
    db.add(hook)
    db.commit()

    delivery = hooks.deliver(db, hook, WebhookEvent.VOTE_CAST, {})
    assert delivery.status is DeliveryStatus.DELIVERED
    assert delivery.attempts == 2
    assert attempts["n"] == 2
    db.refresh(hook)
    assert hook.failure_count == 0


# --------------------------------------------------------------------------- #
# Topic filtering
# --------------------------------------------------------------------------- #


def test_a_hook_only_receives_the_topics_it_asked_for(db, hooked) -> None:
    from app.models import Webhook, WebhookEvent

    event, _organizer = hooked()
    hook = Webhook(
        event_id=event.id,
        url="https://hooks.example.com/f",
        secret="s",
        topics=[WebhookEvent.VOTE_CAST.value],
    )
    assert hook.wants(WebhookEvent.VOTE_CAST) is True
    assert hook.wants(WebhookEvent.COMMENT_POSTED) is False


def test_an_empty_topic_list_means_everything(db, hooked) -> None:
    """The common case: one relay that wants the lot."""
    from app.models import Webhook, WebhookEvent

    event, _organizer = hooked()
    hook = Webhook(event_id=event.id, url="https://hooks.example.com/g", secret="s", topics=[])
    assert all(hook.wants(topic) for topic in WebhookEvent)
