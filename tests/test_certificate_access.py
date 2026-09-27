"""Two certificate fixes, tested together since both touch the same system.

1. **The self-service lookup was public with no ownership check.**
   `GET /submissions/{id}/certificates` used to list every certificate
   issued for a project -- including the `code`, the one thing that
   actually lets someone download it -- to anyone who could search up the
   project name. The isolation test for that gap is written first, per
   instruction, before anything else in this file.

2. **Judges and organizers are now admin-issued, not self-service.**
   `POST /events/{slug}/certificates/issue-for-user` is staff-only and
   creates a `judging` or `organizing` record for one specific account, on
   demand -- never through anything the subject triggers themselves.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from app.models import AssignmentStatus, Role, SubmissionStatus, utcnow


@pytest.fixture()
def finished_event(
    db, make_event, make_user, make_team, make_submission, make_judge, make_assignment,
    make_criterion, make_score,
):
    def _build():
        event = make_event(deadline=utcnow() - timedelta(hours=1))
        event.judging_opens_at = utcnow() - timedelta(days=1)
        event.judging_closes_at = utcnow() - timedelta(hours=1)
        db.commit()
        criterion = make_criterion(event, key="overall", name="Overall", weight=1)

        owner = make_user(Role.PARTICIPANT, name="Wei Zhang")
        teammate = make_user(Role.PARTICIPANT, name="Leah Brennan")
        team = make_team(event, owner, teammate, name="Quorum Collective")
        submission = make_submission(team, name="Quorum", status=SubmissionStatus.SUBMITTED)

        judge_user = make_user(Role.JUDGE, name="Priya Rivera")
        judge = make_judge(event, judge_user)
        ballot = make_assignment(judge, submission, status=AssignmentStatus.COMPLETE)
        make_score(ballot, criterion, 5)

        organizer = make_user(Role.ORGANIZER, name="Marion Vasquez")

        return {
            "event": event,
            "owner": owner,
            "teammate": teammate,
            "submission": submission,
            "judge_user": judge_user,
            "organizer": organizer,
        }

    return _build


# --------------------------------------------------------------------------- #
# Isolation, written first
# --------------------------------------------------------------------------- #


def test_a_stranger_cannot_list_another_teams_certificates(
    client: TestClient, auth, make_user, finished_event
) -> None:
    ctx = finished_event()
    client.post(
        f"/api/events/{ctx['event'].slug}/certificates/issue",
        headers=auth(ctx["organizer"]),
    )
    stranger = make_user(Role.PARTICIPANT, name="Someone Else")

    response = client.get(
        f"/api/submissions/{ctx['submission'].id}/certificates", headers=auth(stranger)
    )
    assert response.status_code == 403


def test_an_anonymous_caller_cannot_list_certificates_for_a_project(
    client: TestClient, auth, finished_event
) -> None:
    ctx = finished_event()
    client.post(
        f"/api/events/{ctx['event'].slug}/certificates/issue", headers=auth(ctx["organizer"])
    )
    response = client.get(f"/api/submissions/{ctx['submission'].id}/certificates")
    assert response.status_code in (401, 403)


def test_the_response_never_carries_another_teams_certificate_code(
    client: TestClient, auth, make_user, make_team, make_submission, finished_event
) -> None:
    """Not just refused for a stranger -- a real team member elsewhere on the
    same event must never see a code belonging to a different project."""
    ctx = finished_event()
    other_team = make_team(ctx["event"], make_user(Role.PARTICIPANT, name="Diego Morales"))
    other_submission = make_submission(
        other_team, name="Latchkey", status=SubmissionStatus.SUBMITTED
    )
    client.post(
        f"/api/events/{ctx['event'].slug}/certificates/issue", headers=auth(ctx["organizer"])
    )

    body = client.get(
        f"/api/submissions/{ctx['submission'].id}/certificates", headers=auth(ctx["owner"])
    ).json()
    dumped = str(body)
    other_codes = client.get(
        f"/api/submissions/{other_submission.id}/certificates", headers=auth(ctx["organizer"])
    ).json()
    for row in other_codes:
        assert row["code"] not in dumped


def test_a_team_member_sees_their_own_certificate(
    client: TestClient, auth, finished_event
) -> None:
    ctx = finished_event()
    client.post(
        f"/api/events/{ctx['event'].slug}/certificates/issue", headers=auth(ctx["organizer"])
    )
    rows = client.get(
        f"/api/submissions/{ctx['submission'].id}/certificates", headers=auth(ctx["owner"])
    ).json()
    kinds = {r["kind"] for r in rows}
    names = {r["subject_name"] for r in rows}
    assert "participation" in kinds
    assert "placement" in kinds
    assert ctx["owner"].display_name in names
    # Not the teammate's own participation record -- only the shared
    # placement record (which names the team, not a person) is visible to
    # both, alongside each member's *own* participation record.
    assert ctx["teammate"].display_name not in names


def test_staff_sees_every_subjects_certificate_for_the_project(
    client: TestClient, auth, finished_event
) -> None:
    ctx = finished_event()
    client.post(
        f"/api/events/{ctx['event'].slug}/certificates/issue", headers=auth(ctx["organizer"])
    )
    rows = client.get(
        f"/api/submissions/{ctx['submission'].id}/certificates", headers=auth(ctx["organizer"])
    ).json()
    names = {r["subject_name"] for r in rows}
    assert ctx["owner"].display_name in names
    assert ctx["teammate"].display_name in names


def test_a_downloaded_code_still_verifies_with_no_account(
    client: TestClient, auth, finished_event
) -> None:
    """The fix is scoped to *discovery* -- verifying a code you already have
    stays exactly as open as it always was."""
    ctx = finished_event()
    client.post(
        f"/api/events/{ctx['event'].slug}/certificates/issue", headers=auth(ctx["organizer"])
    )
    rows = client.get(
        f"/api/submissions/{ctx['submission'].id}/certificates", headers=auth(ctx["owner"])
    ).json()
    code = rows[0]["code"]
    response = client.get(f"/api/certificates/{code}")
    assert response.status_code == 200
    assert response.json()["valid"] is True


# --------------------------------------------------------------------------- #
# Admin-issued judging and organizing certificates
# --------------------------------------------------------------------------- #


def issue_for_user(client, slug, headers, **body):
    return client.post(
        f"/api/events/{slug}/certificates/issue-for-user", json=body, headers=headers
    )


def test_an_organizer_issues_a_judging_certificate_for_a_specific_judge(
    client: TestClient, auth, finished_event
) -> None:
    ctx = finished_event()
    response = issue_for_user(
        client, ctx["event"].slug, auth(ctx["organizer"]),
        user_id=str(ctx["judge_user"].id), kind="judging",
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["kind"] == "judging"
    assert body["subject_name"] == ctx["judge_user"].display_name
    assert "1" in body["payload"]  # reviews_completed appears in the signed payload

    mine = client.get("/api/me/certificates", headers=auth(ctx["judge_user"])).json()
    assert any(c["kind"] == "judging" for c in mine)


def test_an_organizer_issues_an_organizing_certificate(
    client: TestClient, auth, finished_event
) -> None:
    ctx = finished_event()
    response = issue_for_user(
        client, ctx["event"].slug, auth(ctx["organizer"]),
        user_id=str(ctx["organizer"].id), kind="organizing",
    )
    assert response.status_code == 200, response.text
    assert response.json()["kind"] == "organizing"

    mine = client.get("/api/me/certificates", headers=auth(ctx["organizer"])).json()
    assert any(c["kind"] == "organizing" for c in mine)


def test_issuing_for_a_user_is_staff_only(
    client: TestClient, auth, finished_event
) -> None:
    ctx = finished_event()
    response = issue_for_user(
        client, ctx["event"].slug, auth(ctx["owner"]),
        user_id=str(ctx["judge_user"].id), kind="judging",
    )
    assert response.status_code == 403


def test_a_judging_certificate_cannot_be_issued_to_a_non_judge(
    client: TestClient, auth, finished_event
) -> None:
    ctx = finished_event()
    response = issue_for_user(
        client, ctx["event"].slug, auth(ctx["organizer"]),
        user_id=str(ctx["owner"].id), kind="judging",
    )
    assert response.status_code == 422


def test_an_organizing_certificate_cannot_be_issued_to_a_non_organizer(
    client: TestClient, auth, finished_event
) -> None:
    ctx = finished_event()
    response = issue_for_user(
        client, ctx["event"].slug, auth(ctx["organizer"]),
        user_id=str(ctx["owner"].id), kind="organizing",
    )
    assert response.status_code == 422


def test_reissuing_updates_the_same_record_not_a_new_one(
    client: TestClient, auth, finished_event
) -> None:
    ctx = finished_event()
    first = issue_for_user(
        client, ctx["event"].slug, auth(ctx["organizer"]),
        user_id=str(ctx["judge_user"].id), kind="judging",
    )
    second = issue_for_user(
        client, ctx["event"].slug, auth(ctx["organizer"]),
        user_id=str(ctx["judge_user"].id), kind="judging",
    )
    assert first.json()["code"] == second.json()["code"]


def test_participation_and_placement_cannot_be_issued_this_way(
    client: TestClient, auth, finished_event
) -> None:
    ctx = finished_event()
    response = issue_for_user(
        client, ctx["event"].slug, auth(ctx["organizer"]),
        user_id=str(ctx["owner"].id), kind="participation",
    )
    assert response.status_code == 422
