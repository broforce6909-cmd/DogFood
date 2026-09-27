"""Pairwise judging over HTTP: access control, eligibility, and the fitted
ranking.

`tests/test_pairwise.py` proves the Bradley-Terry maths in isolation; this file
proves the three routes in `app/routers/pairwise.py` are wired to it correctly --
who may reach them, which projects a judge is actually shown, and that a
comparison a client fabricates outside what it was shown cannot be written.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from app.models import AuditAction, Role, SubmissionStatus, utcnow


@pytest.fixture()
def pairwise_event(db, make_event, make_user, make_team, make_submission, make_judge):
    """An event with pairwise judging on, a judging window open now, three
    submitted projects, and one judge with no track restriction."""

    def _build(*, n_submissions: int = 3):
        event = make_event()
        event.pairwise_enabled = True
        event.judging_opens_at = utcnow() - timedelta(hours=1)
        event.judging_closes_at = utcnow() + timedelta(days=1)
        db.commit()

        submissions = []
        for i in range(n_submissions):
            team = make_team(event, make_user(Role.PARTICIPANT), name=f"Team {i}")
            submissions.append(
                make_submission(team, name=f"Project {i}", status=SubmissionStatus.SUBMITTED)
            )

        judge_user = make_user(Role.JUDGE)
        judge = make_judge(event, judge_user)
        return event, submissions, judge_user, judge

    return _build


def next_pair(client: TestClient, slug: str, headers):
    return client.get(f"/api/events/{slug}/pairwise/next", headers=headers)


def compare(client: TestClient, slug: str, headers, a, b, winner=None):
    return client.post(
        f"/api/events/{slug}/pairwise/compare",
        headers=headers,
        json={
            "submission_a_id": str(a),
            "submission_b_id": str(b),
            "winner_id": str(winner) if winner else None,
        },
    )


# --------------------------------------------------------------------------- #
# Access control
# --------------------------------------------------------------------------- #


def test_a_non_judge_cannot_fetch_a_pair(client: TestClient, auth, make_user, pairwise_event):
    event, _subs, _judge_user, _judge = pairwise_event()
    r = next_pair(client, event.slug, auth(make_user(Role.PARTICIPANT)))
    assert r.status_code == 404


def test_an_anonymous_caller_cannot_fetch_a_pair(client: TestClient, pairwise_event):
    event, _subs, _judge_user, _judge = pairwise_event()
    assert next_pair(client, event.slug, {}).status_code == 401


def test_pairwise_disabled_refuses_a_judge_who_would_otherwise_be_eligible(
    client: TestClient, auth, db, pairwise_event
):
    event, _subs, judge_user, _judge = pairwise_event()
    event.pairwise_enabled = False
    db.commit()
    assert next_pair(client, event.slug, auth(judge_user)).status_code == 403


def test_outside_the_judging_window_is_refused(client: TestClient, auth, db, pairwise_event):
    event, _subs, judge_user, _judge = pairwise_event()
    event.judging_opens_at = utcnow() + timedelta(days=1)
    db.commit()
    assert next_pair(client, event.slug, auth(judge_user)).status_code == 403


def test_a_deactivated_judge_is_refused(client: TestClient, auth, db, pairwise_event):
    event, _subs, judge_user, judge = pairwise_event()
    judge.is_active = False
    db.commit()
    assert next_pair(client, event.slug, auth(judge_user)).status_code == 403


def test_an_organizer_cannot_compare_on_a_judges_behalf(
    client: TestClient, auth, make_user, pairwise_event
):
    """`_check_judge`'s COMPARE branch stops staff privilege here, the same way
    `_check_assignment`'s SCORE branch stops it for the rubric -- an organizer
    who could compare would make a forged ranking as easy as a forged score."""
    event, subs, _judge_user, _judge = pairwise_event()
    organizer = make_user(Role.ORGANIZER)
    assert next_pair(client, event.slug, auth(organizer)).status_code == 404
    r = compare(client, event.slug, auth(organizer), subs[0].id, subs[1].id, subs[0].id)
    assert r.status_code == 404


def test_results_are_staff_only(client: TestClient, auth, pairwise_event):
    event, _subs, judge_user, _judge = pairwise_event()
    assert (
        client.get(f"/api/events/{event.slug}/pairwise/results", headers=auth(judge_user)).status_code
        == 403
    )
    assert client.get(f"/api/events/{event.slug}/pairwise/results").status_code == 403


def test_organizer_can_read_results(client: TestClient, auth, make_user, pairwise_event):
    event, _subs, _judge_user, _judge = pairwise_event()
    organizer = make_user(Role.ORGANIZER)
    r = client.get(f"/api/events/{event.slug}/pairwise/results", headers=auth(organizer))
    assert r.status_code == 200, r.text


# --------------------------------------------------------------------------- #
# Eligibility: who gets shown to whom
# --------------------------------------------------------------------------- #


def test_a_judges_own_team_is_never_shown_to_them(
    client: TestClient, auth, make_user, make_team, make_submission, pairwise_event
):
    """Conflict of interest, enforced the same way `app/assignment.py` enforces
    it for scored assignment: a judge never sees their own team's project."""
    event, subs, judge_user, judge = pairwise_event(n_submissions=2)
    # Make the judge a member of a *third* team, with its own submission --
    # that one must never appear, while the two pre-existing ones may.
    own_team = make_team(event, judge_user)
    own_submission = make_submission(
        own_team, name="Judge's Own Project", status=SubmissionStatus.SUBMITTED
    )

    headers = auth(judge_user)
    seen_ids = set()
    for _ in range(20):
        r = next_pair(client, event.slug, headers)
        assert r.status_code == 200, r.text
        body = r.json()
        seen_ids.add(body["submission_a"]["id"])
        seen_ids.add(body["submission_b"]["id"])
    assert str(own_submission.id) not in seen_ids
    assert seen_ids == {str(s.id) for s in subs}


