"""Part 3: the winner/community-vote split.

Top `winner_slots` ranks (by normalized judge score) are outright winners; the
next `community_vote_slots` ranks move into a community-voting round scoped to
exactly that subset, reusing the same `Voter`/`Vote` machinery every other
event's voting already uses (`app/routers/voting.py`'s `_votable()`). Everyone
below both slots is tiered as neither.

Eligibility for that round is its own rule, independent of the event's normal
`voting_access`: registered for the event at least
`COMMUNITY_VOTE_REGISTRATION_CUTOFF_DAYS` days before it started
(`Event.community_vote_eligible`). A fresh account cannot swing a round that
exists to let people who were actually around pick among the near-misses.
"""

from __future__ import annotations

from datetime import timedelta

from fastapi.testclient import TestClient

from app.models import (
    COMMUNITY_VOTE_REGISTRATION_CUTOFF_DAYS,
    AssignmentStatus,
    Role,
    SubmissionStatus,
    utcnow,
)

# --------------------------------------------------------------------------- #
# Tiering on the results page
# --------------------------------------------------------------------------- #


def test_results_label_winner_and_community_tier_rows(
    client: TestClient, auth, make_user, tiered_event
) -> None:
    event, subs = tiered_event()
    organizer = make_user(Role.ORGANIZER)
    body = client.get(f"/api/events/{event.slug}/results", headers=auth(organizer)).json()

    tiers = {row["submission_id"]: row["tier"] for row in body["rows"]}
    assert tiers[str(subs[0].id)] == "winner"
    assert tiers[str(subs[1].id)] == "community_tier"
    assert tiers[str(subs[2].id)] == "community_tier"
    assert tiers[str(subs[3].id)] is None


def test_an_event_that_does_not_use_the_split_labels_nothing(
    client: TestClient, auth, make_event, make_user, make_team, make_submission,
    make_judge, make_assignment, make_criterion, make_score,
) -> None:
    event = make_event()
    only = make_criterion(event, key="overall", name="Overall")
    judge = make_judge(event, make_user(Role.JUDGE))
    submission = make_submission(
        make_team(event, make_user(Role.PARTICIPANT)), status=SubmissionStatus.SUBMITTED
    )
    ballot = make_assignment(judge, submission, status=AssignmentStatus.COMPLETE)
    make_score(ballot, only, 5)

    organizer = make_user(Role.ORGANIZER)
    body = client.get(f"/api/events/{event.slug}/results", headers=auth(organizer)).json()
    assert body["rows"][0]["tier"] is None


# --------------------------------------------------------------------------- #
# Voting is scoped to the community tier
# --------------------------------------------------------------------------- #


def test_only_the_community_tier_appears_on_the_ballot(
    client: TestClient, auth, make_user, make_registration, tiered_event
) -> None:
    event, subs = tiered_event()
    voter = make_user(Role.PARTICIPANT)
    make_registration(event, voter, registered_at=utcnow() - timedelta(days=60))

    ballot = client.get(f"/api/events/{event.slug}/ballot", headers=auth(voter)).json()
    project_ids = {p["id"] for p in ballot["projects"]}
    assert project_ids == {str(subs[1].id), str(subs[2].id)}


def test_voting_for_the_winner_or_a_non_tiered_project_is_refused(
    client: TestClient, auth, make_user, make_registration, tiered_event
) -> None:
    event, subs = tiered_event()
    voter = make_user(Role.PARTICIPANT)
    make_registration(event, voter, registered_at=utcnow() - timedelta(days=60))
    headers = auth(voter)

    for outside_tier in (subs[0], subs[3]):
        response = client.put(
            f"/api/events/{event.slug}/votes",
            json={"votes": [{"submission_id": str(outside_tier.id), "credits": 1}]},
            headers=headers,
        )
        assert response.status_code == 422, response.text


def test_voting_for_a_community_tier_project_succeeds(
    client: TestClient, auth, make_user, make_registration, tiered_event
) -> None:
    event, subs = tiered_event()
    voter = make_user(Role.PARTICIPANT)
    make_registration(event, voter, registered_at=utcnow() - timedelta(days=60))

    response = client.put(
        f"/api/events/{event.slug}/votes",
        json={"votes": [{"submission_id": str(subs[1].id), "credits": 1}]},
        headers=auth(voter),
    )
    assert response.status_code == 200, response.text


def test_the_tally_only_counts_the_community_tier(
    client: TestClient, auth, make_user, make_registration, tiered_event
) -> None:
    event, subs = tiered_event()
    voter = make_user(Role.PARTICIPANT)
    make_registration(event, voter, registered_at=utcnow() - timedelta(days=60))
    client.put(
        f"/api/events/{event.slug}/votes",
        json={"votes": [{"submission_id": str(subs[1].id), "credits": 1}]},
        headers=auth(voter),
    )

    organizer = make_user(Role.ORGANIZER)
    tally = client.get(f"/api/events/{event.slug}/voting/results", headers=auth(organizer)).json()
    ids = {row["submission_id"] for row in tally["rows"]}
    assert ids == {str(subs[1].id), str(subs[2].id)}


# --------------------------------------------------------------------------- #
# Eligibility: the registration cutoff
# --------------------------------------------------------------------------- #


