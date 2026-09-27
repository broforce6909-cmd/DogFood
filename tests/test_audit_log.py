"""The audit trail: is it there, is it readable, and is it honest?

The brief's requirement is about the reader -- _"an audit trail an organizer can
read without a database client"_ -- so the assertions here are mostly about the
English in the `summary` column, not just about rows existing. A log with the right
number of rows and no legible content would pass a weaker test and fail the
requirement.

Three properties this file pins down:

* **Sensitive actions are recorded.** Voting, commenting, moderation, judging,
  deadline overrides.
* **Entries read as sentences**, naming the actor and the thing, with no raw ids
  standing in for either.
* **It is append-only and organizer-only.** No API writes it by hand, nothing
  edits it, and nobody below organizer can read it.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from app.models import (
    AuditAction,
    Role,
    SubmissionStatus,
    VotingAccess,
    VotingMethod,
    utcnow,
)


@pytest.fixture()
def votable(db, make_user, make_team, make_submission, voting_event):
    """An open quadratic event with two projects and an organizer."""

    def _build():
        event = voting_event(method=VotingMethod.QUADRATIC, access=VotingAccess.AUTHENTICATED)
        subs = []
        for i in range(2):
            team = make_team(event, make_user(Role.PARTICIPANT), name=f"Team {i}")
            subs.append(
                make_submission(team, name=f"Project {i}", status=SubmissionStatus.SUBMITTED)
            )
        return event, subs, make_user(Role.ORGANIZER)

    return _build


def audit(client: TestClient, slug: str, headers, **params):
    return client.get(f"/api/events/{slug}/audit", headers=headers, params=params)


# --------------------------------------------------------------------------- #
# Who may read it
# --------------------------------------------------------------------------- #


def test_the_audit_log_is_organizer_only(
    client: TestClient, auth, make_user, votable
):
    """FIG. 02's audit column: every row below organizer is a ✗."""
    event, _subs, organizer = votable()
    for role in (Role.PARTICIPANT, Role.JUDGE):
        r = audit(client, event.slug, auth(make_user(role)))
        assert r.status_code == 403, f"{role.value} could read the audit log"
    assert audit(client, event.slug, {}).status_code == 403
    assert audit(client, event.slug, auth(organizer)).status_code == 200


def test_there_is_no_way_to_write_the_log_by_hand(client: TestClient, auth, make_user, votable):
    """A log an organizer can author is not evidence of anything."""
    event, _subs, organizer = votable()
    headers = auth(organizer)
    assert client.post(f"/api/events/{event.slug}/audit", headers=headers, json={}).status_code in (
        404,
        405,
    )
    assert client.delete(f"/api/events/{event.slug}/audit", headers=headers).status_code in (
        404,
        405,
    )


# --------------------------------------------------------------------------- #
# Voting is recorded
# --------------------------------------------------------------------------- #


def test_casting_a_vote_is_recorded_as_a_sentence(
    client: TestClient, auth, make_user, votable
):
    """The central requirement: an organizer reads a line, not a row."""
    event, subs, organizer = votable()
    voter = make_user(Role.PARTICIPANT, email="voter@example.com")
    me = auth(voter)
    client.post(f"/api/events/{event.slug}/voting/claim", headers=me, json={})
    client.put(
        f"/api/events/{event.slug}/votes",
        headers=me,
        json={"votes": [{"submission_id": str(subs[0].id), "credits": 3}]},
    )

    rows = audit(client, event.slug, auth(organizer)).json()["rows"]
    votes = [r for r in rows if r["action"] == AuditAction.VOTE_CAST.value]
    assert votes, f"no vote recorded; actions were {[r['action'] for r in rows]}"

    summary = votes[0]["summary"]
    assert "voter@example.com" in summary
    assert "Project 0" in summary
    assert "3" in summary
    assert "credits" in summary
    # No bare uuids standing in for the actor or the project.
    assert str(subs[0].id) not in summary


def test_changing_a_ballot_is_distinguishable_from_casting_one(
    client: TestClient, auth, make_user, votable
):
    """"They changed their vote" is a different fact from "they voted"."""
    event, subs, organizer = votable()
    me = auth(make_user(Role.PARTICIPANT))
    client.post(f"/api/events/{event.slug}/voting/claim", headers=me, json={})
    body = {"votes": [{"submission_id": str(subs[0].id), "credits": 2}]}
    client.put(f"/api/events/{event.slug}/votes", headers=me, json=body)
    body["votes"][0]["credits"] = 5
    client.put(f"/api/events/{event.slug}/votes", headers=me, json=body)

    actions = [r["action"] for r in audit(client, event.slug, auth(organizer)).json()["rows"]]
    assert AuditAction.VOTE_CHANGED.value in actions