def test_a_track_judge_only_sees_their_own_track(
    client: TestClient, auth, make_track, make_team, make_user, make_submission, make_judge,
    pairwise_event,
):
    event, _subs, _judge_user, _judge = pairwise_event(n_submissions=0)
    civic = make_track(event, key="civic", name="Civic")
    other = make_track(event, key="hardware", name="Hardware")

    civic_team = make_team(event, make_user(Role.PARTICIPANT))
    civic_submission = make_submission(
        civic_team, name="Civic Project", status=SubmissionStatus.SUBMITTED, track=civic
    )
    other_team = make_team(event, make_user(Role.PARTICIPANT))
    make_submission(
        other_team, name="Hardware Project", status=SubmissionStatus.SUBMITTED, track=other
    )
    # A second civic project, so the track judge has >= 2 eligible projects.
    civic_team_2 = make_team(event, make_user(Role.PARTICIPANT))
    civic_submission_2 = make_submission(
        civic_team_2, name="Civic Project 2", status=SubmissionStatus.SUBMITTED, track=civic
    )

    track_judge_user = make_user(Role.JUDGE)
    make_judge(event, track_judge_user, track=civic)

    headers = auth(track_judge_user)
    seen_ids = set()
    for _ in range(20):
        r = next_pair(client, event.slug, headers)
        assert r.status_code == 200, r.text
        body = r.json()
        seen_ids.add(body["submission_a"]["id"])
        seen_ids.add(body["submission_b"]["id"])
    assert seen_ids == {str(civic_submission.id), str(civic_submission_2.id)}


def test_only_submitted_projects_are_eligible(
    client: TestClient, auth, make_team, make_user, make_submission, pairwise_event
):
    event, subs, judge_user, _judge = pairwise_event(n_submissions=2)
    draft_team = make_team(event, make_user(Role.PARTICIPANT))
    make_submission(draft_team, name="Still Drafting", status=SubmissionStatus.DRAFT)

    headers = auth(judge_user)
    seen_ids = set()
    for _ in range(20):
        body = next_pair(client, event.slug, headers).json()
        seen_ids.add(body["submission_a"]["id"])
        seen_ids.add(body["submission_b"]["id"])
    assert seen_ids == {str(s.id) for s in subs}


def test_fewer_than_two_eligible_projects_is_a_409(
    client: TestClient, auth, make_event, make_user, make_judge
):
    event = make_event()
    event.pairwise_enabled = True
    event.judging_opens_at = utcnow() - timedelta(hours=1)
    event.judging_closes_at = utcnow() + timedelta(days=1)
    judge_user = make_user(Role.JUDGE)
    make_judge(event, judge_user)
    r = next_pair(client, event.slug, auth(judge_user))
    assert r.status_code == 409, r.text


# --------------------------------------------------------------------------- #
# Recording a comparison
# --------------------------------------------------------------------------- #


