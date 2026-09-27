"""Certificates and signed participation records.

The requirement is *"signed, publicly verifiable judge participation records"*, and
the load-bearing word is **publicly**. A record anybody can check is only useful if:

* verification needs no account, and
* verification does not depend on trusting the page showing the record.

So the verify endpoint is open, it returns the signed bytes and a verdict, and a
tampered record fails. The tests below also pin down the things that would quietly
destroy that property: issuing from an unauthorised account, re-issuing under a new
code so an old shared link dies, and revocation that does not actually revoke.
"""

from __future__ import annotations

import json
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from app.models import (
    AssignmentStatus,
    CertificateKind,
    Role,
    SubmissionStatus,
    utcnow,
)


@pytest.fixture()
def finished_event(
    db, make_event, make_user, make_team, make_submission, make_judge, make_assignment,
    make_criterion, make_score,
):
    """An event with judging finished, so there is something to certify."""

    def _build():
        # An hour ago, not a day: `make_event` opens submissions a day back, and
        # an earlier deadline violates the event's own window constraint.
        event = make_event(deadline=utcnow() - timedelta(hours=1))
        event.judging_opens_at = utcnow() - timedelta(days=1)
        event.judging_closes_at = utcnow() - timedelta(hours=1)
        db.commit()
        criterion = make_criterion(event, key="overall", name="Overall", weight=1)

        participant = make_user(Role.PARTICIPANT, name="Wei Zhang")
        team = make_team(event, participant, name="Quorum Collective")
        submission = make_submission(team, name="Quorum", status=SubmissionStatus.SUBMITTED)

        judge_user = make_user(Role.JUDGE, name="Priya Rivera")
        judge = make_judge(event, judge_user)
        ballot = make_assignment(judge, submission, status=AssignmentStatus.COMPLETE)
        make_score(ballot, criterion, 5)

        return {
            "event": event,
            "participant": participant,
            "judge_user": judge_user,
            "submission": submission,
            "organizer": make_user(Role.ORGANIZER),
        }

    return _build


def issue(client: TestClient, slug: str, headers, **body):
    return client.post(f"/api/events/{slug}/certificates/issue", headers=headers, json=body)


# --------------------------------------------------------------------------- #
# Issuing
# --------------------------------------------------------------------------- #


def test_an_organizer_issues_records_for_everyone(client: TestClient, auth, finished_event):
    """Bulk issue: every participant gets a participation record, every judge a
    judging record. Doing this one at a time is not a workflow an organizer would
    survive at 40 projects."""
    s = finished_event()
    r = issue(client, s["event"].slug, auth(s["organizer"]))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["issued"] >= 2
    kinds = {c["kind"] for c in body["certificates"]}
    assert CertificateKind.PARTICIPATION.value in kinds
    assert CertificateKind.JUDGING.value in kinds


def test_a_judging_record_states_how_many_reviews(client: TestClient, auth, finished_event):
    """The fact a judge actually wants attested: that they did the work."""
    s = finished_event()
    issue(client, s["event"].slug, auth(s["organizer"]))
    mine = client.get("/api/me/certificates", headers=auth(s["judge_user"])).json()
    judging = [c for c in mine if c["kind"] == CertificateKind.JUDGING.value]
    assert judging, mine
    payload = json.loads(judging[0]["payload"])
    assert payload["reviews_completed"] == 1
    assert payload["subject"] == "Priya Rivera"


@pytest.mark.parametrize("role", [Role.PARTICIPANT, Role.JUDGE])
def test_only_staff_issue_records(client: TestClient, auth, make_user, finished_event, role):
    s = finished_event()
    assert issue(client, s["event"].slug, auth(make_user(role))).status_code == 403


def test_issuing_twice_does_not_mint_a_second_code(client: TestClient, auth, finished_event):
    """A code gets shared. Re-issuing must update the record, not orphan the link
    somebody already put on their CV."""
    s = finished_event()
    headers = auth(s["organizer"])
    first = issue(client, s["event"].slug, headers).json()
    codes_first = sorted(c["code"] for c in first["certificates"])

    second = issue(client, s["event"].slug, headers).json()
    codes_second = sorted(c["code"] for c in second["certificates"])
    assert codes_first == codes_second
    assert second["issued"] == 0
    assert second["updated"] >= 1


