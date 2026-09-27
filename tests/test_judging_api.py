"""The judging API end to end: rubric, judges, assignment, progress, results, CSV.

`test_judging_isolation.py` covers who may do what. This file covers whether the
thing works at all once you are allowed to do it.
"""

from __future__ import annotations

import csv
import io
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from app.models import AssignmentStatus, Role, SubmissionStatus, utcnow


@pytest.fixture()
def event_with_rubric(db, make_event, make_user, make_criterion):
    """An event mid-judging, with an organizer and a two-criterion weighted rubric."""

    def _build():
        event = make_event()
        now = utcnow()
        event.judging_opens_at = now - timedelta(hours=1)
        event.judging_closes_at = now + timedelta(days=2)
        db.commit()
        impact = make_criterion(event, key="impact", name="Impact", weight=3, position=0)
        craft = make_criterion(event, key="craft", name="Craft", weight=1, position=1)
        return event, make_user(Role.ORGANIZER), impact, craft

    return _build


# --------------------------------------------------------------------------- #
# Rubric
# --------------------------------------------------------------------------- #


def test_an_organizer_builds_a_weighted_rubric(client: TestClient, auth, make_event, make_user):
    event = make_event()
    headers = auth(make_user(Role.ORGANIZER))
    r = client.post(
        f"/api/events/{event.slug}/criteria",
        headers=headers,
        json={"key": "impact", "name": "Impact", "weight": 3, "max_score": 5},
    )
    assert r.status_code == 201, r.text
    assert r.json()["weight"] == 3.0

    listed = client.get(f"/api/events/{event.slug}/criteria", headers=headers)
    assert [c["key"] for c in listed.json()] == ["impact"]


def test_a_duplicate_criterion_key_is_a_conflict(client: TestClient, auth, make_event, make_user):
    event = make_event()
    headers = auth(make_user(Role.ORGANIZER))
    body = {"key": "impact", "name": "Impact"}
    assert client.post(f"/api/events/{event.slug}/criteria", headers=headers, json=body).status_code == 201
    again = client.post(f"/api/events/{event.slug}/criteria", headers=headers, json=body)
    assert again.status_code == 409


def test_a_participant_may_read_the_rubric_but_not_change_it(
    client: TestClient, auth, make_event, make_user, make_criterion
):
    """You are entitled to know what you are being judged against."""
    event = make_event()
    make_criterion(event, key="impact", name="Impact")
    headers = auth(make_user(Role.PARTICIPANT))

    assert client.get(f"/api/events/{event.slug}/criteria", headers=headers).status_code == 200
    denied = client.post(
        f"/api/events/{event.slug}/criteria", headers=headers, json={"key": "x", "name": "X"}
    )
    assert denied.status_code == 403


def test_max_score_below_min_score_is_rejected(client: TestClient, auth, make_event, make_user):
    event = make_event()
    headers = auth(make_user(Role.ORGANIZER))
    r = client.post(
        f"/api/events/{event.slug}/criteria",
        headers=headers,
        json={"key": "backwards", "name": "Backwards", "min_score": 5, "max_score": 2},
    )
    assert r.status_code == 422


# --------------------------------------------------------------------------- #
# Judges
# --------------------------------------------------------------------------- #


def test_inviting_a_judge_raises_their_role_to_judge(
    client: TestClient, auth, db, make_event, make_user
):
    event = make_event()
    organizer = make_user(Role.ORGANIZER)
    invitee = make_user(Role.PARTICIPANT, email="reviewer@example.com")

    r = client.post(
        f"/api/events/{event.slug}/judges",
        headers=auth(organizer),
        json={"email": "reviewer@example.com"},
    )
    assert r.status_code == 201, r.text
    db.refresh(invitee)
    assert invitee.role is Role.JUDGE


def test_inviting_an_unknown_address_is_a_404_not_a_new_account(
    client: TestClient, auth, make_event, make_user
):
    """Minting users from an invite form is a spam and takeover vector."""
    event = make_event()
    r = client.post(
        f"/api/events/{event.slug}/judges",
        headers=auth(make_user(Role.ORGANIZER)),
        json={"email": "nobody@example.com"},
    )
    assert r.status_code == 404
    assert "register" in r.json()["detail"]


def test_a_judge_cannot_be_invited_twice_to_one_event(
    client: TestClient, auth, make_event, make_user
):
    event = make_event()
    headers = auth(make_user(Role.ORGANIZER))
    make_user(Role.JUDGE, email="dup@example.com")
    body = {"email": "dup@example.com"}
    assert client.post(f"/api/events/{event.slug}/judges", headers=headers, json=body).status_code == 201
    assert client.post(f"/api/events/{event.slug}/judges", headers=headers, json=body).status_code == 409


def test_a_track_from_another_event_is_rejected(
    client: TestClient, auth, make_event, make_user, make_track
):
    event = make_event()
    other = make_track(make_event(), key="elsewhere", name="Elsewhere")
    make_user(Role.JUDGE, email="tracked@example.com")
    r = client.post(
        f"/api/events/{event.slug}/judges",
        headers=auth(make_user(Role.ORGANIZER)),
        json={"email": "tracked@example.com", "track_id": str(other.id)},
    )
    assert r.status_code == 422


def test_deactivating_a_judge_empties_their_queue(
    client: TestClient, auth, make_event, make_user, make_team, make_submission, make_judge,
    make_assignment, make_criterion,
):
    event = make_event()
    make_criterion(event, key="impact", name="Impact")
    judge_user = make_user(Role.JUDGE)
    judge = make_judge(event, judge_user)
    team = make_team(event, make_user(Role.PARTICIPANT))
    submission = make_submission(team, status=SubmissionStatus.SUBMITTED)
    make_assignment(judge, submission)

    assert len(client.get("/api/judging/queue", headers=auth(judge_user)).json()) == 1

    client.patch(
        f"/api/events/{event.slug}/judges/{judge.id}",
        headers=auth(make_user(Role.ORGANIZER)),
        json={"is_active": False},
    )
    assert client.get("/api/judging/queue", headers=auth(judge_user)).json() == []


