"""Part 4: admin override of final results, and the per-project scoring grid.

"Live + override" is the whole shape: `compute_tiers()` (Part 3) keeps
computing the judge-ranked truth exactly as it always did, and
`ResultOverride` is a separate table an admin corrects on top of it, for
cause. `effective_tiers()` -- the merge of the two -- is what community
voting eligibility and a team's own `GET /submissions/{id}` actually read;
`compute_tiers()`'s own output stays visible alongside it on
`GET /events/{slug}/results`, never silently overwritten.

The scoring grid (`GET /events/{slug}/results/{submission_id}/scoring`) is
read-only: every judge's per-criterion ballot for one project, side by side,
with no route back to editing a `Score`.
"""

from __future__ import annotations

from datetime import timedelta

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.models import ResultOverride, Role, utcnow


def set_override(client, event_slug, submission_id, headers, tier, reason="Rule violation found"):
    return client.put(
        f"/api/events/{event_slug}/results/overrides/{submission_id}",
        json={"tier": tier, "reason": reason},
        headers=headers,
    )


def clear_override(client, event_slug, submission_id, headers, reason="Cleared after review"):
    return client.post(
        f"/api/events/{event_slug}/results/overrides/{submission_id}/clear",
        json={"reason": reason},
        headers=headers,
    )


# --------------------------------------------------------------------------- #
# Setting and clearing an override
# --------------------------------------------------------------------------- #


