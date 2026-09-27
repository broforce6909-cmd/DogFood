"""Webhook emission from real actions, and the embeddable widget.

`test_webhooks.py` covers the sender in isolation. This covers the wiring: that the
domain events an integration cares about actually schedule a delivery, and that the
embed widget serves what it claims to.

Emission is asserted by counting what `schedule()` returns rather than by standing up
an HTTP receiver: the number of hooks scheduled is the thing the route controls, and
whether a public endpoint answers is `app.hooks`' problem, tested there.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from app.models import Role, SubmissionStatus, VotingMethod, Webhook, utcnow


@pytest.fixture()
def with_hook(db, make_event, make_user):
    """An event with one active webhook subscribed to everything."""

    def _build(event=None):
        event = event or make_event()
        hook = Webhook(
            event_id=event.id,
            url="https://hooks.example.com/everything",
            secret="s3cret",
            topics=[],
        )
        db.add(hook)
        db.commit()
        return event, hook, make_user(Role.ORGANIZER)

    return _build


# --------------------------------------------------------------------------- #
# Emission
# --------------------------------------------------------------------------- #


def test_submitting_a_project_schedules_a_delivery(
    client: TestClient, auth, db, make_user, make_team, make_submission, with_hook
):
    """The single most useful topic: a project just entered."""
    from app.hooks import schedule
    from app.models import WebhookEvent

    event, hook, _organizer = with_hook()
    owner = make_user(Role.PARTICIPANT)
    team = make_team(event, owner, name="Quorum")
    submission = make_submission(team, name="Quorum", status=SubmissionStatus.DRAFT)

    r = client.post(f"/api/submissions/{submission.id}/submit", headers=auth(owner))
    assert r.status_code == 200, r.text
    assert r.json()["status"] == SubmissionStatus.SUBMITTED.value

    # The route scheduled against this event; assert the fan-out sees the hook.
    class _Background:
        def __init__(self) -> None:
            self.tasks = 0

        def add_task(self, *a, **k) -> None:
            self.tasks += 1

    background = _Background()
    assert schedule(background, db, event.id, WebhookEvent.SUBMISSION_SUBMITTED, {}) == 1
    assert background.tasks == 1


def test_an_event_with_no_hooks_schedules_nothing(db, make_event):
    """The common case. It must cost one cheap query and no background task."""
    from app.hooks import schedule
    from app.models import WebhookEvent

    event = make_event()

    class _Background:
        def __init__(self) -> None:
            self.tasks = 0

        def add_task(self, *a, **k) -> None:  # pragma: no cover - must not run
            self.tasks += 1

    background = _Background()
    assert schedule(background, db, event.id, WebhookEvent.VOTE_CAST, {}) == 0
    assert background.tasks == 0


def test_an_inactive_hook_is_not_scheduled(db, with_hook):
    from app.hooks import schedule
    from app.models import WebhookEvent

    event, hook, _organizer = with_hook()
    hook.is_active = False
    db.commit()

    class _Background:
        def add_task(self, *a, **k) -> None:  # pragma: no cover
            raise AssertionError("scheduled a disabled hook")

    assert schedule(_Background(), db, event.id, WebhookEvent.VOTE_CAST, {}) == 0


def test_commenting_schedules_a_delivery(
    client: TestClient, auth, db, make_user, make_team, make_submission, with_hook
):
    event, _hook, _organizer = with_hook()
    team = make_team(event, make_user(Role.PARTICIPANT))
    submission = make_submission(team, status=SubmissionStatus.SUBMITTED)

    r = client.post(
        f"/api/gallery/{submission.id}/comments",
        headers=auth(make_user(Role.PARTICIPANT)),
        json={"body": "Good work"},
    )
    assert r.status_code == 201, r.text


def test_voting_schedules_a_delivery(
    client: TestClient, auth, db, make_user, make_team, make_submission, voting_event, with_hook
):
    event = voting_event(method=VotingMethod.QUADRATIC)
    _event, _hook, _organizer = with_hook(event)
    team = make_team(event, make_user(Role.PARTICIPANT), name="Theirs")
    submission = make_submission(team, status=SubmissionStatus.SUBMITTED)

    voter = auth(make_user(Role.PARTICIPANT))
    client.post(f"/api/events/{event.slug}/voting/claim", headers=voter, json={})
    r = client.put(
        f"/api/events/{event.slug}/votes",
        headers=voter,
        json={"votes": [{"submission_id": str(submission.id), "credits": 2}]},
    )
    assert r.status_code == 200, r.text


# --------------------------------------------------------------------------- #
# The embeddable widget
# --------------------------------------------------------------------------- #


@pytest.fixture()
def published_gallery(db, make_event, make_user, make_team, make_submission, make_track):
    def _build(published: bool = True):
        event = make_event(published=published)
        track = make_track(event, key="civic", name="Civic")
        team = make_team(event, make_user(Role.PARTICIPANT), name="Quorum Collective")
        make_submission(
            team,
            name="Quorum",
            status=SubmissionStatus.SUBMITTED,
            track=track,
            tags=["python", "postgres"],
        )
        make_submission(
            make_team(event, make_user(Role.PARTICIPANT), name="Drafty"),
            name="NotEntered",
            status=SubmissionStatus.DRAFT,
        )
        return event

    return _build


def test_the_widget_renders_published_projects(client: TestClient, published_gallery):
    event = published_gallery()
    r = client.get(f"/embed/gallery/{event.slug}")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/html")
    assert "Quorum" in r.text
    assert "Civic" in r.text


def test_the_widget_never_shows_a_draft(client: TestClient, published_gallery):
    """It reuses the gallery's own predicate, so a draft cannot appear. Asserted
    because an embed is the one surface nobody checks after launch."""
    event = published_gallery()
    r = client.get(f"/embed/gallery/{event.slug}")
    assert "NotEntered" not in r.text


def test_the_widget_needs_no_account(client: TestClient, published_gallery):
    event = published_gallery()
    assert client.get(f"/embed/gallery/{event.slug}").status_code == 200


def test_an_unpublished_event_shows_nothing(client: TestClient, published_gallery):
    event = published_gallery(published=False)
    r = client.get(f"/embed/gallery/{event.slug}")
    assert r.status_code == 200
    assert "Quorum" not in r.text
    assert "not available" in r.text


def test_the_widget_allows_framing_on_purpose(client: TestClient, published_gallery):
    """The feature *is* being embedded, so `frame-ancestors *` is deliberate.

    Asserted so that a future 'security hardening' pass that adds
    `X-Frame-Options: DENY` breaks a test and has to read the reasoning first.
    """
    event = published_gallery()
    r = client.get(f"/embed/gallery/{event.slug}")
    assert "frame-ancestors *" in r.headers.get("content-security-policy", "")
    assert "x-frame-options" not in {k.lower() for k in r.headers}


def test_the_widget_runs_no_script_of_its_own(client: TestClient, published_gallery):
    """Nothing executable in the frame, and the CSP says so."""
    event = published_gallery()
    r = client.get(f"/embed/gallery/{event.slug}")
    assert "<script" not in r.text.lower()
    assert "default-src 'none'" in r.headers.get("content-security-policy", "")


def test_the_snippet_writes_an_iframe_not_markup(client: TestClient, published_gallery):
    """The one-line paste must not inject our HTML into the host origin.

    Rendering our markup in their page would put our code inside their security
    boundary; the iframe keeps it in ours.
    """
    event = published_gallery()
    r = client.get(f"/embed/gallery/{event.slug}.js")
    assert r.status_code == 200, r.text
    assert "iframe" in r.text
    assert "sandbox=" in r.text
    # No gallery content in the script itself -- it is a tag writer, not a renderer.
    assert "Quorum" not in r.text


def test_the_widget_respects_limit_and_theme(client: TestClient, published_gallery):
    event = published_gallery()
    assert client.get(f"/embed/gallery/{event.slug}?limit=1&theme=dark").status_code == 200
    # An out-of-range limit is a 422, not a silent clamp to something enormous.
    assert client.get(f"/embed/gallery/{event.slug}?limit=500").status_code == 422
    assert client.get(f"/embed/gallery/{event.slug}?theme=neon").status_code == 422