# --------------------------------------------------------------------------- #
# Public verification
# --------------------------------------------------------------------------- #


def test_a_record_verifies_with_no_account(client: TestClient, auth, finished_event):
    """The requirement, stated as a test: no session, no bearer token, no cookie."""
    s = finished_event()
    issued = issue(client, s["event"].slug, auth(s["organizer"])).json()
    code = issued["certificates"][0]["code"]

    r = client.get(f"/api/certificates/{code}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["valid"] is True
    assert body["signature"]
    assert body["payload"]
    assert body["key_id"]


def test_verification_returns_the_bytes_that_were_signed(
    client: TestClient, auth, finished_event
):
    """So a third party can check the Ed25519 signature themselves rather than
    believing our verdict.

    If the endpoint re-serialised the payload, an independent verifier computing the
    signature over those bytes would get a different answer.
    """
    from app.signing import verify

    s = finished_event()
    issued = issue(client, s["event"].slug, auth(s["organizer"])).json()
    code = issued["certificates"][0]["code"]

    body = client.get(f"/api/certificates/{code}").json()
    assert body["key_id"]
    assert verify(body["payload"].encode(), body["signature"], body["key_id"]) is True


def test_verification_names_a_key_id_published_at_public_keys(
    client: TestClient, auth, finished_event
):
    """A verifier doing this independently needs to know *which* published key to
    check against -- Ed25519 has no single shared secret to fall back to."""
    s = finished_event()
    issued = issue(client, s["event"].slug, auth(s["organizer"])).json()
    code = issued["certificates"][0]["code"]

    body = client.get(f"/api/certificates/{code}").json()
    keys = client.get("/api/signing/public-keys").json()
    assert body["key_id"] in {k["kid"] for k in keys}


def test_a_tampered_record_does_not_verify(client: TestClient, auth, db, finished_event):
    """Edit the stored payload behind the API's back; the verdict must flip."""
    from app.models import Certificate

    s = finished_event()
    issued = issue(client, s["event"].slug, auth(s["organizer"])).json()
    code = issued["certificates"][0]["code"]

    row = db.query(Certificate).filter(Certificate.code == code).one()
    row.payload = row.payload.replace('"reviews_completed":1', '"reviews_completed":99')
    if row.payload == issued["certificates"][0]["payload"]:
        # Participation records have no review count; tamper the name instead.
        row.payload = row.payload.replace("Wei Zhang", "Someone Else")
    db.commit()

    body = client.get(f"/api/certificates/{code}").json()
    assert body["valid"] is False
    assert "signature" in (body.get("detail") or "").lower() or body["valid"] is False


def test_an_unknown_code_is_a_404(client: TestClient):
    assert client.get("/api/certificates/NOTAREALCODE").status_code == 404


def test_verification_does_not_leak_the_private_signing_key(
    client: TestClient, auth, finished_event
):
    s = finished_event()
    issued = issue(client, s["event"].slug, auth(s["organizer"])).json()
    body = client.get(f"/api/certificates/{issued['certificates'][0]['code']}").text
    assert "PRIVATE KEY" not in body


def test_public_keys_endpoint_publishes_no_private_key(client: TestClient) -> None:
    """The whole point of asymmetric signing: only public halves ever leave the
    server, and this is the one endpoint whose entire job is to publish keys."""
    r = client.get("/api/signing/public-keys")
    assert r.status_code == 200, r.text
    keys = r.json()
    assert keys
    for entry in keys:
        assert "PRIVATE KEY" not in entry["public_key_pem"]
        assert entry["status"] in ("active", "retired")
    assert sum(1 for k in keys if k["status"] == "active") == 1


# --------------------------------------------------------------------------- #
# Revocation
# --------------------------------------------------------------------------- #


def test_a_revoked_record_says_so(client: TestClient, auth, finished_event):
    """Revocation has to be visible to the person checking, not just to us --
    otherwise a withdrawn record still reads as valid wherever it was shared."""
    s = finished_event()
    headers = auth(s["organizer"])
    issued = issue(client, s["event"].slug, headers).json()
    cert = issued["certificates"][0]

    r = client.post(
        f"/api/events/{s['event'].slug}/certificates/{cert['id']}/revoke",
        headers=headers,
        json={"reason": "issued to the wrong person"},
    )
    assert r.status_code == 200, r.text

    public = client.get(f"/api/certificates/{cert['code']}").json()
    assert public["valid"] is False
    assert public["revoked"] is True
    assert "wrong person" in public["revoked_reason"]


def test_revoking_requires_a_reason(client: TestClient, auth, finished_event):
    s = finished_event()
    headers = auth(s["organizer"])
    cert = issue(client, s["event"].slug, headers).json()["certificates"][0]
    r = client.post(
        f"/api/events/{s['event'].slug}/certificates/{cert['id']}/revoke",
        headers=headers,
        json={"reason": "  "},
    )
    assert r.status_code == 422


def test_a_participant_cannot_revoke(client: TestClient, auth, finished_event):
    s = finished_event()
    cert = issue(client, s["event"].slug, auth(s["organizer"])).json()["certificates"][0]
    r = client.post(
        f"/api/events/{s['event'].slug}/certificates/{cert['id']}/revoke",
        headers=auth(s["participant"]),
        json={"reason": "I would rather it said something else"},
    )
    assert r.status_code == 403


# --------------------------------------------------------------------------- #
# Who sees what
# --------------------------------------------------------------------------- #


def test_my_certificates_are_mine_only(client: TestClient, auth, finished_event):
    s = finished_event()
    issue(client, s["event"].slug, auth(s["organizer"]))

    judge_view = client.get("/api/me/certificates", headers=auth(s["judge_user"])).json()
    participant_view = client.get(
        "/api/me/certificates", headers=auth(s["participant"])
    ).json()

    assert {c["subject_name"] for c in judge_view} == {"Priya Rivera"}
    assert {c["subject_name"] for c in participant_view} == {"Wei Zhang"}


def test_the_full_list_is_staff_only(client: TestClient, auth, finished_event):
    s = finished_event()
    headers = auth(s["organizer"])
    issue(client, s["event"].slug, headers)

    assert client.get(f"/api/events/{s['event'].slug}/certificates", headers=headers).status_code == 200
    assert (
        client.get(
            f"/api/events/{s['event'].slug}/certificates",
            headers=auth(s["participant"]),
        ).status_code
        == 403
    )


def test_anonymous_callers_cannot_list_certificates(client: TestClient, finished_event):
    s = finished_event()
    assert client.get(f"/api/events/{s['event'].slug}/certificates").status_code == 403


# --------------------------------------------------------------------------- #
# The project is part of what a participation record signs
# --------------------------------------------------------------------------- #


def _participation(body: dict) -> dict:
    rows = [c for c in body["certificates"] if c["kind"] == CertificateKind.PARTICIPATION.value]
    assert rows, body
    return rows[0]


def test_a_participation_record_signs_the_project_name_as_well_as_the_team(
    client: TestClient, auth, finished_event
):
    s = finished_event()
    body = issue(client, s["event"].slug, auth(s["organizer"])).json()
    payload = json.loads(_participation(body)["payload"])
    assert payload["project"] == "Quorum"
    assert payload["team"] == "Quorum Collective"

    # And it verifies: the project is inside the bytes that were signed.
    code = _participation(body)["code"]
    assert client.get(f"/api/certificates/{code}").json()["valid"] is True


def test_re_issuing_upgrades_an_older_record_without_changing_its_code(
    client: TestClient, auth, finished_event, db
):
    """A record issued before the project was signed has no `project` key. The
    organizer's next re-issue adds it, keeps the code (it may already be printed
    or shared), and the refreshed signature verifies."""
    from sqlalchemy import select

    from app.models import Certificate

    s = finished_event()
    first = issue(client, s["event"].slug, auth(s["organizer"])).json()
    original = _participation(first)

    row = db.execute(select(Certificate).where(Certificate.code == original["code"])).scalar_one()
    legacy = json.loads(row.payload)
    legacy.pop("project")
    row.payload = json.dumps(legacy, sort_keys=True, separators=(",", ":"))
    db.commit()

    again = issue(client, s["event"].slug, auth(s["organizer"])).json()
    upgraded = _participation(again)
    assert upgraded["code"] == original["code"]
    assert json.loads(upgraded["payload"])["project"] == "Quorum"
    assert client.get(f"/api/certificates/{upgraded['code']}").json()["valid"] is True