def test_withdrawing_a_ballot_is_recorded(client: TestClient, auth, make_user, votable):
    event, subs, organizer = votable()
    me = auth(make_user(Role.PARTICIPANT))
    client.post(f"/api/events/{event.slug}/voting/claim", headers=me, json={})
    client.put(
        f"/api/events/{event.slug}/votes",
        headers=me,
        json={"votes": [{"submission_id": str(subs[0].id), "credits": 2}]},
    )
    client.delete(f"/api/events/{event.slug}/votes", headers=me)

    actions = [r["action"] for r in audit(client, event.slug, auth(organizer)).json()["rows"]]
    assert AuditAction.VOTE_WITHDRAWN.value in actions


def test_an_anonymous_voter_gets_a_countable_pseudonym(
    client: TestClient, auth, make_user, voting_event, make_team, make_submission
):
    """Open-link voters have no account, and "anonymous" for all of them would make
    the log useless for investigating abuse. Each ballot gets a short stable label.
    """
    event = voting_event(access=VotingAccess.OPEN_LINK)
    team = make_team(event, make_user(Role.PARTICIPANT))
    make_submission(team, status=SubmissionStatus.SUBMITTED)
    client.post(f"/api/events/{event.slug}/voting/claim", json={})

    rows = audit(client, event.slug, auth(make_user(Role.ORGANIZER))).json()["rows"]
    labels = [r["actor_label"] for r in rows]
    assert any(label.startswith("anonymous:") for label in labels), labels


# --------------------------------------------------------------------------- #
# Comments and moderation are recorded
# --------------------------------------------------------------------------- #


def test_moderation_records_who_and_why(
    client: TestClient, auth, make_user, make_team, make_submission, voting_event
):
    """The entry a participant will later ask about, so the reason is mandatory."""
    event = voting_event()
    team = make_team(event, make_user(Role.PARTICIPANT))
    submission = make_submission(team, name="Quorum", status=SubmissionStatus.SUBMITTED)

    author = make_user(Role.PARTICIPANT, email="author@example.com")
    posted = client.post(
        f"/api/gallery/{submission.id}/comments",
        headers=auth(author),
        json={"body": "Off-topic spam"},
    )
    assert posted.status_code == 201, posted.text

    organizer = make_user(Role.ORGANIZER, email="mod@example.com")
    hidden = client.post(
        f"/api/comments/{posted.json()['id']}/hide",
        headers=auth(organizer),
        json={"reason": "off topic"},
    )
    assert hidden.status_code == 200, hidden.text

    rows = audit(client, event.slug, auth(organizer)).json()["rows"]
    hides = [r for r in rows if r["action"] == AuditAction.COMMENT_HIDDEN.value]
    assert hides, [r["action"] for r in rows]
    assert "mod@example.com" in hides[0]["summary"]
    assert "author@example.com" in hides[0]["summary"]
    assert "off topic" in hides[0]["summary"]


def test_posting_a_comment_is_recorded(
    client: TestClient, auth, make_user, make_team, make_submission, voting_event
):
    event = voting_event()
    team = make_team(event, make_user(Role.PARTICIPANT))
    submission = make_submission(team, name="Quorum", status=SubmissionStatus.SUBMITTED)
    client.post(
        f"/api/gallery/{submission.id}/comments",
        headers=auth(make_user(Role.PARTICIPANT)),
        json={"body": "Good stuff"},
    )
    rows = audit(client, event.slug, auth(make_user(Role.ORGANIZER))).json()["rows"]
    posts = [r for r in rows if r["action"] == AuditAction.COMMENT_POSTED.value]
    assert posts and "Quorum" in posts[0]["summary"]


# --------------------------------------------------------------------------- #
# Filtering and paging
# --------------------------------------------------------------------------- #


def test_the_log_can_be_filtered_by_action_and_actor(
    client: TestClient, auth, make_user, votable
):
    event, subs, organizer = votable()
    headers = auth(organizer)
    voter = make_user(Role.PARTICIPANT, email="findme@example.com")
    me = auth(voter)
    client.post(f"/api/events/{event.slug}/voting/claim", headers=me, json={})
    client.put(
        f"/api/events/{event.slug}/votes",
        headers=me,
        json={"votes": [{"submission_id": str(subs[0].id), "credits": 1}]},
    )

    by_action = audit(client, event.slug, headers, action=AuditAction.VOTE_CAST.value).json()
    assert by_action["total"] >= 1
    assert {r["action"] for r in by_action["rows"]} == {AuditAction.VOTE_CAST.value}

    by_actor = audit(client, event.slug, headers, actor="findme").json()
    assert by_actor["total"] >= 1
    assert all("findme" in r["actor_label"] for r in by_actor["rows"])


