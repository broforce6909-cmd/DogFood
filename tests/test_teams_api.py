"""Team formation by invite link."""

from __future__ import annotations

from datetime import timedelta

from fastapi.testclient import TestClient

from app.models import Role, SubmissionStatus, utcnow


def test_creating_a_team_makes_the_creator_its_owner(
    client: TestClient, make_event, make_user, auth
) -> None:
    event = make_event()
    user = make_user(name="Founder")
    response = client.post(
        f"/api/events/{event.slug}/teams", json={"name": "Quorum"}, headers=auth(user)
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["name"] == "Quorum"
    assert body["members"][0]["team_role"] == "owner"
    assert body["members"][0]["user"]["display_name"] == "Founder"


def test_a_visitor_cannot_create_a_team(client: TestClient, make_event) -> None:
    event = make_event()
    assert (
        client.post(f"/api/events/{event.slug}/teams", json={"name": "Anon"}).status_code == 401
    )


def test_one_user_one_team_per_event(
    client: TestClient, make_event, make_user, auth
) -> None:
    """Enforced by a unique index, not by a handler remembering to look."""
    event = make_event()
    headers = auth(make_user())
    assert (
        client.post(
            f"/api/events/{event.slug}/teams", json={"name": "First"}, headers=headers
        ).status_code
        == 201
    )
    assert (
        client.post(
            f"/api/events/{event.slug}/teams", json={"name": "Second"}, headers=headers
        ).status_code
        == 409
    )


def test_the_same_user_may_be_on_a_team_in_a_different_event(
    client: TestClient, make_event, make_user, auth
) -> None:
    headers = auth(make_user())
    for slug in ("spring", "autumn"):
        make_event(slug=slug)
        assert (
            client.post(
                f"/api/events/{slug}/teams", json={"name": "Recurring"}, headers=headers
            ).status_code
            == 201
        )


def test_the_invite_link_is_visible_to_members_and_nobody_else(
    client: TestClient, make_event, make_team, make_user, auth
) -> None:
    event = make_event()
    owner, member, stranger = make_user(), make_user(), make_user()
    team = make_team(event, owner, member)

    assert client.get(f"/api/teams/{team.id}/invite", headers=auth(owner)).status_code == 200
    assert client.get(f"/api/teams/{team.id}/invite", headers=auth(member)).status_code == 200
    assert client.get(f"/api/teams/{team.id}/invite", headers=auth(stranger)).status_code == 403
    assert client.get(f"/api/teams/{team.id}/invite").status_code == 403


def test_the_token_is_not_leaked_by_the_team_endpoint(
    client: TestClient, make_event, make_team, make_user, auth
) -> None:
    """Reading the invite is its own authorized act, so the token must not ride
    along on the resource everybody can see."""
    event = make_event()
    team = make_team(event, make_user())
    body = client.get(f"/api/teams/{team.id}").text
    assert team.invite_token not in body


def test_joining_by_invite(
    client: TestClient, make_event, make_team, make_user, auth
) -> None:
    event = make_event()
    owner, joiner = make_user(), make_user(name="Joiner")
    team = make_team(event, owner)
    token = client.get(f"/api/teams/{team.id}/invite", headers=auth(owner)).json()["token"]

    response = client.post("/api/teams/join", json={"token": token}, headers=auth(joiner))
    assert response.status_code == 200, response.text
    names = {m["user"]["display_name"] for m in response.json()["members"]}
    assert "Joiner" in names


def test_joining_twice_is_a_no_op(
    client: TestClient, make_event, make_team, make_user, auth
) -> None:
    event = make_event()
    owner, joiner = make_user(), make_user()
    team = make_team(event, owner)
    token = client.get(f"/api/teams/{team.id}/invite", headers=auth(owner)).json()["token"]
    headers = auth(joiner)

    first = client.post("/api/teams/join", json={"token": token}, headers=headers)
    second = client.post("/api/teams/join", json={"token": token}, headers=headers)
    assert first.status_code == second.status_code == 200
    assert len(second.json()["members"]) == 2


def test_an_unknown_token_is_a_404(client: TestClient, make_user, auth) -> None:
    response = client.post(
        "/api/teams/join", json={"token": "not-a-real-token"}, headers=auth(make_user())
    )
    assert response.status_code == 404


def test_rotating_the_invite_kills_the_old_link(
    client: TestClient, make_event, make_team, make_user, auth
) -> None:
    event = make_event()
    owner, joiner = make_user(), make_user()
    team = make_team(event, owner)
    owner_headers = auth(owner)

    old = client.get(f"/api/teams/{team.id}/invite", headers=owner_headers).json()["token"]
    new = client.post(
        f"/api/teams/{team.id}/invite/rotate", headers=owner_headers
    ).json()["token"]
    assert new != old

    assert (
        client.post("/api/teams/join", json={"token": old}, headers=auth(joiner)).status_code
        == 404
    )
    assert (
        client.post("/api/teams/join", json={"token": new}, headers=auth(joiner)).status_code
        == 200
    )


def test_only_the_owner_rotates_the_invite(
    client: TestClient, make_event, make_team, make_user, auth
) -> None:
    event = make_event()
    owner, member = make_user(), make_user()
    team = make_team(event, owner, member)
    assert (
        client.post(f"/api/teams/{team.id}/invite/rotate", headers=auth(member)).status_code
        == 403
    )


def test_a_full_team_refuses_a_new_member(
    client: TestClient, make_event, make_team, make_user, auth
) -> None:
    event = make_event(max_team_size=2)
    owner, member, latecomer = make_user(), make_user(), make_user()
    team = make_team(event, owner, member)
    token = client.get(f"/api/teams/{team.id}/invite", headers=auth(owner)).json()["token"]

    response = client.post("/api/teams/join", json={"token": token}, headers=auth(latecomer))
    assert response.status_code == 409


def test_registration_closes_with_the_deadline(
    client: TestClient, make_event, make_team, make_user, auth
) -> None:
    closed = make_event(deadline=utcnow() - timedelta(hours=1))
    owner, latecomer = make_user(), make_user()
    team = make_team(closed, owner)
    token = client.get(f"/api/teams/{team.id}/invite", headers=auth(owner)).json()["token"]

    assert (
        client.post("/api/teams/join", json={"token": token}, headers=auth(latecomer)).status_code
        == 403
    )
    assert (
        client.post(
            f"/api/events/{closed.slug}/teams", json={"name": "Too Late"}, headers=auth(latecomer)
        ).status_code
        == 403
    )


def test_a_member_may_leave_and_an_owner_may_remove(
    client: TestClient, make_event, make_team, make_user, auth
) -> None:
    event = make_event()
    owner, member, stranger = make_user(), make_user(), make_user()
    team = make_team(event, owner, member)

    # A stranger cannot remove anybody.
    assert (
        client.delete(
            f"/api/teams/{team.id}/members/{member.id}", headers=auth(stranger)
        ).status_code
        == 403
    )
    # A member may remove themselves.
    assert (
        client.delete(
            f"/api/teams/{team.id}/members/{member.id}", headers=auth(member)
        ).status_code
        == 204
    )
    assert len(client.get(f"/api/teams/{team.id}").json()["members"]) == 1


def test_the_last_owner_cannot_abandon_a_populated_team(
    client: TestClient, make_event, make_team, make_user, auth
) -> None:
    event = make_event()
    owner, member = make_user(), make_user()
    team = make_team(event, owner, member)
    response = client.delete(f"/api/teams/{team.id}/members/{owner.id}", headers=auth(owner))
    assert response.status_code == 409


def test_only_the_owner_renames_a_team(
    client: TestClient, make_event, make_team, make_user, auth
) -> None:
    event = make_event()
    owner, member = make_user(), make_user()
    team = make_team(event, owner, member)

    assert (
        client.patch(
            f"/api/teams/{team.id}", json={"name": "Hostile Rename"}, headers=auth(member)
        ).status_code
        == 403
    )
    assert (
        client.patch(
            f"/api/teams/{team.id}", json={"name": "Agreed Rename"}, headers=auth(owner)
        ).status_code
        == 200
    )


def test_an_organizer_can_step_in(
    client: TestClient, make_event, make_team, make_user, auth
) -> None:
    event = make_event()
    team = make_team(event, make_user())
    organizer = make_user(Role.ORGANIZER)
    assert client.get(f"/api/teams/{team.id}/invite", headers=auth(organizer)).status_code == 200
    assert (
        client.patch(
            f"/api/teams/{team.id}", json={"name": "Renamed By Staff"}, headers=auth(organizer)
        ).status_code
        == 200
    )


# --------------------------------------------------------------------------- #
# Admin removal, for cause (Phase 6)
# --------------------------------------------------------------------------- #


def test_staff_removing_a_member_via_plain_delete_is_redirected_to_the_reasoned_route(
    client: TestClient, make_event, make_team, make_user, auth
) -> None:
    """The plain `DELETE` stays justification-free for a member leaving or an
    owner managing their own roster; staff stepping into someone else's team
    do not get that same free pass -- they are pointed at the route that
    requires a reason instead of being let through silently."""
    event = make_event()
    owner, member = make_user(), make_user()
    team = make_team(event, owner, member)
    organizer = make_user(Role.ORGANIZER)

    response = client.delete(
        f"/api/teams/{team.id}/members/{member.id}", headers=auth(organizer)
    )
    assert response.status_code == 409
    assert len(client.get(f"/api/teams/{team.id}").json()["members"]) == 2


def test_an_organizer_who_owns_the_team_may_still_use_plain_delete(
    client: TestClient, make_event, make_team, make_user, auth
) -> None:
    """An organizer managing their own team is acting as its owner, not
    intervening as staff -- the new redirect must not catch this case."""
    event = make_event()
    organizer = make_user(Role.ORGANIZER)
    member = make_user()
    team = make_team(event, organizer, member)

    response = client.delete(
        f"/api/teams/{team.id}/members/{member.id}", headers=auth(organizer)
    )
    assert response.status_code == 204


def test_removing_a_member_for_cause_requires_a_reason(
    client: TestClient, make_event, make_team, make_user, auth
) -> None:
    event = make_event()
    owner, member = make_user(), make_user()
    team = make_team(event, owner, member)
    response = client.post(
        f"/api/teams/{team.id}/members/{member.id}/remove",
        json={},
        headers=auth(make_user(Role.ORGANIZER)),
    )
    assert response.status_code == 422


def test_removing_a_member_for_cause_is_staff_only(
    client: TestClient, make_event, make_team, make_user, auth
) -> None:
    event = make_event()
    owner, member = make_user(), make_user()
    team = make_team(event, owner, member)
    payload = {"reason": "Code of conduct violation"}

    assert (
        client.post(
            f"/api/teams/{team.id}/members/{member.id}/remove", json=payload, headers=auth(owner)
        ).status_code
        == 403
    )
    assert (
        client.post(
            f"/api/teams/{team.id}/members/{member.id}/remove", json=payload
        ).status_code
        == 403
    )


def test_removing_one_of_several_members_for_cause_leaves_the_team_intact(
    client: TestClient, make_event, make_team, make_submission, make_user, auth
) -> None:
    event = make_event()
    owner, member = make_user(), make_user()
    team = make_team(event, owner, member)
    submission = make_submission(team)
    organizer = make_user(Role.ORGANIZER)

    response = client.post(
        f"/api/teams/{team.id}/members/{member.id}/remove",
        json={"reason": "Requested by the participant's captain"},
        headers=auth(organizer),
    )
    assert response.status_code == 204

    team_body = client.get(f"/api/teams/{team.id}").json()
    assert len(team_body["members"]) == 1

    submission_body = client.get(
        f"/api/submissions/{submission.id}", headers=auth(owner)
    ).json()
    assert submission_body["status"] == "draft"


def test_removing_a_solo_participant_disqualifies_their_submission_but_keeps_the_team(
    client: TestClient, make_event, make_team, make_submission, make_user, auth
) -> None:
    """The team persists (never hard-deleted) but ends up with zero members,
    same as a solo self-leave already allows -- what is new here is that the
    submission it owned does not silently become an orphaned draft: it is
    disqualified, the same as a direct `/disqualify` call would do."""
    event = make_event()
    owner = make_user()
    team = make_team(event, owner)
    submission = make_submission(team, status=SubmissionStatus.SUBMITTED)
    organizer = make_user(Role.ORGANIZER)

    response = client.post(
        f"/api/teams/{team.id}/members/{owner.id}/remove",
        json={"reason": "Account found to be fraudulent"},
        headers=auth(organizer),
    )
    assert response.status_code == 204

    team_body = client.get(f"/api/teams/{team.id}", headers=auth(organizer)).json()
    assert team_body["members"] == []

    submission_body = client.get(
        f"/api/submissions/{submission.id}", headers=auth(organizer)
    ).json()
    assert submission_body["status"] == SubmissionStatus.DISQUALIFIED.value

    rows = client.get(f"/api/events/{event.slug}/audit", headers=auth(organizer)).json()["rows"]
    disqualified = next(r for r in rows if r["action"] == "submission_disqualified")
    assert "Account found to be fraudulent" in disqualified["summary"]
    assert submission.name in disqualified["summary"]


def test_removing_a_solo_participant_with_no_submission_does_not_error(
    client: TestClient, make_event, make_team, make_user, auth
) -> None:
    event = make_event()
    owner = make_user()
    team = make_team(event, owner)
    response = client.post(
        f"/api/teams/{team.id}/members/{owner.id}/remove",
        json={"reason": "Never started a project"},
        headers=auth(make_user(Role.ORGANIZER)),
    )
    assert response.status_code == 204


def test_removal_for_cause_is_recorded_in_the_audit_log_with_the_reason(
    client: TestClient, make_event, make_team, make_user, auth
) -> None:
    event = make_event()
    owner, member = make_user(), make_user()
    team = make_team(event, owner, member)
    organizer = make_user(Role.ORGANIZER)
    headers = auth(organizer)

    client.post(
        f"/api/teams/{team.id}/members/{member.id}/remove",
        json={"reason": "Repeated harassment reports"},
        headers=headers,
    )

    rows = client.get(f"/api/events/{event.slug}/audit", headers=headers).json()["rows"]
    entry = next(r for r in rows if r["action"] == "team_member_removed")
    assert "Repeated harassment reports" in entry["summary"]
    assert member.email in entry["summary"]


# --------------------------------------------------------------------------- #
# Pagination (Phase 5): `/teams` returns a `Page`, `/teams/mine` is the
# dedicated lookup that must not depend on which page a caller's team is on.
# --------------------------------------------------------------------------- #


def test_teams_list_is_a_page(client: TestClient, make_event, make_team, make_user) -> None:
    event = make_event()
    for _ in range(3):
        make_team(event, make_user())

    body = client.get(f"/api/events/{event.slug}/teams").json()
    assert body["total"] == 3
    assert len(body["items"]) == 3
    assert body["page"] == 1
    assert body["pages"] == 1


def test_teams_list_respects_per_page(
    client: TestClient, make_event, make_team, make_user
) -> None:
    event = make_event()
    for _ in range(5):
        make_team(event, make_user())

    page1 = client.get(f"/api/events/{event.slug}/teams?per_page=2").json()
    assert page1["total"] == 5
    assert page1["pages"] == 3
    assert len(page1["items"]) == 2

    page3 = client.get(f"/api/events/{event.slug}/teams?per_page=2&page=3").json()
    assert len(page3["items"]) == 1

    # The two pages together, plus the middle one implied by `pages == 3`,
    # cover every team exactly once -- no row duplicated or dropped at the
    # boundary between pages.
    page2 = client.get(f"/api/events/{event.slug}/teams?per_page=2&page=2").json()
    seen = {t["id"] for t in page1["items"] + page2["items"] + page3["items"]}
    assert len(seen) == 5


def test_teams_mine_finds_the_callers_team_regardless_of_page(
    client: TestClient, auth, make_event, make_team, make_user
) -> None:
    """The regression this route exists to prevent: a participant's own team
    must be findable even when it would not be on page 1 of a large roster."""
    event = make_event()
    me = make_user()
    for _ in range(9):
        make_team(event, make_user())  # push `me`'s team past a small page size
    mine = make_team(event, me, name="Findable Even On Page Ten")

    found = client.get(f"/api/events/{event.slug}/teams/mine", headers=auth(me)).json()
    assert found is not None
    assert found["id"] == str(mine.id)


def test_teams_mine_is_null_with_no_team(
    client: TestClient, auth, make_event, make_user
) -> None:
    event = make_event()
    assert client.get(f"/api/events/{event.slug}/teams/mine", headers=auth(make_user())).json() is None


def test_teams_mine_is_null_for_an_anonymous_caller(
    client: TestClient, make_event
) -> None:
    event = make_event()
    r = client.get(f"/api/events/{event.slug}/teams/mine")
    assert r.status_code == 200, r.text
    assert r.json() is None
