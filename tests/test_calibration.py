"""Judge calibration: the maths, and the API around it.

Two halves. The first checks `app.calibration.assess` with numbers a reader can
verify by hand. The second checks the two properties that matter about the routes:
a judge can never see an expected score, and practising can never move a real
result.
"""

from __future__ import annotations

import uuid

from fastapi.testclient import TestClient

from app.calibration import CALIBRATION_THRESHOLD, CalibrationCriterion, assess
from app.models import Role

# --------------------------------------------------------------------------- #
# The maths
# --------------------------------------------------------------------------- #

P1, P2 = uuid.uuid4(), uuid.uuid4()
C1, C2 = uuid.uuid4(), uuid.uuid4()
CRITERIA = [CalibrationCriterion(C1, 1, 5), CalibrationCriterion(C2, 1, 5)]
EXPECTED = {(P1, C1): 4.0, (P1, C2): 3.0, (P2, C1): 2.0, (P2, C2): 5.0}


def run(g: dict, projects=(P1, P2)):
    return assess(project_ids=list(projects), criteria=CRITERIA, expected=EXPECTED, given=g)


def test_a_judge_who_matches_the_expected_scores_is_aligned() -> None:
    a = run(dict(EXPECTED))
    assert a.verdict == "aligned"
    assert a.mean_signed_deviation == 0
    assert a.mean_abs_deviation == 0


def test_a_consistently_low_judge_is_harsh() -> None:
    # One point under on every criterion of a 1-5 rubric: -1/4 = -0.25 each.
    g = {(P1, C1): 3.0, (P1, C2): 2.0, (P2, C1): 1.0, (P2, C2): 4.0}
    a = run(g)
    assert a.verdict == "harsh"
    assert a.mean_signed_deviation == -0.25


def test_a_consistently_high_judge_is_generous() -> None:
    g = {(P1, C1): 5.0, (P1, C2): 4.0, (P2, C1): 3.0, (P2, C2): 5.0}
    a = run(g)
    assert a.verdict == "generous"
    assert a.mean_signed_deviation > CALIBRATION_THRESHOLD


def test_offsetting_errors_average_to_zero_but_show_in_absolute_deviation() -> None:
    # Too high on P1, too low on P2: signed cancels, absolute does not.
    g = {(P1, C1): 5.0, (P1, C2): 4.0, (P2, C1): 1.0, (P2, C2): 4.0}
    a = run(g)
    assert a.verdict == "aligned"
    assert a.mean_abs_deviation is not None and a.mean_abs_deviation > 0.1


def test_no_flag_is_issued_until_every_practice_project_is_scored() -> None:
    only_p1 = {(P1, C1): 1.0, (P1, C2): 1.0}
    a = run(only_p1)
    assert a.verdict == "incomplete"
    assert a.projects_scored == 1

    assert run({}).verdict == "not_started"


def test_a_half_scored_project_does_not_count() -> None:
    a = run({(P1, C1): 4.0})
    assert a.verdict == "not_started"
    assert a.projects_scored == 0


def test_a_criterion_is_measured_against_its_own_range() -> None:
    wide = CalibrationCriterion(C1, 0, 10)
    a = assess(
        project_ids=[P1],
        criteria=[wide],
        expected={(P1, C1): 5.0},
        given={(P1, C1): 6.0},
    )
    assert abs(a.mean_signed_deviation - 0.1) < 1e-9


# --------------------------------------------------------------------------- #
# The API
# --------------------------------------------------------------------------- #


def setup_event(make_event, make_criterion, judging_event):
    event = judging_event()
    c1 = make_criterion(event, key="impact", name="Impact", position=0)
    c2 = make_criterion(event, key="craft", name="Craft", position=1)
    return event, c1, c2


def add_project(client, slug, headers, c1, c2, name="Practice A", e1=4.0, e2=3.0):
    return client.post(
        f"/api/events/{slug}/calibration/projects",
        json={
            "name": name,
            "description": "A known project.",
            "expected": [
                {"criterion_id": str(c1.id), "value": e1},
                {"criterion_id": str(c2.id), "value": e2},
            ],
        },
        headers=headers,
    )


def test_an_organizer_can_add_and_list_practice_projects(
    client: TestClient, auth, make_user, make_event, make_criterion, judging_event
) -> None:
    event, c1, c2 = setup_event(make_event, make_criterion, judging_event)
    organizer = make_user(Role.ORGANIZER)

    r = add_project(client, event.slug, auth(organizer), c1, c2)
    assert r.status_code == 201, r.text
    assert {e["value"] for e in r.json()["expected"]} == {4.0, 3.0}

    listed = client.get(f"/api/events/{event.slug}/calibration/projects", headers=auth(organizer))
    assert listed.status_code == 200
    assert [p["name"] for p in listed.json()] == ["Practice A"]


def test_expected_scores_must_cover_every_criterion_and_stay_in_range(
    client: TestClient, auth, make_user, make_event, make_criterion, judging_event
) -> None:
    event, c1, c2 = setup_event(make_event, make_criterion, judging_event)
    organizer = make_user(Role.ORGANIZER)

    missing = client.post(
        f"/api/events/{event.slug}/calibration/projects",
        json={"name": "X", "expected": [{"criterion_id": str(c1.id), "value": 3.0}]},
        headers=auth(organizer),
    )
    assert missing.status_code == 422

    out_of_range = add_project(client, event.slug, auth(organizer), c1, c2, e1=9.0)
    assert out_of_range.status_code == 422

    add_project(client, event.slug, auth(organizer), c1, c2)
    duplicate = add_project(client, event.slug, auth(organizer), c1, c2)
    assert duplicate.status_code == 409


