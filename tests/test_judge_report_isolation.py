"""Part 5: the per-project judge report, and the isolation it has to hold.

A team may read their own project's judge feedback and nothing else --
`GET /events/{slug}/results/{submission_id}/report` is scoped by
`Action.READ_JUDGE_REPORT` on the `Submission` itself, not on the `Event`,
so "which project" is part of the authorization decision, not just routing.
Every judge in the response is anonymized to `"Judge 1"`, `"Judge 2"`, ...
-- `access.py` already treats who is judging as staff-only information
everywhere else, and this is that same rule applied to the one place a
participant is shown ballot content at all.

The isolation test is written first, per instruction, and every other test
in this file builds on the same `tiered_event` fixture it uses.
"""

from __future__ import annotations

from datetime import timedelta

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.models import Judge, Role, utcnow


def _member(submission):
    return submission.team.members[0].user


# --------------------------------------------------------------------------- #
# Isolation -- written first
# --------------------------------------------------------------------------- #


def test_a_team_cannot_read_another_teams_judge_report(
    client: TestClient, auth, db, tiered_event
) -> None:
    event, subs = tiered_event()
    event.results_public_at = utcnow() - timedelta(minutes=1)
    db.commit()

    outsider = _member(subs[1])
    response = client.get(
        f"/api/events/{event.slug}/results/{subs[0].id}/report", headers=auth(outsider)
    )
    assert response.status_code == 403


def test_a_teams_report_never_contains_another_projects_data(
    client: TestClient, auth, db, tiered_event
) -> None:
    """Not just refused for the wrong project -- the *right* project's own
    report must never carry another submission's id or name anywhere in it."""
    event, subs = tiered_event()
    event.results_public_at = utcnow() - timedelta(minutes=1)
    db.commit()

    owner = _member(subs[0])
    body = client.get(
        f"/api/events/{event.slug}/results/{subs[0].id}/report", headers=auth(owner)
    ).json()
    assert body["submission_id"] == str(subs[0].id)
    assert body["submission_name"] == subs[0].name

    dumped = str(body)
    for other in subs[1:]:
        assert str(other.id) not in dumped
        assert other.name not in dumped


def test_an_anonymous_caller_cannot_read_a_judge_report(
    client: TestClient, db, tiered_event
) -> None:
    event, subs = tiered_event()
    event.results_public_at = utcnow() - timedelta(minutes=1)
    db.commit()
    response = client.get(f"/api/events/{event.slug}/results/{subs[0].id}/report")
    assert response.status_code == 403


def test_a_stranger_participant_not_on_the_team_cannot_read_the_report(
    client: TestClient, auth, make_user, db, tiered_event
) -> None:
    event, subs = tiered_event()
    event.results_public_at = utcnow() - timedelta(minutes=1)
    db.commit()
    response = client.get(
        f"/api/events/{event.slug}/results/{subs[0].id}/report",
        headers=auth(make_user(Role.PARTICIPANT)),
    )
    assert response.status_code == 403


# --------------------------------------------------------------------------- #
# The report itself
# --------------------------------------------------------------------------- #


def test_a_team_reads_their_own_report_once_results_are_public(
    client: TestClient, auth, db, tiered_event
) -> None:
    event, subs = tiered_event()
    event.results_public_at = utcnow() - timedelta(minutes=1)
    db.commit()

    body = client.get(
        f"/api/events/{event.slug}/results/{subs[0].id}/report", headers=auth(_member(subs[0]))
    ).json()
    assert body["submission_id"] == str(subs[0].id)
    assert body["n_reviews"] == 1
    assert body["tier"] == "winner"
    assert len(body["judges"]) == 1
    assert body["judges"][0]["label"] == "Judge 1"
    assert body["judges"][0]["scores"][0]["value"] == 5


def test_judges_are_anonymized(client: TestClient, auth, db, tiered_event) -> None:
    event, subs = tiered_event()
    event.results_public_at = utcnow() - timedelta(minutes=1)
    db.commit()

    body = client.get(
        f"/api/events/{event.slug}/results/{subs[0].id}/report", headers=auth(_member(subs[0]))
    ).json()
    dumped = str(body)
    assert "label" in body["judges"][0]
    assert "judge_id" not in body["judges"][0]
    assert "judge_name" not in body["judges"][0]
    # No real judge display name leaks anywhere in the payload either.
    judge = db.execute(select(Judge)).scalars().first()
    assert judge.user.display_name not in dumped


def test_the_report_is_not_available_before_results_are_public(
    client: TestClient, auth, tiered_event
) -> None:
    event, subs = tiered_event()  # results_public_at is None by default
    response = client.get(
        f"/api/events/{event.slug}/results/{subs[0].id}/report", headers=auth(_member(subs[0]))
    )
    assert response.status_code == 403


def test_staff_can_read_a_report_before_results_are_public(
    client: TestClient, auth, make_user, tiered_event
) -> None:
    event, subs = tiered_event()
    response = client.get(
        f"/api/events/{event.slug}/results/{subs[0].id}/report",
        headers=auth(make_user(Role.ORGANIZER)),
    )
    assert response.status_code == 200


# --------------------------------------------------------------------------- #
# The public results dashboard
# --------------------------------------------------------------------------- #


def test_the_public_dashboard_lists_every_submitted_project(
    client: TestClient, auth, db, tiered_event
) -> None:
    """Not just winners -- every submitted project, ranked or not."""
    event, subs = tiered_event()
    event.results_public_at = utcnow() - timedelta(minutes=1)
    db.commit()

    body = client.get(f"/api/events/{event.slug}/results/public").json()
    ids = {row["submission_id"] for row in body["rows"]}
    assert ids == {str(s.id) for s in subs}

    by_id = {row["submission_id"]: row for row in body["rows"]}
    assert by_id[str(subs[0].id)]["tier"] == "winner"
    assert by_id[str(subs[1].id)]["tier"] == "community_tier"
    assert by_id[str(subs[3].id)]["tier"] is None
    assert by_id[str(subs[0].id)]["normalized_rank"] == 1


def test_the_public_dashboard_is_hidden_before_results_are_public(
    client: TestClient, tiered_event
) -> None:
    event, _subs = tiered_event()
    response = client.get(f"/api/events/{event.slug}/results/public")
    assert response.status_code == 403


def test_the_public_dashboard_is_distinct_from_the_vote_tally(
    client: TestClient, auth, make_user, make_registration, db, tiered_event
) -> None:
    """Judge score/rank on the public dashboard, vote counts on the tally --
    two different endpoints, two different secrets."""
    event, subs = tiered_event()
    event.results_public_at = utcnow() - timedelta(minutes=1)
    db.commit()

    voter = make_user(Role.PARTICIPANT)
    make_registration(event, voter, registered_at=utcnow() - timedelta(days=60))
    client.put(
        f"/api/events/{event.slug}/votes",
        json={"votes": [{"submission_id": str(subs[1].id), "credits": 1}]},
        headers=auth(voter),
    )

    dashboard = client.get(f"/api/events/{event.slug}/results/public").json()
    assert "normalized_score" in dashboard["rows"][0]
    assert "votes" not in dashboard["rows"][0]

    tally = client.get(
        f"/api/events/{event.slug}/voting/results", headers=auth(make_user(Role.ORGANIZER))
    ).json()
    assert "votes" in tally["rows"][0]
    assert "normalized_score" not in tally["rows"][0]