# --------------------------------------------------------------------------- #
# Removal for cause: incomplete ballots dropped, complete ones kept
# --------------------------------------------------------------------------- #


def remove_judge(client: TestClient, slug: str, headers, judge_id, reason: str | None = "Left the event"):
    body = {} if reason is None else {"reason": reason}
    return client.post(f"/api/events/{slug}/judges/{judge_id}/remove", headers=headers, json=body)


def test_removing_a_judge_requires_a_reason(
    client: TestClient, auth, make_event, make_user, make_judge
) -> None:
    event = make_event()
    judge = make_judge(event, make_user(Role.JUDGE))
    r = remove_judge(client, event.slug, auth(make_user(Role.ORGANIZER)), judge.id, reason=None)
    assert r.status_code == 422


def test_removing_a_judge_deactivates_rather_than_deletes(
    client: TestClient, auth, make_event, make_user, make_judge
) -> None:
    event = make_event()
    judge = make_judge(event, make_user(Role.JUDGE))
    organizer = make_user(Role.ORGANIZER)

    r = remove_judge(client, event.slug, auth(organizer), judge.id)
    assert r.status_code == 200, r.text
    assert r.json()["is_active"] is False

    # The row survives -- still listed, not gone.
    judges = client.get(f"/api/events/{event.slug}/judges", headers=auth(organizer)).json()
    assert any(j["id"] == str(judge.id) for j in judges)


def test_removing_a_judge_drops_incomplete_ballots_but_keeps_completed_ones(
    client: TestClient, auth, make_event, make_user, make_team, make_submission, make_judge,
    make_assignment, make_criterion, make_score,
) -> None:
    event = make_event()
    only = make_criterion(event, key="overall", name="Overall")
    judge_user = make_user(Role.JUDGE)
    judge = make_judge(event, judge_user)
    organizer = make_user(Role.ORGANIZER)

    complete_sub = make_submission(
        make_team(event, make_user(Role.PARTICIPANT)), name="Finished", status=SubmissionStatus.SUBMITTED
    )
    pending_sub = make_submission(
        make_team(event, make_user(Role.PARTICIPANT)), name="Untouched", status=SubmissionStatus.SUBMITTED
    )
    in_progress_sub = make_submission(
        make_team(event, make_user(Role.PARTICIPANT)), name="Half-done", status=SubmissionStatus.SUBMITTED
    )

    completed_ballot = make_assignment(judge, complete_sub, status=AssignmentStatus.COMPLETE)
    make_score(completed_ballot, only, 4)
    make_assignment(judge, pending_sub, status=AssignmentStatus.PENDING)
    in_progress_ballot = make_assignment(judge, in_progress_sub, status=AssignmentStatus.IN_PROGRESS)
    make_score(in_progress_ballot, only, 2)

    # Captured now, as plain strings: the route's own `db.commit()` expires
    # every ORM object in this shared test session, and the incomplete
    # ballot's row will not exist anymore to refresh from afterward.
    completed_id = str(completed_ballot.id)
    in_progress_id = str(in_progress_ballot.id)

    r = remove_judge(client, event.slug, auth(organizer), judge.id, reason="No longer available")
    assert r.status_code == 200, r.text

    remaining = client.get(
        f"/api/events/{event.slug}/assignments?per_page=50", headers=auth(organizer)
    ).json()["items"]
    remaining_ids = {a["id"] for a in remaining}
    assert completed_id in remaining_ids, "a completed ballot must survive removal"
    assert in_progress_id not in remaining_ids, "an incomplete ballot must be dropped"
    assert len(remaining) == 1

    # The completed ballot's score is untouched, not just the row.
    kept = next(a for a in remaining if a["id"] == completed_id)
    assert kept["scores"][0]["value"] == 4


def test_removal_is_recorded_in_the_audit_log_with_the_reason(
    client: TestClient, auth, make_event, make_user, make_judge
) -> None:
    event = make_event()
    judge_user = make_user(Role.JUDGE)
    judge = make_judge(event, judge_user)
    organizer = make_user(Role.ORGANIZER)

    remove_judge(client, event.slug, auth(organizer), judge.id, reason="Unresponsive during the event")

    rows = client.get(f"/api/events/{event.slug}/audit", headers=auth(organizer)).json()["rows"]
    entry = next(r for r in rows if r["action"] == "judge_removed")
    assert "Unresponsive during the event" in entry["summary"]
    assert judge_user.email in entry["summary"]


def test_removing_a_judge_is_staff_only(
    client: TestClient, auth, make_event, make_user, make_judge
) -> None:
    event = make_event()
    judge = make_judge(event, make_user(Role.JUDGE))
    other_judge = make_user(Role.JUDGE)

    assert remove_judge(client, event.slug, auth(other_judge), judge.id).status_code == 403
    # Anonymous gets the same 403 as an authenticated non-staff caller, the
    # same convention every other staff-only route in this codebase uses
    # (see e.g. `test_an_anonymous_caller_cannot_list_webhooks`) -- a 401
    # here would be a different, and inconsistent, answer.
    assert remove_judge(client, event.slug, {}, judge.id).status_code == 403


def test_a_removed_judge_is_excluded_from_the_next_assignment_run(
    client: TestClient, auth, make_event, make_user, make_team, make_submission, make_judge,
    make_criterion,
) -> None:
    event = make_event()
    make_criterion(event, key="overall", name="Overall")
    organizer = make_user(Role.ORGANIZER)
    judge = make_judge(event, make_user(Role.JUDGE))
    make_submission(
        make_team(event, make_user(Role.PARTICIPANT)), name="Needs Review", status=SubmissionStatus.SUBMITTED
    )

    remove_judge(client, event.slug, auth(organizer), judge.id)

    r = client.post(
        f"/api/events/{event.slug}/assignments",
        headers=auth(organizer),
        json={"reviews_per_submission": 1},
    )
    assert r.status_code == 409  # no *active* judges left, same as having none at all