def test_a_judge_sees_practice_projects_but_never_the_expected_scores(
    client: TestClient, auth, make_user, make_event, make_criterion, make_judge, judging_event
) -> None:
    event, c1, c2 = setup_event(make_event, make_criterion, judging_event)
    organizer = make_user(Role.ORGANIZER)
    judge_user = make_user(Role.JUDGE)
    make_judge(event, judge_user)
    add_project(client, event.slug, auth(organizer), c1, c2)

    r = client.get("/api/judging/calibration", headers=auth(judge_user))
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body) == 1
    assert "expected" not in body[0]
    assert body[0]["my_scores"] == []
    assert body[0]["complete"] is False

    # And the staff-only routes refuse a judge outright.
    assert (
        client.get(f"/api/events/{event.slug}/calibration/projects", headers=auth(judge_user)).status_code
        in (403, 404)
    )
    assert (
        client.get(f"/api/events/{event.slug}/calibration/report", headers=auth(judge_user)).status_code
        in (403, 404)
    )


def test_a_harsh_judge_is_flagged_in_the_report(
    client: TestClient, auth, make_user, make_event, make_criterion, make_judge, judging_event
) -> None:
    event, c1, c2 = setup_event(make_event, make_criterion, judging_event)
    organizer = make_user(Role.ORGANIZER)
    harsh_user = make_user(Role.JUDGE, name="Harsh Judge")
    fair_user = make_user(Role.JUDGE, name="Fair Judge")
    make_judge(event, harsh_user)
    make_judge(event, fair_user)
    project = add_project(client, event.slug, auth(organizer), c1, c2).json()

    def score(user, v1, v2):
        return client.put(
            f"/api/judging/calibration/{project['id']}/scores",
            json={
                "scores": [
                    {"criterion_id": str(c1.id), "value": v1},
                    {"criterion_id": str(c2.id), "value": v2},
                ]
            },
            headers=auth(user),
        )

    r = score(harsh_user, 2.5, 1.5)  # expected 4.0 / 3.0 -> -1.5 each, of a 4-point range
    assert r.status_code == 200, r.text
    assert r.json()["complete"] is True
    assert score(fair_user, 4.0, 3.0).status_code == 200

    report = client.get(f"/api/events/{event.slug}/calibration/report", headers=auth(organizer))
    assert report.status_code == 200
    by_name = {row["judge_name"]: row for row in report.json()["rows"]}
    assert by_name["Harsh Judge"]["verdict"] == "harsh"
    assert by_name["Fair Judge"]["verdict"] == "aligned"


def test_a_judge_cannot_score_another_events_practice_project(
    client: TestClient, auth, make_user, make_event, make_criterion, make_judge, judging_event
) -> None:
    event, c1, c2 = setup_event(make_event, make_criterion, judging_event)
    other, _, _ = setup_event(make_event, make_criterion, judging_event)
    organizer = make_user(Role.ORGANIZER)
    outsider = make_user(Role.JUDGE)
    make_judge(other, outsider)  # judges a different event only
    project = add_project(client, event.slug, auth(organizer), c1, c2).json()

    r = client.put(
        f"/api/judging/calibration/{project['id']}/scores",
        json={"scores": [{"criterion_id": str(c1.id), "value": 3.0}]},
        headers=auth(outsider),
    )
    assert r.status_code == 404


def test_practice_scores_never_reach_real_results(
    client: TestClient, auth, make_user, make_event, make_criterion, make_judge, judging_event
) -> None:
    event, c1, c2 = setup_event(make_event, make_criterion, judging_event)
    organizer = make_user(Role.ORGANIZER)
    judge_user = make_user(Role.JUDGE)
    make_judge(event, judge_user)
    project = add_project(client, event.slug, auth(organizer), c1, c2).json()
    client.put(
        f"/api/judging/calibration/{project['id']}/scores",
        json={
            "scores": [
                {"criterion_id": str(c1.id), "value": 5.0},
                {"criterion_id": str(c2.id), "value": 5.0},
            ]
        },
        headers=auth(judge_user),
    )
    results = client.get(f"/api/events/{event.slug}/results", headers=auth(organizer))
    assert results.status_code == 200
    assert results.json()["rows"] == []


def test_removing_a_practice_project_removes_its_scores(
    client: TestClient, auth, make_user, make_event, make_criterion, make_judge, judging_event
) -> None:
    event, c1, c2 = setup_event(make_event, make_criterion, judging_event)
    organizer = make_user(Role.ORGANIZER)
    judge_user = make_user(Role.JUDGE)
    make_judge(event, judge_user)
    project = add_project(client, event.slug, auth(organizer), c1, c2).json()

    r = client.delete(
        f"/api/events/{event.slug}/calibration/projects/{project['id']}", headers=auth(organizer)
    )
    assert r.status_code == 204
    assert client.get("/api/judging/calibration", headers=auth(judge_user)).json() == []