def test_a_comparison_is_recorded_and_returned(client: TestClient, auth, pairwise_event):
    event, subs, judge_user, _judge = pairwise_event()
    r = compare(client, event.slug, auth(judge_user), subs[0].id, subs[1].id, subs[0].id)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["winner_id"] == str(subs[0].id)
    assert body["submission_a_id"] == str(subs[0].id)
    assert body["submission_b_id"] == str(subs[1].id)


def test_a_tie_is_recorded_with_a_null_winner(client: TestClient, auth, pairwise_event):
    event, subs, judge_user, _judge = pairwise_event()
    r = compare(client, event.slug, auth(judge_user), subs[0].id, subs[1].id, None)
    assert r.status_code == 201, r.text
    assert r.json()["winner_id"] is None


def test_comparing_two_ineligible_projects_is_refused(
    client: TestClient, auth, make_team, make_user, make_submission, pairwise_event
):
    """A client that fabricates a pair it was never shown -- say, the judge's
    own team's project -- must not be able to write a comparison for it, even
    though the request body alone looks well-formed."""
    event, subs, judge_user, _judge = pairwise_event(n_submissions=2)
    own_team = make_team(event, judge_user)
    own_submission = make_submission(own_team, status=SubmissionStatus.SUBMITTED)

    r = compare(client, event.slug, auth(judge_user), subs[0].id, own_submission.id, subs[0].id)
    assert r.status_code == 409, r.text


def test_winner_id_must_be_one_of_the_two_submissions(client: TestClient, auth, pairwise_event):
    event, subs, judge_user, _judge = pairwise_event()
    r = compare(client, event.slug, auth(judge_user), subs[0].id, subs[1].id, uuid.uuid4())
    assert r.status_code == 422


def test_a_comparison_is_recorded_in_the_audit_log(
    client: TestClient, auth, make_user, pairwise_event
):
    event, subs, judge_user, _judge = pairwise_event()
    compare(client, event.slug, auth(judge_user), subs[0].id, subs[1].id, subs[0].id)

    organizer = make_user(Role.ORGANIZER)
    rows = client.get(f"/api/events/{event.slug}/audit", headers=auth(organizer)).json()["rows"]
    entries = [r for r in rows if r["action"] == AuditAction.PAIRWISE_COMPARED.value]
    assert entries, [r["action"] for r in rows]
    assert judge_user.email in entries[0]["summary"]
    assert "Project 0" in entries[0]["summary"]


# --------------------------------------------------------------------------- #
# Results: cold start, then a real ranking
# --------------------------------------------------------------------------- #


def test_results_include_every_submitted_project_even_with_zero_comparisons(
    client: TestClient, auth, make_user, pairwise_event
):
    event, subs, _judge_user, _judge = pairwise_event(n_submissions=3)
    organizer = make_user(Role.ORGANIZER)
    body = client.get(f"/api/events/{event.slug}/pairwise/results", headers=auth(organizer)).json()

    assert body["total_comparisons"] == 0
    assert {row["submission_id"] for row in body["rows"]} == {str(s.id) for s in subs}
    assert all(row["rating"] == 0.0 for row in body["rows"])
    assert all(row["rank"] == 1 for row in body["rows"])

    cov = body["coverage"]
    assert cov["total_submissions"] == 3
    assert cov["coverage_pct"] == 0.0


def test_results_coverage_reflects_real_comparisons(
    client: TestClient, auth, make_user, pairwise_event
):
    """The stat an organizer checks to answer "is this round actually
    balanced" without reading the per-project table row by row -- see
    `app/pairwise.coverage`."""
    event, subs, judge_user, _judge = pairwise_event(n_submissions=3)
    for _ in range(4):
        compare(client, event.slug, auth(judge_user), subs[0].id, subs[1].id, subs[0].id)

    organizer = make_user(Role.ORGANIZER)
    body = client.get(f"/api/events/{event.slug}/pairwise/results", headers=auth(organizer)).json()

    cov = body["coverage"]
    assert cov["total_submissions"] == 3
    assert cov["total_comparisons"] == 4
    assert cov["min_comparisons"] == 0  # subs[2] was never compared
    assert cov["max_comparisons"] == 4
    assert cov["coverage_pct"] == pytest.approx(2 / 3 * 100, abs=0.1)


