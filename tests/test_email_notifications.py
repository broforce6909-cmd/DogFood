"""Email notifications actually get scheduled: registration confirmation and
the results-published broadcast.

Same shape as `test_webhook_topics_are_wired.py` and for the same reason: a
webhook or an email topic that nothing schedules is a promise, not a
feature. `schedule_email` is patched at the point each router imported it,
not at `app.email.schedule_email` -- the import already bound a local name
by the time these tests run.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.models import Role, utcnow


class _Recorder:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    def __call__(self, background, *, to, subject, body):
        self.sent.append({"to": to, "subject": subject, "body": body})


@pytest.fixture()
def email_recorder(monkeypatch):
    rec = _Recorder()
    for module in ("app.routers.registrations", "app.routers.voting"):
        monkeypatch.setattr(f"{module}.schedule_email", rec)
    return rec


def test_registering_sends_a_confirmation_email(
    client: TestClient, auth, make_event, make_user, email_recorder
) -> None:
    event = make_event()
    user = make_user(email="priya@example.com", name="Priya Rivera")
    response = client.post(
        f"/api/events/{event.slug}/register",
        json={"discord_username": "priyar"},
        headers=auth(user),
    )
    assert response.status_code == 201, response.text
    assert len(email_recorder.sent) == 1
    assert email_recorder.sent[0]["to"] == "priya@example.com"
    assert event.name in email_recorder.sent[0]["subject"]
    assert "priyar" in email_recorder.sent[0]["body"]


def test_registration_succeeds_even_if_the_smtp_send_itself_fails(
    client: TestClient, auth, make_event, make_user, monkeypatch
) -> None:
    """The real `schedule_email` (unpatched) queues a `BackgroundTasks`
    callback that runs *after* the response is built -- so a `send_email`
    that fails at the SMTP layer must not turn into a failed registration.
    `send_email` already never raises on its own (see `test_email.py`); this
    proves the registration route does not additionally depend on it
    succeeding."""

    def _failing_send(*, to, subject, body):
        return False  # exactly what a real SMTP failure returns, never raises

    monkeypatch.setattr("app.email.send_email", _failing_send)
    event = make_event()
    response = client.post(
        f"/api/events/{event.slug}/register",
        json={"discord_username": "priyar"},
        headers=auth(make_user()),
    )
    assert response.status_code == 201, response.text


# --------------------------------------------------------------------------- #
# Results-published broadcast
# --------------------------------------------------------------------------- #


def test_publishing_results_notifies_every_registrant(
    client: TestClient, auth, make_event, make_user, make_registration, email_recorder
) -> None:
    event = make_event()
    now = utcnow()
    make_registration(event, make_user(email="a@example.com"), registered_at=now)
    make_registration(event, make_user(email="b@example.com"), registered_at=now)

    organizer = make_user(Role.ORGANIZER)
    response = client.post(
        f"/api/events/{event.slug}/voting/publish", headers=auth(organizer)
    )
    assert response.status_code == 200, response.text
    recipients = {sent["to"] for sent in email_recorder.sent}
    assert recipients == {"a@example.com", "b@example.com"}
    assert event.name in email_recorder.sent[0]["subject"]


def test_republishing_results_does_not_resend_the_notification(
    client: TestClient, auth, make_event, make_user, make_registration, email_recorder
) -> None:
    event = make_event()
    make_registration(event, make_user(email="a@example.com"), registered_at=utcnow())
    organizer = make_user(Role.ORGANIZER)
    headers = auth(organizer)

    client.post(f"/api/events/{event.slug}/voting/publish", headers=headers)
    assert len(email_recorder.sent) == 1

    client.post(f"/api/events/{event.slug}/voting/publish", headers=headers)
    assert len(email_recorder.sent) == 1  # still just the one, from the first publish


def test_unpublishing_results_sends_nothing(
    client: TestClient, auth, make_event, make_user, make_registration, email_recorder
) -> None:
    event = make_event()
    make_registration(event, make_user(email="a@example.com"), registered_at=utcnow())
    organizer = make_user(Role.ORGANIZER)

    response = client.post(
        f"/api/events/{event.slug}/voting/publish?public=false", headers=auth(organizer)
    )
    assert response.status_code == 200, response.text
    assert email_recorder.sent == []


def test_publishing_with_no_registrants_sends_nothing_and_does_not_error(
    client: TestClient, auth, make_event, make_user, email_recorder
) -> None:
    event = make_event()
    response = client.post(
        f"/api/events/{event.slug}/voting/publish", headers=auth(make_user(Role.ORGANIZER))
    )
    assert response.status_code == 200, response.text
    assert email_recorder.sent == []