def test_the_log_is_newest_first(client: TestClient, auth, make_user, votable):
    """An organizer opens this page to ask "what just happened"."""
    event, subs, organizer = votable()
    me = auth(make_user(Role.PARTICIPANT))
    client.post(f"/api/events/{event.slug}/voting/claim", headers=me, json={})
    client.put(
        f"/api/events/{event.slug}/votes",
        headers=me,
        json={"votes": [{"submission_id": str(subs[0].id), "credits": 1}]},
    )
    rows = audit(client, event.slug, auth(organizer)).json()["rows"]
    stamps = [r["created_at"] for r in rows]
    assert stamps == sorted(stamps, reverse=True)


def test_the_actions_endpoint_lists_only_what_happened(
    client: TestClient, auth, make_user, votable
):
    """A filter offering verbs this event never produced looks broken when used."""
    event, subs, organizer = votable()
    headers = auth(organizer)
    me = auth(make_user(Role.PARTICIPANT))
    client.post(f"/api/events/{event.slug}/voting/claim", headers=me, json={})

    present = client.get(f"/api/events/{event.slug}/audit/actions", headers=headers).json()
    assert AuditAction.VOTE_CAST.value in present
    assert AuditAction.RUBRIC_CHANGED.value not in present


# --------------------------------------------------------------------------- #
# Judging actions land in the same log
# --------------------------------------------------------------------------- #


def test_scoring_a_ballot_is_recorded(
    client: TestClient, auth, db, make_user, make_team, make_submission, make_judge,
    make_assignment, make_criterion, voting_event,
):
    """The Phase 2 gap this closes: who changed a score, and when."""
    event = voting_event()
    event.judging_opens_at = utcnow() - timedelta(hours=1)
    event.judging_closes_at = utcnow() + timedelta(days=1)
    db.commit()
    criterion = make_criterion(event, key="impact", name="Impact", weight=1)
    judge_user = make_user(Role.JUDGE, email="judge@example.com")
    judge = make_judge(event, judge_user)
    team = make_team(event, make_user(Role.PARTICIPANT))
    submission = make_submission(team, name="Switchyard", status=SubmissionStatus.SUBMITTED)
    ballot = make_assignment(judge, submission)

    r = client.put(
        f"/api/judging/assignments/{ballot.id}/scores",
        headers=auth(judge_user),
        json={"scores": [{"criterion_id": str(criterion.id), "value": 4}]},
    )
    assert r.status_code == 200, r.text

    rows = audit(client, event.slug, auth(make_user(Role.ORGANIZER))).json()["rows"]
    scored = [r for r in rows if r["action"] == AuditAction.BALLOT_SCORED.value]
    assert scored, [r["action"] for r in rows]
    assert "judge@example.com" in scored[0]["summary"]
    assert "Switchyard" in scored[0]["summary"]


def test_an_organizer_editing_after_the_deadline_is_recorded(
    client: TestClient, auth, db, make_user, make_team, make_submission, make_event
):
    """`access.py` has always documented this as a deliberate exception. Phase 3
    is where it stops being invisible."""
    # An hour ago, not a day: `make_event` opens submissions a day back, and a
    # deadline before that would violate the event's own window constraint.
    event = make_event(deadline=utcnow() - timedelta(hours=1))
    team = make_team(event, make_user(Role.PARTICIPANT))
    submission = make_submission(team, name="Latchkey", status=SubmissionStatus.SUBMITTED)
    organizer = make_user(Role.ORGANIZER, email="boss@example.com")

    r = client.patch(
        f"/api/submissions/{submission.id}",
        headers=auth(organizer),
        json={"tagline": "fixed a broken repo link"},
    )
    assert r.status_code == 200, r.text

    rows = audit(client, event.slug, auth(organizer)).json()["rows"]
    overrides = [
        r
        for r in rows
        if r["action"] == AuditAction.SUBMISSION_EDITED_AFTER_DEADLINE.value
    ]
    assert overrides, [r["action"] for r in rows]
    assert "boss@example.com" in overrides[0]["summary"]
    assert "Latchkey" in overrides[0]["summary"]


# --------------------------------------------------------------------------- #
# Export
# --------------------------------------------------------------------------- #


def test_the_log_exports_as_csv(client: TestClient, auth, make_user, votable):
    """An organizer who wants it in a spreadsheet should not need a database client
    for that either."""
    event, subs, organizer = votable()
    headers = auth(organizer)
    me = auth(make_user(Role.PARTICIPANT))
    client.post(f"/api/events/{event.slug}/voting/claim", headers=me, json={})

    r = client.get(f"/api/events/{event.slug}/export/audit.csv", headers=headers)
    assert r.status_code == 200, r.text
    lines = [line for line in r.text.splitlines() if line.strip()]
    assert lines[0].startswith("at,action,actor")
    assert len(lines) > 1


def test_the_audit_export_is_not_public(client: TestClient, auth, make_user, votable):
    event, _subs, _organizer = votable()
    r = client.get(
        f"/api/events/{event.slug}/export/audit.csv", headers=auth(make_user(Role.JUDGE))
    )
    assert r.status_code == 403