def test_results_reflect_real_comparisons_and_report_judge_progress(
    client: TestClient, auth, make_user, pairwise_event
):
    event, subs, judge_user, _judge = pairwise_event(n_submissions=3)
    for _ in range(4):
        compare(client, event.slug, auth(judge_user), subs[0].id, subs[1].id, subs[0].id)

    organizer = make_user(Role.ORGANIZER)
    body = client.get(f"/api/events/{event.slug}/pairwise/results", headers=auth(organizer)).json()

    assert body["total_comparisons"] == 4
    assert body["converged"] is True
    by_id = {row["submission_id"]: row for row in body["rows"]}
    assert by_id[str(subs[0].id)]["rank"] == 1
    assert by_id[str(subs[0].id)]["rating"] > by_id[str(subs[2].id)]["rating"] > by_id[str(subs[1].id)]["rating"]

    assert len(body["judges"]) == 1
    assert body["judges"][0]["n_comparisons"] == 4


# --------------------------------------------------------------------------- #
# Configuration: the toggle itself
# --------------------------------------------------------------------------- #


def test_an_organizer_can_toggle_pairwise_judging(client: TestClient, auth, make_user, make_event):
    event = make_event()
    organizer = make_user(Role.ORGANIZER)
    assert event.pairwise_enabled is False

    r = client.patch(
        f"/api/events/{event.slug}", headers=auth(organizer), json={"pairwise_enabled": True}
    )
    assert r.status_code == 200, r.text
    assert r.json()["pairwise_enabled"] is True


# --------------------------------------------------------------------------- #
# The voting-configuration gap this same pass closed
# --------------------------------------------------------------------------- #


def test_voting_access_and_method_are_now_settable_over_the_api(
    client: TestClient, auth, make_user, make_event
):
    """Until this pass, `voting_access`, `voting_method`, `vote_credits` and
    `votes_per_voter` were readable on every event but writable nowhere --
    `EventCreate` and `EventUpdate` simply had no field for any of them, so a
    real organizer had no way to turn on quadratic or open-link voting at
    all. This is the regression test for that gap."""
    event = make_event()
    organizer = make_user(Role.ORGANIZER)

    r = client.patch(
        f"/api/events/{event.slug}",
        headers=auth(organizer),
        json={
            "voting_access": "open_link",
            "voting_method": "quadratic",
            "vote_credits": 64,
            "votes_per_voter": 5,
            "comments_enabled": False,
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["voting_access"] == "open_link"
    assert body["voting_method"] == "quadratic"
    assert body["vote_credits"] == 64
    assert body["votes_per_voter"] == 5
    assert body["comments_enabled"] is False


def test_voting_config_is_also_settable_at_creation(client: TestClient, auth, make_user):
    now = utcnow()
    organizer = make_user(Role.ORGANIZER)
    payload = {
        "slug": "quadratic-launch",
        "name": "Quadratic Launch",
        "starts_at": (now + timedelta(days=1)).isoformat(),
        "ends_at": (now + timedelta(days=3)).isoformat(),
        "registration_opens_at": now.isoformat(),
        "submission_opens_at": (now + timedelta(days=1)).isoformat(),
        "submission_deadline": (now + timedelta(days=3)).isoformat(),
        "voting_access": "email_gated",
        "voting_method": "quadratic",
        "vote_credits": 50,
    }
    r = client.post("/api/events", headers=auth(organizer), json=payload)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["voting_access"] == "email_gated"
    assert body["voting_method"] == "quadratic"
    assert body["vote_credits"] == 50


def test_voting_window_ordering_is_validated_on_create_and_update(
    client: TestClient, auth, make_user, make_event
):
    now = utcnow()
    organizer = make_user(Role.ORGANIZER)
    payload = {
        "slug": "bad-voting-window",
        "name": "Bad Voting Window",
        "starts_at": (now + timedelta(days=1)).isoformat(),
        "ends_at": (now + timedelta(days=3)).isoformat(),
        "registration_opens_at": now.isoformat(),
        "submission_opens_at": (now + timedelta(days=1)).isoformat(),
        "submission_deadline": (now + timedelta(days=3)).isoformat(),
        "voting_opens_at": (now + timedelta(days=5)).isoformat(),
        "voting_closes_at": (now + timedelta(days=4)).isoformat(),
    }
    assert client.post("/api/events", headers=auth(organizer), json=payload).status_code == 422

    event = make_event()
    r = client.patch(
        f"/api/events/{event.slug}",
        headers=auth(organizer),
        json={
            "voting_opens_at": (now + timedelta(days=5)).isoformat(),
            "voting_closes_at": (now + timedelta(days=4)).isoformat(),
        },
    )
    assert r.status_code == 422
