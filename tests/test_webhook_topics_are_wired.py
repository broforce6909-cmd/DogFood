"""Every documented webhook topic actually gets scheduled by something.

`GET /api/events/{slug}/webhooks/topics` lists all thirteen `WebhookEvent`
members as valid, subscribable topics -- but until this file's fixes, six of them
(`submission.updated`, `team.created`, `judge.invited`, `assignments.created`,
`results.published`, `ballot.completed`) were never once passed to
`hooks.schedule()` anywhere in the application. An organizer who subscribed to one
of those six got a webhook that silently never fired: no error, no delivery, no
way to discover the gap short of reading the router source. Found by checking every
member of the enum against every real call site, the same way the six dead
`AuditAction` members were found earlier in this project's Phase 5 self-audit.

Each test here monkeypatches the `schedule` name *as imported into the router
module* (not `app.hooks.schedule` -- the import already bound a local reference by
the time these tests run, so patching the origin would not affect the routers) and
asserts the specific topic used, rather than asserting a real delivery happened --
that end-to-end path is `test_webhooks.py`'s job. This file's job is narrower and
was the actual gap: proving the *call* happens at all.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from app.models import AssignmentStatus, Role, SubmissionStatus, VotingAccess, utcnow


class _Recorder:
    """Stands in for `hooks.schedule`. Records the topic of every call."""

    def __init__(self) -> None:
        self.topics: list[str] = []

    def __call__(self, background, db, event_id, topic, data):
        self.topics.append(topic.value if hasattr(topic, "value") else str(topic))
        return 1


@pytest.fixture()
def recorder(monkeypatch):
    """Patches `schedule` in every router that should be calling it, and hands
    back one recorder shared across all of them."""
    rec = _Recorder()
    for module in (
        "app.routers.submissions",
        "app.routers.teams",
        "app.routers.judges",
        "app.routers.voting",
        "app.routers.judging",
        "app.routers.comments",
        "app.routers.certificates",
        "app.routers.registrations",
        "app.routers.announcements",
        "app.routers.results",
    ):
        monkeypatch.setattr(f"{module}.schedule", rec)
    return rec


def test_submission_updated_fires_on_editing_a_submitted_project(
    client: TestClient, auth, recorder, make_user, make_event, make_team, make_submission
):
    event = make_event()
    owner = make_user(Role.PARTICIPANT)
    team = make_team(event, owner)
    submission = make_submission(team, status=SubmissionStatus.SUBMITTED)

    r = client.patch(
        f"/api/submissions/{submission.id}",
        headers=auth(owner),
        json={"tagline": "an update"},
    )
    assert r.status_code == 200, r.text
    assert "submission.updated" in recorder.topics, recorder.topics


def test_submission_updated_does_not_fire_for_an_unsubmitted_draft(
    client: TestClient, auth, recorder, make_user, make_event, make_team, make_submission
):
    """A draft's edits are noise an integration would want filtered back out --
    firing on every keystroke of an unsubmitted project would make the topic
    worse than useless."""
    event = make_event()
    owner = make_user(Role.PARTICIPANT)
    team = make_team(event, owner)
    submission = make_submission(team, status=SubmissionStatus.DRAFT)

    r = client.patch(
        f"/api/submissions/{submission.id}",
        headers=auth(owner),
        json={"tagline": "still drafting"},
    )
    assert r.status_code == 200, r.text
    assert "submission.updated" not in recorder.topics, recorder.topics


def test_team_created_fires(client: TestClient, auth, recorder, make_user, make_event):
    event = make_event()
    owner = make_user(Role.PARTICIPANT)

    r = client.post(
        f"/api/events/{event.slug}/teams", headers=auth(owner), json={"name": "New Team"}
    )
    assert r.status_code == 201, r.text
    assert "team.created" in recorder.topics, recorder.topics


def test_registration_created_fires(client: TestClient, auth, recorder, make_user, make_event):
    event = make_event()
    r = client.post(
        f"/api/events/{event.slug}/register",
        headers=auth(make_user(Role.PARTICIPANT)),
        json={"discord_username": "newcomer"},
    )
    assert r.status_code == 201, r.text
    assert "registration.created" in recorder.topics, recorder.topics


def test_announcement_posted_fires(client: TestClient, auth, recorder, make_user, make_event):
    event = make_event()
    r = client.post(
        f"/api/events/{event.slug}/announcements",
        headers=auth(make_user(Role.ORGANIZER)),
        json={"title": "Lunch moved", "body": "Lunch is now in the atrium."},
    )
    assert r.status_code == 201, r.text
    assert "announcement.posted" in recorder.topics, recorder.topics


def test_results_finalized_fires(
    client: TestClient, auth, recorder, make_user, make_event, make_team, make_submission
):
    event = make_event()
    submission = make_submission(make_team(event, make_user()))
    organizer = make_user(Role.ORGANIZER)
    r = client.put(
        f"/api/events/{event.slug}/results/overrides/{submission.id}",
        headers=auth(organizer),
        json={"tier": "winner", "reason": "Manual correction"},
    )
    assert r.status_code == 200, r.text
    assert "results.finalized" in recorder.topics, recorder.topics


def test_judge_invited_fires(client: TestClient, auth, recorder, make_user, make_event):
    event = make_event()
    organizer = make_user(Role.ORGANIZER)
    candidate = make_user(Role.PARTICIPANT, email="future-judge@example.com")

    r = client.post(
        f"/api/events/{event.slug}/judges",
        headers=auth(organizer),
        json={"email": candidate.email},
    )
    assert r.status_code == 201, r.text
    assert "judge.invited" in recorder.topics, recorder.topics


def test_assignments_created_fires_on_a_real_run_but_not_a_dry_run(
    client: TestClient, auth, recorder, make_user, make_event, make_team,
    make_submission, make_judge,
):
    event = make_event()
    organizer = make_user(Role.ORGANIZER)
    make_judge(event, make_user(Role.JUDGE))
    team = make_team(event, make_user(Role.PARTICIPANT))
    make_submission(team, status=SubmissionStatus.SUBMITTED)

    dry = client.post(
        f"/api/events/{event.slug}/assignments",
        headers=auth(organizer),
        json={"reviews_per_submission": 1, "dry_run": True},
    )
    assert dry.status_code == 200, dry.text
    assert "assignments.created" not in recorder.topics, "a dry run must not notify"

    real = client.post(
        f"/api/events/{event.slug}/assignments",
        headers=auth(organizer),
        json={"reviews_per_submission": 1, "dry_run": False},
    )
    assert real.status_code == 200, real.text
    assert "assignments.created" in recorder.topics, recorder.topics


def test_results_published_fires_both_ways(
    client: TestClient, auth, recorder, make_user, voting_event
):
    event = voting_event(access=VotingAccess.AUTHENTICATED)
    organizer = make_user(Role.ORGANIZER)

    published = client.post(
        f"/api/events/{event.slug}/voting/publish",
        headers=auth(organizer),
        params={"public": "true"},
    )
    assert published.status_code == 200, published.text
    assert recorder.topics.count("results.published") == 1

    unpublished = client.post(
        f"/api/events/{event.slug}/voting/publish",
        headers=auth(organizer),
        params={"public": "false"},
    )
    assert unpublished.status_code == 200, unpublished.text
    assert recorder.topics.count("results.published") == 2


def test_ballot_completed_fires_once_on_the_transition_to_complete(
    client: TestClient, auth, db, recorder, make_user, make_event, make_team,
    make_submission, make_judge, make_assignment, make_criterion,
):
    event = make_event()
    event.judging_opens_at = utcnow() - timedelta(hours=1)
    event.judging_closes_at = utcnow() + timedelta(days=1)
    db.commit()
    criterion = make_criterion(event, key="overall", weight=1)
    judge_user = make_user(Role.JUDGE)
    judge = make_judge(event, judge_user)
    team = make_team(event, make_user(Role.PARTICIPANT))
    submission = make_submission(team, status=SubmissionStatus.SUBMITTED)
    ballot = make_assignment(judge, submission, status=AssignmentStatus.PENDING)

    body = {"scores": [{"criterion_id": str(criterion.id), "value": 4}], "complete": True}
    first = client.put(
        f"/api/judging/assignments/{ballot.id}/scores", headers=auth(judge_user), json=body
    )
    assert first.status_code == 200, first.text
    assert recorder.topics.count("ballot.completed") == 1, recorder.topics

    # Re-saving an already-complete ballot (a tweak to a comment, say) must not
    # re-notify an integration that already heard this ballot finished.
    second = client.put(
        f"/api/judging/assignments/{ballot.id}/scores", headers=auth(judge_user), json=body
    )
    assert second.status_code == 200, second.text
    assert recorder.topics.count("ballot.completed") == 1, recorder.topics


def test_every_documented_topic_is_reachable_from_some_route(client: TestClient) -> None:
    """The other half of the property: not just that six specific topics now
    fire, but that the full advertised list and the set this file (plus
    `test_webhooks.py`'s existing coverage of `vote.cast`, `comment.posted`,
    `submission.submitted` and `certificate.issued`) exercises are the same
    set. If a future topic is added to the enum and never wired, this test
    does not catch it by itself -- but it pins the current thirteen so a silent
    regression on any of them is at least visible in a diff here.
    """
    topics = set(client.get("/api/events/any-slug/webhooks/topics").json())
    assert topics == {
        "submission.submitted",
        "submission.updated",
        "team.created",
        "judge.invited",
        "assignments.created",
        "ballot.completed",
        "vote.cast",
        "comment.posted",
        "results.published",
        "certificate.issued",
        "registration.created",
        "announcement.posted",
        "results.finalized",
    }
