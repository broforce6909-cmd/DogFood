"""Role isolation, tested at the API.

This file was written *before* the judging feature it tests, and it is the
reason the feature has the shape it does.

The brief is blunt about what a weak answer looks like here: _"Role checks that
live only in the frontend, if I can curl another judge's scores it is not
isolation."_ So every assertion in this file is an HTTP request with a bearer
token -- the same thing `curl` does -- and never a call to `check_access`. The
pure-predicate tests live in `test_access_control.py`; these exist to prove the
predicate is actually *wired up* to every route, which is the part a unit test
cannot tell you.

FIG. 02 of the brief, the row that matters:

    JUDGE   own scores +   peer scores ✗   other track ✗   aggregate ✗

`ARCHITECTURE.md` commits to **403** for a peer's scores, so that is what is
asserted. 403 rather than 404 is deliberate here and different from a draft:
that a peer judge's ballot exists is not a secret, and the acceptance suite is
documented as expecting 403.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from app.models import AssignmentStatus, Role, SubmissionStatus, utcnow

# --------------------------------------------------------------------------- #
# A scored event, built once per test that needs it.
# --------------------------------------------------------------------------- #


@pytest.fixture()
def scored_event(
    db,
    make_event,
    make_user,
    make_team,
    make_submission,
    make_track,
    make_judge,
    make_criterion,
    make_assignment,
    make_score,
):
    """Two judges, two tracks, two submissions, and a score on each ballot.

    Returned as a dict rather than a tuple because the tests below pick out
    different parts of it and positional unpacking of a dozen things is
    unreadable.
    """

    def _build(*, judging_open: bool = True) -> dict:
        event = make_event()
        now = utcnow()
        event.judging_opens_at = (
            now - timedelta(hours=1) if judging_open else now + timedelta(days=1)
        )
        event.judging_closes_at = now + timedelta(days=3)
        db.commit()

        alpha = make_track(event, key="alpha", name="Alpha")
        beta = make_track(event, key="beta", name="Beta")

        # Two teams with submitted projects, one per track.
        team_a = make_team(event, make_user(Role.PARTICIPANT), name="Team Alpha")
        team_b = make_team(event, make_user(Role.PARTICIPANT), name="Team Beta")
        sub_a = make_submission(
            team_a, name="Project A", status=SubmissionStatus.SUBMITTED, track=alpha
        )
        sub_b = make_submission(
            team_b, name="Project B", status=SubmissionStatus.SUBMITTED, track=beta
        )

        # Two all-track judges, plus one locked to the beta track.
        u_one = make_user(Role.JUDGE, name="Judge One")
        u_two = make_user(Role.JUDGE, name="Judge Two")
        u_beta = make_user(Role.JUDGE, name="Beta Judge")
        judge_one = make_judge(event, u_one)
        judge_two = make_judge(event, u_two)
        judge_beta = make_judge(event, u_beta, track=beta)

        criterion = make_criterion(event, key="impact", name="Impact", weight=2)

        # Both all-track judges hold a ballot on Project A; the beta judge holds
        # one on Project B. The comments are tripwires: they are the string the
        # leak tests grep the response body for.
        ballot_one = make_assignment(judge_one, sub_a)
        ballot_two = make_assignment(judge_two, sub_a)
        ballot_beta = make_assignment(judge_beta, sub_b)
        make_score(ballot_one, criterion, 5, comment="OWN-SCORE-TRIPWIRE")
        make_score(ballot_two, criterion, 2, comment="PEER-SCORE-TRIPWIRE")

        return {
            "event": event,
            "alpha": alpha,
            "beta": beta,
            "sub_a": sub_a,
            "sub_b": sub_b,
            "u_one": u_one,
            "u_two": u_two,
            "u_beta": u_beta,
            "judge_one": judge_one,
            "judge_two": judge_two,
            "judge_beta": judge_beta,
            "criterion": criterion,
            "ballot_one": ballot_one,
            "ballot_two": ballot_two,
            "ballot_beta": ballot_beta,
            "organizer": make_user(Role.ORGANIZER),
            "participant": make_user(Role.PARTICIPANT),
        }

    return _build


# --------------------------------------------------------------------------- #
# A judge may read their own ballot.
# --------------------------------------------------------------------------- #


def test_a_judge_reads_their_own_ballot(client: TestClient, auth, scored_event) -> None:
    s = scored_event()
    r = client.get(
        f"/api/judging/assignments/{s['ballot_one'].id}", headers=auth(s["u_one"])
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["submission"]["name"] == "Project A"
    assert [sc["value"] for sc in body["scores"]] == [5]


def test_a_judge_sees_only_their_own_ballots_in_their_queue(client: TestClient, auth, scored_event) -> None:
    s = scored_event()
    r = client.get("/api/judging/queue", headers=auth(s["u_one"]))
    assert r.status_code == 200, r.text
    ids = {row["id"] for row in r.json()}
    assert str(s["ballot_one"].id) in ids
    assert str(s["ballot_two"].id) not in ids, "a peer's ballot leaked into the queue"


# --------------------------------------------------------------------------- #
# THE test. A judge must never see another judge's scores.
# --------------------------------------------------------------------------- #


def test_a_judge_cannot_read_a_peers_ballot(client: TestClient, auth, scored_event) -> None:
    s = scored_event()
    r = client.get(
        f"/api/judging/assignments/{s['ballot_two'].id}", headers=auth(s["u_one"])
    )
    assert r.status_code == 403, f"expected 403, got {r.status_code}: {r.text}"
    assert "PEER-SCORE-TRIPWIRE" not in r.text


def test_a_judge_cannot_write_to_a_peers_ballot(client: TestClient, auth, scored_event) -> None:
    s = scored_event()
    r = client.put(
        f"/api/judging/assignments/{s['ballot_two'].id}/scores",
        headers=auth(s["u_one"]),
        json={"scores": [{"criterion_id": str(s["criterion"].id), "value": 1}]},
    )
    assert r.status_code == 403, f"expected 403, got {r.status_code}: {r.text}"


def test_peer_scores_do_not_leak_through_the_submission_route(client: TestClient, auth, scored_event) -> None:
    """The same secret, reachable by a different path, is the same leak.

    A judge reading the *submission* they are scoring must not receive the other
    judges' ballots along with it.
    """
    s = scored_event()
    r = client.get(f"/api/submissions/{s['sub_a'].id}", headers=auth(s["u_one"]))
    assert r.status_code == 200, r.text
    assert "PEER-SCORE-TRIPWIRE" not in r.text


# --------------------------------------------------------------------------- #
# A track judge must never see another track.
# --------------------------------------------------------------------------- #


def test_a_track_judge_cannot_read_a_ballot_outside_their_track(client: TestClient, auth, scored_event) -> None:
    """The beta judge owns `ballot_beta`. `ballot_one` is an alpha-track ballot
    belonging to someone else -- denied on both counts, and denied either way."""
    s = scored_event()
    r = client.get(
        f"/api/judging/assignments/{s['ballot_one'].id}", headers=auth(s["u_beta"])
    )
    assert r.status_code == 403, f"expected 403, got {r.status_code}: {r.text}"


def test_a_track_judges_queue_holds_only_their_own_track(client: TestClient, auth, scored_event) -> None:
    s = scored_event()
    r = client.get("/api/judging/queue", headers=auth(s["u_beta"]))
    assert r.status_code == 200, r.text
    rows = r.json()
    assert {row["id"] for row in rows} == {str(s["ballot_beta"].id)}
    assert all(row["submission"]["track"]["key"] == "beta" for row in rows)


# --------------------------------------------------------------------------- #
# A judge must not see the aggregate, and neither must anybody else.
# --------------------------------------------------------------------------- #


def test_a_judge_cannot_read_results(client: TestClient, auth, scored_event) -> None:
    s = scored_event()
    r = client.get(f"/api/events/{s['event'].slug}/results", headers=auth(s["u_one"]))
    assert r.status_code == 403, f"expected 403, got {r.status_code}: {r.text}"


def test_a_judge_cannot_read_the_progress_dashboard(client: TestClient, auth, scored_event) -> None:
    s = scored_event()
    r = client.get(
        f"/api/events/{s['event'].slug}/judging/progress", headers=auth(s["u_one"])
    )
    assert r.status_code == 403, f"expected 403, got {r.status_code}: {r.text}"


def test_a_judge_cannot_list_the_events_judges(client: TestClient, auth, scored_event) -> None:
    s = scored_event()
    r = client.get(f"/api/events/{s['event'].slug}/judges", headers=auth(s["u_one"]))
    assert r.status_code == 403, f"expected 403, got {r.status_code}: {r.text}"


def test_a_judge_cannot_read_every_assignment(client: TestClient, auth, scored_event) -> None:
    s = scored_event()
    r = client.get(f"/api/events/{s['event'].slug}/assignments", headers=auth(s["u_one"]))
    assert r.status_code == 403, f"expected 403, got {r.status_code}: {r.text}"


# --------------------------------------------------------------------------- #
# Participants and visitors see none of it.
# --------------------------------------------------------------------------- #


def test_a_participant_sees_no_judging_surface_at_all(client: TestClient, auth, scored_event) -> None:
    s = scored_event()
    headers = auth(s["participant"])
    slug = s["event"].slug
    for method, path in [
        ("get", f"/api/judging/assignments/{s['ballot_one'].id}"),
        ("get", f"/api/events/{slug}/results"),
        ("get", f"/api/events/{slug}/judging/progress"),
        ("get", f"/api/events/{slug}/judges"),
        ("get", f"/api/events/{slug}/assignments"),
        ("get", f"/api/events/{slug}/export/scores.csv"),
    ]:
        r = getattr(client, method)(path, headers=headers)
        assert r.status_code == 403, f"{method.upper()} {path} -> {r.status_code}"


def test_an_anonymous_visitor_sees_no_judging_surface_at_all(client: TestClient, scored_event) -> None:
    s = scored_event()
    slug = s["event"].slug
    for path in [
        f"/api/judging/assignments/{s['ballot_one'].id}",
        f"/api/judging/queue",
        f"/api/events/{slug}/results",
        f"/api/events/{slug}/judging/progress",
        f"/api/events/{slug}/judges",
        f"/api/events/{slug}/assignments",
        f"/api/events/{slug}/export/scores.csv",
    ]:
        r = client.get(path)
        assert r.status_code in (401, 403), f"GET {path} -> {r.status_code}"


# --------------------------------------------------------------------------- #
# The organizer is the one who sees everything.
# --------------------------------------------------------------------------- #


def test_an_organizer_reads_every_ballot_and_the_aggregate(client: TestClient, auth, scored_event) -> None:
    s = scored_event()
    headers = auth(s["organizer"])
    slug = s["event"].slug
    for path in [
        f"/api/judging/assignments/{s['ballot_one'].id}",
        f"/api/judging/assignments/{s['ballot_two'].id}",
        f"/api/events/{slug}/results",
        f"/api/events/{slug}/judging/progress",
        f"/api/events/{slug}/judges",
        f"/api/events/{slug}/assignments",
    ]:
        r = client.get(path, headers=headers)
        assert r.status_code == 200, f"GET {path} -> {r.status_code}: {r.text}"


# --------------------------------------------------------------------------- #
# Nobody writes a score that is not theirs -- including the organizer.
# --------------------------------------------------------------------------- #


def test_an_organizer_cannot_write_a_judges_score(client: TestClient, auth, scored_event) -> None:
    """Deliberate, and the one place staff privilege stops.

    An organizer who can write a ballot can forge a result, and there is no way
    to tell the forgery from the judgement afterwards. Organizers may read
    everything and may delete an assignment; they may not author a score.
    """
    s = scored_event()
    r = client.put(
        f"/api/judging/assignments/{s['ballot_one'].id}/scores",
        headers=auth(s["organizer"]),
        json={"scores": [{"criterion_id": str(s["criterion"].id), "value": 1}]},
    )
    assert r.status_code == 403, f"expected 403, got {r.status_code}: {r.text}"


def test_a_judge_writes_their_own_score(client: TestClient, auth, scored_event) -> None:
    s = scored_event()
    r = client.put(
        f"/api/judging/assignments/{s['ballot_one'].id}/scores",
        headers=auth(s["u_one"]),
        json={
            "scores": [{"criterion_id": str(s["criterion"].id), "value": 4}],
            "comment": "Solid work.",
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == AssignmentStatus.COMPLETE.value


# --------------------------------------------------------------------------- #
# The judging window is a time check in the same predicate.
# --------------------------------------------------------------------------- #


def test_a_judge_cannot_score_before_judging_opens(client: TestClient, auth, scored_event) -> None:
    s = scored_event(judging_open=False)
    r = client.put(
        f"/api/judging/assignments/{s['ballot_one'].id}/scores",
        headers=auth(s["u_one"]),
        json={"scores": [{"criterion_id": str(s["criterion"].id), "value": 4}]},
    )
    assert r.status_code == 409, f"expected 409, got {r.status_code}: {r.text}"


def test_a_deactivated_judge_cannot_score(client: TestClient, auth, db, scored_event) -> None:
    s = scored_event()
    s["judge_one"].is_active = False
    db.commit()
    r = client.put(
        f"/api/judging/assignments/{s['ballot_one'].id}/scores",
        headers=auth(s["u_one"]),
        json={"scores": [{"criterion_id": str(s["criterion"].id), "value": 4}]},
    )
    assert r.status_code == 403, f"expected 403, got {r.status_code}: {r.text}"