def test_an_organizer_can_override_a_submissions_tier(
    client: TestClient, auth, make_user, tiered_event
) -> None:
    event, subs = tiered_event()
    organizer = make_user(Role.ORGANIZER)

    # subs[3] computed to "neither" -- promote it to the community tier.
    response = set_override(
        client, event.slug, subs[3].id, auth(organizer), "community_tier",
        reason="Disagreement flag resolved in this project's favor",
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["computed_tier"] is None
    assert body["tier"] == "community_tier"
    assert body["override_reason"] == "Disagreement flag resolved in this project's favor"
    assert body["overridden_by"] == organizer.display_name


def test_overriding_a_submission_requires_a_reason(
    client: TestClient, auth, make_user, tiered_event
) -> None:
    event, subs = tiered_event()
    response = client.put(
        f"/api/events/{event.slug}/results/overrides/{subs[0].id}",
        json={"tier": "neither"},
        headers=auth(make_user(Role.ORGANIZER)),
    )
    assert response.status_code == 422


def test_overriding_is_staff_only(
    client: TestClient, auth, make_user, tiered_event
) -> None:
    event, subs = tiered_event()
    response = set_override(
        client, event.slug, subs[0].id, auth(make_user(Role.PARTICIPANT)), "neither"
    )
    assert response.status_code == 403
    anonymous = client.put(
        f"/api/events/{event.slug}/results/overrides/{subs[0].id}",
        json={"tier": "neither", "reason": "test"},
    )
    assert anonymous.status_code == 403


def test_setting_an_override_twice_updates_it_in_place(
    client: TestClient, auth, make_user, db, tiered_event
) -> None:
    event, subs = tiered_event()
    headers = auth(make_user(Role.ORGANIZER))
    set_override(client, event.slug, subs[0].id, headers, "neither", reason="First pass")
    second = set_override(
        client, event.slug, subs[0].id, headers, "community_tier", reason="Reconsidered"
    )
    assert second.status_code == 200, second.text
    assert second.json()["tier"] == "community_tier"
    assert second.json()["override_reason"] == "Reconsidered"

    rows = db.execute(
        select(ResultOverride).where(ResultOverride.submission_id == subs[0].id)
    ).scalars().all()
    assert len(rows) == 1  # updated in place, not a growing history table


def test_the_computed_tier_never_changes_when_overridden(
    client: TestClient, auth, make_user, tiered_event
) -> None:
    """The judge-computed ranking is never silently overwritten -- it stays
    exactly what `compute_tiers()` says, visible alongside the override."""
    event, subs = tiered_event()
    headers = auth(make_user(Role.ORGANIZER))
    set_override(client, event.slug, subs[0].id, headers, "neither")

    results = client.get(f"/api/events/{event.slug}/results", headers=headers).json()
    row = next(r for r in results["rows"] if r["submission_id"] == str(subs[0].id))
    assert row["computed_tier"] == "winner"  # untouched
    assert row["tier"] is None  # overridden to neither


def test_clearing_an_override_restores_the_computed_tier(
    client: TestClient, auth, make_user, tiered_event
) -> None:
    event, subs = tiered_event()
    headers = auth(make_user(Role.ORGANIZER))
    set_override(client, event.slug, subs[0].id, headers, "neither")

    response = clear_override(client, event.slug, subs[0].id, headers)
    assert response.status_code == 200, response.text
    assert response.json()["tier"] == "winner"
    assert response.json()["override_reason"] is None


def test_clearing_requires_a_reason(
    client: TestClient, auth, make_user, tiered_event
) -> None:
    event, subs = tiered_event()
    headers = auth(make_user(Role.ORGANIZER))
    set_override(client, event.slug, subs[0].id, headers, "neither")
    response = client.post(
        f"/api/events/{event.slug}/results/overrides/{subs[0].id}/clear",
        json={},
        headers=headers,
    )
    assert response.status_code == 422


def test_clearing_a_submission_with_no_override_is_a_conflict(
    client: TestClient, auth, make_user, tiered_event
) -> None:
    event, subs = tiered_event()
    response = clear_override(client, event.slug, subs[0].id, auth(make_user(Role.ORGANIZER)))
    assert response.status_code == 409


def test_an_override_is_recorded_in_the_audit_log_with_the_reason(
    client: TestClient, auth, make_user, tiered_event
) -> None:
    event, subs = tiered_event()
    organizer = make_user(Role.ORGANIZER)
    headers = auth(organizer)
    set_override(
        client, event.slug, subs[3].id, headers, "winner", reason="Manual tie-break after review"
    )

    rows = client.get(f"/api/events/{event.slug}/audit", headers=headers).json()["rows"]
    entry = next(r for r in rows if r["action"] == "result_overridden")
    assert "Manual tie-break after review" in entry["summary"]
    assert organizer.email in entry["summary"]

    clear_override(client, event.slug, subs[3].id, headers, reason="Undoing that after all")
    rows2 = client.get(f"/api/events/{event.slug}/audit", headers=headers).json()["rows"]
    cleared = next(r for r in rows2 if r["action"] == "result_override_cleared")
    assert "Undoing that after all" in cleared["summary"]


# --------------------------------------------------------------------------- #
# The override is authoritative for voting and for the team's own view
# --------------------------------------------------------------------------- #


def test_an_override_changes_who_is_votable(
    client: TestClient, auth, make_user, make_registration, db, tiered_event
) -> None:
    event, subs = tiered_event()
    organizer = make_user(Role.ORGANIZER)
    # Demote subs[1] out of the community tier, promote subs[3] into it.
    set_override(client, event.slug, subs[1].id, auth(organizer), "neither")
    set_override(client, event.slug, subs[3].id, auth(organizer), "community_tier")

    voter = make_user(Role.PARTICIPANT)
    make_registration(event, voter, registered_at=utcnow() - timedelta(days=60))
    ballot = client.get(f"/api/events/{event.slug}/ballot", headers=auth(voter)).json()
    project_ids = {p["id"] for p in ballot["projects"]}
    assert project_ids == {str(subs[2].id), str(subs[3].id)}


def test_a_team_sees_the_overridden_tier_not_the_computed_one(
    client: TestClient, auth, make_user, db, tiered_event
) -> None:
    event, subs = tiered_event()
    event.results_public_at = utcnow() - timedelta(minutes=1)
    db.commit()

    organizer = make_user(Role.ORGANIZER)
    set_override(client, event.slug, subs[0].id, auth(organizer), "neither")

    owner = subs[0].team.members[0].user
    body = client.get(f"/api/submissions/{subs[0].id}", headers=auth(owner)).json()
    assert body["tier"] is None


# --------------------------------------------------------------------------- #
# The scoring review grid
# --------------------------------------------------------------------------- #


def test_the_scoring_grid_shows_every_judges_ballot(
    client: TestClient, auth, make_user, tiered_event
) -> None:
    event, subs = tiered_event()
    organizer = make_user(Role.ORGANIZER)
    body = client.get(
        f"/api/events/{event.slug}/results/{subs[0].id}/scoring", headers=auth(organizer)
    ).json()

    assert body["submission_id"] == str(subs[0].id)
    assert len(body["criteria"]) == 1
    assert len(body["judges"]) == 1
    judge_row = body["judges"][0]
    assert judge_row["status"] == "complete"
    assert judge_row["scores"][0]["value"] == 5
    assert judge_row["raw_mean"] == 5.0
    assert judge_row["normalized_mean"] is not None
    assert body["final_raw_mean"] == 5.0
    assert body["n_reviews"] == 1


def test_the_scoring_grid_is_staff_only(
    client: TestClient, auth, make_user, tiered_event
) -> None:
    event, subs = tiered_event()
    response = client.get(
        f"/api/events/{event.slug}/results/{subs[0].id}/scoring",
        headers=auth(make_user(Role.PARTICIPANT)),
    )
    assert response.status_code == 403


def test_the_scoring_grid_never_writes_a_score(
    client: TestClient, auth, make_user, db, tiered_event
) -> None:
    """Read-only, in the strongest sense this test can check: the endpoint
    only exposes a GET, so there is no request shape that could mutate a
    ballot through it."""
    event, subs = tiered_event()
    organizer = make_user(Role.ORGANIZER)
    before = client.get(
        f"/api/events/{event.slug}/results/{subs[0].id}/scoring", headers=auth(organizer)
    ).json()
    again = client.get(
        f"/api/events/{event.slug}/results/{subs[0].id}/scoring", headers=auth(organizer)
    ).json()
    assert before == again