# --------------------------------------------------------------------------- #
# Assignment
# --------------------------------------------------------------------------- #


@pytest.fixture()
def assignable(db, make_event, make_user, make_team, make_submission, make_judge, make_criterion):
    """Six submitted projects and four judges, none of them on a team."""

    def _build(*, reviews: int = 3):
        event = make_event()
        now = utcnow()
        event.judging_opens_at = now - timedelta(hours=1)
        event.judging_closes_at = now + timedelta(days=2)
        db.commit()
        make_criterion(event, key="impact", name="Impact", weight=3)

        for i in range(6):
            team = make_team(event, make_user(Role.PARTICIPANT), name=f"Team {i}")
            make_submission(team, name=f"Project {i}", status=SubmissionStatus.SUBMITTED)
        judges = [make_judge(event, make_user(Role.JUDGE, name=f"Judge {i}")) for i in range(4)]
        return event, make_user(Role.ORGANIZER), judges

    return _build


def test_batch_assignment_covers_every_project(client: TestClient, auth, assignable):
    event, organizer, judges = assignable()
    r = client.post(
        f"/api/events/{event.slug}/assignments",
        headers=auth(organizer),
        json={"reviews_per_submission": 2, "seed": 7},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["created"] == 12  # 6 projects x 2 reviews
    assert body["shortfalls"] == []
    assert body["balanced"] is True
    assert sorted(body["loads"].values()) == [3, 3, 3, 3]


def test_a_dry_run_writes_nothing(client: TestClient, auth, assignable):
    event, organizer, _ = assignable()
    headers = auth(organizer)
    r = client.post(
        f"/api/events/{event.slug}/assignments",
        headers=headers,
        json={"reviews_per_submission": 2, "seed": 7, "dry_run": True},
    )
    assert r.json()["created"] == 12
    assert r.json()["dry_run"] is True
    assert client.get(f"/api/events/{event.slug}/assignments", headers=headers).json()["items"] == []


def test_re_running_assignment_tops_up_rather_than_duplicating(
    client: TestClient, auth, assignable
):
    """Idempotence, which is what makes the button safe to press twice."""
    event, organizer, _ = assignable()
    headers = auth(organizer)
    body = {"reviews_per_submission": 2, "seed": 7}

    client.post(f"/api/events/{event.slug}/assignments", headers=headers, json=body)
    second = client.post(f"/api/events/{event.slug}/assignments", headers=headers, json=body)
    assert second.json()["created"] == 0

    total = client.get(f"/api/events/{event.slug}/assignments", headers=headers).json()["total"]
    assert total == 12


def test_raising_the_review_target_adds_only_what_is_missing(
    client: TestClient, auth, assignable
):
    event, organizer, _ = assignable()
    headers = auth(organizer)
    client.post(
        f"/api/events/{event.slug}/assignments", headers=headers,
        json={"reviews_per_submission": 2, "seed": 7},
    )
    bumped = client.post(
        f"/api/events/{event.slug}/assignments", headers=headers,
        json={"reviews_per_submission": 3, "seed": 7},
    )
    assert bumped.json()["created"] == 6  # one more review for each of six projects


def test_assignment_needs_judges_and_projects(
    client: TestClient, auth, make_event, make_user, make_team, make_submission
):
    event = make_event()
    headers = auth(make_user(Role.ORGANIZER))
    no_judges = client.post(
        f"/api/events/{event.slug}/assignments", headers=headers, json={"reviews_per_submission": 2}
    )
    assert no_judges.status_code == 409
    assert "no active judges" in no_judges.json()["detail"]


def test_a_draft_is_never_assigned(
    client: TestClient, auth, make_event, make_user, make_team, make_submission, make_judge, db
):
    """A draft is not an entry, so it is not judged."""
    event = make_event()
    make_judge(event, make_user(Role.JUDGE))
    team = make_team(event, make_user(Role.PARTICIPANT))
    make_submission(team, status=SubmissionStatus.DRAFT)

    r = client.post(
        f"/api/events/{event.slug}/assignments",
        headers=auth(make_user(Role.ORGANIZER)),
        json={"reviews_per_submission": 1},
    )
    assert r.status_code == 409
    assert "no submitted projects" in r.json()["detail"]


# --------------------------------------------------------------------------- #
# Scoring a ballot
# --------------------------------------------------------------------------- #


@pytest.fixture()
def one_ballot(
    db, make_event, make_user, make_team, make_submission, make_judge, make_assignment,
    make_criterion,
):
    def _build():
        event = make_event()
        now = utcnow()
        event.judging_opens_at = now - timedelta(hours=1)
        event.judging_closes_at = now + timedelta(days=2)
        db.commit()
        impact = make_criterion(event, key="impact", name="Impact", weight=3, position=0)
        craft = make_criterion(event, key="craft", name="Craft", weight=1, position=1)
        judge_user = make_user(Role.JUDGE)
        judge = make_judge(event, judge_user)
        team = make_team(event, make_user(Role.PARTICIPANT))
        submission = make_submission(team, status=SubmissionStatus.SUBMITTED)
        return event, judge_user, make_assignment(judge, submission), impact, craft

    return _build


def test_a_full_ballot_completes_and_reports_its_weighted_score(
    client: TestClient, auth, one_ballot
):
    """impact×3 = 5, craft×1 = 1  ->  (15 + 1) / 4 = 4.0"""
    _event, judge_user, ballot, impact, craft = one_ballot()
    r = client.put(
        f"/api/judging/assignments/{ballot.id}/scores",
        headers=auth(judge_user),
        json={
            "scores": [
                {"criterion_id": str(impact.id), "value": 5},
                {"criterion_id": str(craft.id), "value": 1},
            ],
            "comment": "Strong idea, rough edges.",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == AssignmentStatus.COMPLETE.value
    assert body["raw_score"] == pytest.approx(4.0)
    assert body["comment"] == "Strong idea, rough edges."


def test_a_partial_ballot_stays_in_progress(client: TestClient, auth, one_ballot):
    """Claiming completion with half a rubric filled in would corrupt both the
    progress dashboard and the normalization."""
    _event, judge_user, ballot, impact, _craft = one_ballot()
    r = client.put(
        f"/api/judging/assignments/{ballot.id}/scores",
        headers=auth(judge_user),
        json={"scores": [{"criterion_id": str(impact.id), "value": 4}], "complete": True},
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == AssignmentStatus.IN_PROGRESS.value


def test_re_scoring_updates_in_place(client: TestClient, auth, one_ballot):
    _event, judge_user, ballot, impact, craft = one_ballot()
    headers = auth(judge_user)
    payload = {
        "scores": [
            {"criterion_id": str(impact.id), "value": 2},
            {"criterion_id": str(craft.id), "value": 2},
        ]
    }
    client.put(f"/api/judging/assignments/{ballot.id}/scores", headers=headers, json=payload)
    payload["scores"][0]["value"] = 5
    again = client.put(
        f"/api/judging/assignments/{ballot.id}/scores", headers=headers, json=payload
    )
    assert again.status_code == 200
    assert len(again.json()["scores"]) == 2  # updated, not appended
    assert again.json()["raw_score"] == pytest.approx((5 * 3 + 2 * 1) / 4)


def test_a_score_outside_the_rubrics_range_is_rejected(client: TestClient, auth, one_ballot):
    _event, judge_user, ballot, impact, _craft = one_ballot()
    r = client.put(
        f"/api/judging/assignments/{ballot.id}/scores",
        headers=auth(judge_user),
        json={"scores": [{"criterion_id": str(impact.id), "value": 9}]},
    )
    assert r.status_code == 422
    assert "outside 1-5" in r.json()["detail"]


def test_a_ballot_accepts_a_continuous_decimal_score(client: TestClient, auth, one_ballot):
    """Scoring is continuous, not discrete: 4.5 is a real judgement between 4
    and 5, not something that has to round to one or the other."""
    _event, judge_user, ballot, impact, craft = one_ballot()
    r = client.put(
        f"/api/judging/assignments/{ballot.id}/scores",
        headers=auth(judge_user),
        json={
            "scores": [
                {"criterion_id": str(impact.id), "value": 4.5},
                {"criterion_id": str(craft.id), "value": 3.1},
            ],
            "complete": True,
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == AssignmentStatus.COMPLETE.value
    values = {s["criterion_id"]: s["value"] for s in body["scores"]}
    assert values[str(impact.id)] == pytest.approx(4.5)
    assert values[str(craft.id)] == pytest.approx(3.1)
    assert body["raw_score"] == pytest.approx((4.5 * 3 + 3.1 * 1) / 4)


def test_a_whole_number_score_is_still_a_valid_point_on_the_continuous_scale(
    client: TestClient, auth, one_ballot
):
    """A regression for the move off a discrete column: a plain integer --
    exactly what every already-seeded ballot holds -- is not a special case,
    it is one ordinary value among many on the same 0.1-step scale."""
    _event, judge_user, ballot, impact, craft = one_ballot()
    r = client.put(
        f"/api/judging/assignments/{ballot.id}/scores",
        headers=auth(judge_user),
        json={
            "scores": [
                {"criterion_id": str(impact.id), "value": 5},
                {"criterion_id": str(craft.id), "value": 1},
            ],
            "complete": True,
        },
    )
    assert r.status_code == 200, r.text
    values = {s["criterion_id"]: s["value"] for s in r.json()["scores"]}
    assert values[str(impact.id)] == pytest.approx(5.0)
    assert values[str(craft.id)] == pytest.approx(1.0)


def test_a_score_finer_than_one_tenth_is_rejected(client: TestClient, auth, one_ballot):
    """0.1 is the smallest step a judge can express -- 4.33 is not a coarser
    version of some real value, it is simply outside the scale's precision."""
    _event, judge_user, ballot, impact, _craft = one_ballot()
    r = client.put(
        f"/api/judging/assignments/{ballot.id}/scores",
        headers=auth(judge_user),
        json={"scores": [{"criterion_id": str(impact.id), "value": 4.33}]},
    )
    assert r.status_code == 422


def test_a_criterion_from_another_event_is_rejected(
    client: TestClient, auth, one_ballot, make_event, make_criterion
):
    _event, judge_user, ballot, _impact, _craft = one_ballot()
    foreign = make_criterion(make_event(), key="foreign", name="Foreign")
    r = client.put(
        f"/api/judging/assignments/{ballot.id}/scores",
        headers=auth(judge_user),
        json={"scores": [{"criterion_id": str(foreign.id), "value": 3}]},
    )
    assert r.status_code == 422
    assert "does not belong to this event" in r.json()["detail"]


def test_scoring_a_disqualified_project_is_refused(
    client: TestClient, auth, one_ballot, make_user
) -> None:
    """A judge whose assignment predates the disqualification cannot complete
    it after the fact -- distinct, with its own message, from `submit()`'s own
    "never submitted" case."""
    _event, judge_user, ballot, impact, craft = one_ballot()
    disqualify = client.post(
        f"/api/submissions/{ballot.submission_id}/disqualify",
        json={"reason": "Withdrawn for cause"},
        headers=auth(make_user(Role.ORGANIZER)),
    )
    assert disqualify.status_code == 200, disqualify.text

    r = client.put(
        f"/api/judging/assignments/{ballot.id}/scores",
        headers=auth(judge_user),
        json={
            "scores": [
                {"criterion_id": str(impact.id), "value": 5},
                {"criterion_id": str(craft.id), "value": 1},
            ]
        },
    )
    assert r.status_code == 409
    assert r.json()["detail"] == "That project has been disqualified"


def test_two_scores_for_one_criterion_is_rejected(client: TestClient, auth, one_ballot):
    _event, judge_user, ballot, impact, _craft = one_ballot()
    r = client.put(
        f"/api/judging/assignments/{ballot.id}/scores",
        headers=auth(judge_user),
        json={
            "scores": [
                {"criterion_id": str(impact.id), "value": 3},
                {"criterion_id": str(impact.id), "value": 4},
            ]
        },
    )
    assert r.status_code == 422


def test_scoring_an_event_with_no_rubric_is_a_conflict(
    client: TestClient, auth, db, make_event, make_user, make_team, make_submission, make_judge,
    make_assignment,
):
    event = make_event()
    now = utcnow()
    event.judging_opens_at = now - timedelta(hours=1)
    event.judging_closes_at = now + timedelta(days=2)
    db.commit()
    judge_user = make_user(Role.JUDGE)
    judge = make_judge(event, judge_user)
    team = make_team(event, make_user(Role.PARTICIPANT))
    submission = make_submission(team, status=SubmissionStatus.SUBMITTED)
    ballot = make_assignment(judge, submission)

    import uuid as _uuid

    r = client.put(
        f"/api/judging/assignments/{ballot.id}/scores",
        headers=auth(judge_user),
        json={"scores": [{"criterion_id": str(_uuid.uuid4()), "value": 3}]},
    )
    assert r.status_code == 409
    assert "no rubric" in r.json()["detail"]


# --------------------------------------------------------------------------- #
# Progress
# --------------------------------------------------------------------------- #


def test_progress_names_who_has_not_started(client: TestClient, auth, assignable):
    """The question the dashboard exists to answer."""
    event, organizer, judges = assignable()
    headers = auth(organizer)
    client.post(
        f"/api/events/{event.slug}/assignments",
        headers=headers,
        json={"reviews_per_submission": 2, "seed": 7},
    )
    r = client.get(f"/api/events/{event.slug}/judging/progress", headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["assignments_total"] == 12
    assert body["assignments_complete"] == 0
    assert body["percent_complete"] == 0.0
    assert body["judging_open"] is True
    assert len(body["not_started"]) == 4
    assert body["submissions_fully_reviewed"] == 0


def test_progress_counts_a_finished_ballot(client: TestClient, auth, one_ballot, make_user):
    event, judge_user, ballot, impact, craft = one_ballot()
    client.put(
        f"/api/judging/assignments/{ballot.id}/scores",
        headers=auth(judge_user),
        json={
            "scores": [
                {"criterion_id": str(impact.id), "value": 4},
                {"criterion_id": str(craft.id), "value": 4},
            ]
        },
    )
    r = client.get(
        f"/api/events/{event.slug}/judging/progress", headers=auth(make_user(Role.ORGANIZER))
    )
    body = r.json()
    assert body["assignments_complete"] == 1
    assert body["percent_complete"] == 100.0
    assert body["submissions_fully_reviewed"] == 1
    assert body["not_started"] == []


# --------------------------------------------------------------------------- #
# Results
# --------------------------------------------------------------------------- #


@pytest.fixture()
def scored_results(
    db, make_event, make_user, make_team, make_submission, make_judge, make_assignment,
    make_criterion, make_score,
):
    """A harsh judge and a generous judge, both reviewing all three projects.

    The shape the normalization exists for: identical orderings, different levels.
    """

    def _build():
        event = make_event()
        now = utcnow()
        event.judging_opens_at = now - timedelta(hours=1)
        event.judging_closes_at = now + timedelta(days=2)
        db.commit()
        only = make_criterion(event, key="overall", name="Overall", weight=1)

        subs = []
        for i in range(3):
            team = make_team(event, make_user(Role.PARTICIPANT), name=f"Team {i}")
            subs.append(
                make_submission(team, name=f"Project {i}", status=SubmissionStatus.SUBMITTED)
            )

        harsh = make_judge(event, make_user(Role.JUDGE, name="Harsh"))
        generous = make_judge(event, make_user(Role.JUDGE, name="Generous"))
        for judge, values in ((harsh, [1, 2, 3]), (generous, [3, 4, 5])):
            for submission, value in zip(subs, values):
                ballot = make_assignment(judge, submission, status=AssignmentStatus.COMPLETE)
                make_score(ballot, only, value)

        return event, make_user(Role.ORGANIZER), subs

    return _build


def test_results_publish_raw_and_normalized_side_by_side(
    client: TestClient, auth, scored_results
):
    event, organizer, _subs = scored_results()
    r = client.get(f"/api/events/{event.slug}/results", headers=auth(organizer))
    assert r.status_code == 200, r.text
    body = r.json()

    assert len(body["rows"]) == 3
    assert body["rows"][0]["normalized_rank"] == 1
    # Both judges ranked Project 2 top, so it wins on either measure.
    assert body["rows"][0]["submission_name"] == "Project 2"
    for row in body["rows"]:
        assert row["n_reviews"] == 2
        assert row["rank_delta"] == row["raw_rank"] - row["normalized_rank"]
    assert "z-score" in body["method"]


def test_results_publish_each_judges_calibration(client: TestClient, auth, scored_results):
    """An organizer asked "why did this move" needs the per-judge numbers."""
    event, organizer, _ = scored_results()
    body = client.get(f"/api/events/{event.slug}/results", headers=auth(organizer)).json()
    by_name = {c["judge_name"]: c for c in body["calibrations"]}
    assert set(by_name) == {"Harsh", "Generous"}
    assert by_name["Harsh"]["mean"] == pytest.approx(2.0)
    assert by_name["Generous"]["mean"] == pytest.approx(4.0)
    assert by_name["Harsh"]["flat"] is False


def test_disqualifying_a_submission_excludes_it_from_results(
    client: TestClient, auth, scored_results
):
    """`gather()` had no `Submission.status` filter at all before Phase 6 --
    every other status-sensitive query in the codebase excludes a non-
    `SUBMITTED` row by construction, but this table never checked status to
    begin with, since it predates there being a third one to exclude."""
    event, organizer, subs = scored_results()
    target = subs[1]
    headers = auth(organizer)

    disqualify = client.post(
        f"/api/submissions/{target.id}/disqualify",
        json={"reason": "Post-deadline rule violation"},
        headers=headers,
    )
    assert disqualify.status_code == 200, disqualify.text

    body = client.get(f"/api/events/{event.slug}/results", headers=headers).json()
    names = {row["submission_name"] for row in body["rows"]}
    assert target.name not in names
    assert len(body["rows"]) == 2


def test_unfinished_ballots_are_excluded_from_results_by_default(
    client: TestClient, auth, db, make_event, make_user, make_team, make_submission, make_judge,
    make_assignment, make_criterion, make_score,
):
    """A half-filled ballot is not a judgement."""
    event = make_event()
    only = make_criterion(event, key="overall", name="Overall")
    judge = make_judge(event, make_user(Role.JUDGE))
    team = make_team(event, make_user(Role.PARTICIPANT))
    submission = make_submission(team, status=SubmissionStatus.SUBMITTED)
    ballot = make_assignment(judge, submission, status=AssignmentStatus.IN_PROGRESS)
    make_score(ballot, only, 5)

    headers = auth(make_user(Role.ORGANIZER))
    assert client.get(f"/api/events/{event.slug}/results", headers=headers).json()["rows"] == []

    provisional = client.get(
        f"/api/events/{event.slug}/results?provisional=true", headers=headers
    ).json()
    assert len(provisional["rows"]) == 1


# --------------------------------------------------------------------------- #
# CSV export
# --------------------------------------------------------------------------- #


def _rows(response) -> list[list[str]]:
    return list(csv.reader(io.StringIO(response.text)))


def test_every_export_is_csv_with_a_header(client: TestClient, auth, scored_results):
    event, organizer, _ = scored_results()
    headers = auth(organizer)
    for entity in ("teams", "submissions", "directory", "judges", "assignments", "scores", "results"):
        r = client.get(f"/api/events/{event.slug}/export/{entity}.csv", headers=headers)
        assert r.status_code == 200, f"{entity}: {r.text}"
        assert r.headers["content-type"].startswith("text/csv")
        assert f"{event.slug}-{entity}.csv" in r.headers["content-disposition"]
        assert len(_rows(r)[0]) > 1, f"{entity} had no header"


def test_the_scores_export_is_one_row_per_judge_project_criterion(
    client: TestClient, auth, scored_results
):
    event, organizer, _ = scored_results()
    r = client.get(f"/api/events/{event.slug}/export/scores.csv", headers=auth(organizer))
    rows = _rows(r)
    assert rows[0][:4] == ["judge_email", "judge_name", "submission", "team"]
    # 2 judges x 3 projects x 1 criterion
    assert len([row for row in rows[1:] if row]) == 6


def test_the_results_export_carries_both_rankings(client: TestClient, auth, scored_results):
    event, organizer, _ = scored_results()
    r = client.get(f"/api/events/{event.slug}/export/results.csv", headers=auth(organizer))
    rows = _rows(r)
    assert rows[0][:3] == ["rank", "raw_rank", "rank_delta"]
    assert len([row for row in rows[1:] if row]) == 3


def test_an_empty_event_still_exports_a_header(
    client: TestClient, auth, make_event, make_user
):
    """A consumer that has to special-case the empty file will break on a quiet
    event."""
    event = make_event()
    r = client.get(
        f"/api/events/{event.slug}/export/scores.csv", headers=auth(make_user(Role.ORGANIZER))
    )
    assert r.status_code == 200
    rows = [row for row in _rows(r) if row]
    assert len(rows) == 1
    assert rows[0][0] == "judge_email"


def test_an_unknown_export_entity_is_a_404(client: TestClient, auth, make_event, make_user):
    event = make_event()
    r = client.get(
        f"/api/events/{event.slug}/export/nonsense.csv", headers=auth(make_user(Role.ORGANIZER))
    )
    assert r.status_code == 404
    assert "Available" in r.json()["detail"]


# --------------------------------------------------------------------------- #
# Judging integrity: disagreement, third review, consistency, recusal
# --------------------------------------------------------------------------- #


@pytest.fixture()
def disagreeing_pair(
    db, make_event, make_user, make_team, make_submission, make_judge, make_assignment,
    make_criterion, make_score,
):
    """Two judges whose *normalized* scores still cross on two of three
    projects -- real disagreement about the project, not just a different
    level -- plus a third judge with no ballots yet, free to adjudicate."""

    def _build():
        event = make_event()
        now = utcnow()
        event.judging_opens_at = now - timedelta(hours=1)
        event.judging_closes_at = now + timedelta(days=2)
        db.commit()
        only = make_criterion(event, key="overall", name="Overall", weight=1)

        subs = []
        for i in range(3):
            team = make_team(event, make_user(Role.PARTICIPANT), name=f"Team {i}")
            subs.append(
                make_submission(team, name=f"Project {i}", status=SubmissionStatus.SUBMITTED)
            )

        users = {
            "harsh": make_user(Role.JUDGE, name="Harsh"),
            "generous": make_user(Role.JUDGE, name="Generous"),
            "third": make_user(Role.JUDGE, name="Referee"),
        }
        judges = {name: make_judge(event, user) for name, user in users.items()}
        for name, values in (("harsh", [1, 3, 5]), ("generous", [5, 3, 1])):
            for submission, value in zip(subs, values):
                ballot = make_assignment(judges[name], submission, status=AssignmentStatus.COMPLETE)
                make_score(ballot, only, value)

        return event, make_user(Role.ORGANIZER), subs, judges, users

    return _build


def test_disagreement_flags_the_projects_the_judges_crossed_on(
    client: TestClient, auth, disagreeing_pair
):
    event, organizer, subs, _judges, _users = disagreeing_pair()
    body = client.get(
        f"/api/events/{event.slug}/results/disagreement", headers=auth(organizer)
    ).json()
    flagged = {r["submission_id"] for r in body["submissions"] if r["needs_review"]}
    assert str(subs[0].id) in flagged  # harsh=1, generous=5
    assert str(subs[2].id) in flagged  # harsh=5, generous=1
    assert str(subs[1].id) not in flagged  # both scored 3
    assert body["needs_review_count"] == 2


def test_disagreement_is_staff_only(client: TestClient, auth, disagreeing_pair):
    event, _organizer, _subs, _judges, users = disagreeing_pair()
    assert (
        client.get(
            f"/api/events/{event.slug}/results/disagreement", headers=auth(users["third"])
        ).status_code
        == 403
    )
    assert client.get(f"/api/events/{event.slug}/results/disagreement").status_code == 403


def test_third_review_assigns_the_uninvolved_judge(client: TestClient, auth, disagreeing_pair):
    event, organizer, subs, judges, _users = disagreeing_pair()
    headers = auth(organizer)
    r = client.post(
        f"/api/events/{event.slug}/results/disagreement/{subs[0].id}/third-review",
        headers=headers,
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["judge_id"] == str(judges["third"].id)
    assert body["submission_id"] == str(subs[0].id)

    assignments = client.get(f"/api/events/{event.slug}/assignments", headers=headers).json()["items"]
    new = next(a for a in assignments if a["id"] == body["assignment_id"])
    assert new["is_adjudication"] is True
    assert new["judge_id"] == str(judges["third"].id)


def test_third_review_does_not_touch_the_original_ballots(
    client: TestClient, auth, disagreeing_pair
):
    """Additive, never a replacement: adding a third reviewer must not change
    what the two judges who disagreed already scored."""
    event, organizer, subs, _judges, _users = disagreeing_pair()
    headers = auth(organizer)
    before = client.get(f"/api/events/{event.slug}/results", headers=headers).json()
    client.post(
        f"/api/events/{event.slug}/results/disagreement/{subs[0].id}/third-review",
        headers=headers,
    )
    after = client.get(f"/api/events/{event.slug}/results", headers=headers).json()
    row_before = next(r for r in before["rows"] if r["submission_id"] == str(subs[0].id))
    row_after = next(r for r in after["rows"] if r["submission_id"] == str(subs[0].id))
    assert row_before == row_after


def test_third_review_refuses_when_no_judge_is_eligible(client: TestClient, auth, scored_results):
    """`scored_results`'s two judges have both already reviewed every
    project -- there is nobody left uninvolved to adjudicate."""
    event, organizer, subs = scored_results()
    r = client.post(
        f"/api/events/{event.slug}/results/disagreement/{subs[0].id}/third-review",
        headers=auth(organizer),
    )
    assert r.status_code == 409


def test_third_review_is_recorded_in_the_audit_log(client: TestClient, auth, disagreeing_pair):
    event, organizer, subs, _judges, _users = disagreeing_pair()
    headers = auth(organizer)
    client.post(
        f"/api/events/{event.slug}/results/disagreement/{subs[0].id}/third-review",
        headers=headers,
    )
    rows = client.get(f"/api/events/{event.slug}/audit", headers=headers).json()["rows"]
    assert any(r["action"] == "third_review_assigned" for r in rows)


def test_consistency_flags_a_judge_with_uniform_scores(
    client: TestClient, auth, db, make_event, make_user, make_team, make_submission,
    make_judge, make_assignment, make_criterion, make_score,
):
    event = make_event()
    now = utcnow()
    event.judging_opens_at = now - timedelta(hours=1)
    event.judging_closes_at = now + timedelta(days=2)
    db.commit()
    only = make_criterion(event, key="overall", name="Overall", weight=1)
    judge = make_judge(event, make_user(Role.JUDGE, name="Flatline"))
    for i in range(4):
        team = make_team(event, make_user(Role.PARTICIPANT), name=f"Team {i}")
        submission = make_submission(team, name=f"Project {i}", status=SubmissionStatus.SUBMITTED)
        ballot = make_assignment(judge, submission, status=AssignmentStatus.COMPLETE)
        make_score(ballot, only, 3)

    organizer = make_user(Role.ORGANIZER)
    body = client.get(f"/api/events/{event.slug}/results/consistency", headers=auth(organizer)).json()
    flags = [f for f in body["flags"] if f["judge_id"] == str(judge.id)]
    assert any(f["reason"] == "near_identical_scores" for f in flags)


def test_consistency_flags_ballots_completed_in_rapid_succession(
    client: TestClient, auth, db, make_event, make_user, make_team, make_submission,
    make_judge, make_assignment, make_criterion, make_score,
):
    event = make_event()
    now = utcnow()
    event.judging_opens_at = now - timedelta(hours=1)
    event.judging_closes_at = now + timedelta(days=2)
    db.commit()
    only = make_criterion(event, key="overall", name="Overall", weight=1)
    judge = make_judge(event, make_user(Role.JUDGE, name="Speedy"))
    for i, value in enumerate([1, 4, 2]):  # varied, so this is not also near-identical
        team = make_team(event, make_user(Role.PARTICIPANT), name=f"Team {i}")
        submission = make_submission(team, name=f"Project {i}", status=SubmissionStatus.SUBMITTED)
        ballot = make_assignment(judge, submission, status=AssignmentStatus.COMPLETE)
        make_score(ballot, only, value)

    organizer = make_user(Role.ORGANIZER)
    body = client.get(f"/api/events/{event.slug}/results/consistency", headers=auth(organizer)).json()
    flags = [f for f in body["flags"] if f["judge_id"] == str(judge.id)]
    assert any(f["reason"] == "fast_completion" for f in flags)
    assert not any(f["reason"] == "near_identical_scores" for f in flags)


def test_consistency_is_staff_only(client: TestClient, auth, disagreeing_pair):
    event, _organizer, _subs, _judges, users = disagreeing_pair()
    assert (
        client.get(
            f"/api/events/{event.slug}/results/consistency", headers=auth(users["third"])
        ).status_code
        == 403
    )
    assert client.get(f"/api/events/{event.slug}/results/consistency").status_code == 403


def test_organizer_can_recuse_a_judge_from_one_project(client: TestClient, auth, disagreeing_pair):
    event, organizer, subs, judges, _users = disagreeing_pair()
    headers = auth(organizer)
    r = client.post(
        f"/api/events/{event.slug}/judges/{judges['third'].id}/recusals",
        headers=headers,
        json={"submission_id": str(subs[0].id), "reason": "Personal friend of the team"},
    )
    assert r.status_code == 201, r.text
    assert r.json()["judge_name"] == "Referee"

    listed = client.get(
        f"/api/events/{event.slug}/judges/{judges['third'].id}/recusals", headers=headers
    ).json()
    assert len(listed) == 1
    assert listed[0]["submission_id"] == str(subs[0].id)


def test_a_duplicate_recusal_is_rejected(client: TestClient, auth, disagreeing_pair):
    event, organizer, subs, judges, _users = disagreeing_pair()
    headers = auth(organizer)
    body = {"submission_id": str(subs[0].id)}
    url = f"/api/events/{event.slug}/judges/{judges['third'].id}/recusals"
    assert client.post(url, headers=headers, json=body).status_code == 201
    assert client.post(url, headers=headers, json=body).status_code == 409


def test_a_recused_judge_is_excluded_from_third_review(client: TestClient, auth, disagreeing_pair):
    event, organizer, subs, judges, _users = disagreeing_pair()
    headers = auth(organizer)
    client.post(
        f"/api/events/{event.slug}/judges/{judges['third'].id}/recusals",
        headers=headers,
        json={"submission_id": str(subs[0].id)},
    )
    r = client.post(
        f"/api/events/{event.slug}/results/disagreement/{subs[0].id}/third-review",
        headers=headers,
    )
    assert r.status_code == 409  # the only eligible judge is now recused


def test_recusal_removes_a_stale_incomplete_assignment(
    client: TestClient, auth, disagreeing_pair, make_assignment
):
    event, organizer, subs, judges, _users = disagreeing_pair()
    headers = auth(organizer)
    pending = make_assignment(judges["third"], subs[1], status=AssignmentStatus.PENDING)

    client.post(
        f"/api/events/{event.slug}/judges/{judges['third'].id}/recusals",
        headers=headers,
        json={"submission_id": str(subs[1].id)},
    )
    assignments = client.get(f"/api/events/{event.slug}/assignments", headers=headers).json()["items"]
    assert not any(a["id"] == str(pending.id) for a in assignments)


def test_recusal_leaves_a_complete_assignment_in_place(client: TestClient, auth, disagreeing_pair):
    """A completed ballot is a historical record -- recusing a judge after the
    fact does not retroactively erase it."""
    event, organizer, subs, judges, _users = disagreeing_pair()
    headers = auth(organizer)
    client.post(
        f"/api/events/{event.slug}/judges/{judges['harsh'].id}/recusals",
        headers=headers,
        json={"submission_id": str(subs[0].id)},
    )
    assignments = client.get(f"/api/events/{event.slug}/assignments", headers=headers).json()["items"]
    complete = [
        a for a in assignments
        if a["judge_id"] == str(judges["harsh"].id) and a["submission"]["id"] == str(subs[0].id)
    ]
    assert len(complete) == 1
    assert complete[0]["status"] == "complete"


def test_a_judge_can_recuse_themself_from_an_incomplete_ballot(
    client: TestClient, auth, disagreeing_pair, make_assignment
):
    event, organizer, subs, judges, users = disagreeing_pair()
    pending = make_assignment(judges["third"], subs[1], status=AssignmentStatus.PENDING)

    r = client.post(
        f"/api/judging/assignments/{pending.id}/recuse",
        headers=auth(users["third"]),
        json={"reason": "I know this team personally"},
    )
    assert r.status_code == 201, r.text
    assert r.json()["submission_id"] == str(subs[1].id)

    headers = auth(organizer)
    assignments = client.get(f"/api/events/{event.slug}/assignments", headers=headers).json()["items"]
    assert not any(a["id"] == str(pending.id) for a in assignments)


def test_a_judge_cannot_recuse_themself_from_a_complete_ballot(
    client: TestClient, auth, disagreeing_pair
):
    event, _organizer, subs, judges, users = disagreeing_pair()
    complete_assignment_id = None
    # `harsh` already has a COMPLETE ballot on subs[0] from the fixture.
    headers = auth(users["harsh"])
    queue = client.get("/api/judging/queue", headers=headers, params={"event": event.slug}).json()
    complete_assignment_id = next(
        a["id"] for a in queue if a["submission"]["id"] == str(subs[0].id)
    )

    r = client.post(
        f"/api/judging/assignments/{complete_assignment_id}/recuse",
        headers=headers,
        json={},
    )
    assert r.status_code == 409


def test_a_judge_cannot_recuse_a_peers_ballot(client: TestClient, auth, disagreeing_pair, make_assignment):
    event, _organizer, subs, judges, users = disagreeing_pair()
    pending = make_assignment(judges["third"], subs[1], status=AssignmentStatus.PENDING)

    r = client.post(
        f"/api/judging/assignments/{pending.id}/recuse",
        headers=auth(users["harsh"]),
        json={},
    )
    assert r.status_code == 403


def test_removing_a_recusal_is_staff_only_and_works(client: TestClient, auth, disagreeing_pair):
    event, organizer, subs, judges, users = disagreeing_pair()
    headers = auth(organizer)
    created = client.post(
        f"/api/events/{event.slug}/judges/{judges['third'].id}/recusals",
        headers=headers,
        json={"submission_id": str(subs[0].id)},
    ).json()

    assert (
        client.delete(
            f"/api/events/{event.slug}/recusals/{created['id']}", headers=auth(users["third"])
        ).status_code
        == 403
    )
    assert (
        client.delete(f"/api/events/{event.slug}/recusals/{created['id']}", headers=headers).status_code
        == 204
    )
    listed = client.get(
        f"/api/events/{event.slug}/judges/{judges['third'].id}/recusals", headers=headers
    ).json()
    assert listed == []