def test_a_voter_who_registered_before_the_cutoff_is_eligible(
    client: TestClient, auth, make_user, make_registration, tiered_event
) -> None:
    event, _subs = tiered_event(starts_at=utcnow())  # starts today, cutoff is 7 days ago
    voter = make_user(Role.PARTICIPANT)
    cutoff = event.starts_at - timedelta(days=COMMUNITY_VOTE_REGISTRATION_CUTOFF_DAYS)
    make_registration(event, voter, registered_at=cutoff)  # exactly on the boundary

    response = client.post(f"/api/events/{event.slug}/voting/claim", json={}, headers=auth(voter))
    assert response.status_code == 200, response.text


def test_a_voter_who_registered_one_second_after_the_cutoff_is_ineligible(
    client: TestClient, auth, make_user, make_registration, tiered_event
) -> None:
    event, _subs = tiered_event(starts_at=utcnow())
    voter = make_user(Role.PARTICIPANT)
    cutoff = event.starts_at - timedelta(days=COMMUNITY_VOTE_REGISTRATION_CUTOFF_DAYS)
    make_registration(event, voter, registered_at=cutoff + timedelta(seconds=1))

    response = client.post(f"/api/events/{event.slug}/voting/claim", json={}, headers=auth(voter))
    assert response.status_code == 403, response.text
    assert "registered" in response.json()["detail"]
    assert str(COMMUNITY_VOTE_REGISTRATION_CUTOFF_DAYS) in response.json()["detail"]


def test_a_voter_who_never_registered_is_ineligible(
    client: TestClient, auth, make_user, tiered_event
) -> None:
    event, _subs = tiered_event()
    voter = make_user(Role.PARTICIPANT)
    response = client.post(f"/api/events/{event.slug}/voting/claim", json={}, headers=auth(voter))
    assert response.status_code == 403
    assert "registered" in response.json()["detail"]


def test_an_anonymous_caller_cannot_claim_a_community_tier_ballot(
    client: TestClient, tiered_event
) -> None:
    event, _subs = tiered_event()
    response = client.post(f"/api/events/{event.slug}/voting/claim", json={})
    assert response.status_code == 403


def test_casting_votes_implicitly_claims_a_ballot_and_still_checks_eligibility(
    client: TestClient, auth, make_user, tiered_event
) -> None:
    """`cast_votes` auto-creates a ballot on demand for an authenticated
    caller without a separate claim step -- the eligibility gate must hold on
    that implicit path too, not just the explicit `/voting/claim` route."""
    event, subs = tiered_event()
    voter = make_user(Role.PARTICIPANT)  # never registered

    response = client.put(
        f"/api/events/{event.slug}/votes",
        json={"votes": [{"submission_id": str(subs[1].id), "credits": 1}]},
        headers=auth(voter),
    )
    assert response.status_code == 403
    assert "registered" in response.json()["detail"]


def test_eligibility_is_rechecked_when_casting_not_just_when_claiming(
    client: TestClient, auth, make_user, make_registration, db, tiered_event
) -> None:
    """A ballot claimed while eligible must not go on writing votes forever
    if the registration backing that eligibility is later removed."""
    event, subs = tiered_event()
    voter = make_user(Role.PARTICIPANT)
    registration = make_registration(
        event, voter, registered_at=utcnow() - timedelta(days=60)
    )
    headers = auth(voter)
    claim = client.post(f"/api/events/{event.slug}/voting/claim", json={}, headers=headers)
    assert claim.status_code == 200

    db.delete(registration)
    db.commit()

    response = client.put(
        f"/api/events/{event.slug}/votes",
        json={"votes": [{"submission_id": str(subs[1].id), "credits": 1}]},
        headers=headers,
    )
    assert response.status_code == 403
    assert "registered" in response.json()["detail"]


# --------------------------------------------------------------------------- #
# Tier status shown to the team
# --------------------------------------------------------------------------- #


def test_staff_always_sees_tier_on_the_submission(
    client: TestClient, auth, make_user, tiered_event
) -> None:
    event, subs = tiered_event()
    organizer = make_user(Role.ORGANIZER)
    body = client.get(f"/api/submissions/{subs[0].id}", headers=auth(organizer)).json()
    assert body["tier"] == "winner"


def test_a_team_does_not_see_tier_before_results_are_public(
    client: TestClient, auth, tiered_event
) -> None:
    event, subs = tiered_event()
    winning_team = subs[0].team
    owner = winning_team.members[0].user
    body = client.get(f"/api/submissions/{subs[0].id}", headers=auth(owner)).json()
    assert body["tier"] is None


def test_a_team_sees_its_own_tier_once_results_are_public(
    client: TestClient, auth, db, tiered_event
) -> None:
    event, subs = tiered_event()
    event.results_public_at = utcnow() - timedelta(minutes=1)
    db.commit()

    winning_team = subs[0].team
    owner = winning_team.members[0].user
    body = client.get(f"/api/submissions/{subs[0].id}", headers=auth(owner)).json()
    assert body["tier"] == "winner"

    non_tiered_team = subs[3].team
    non_owner = non_tiered_team.members[0].user
    body2 = client.get(f"/api/submissions/{subs[3].id}", headers=auth(non_owner)).json()
    assert body2["tier"] is None
