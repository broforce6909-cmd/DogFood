"""Per-event registration, ahead of team formation.

Additive, not a gate: these tests specifically check that team creation still
works with no prior registration, because that is the property that makes
this a roster rather than a new authorization layer. See
`models.EventRegistration`'s own docstring.
"""

from __future__ import annotations

from datetime import timedelta

from fastapi.testclient import TestClient

from app.models import Role, utcnow


def register(client: TestClient, slug: str, headers, **body):
    return client.post(f"/api/events/{slug}/register", json=body, headers=headers)


def test_registering_creates_a_roster_row(client: TestClient, make_event, make_user, auth) -> None:
    event = make_event()
    user = make_user(email="priya@example.com", name="Priya Rivera")
    r = register(client, event.slug, auth(user), discord_username="priyar")
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["email"] == "priya@example.com"  # defaulted from the account
    assert body["discord_username"] == "priyar"
    assert body["is_team_leader"] is False
    assert body["leader_name"] is None
    assert body["has_team"] is False
    assert body["user_display_name"] == "Priya Rivera"


def test_an_explicit_email_overrides_the_account_email(
    client: TestClient, make_event, make_user, auth
) -> None:
    event = make_event()
    user = make_user(email="work@example.com")
    r = register(
        client, event.slug, auth(user), discord_username="worker", email="personal@example.com"
    )
    assert r.status_code == 201, r.text
    assert r.json()["email"] == "personal@example.com"


def test_registering_as_team_leader_requires_a_leader_name(
    client: TestClient, make_event, make_user, auth
) -> None:
    event = make_event()
    r = register(
        client, event.slug, auth(make_user()), discord_username="lead", is_team_leader=True
    )
    assert r.status_code == 422


def test_registering_as_team_leader_with_a_name_succeeds(
    client: TestClient, make_event, make_user, auth
) -> None:
    event = make_event()
    r = register(
        client,
        event.slug,
        auth(make_user()),
        discord_username="lead",
        is_team_leader=True,
        leader_name="Kwame Osei",
    )
    assert r.status_code == 201, r.text
    assert r.json()["is_team_leader"] is True
    assert r.json()["leader_name"] == "Kwame Osei"


def test_a_leader_name_without_leader_intent_is_rejected(
    client: TestClient, make_event, make_user, auth
) -> None:
    """A leader name only means something alongside `is_team_leader` -- a
    stray name with the box unchecked is a client bug, not free-form data."""
    event = make_event()
    r = register(
        client, event.slug, auth(make_user()), discord_username="x", leader_name="Somebody"
    )
    assert r.status_code == 422


def test_a_visitor_cannot_register(client: TestClient, make_event) -> None:
    event = make_event()
    assert (
        client.post(
            f"/api/events/{event.slug}/register", json={"discord_username": "visitor"}
        ).status_code
        == 401
    )


def test_registering_twice_is_a_conflict(client: TestClient, make_event, make_user, auth) -> None:
    event = make_event()
    headers = auth(make_user())
    assert register(client, event.slug, headers, discord_username="first").status_code == 201
    assert register(client, event.slug, headers, discord_username="second").status_code == 409


def test_registering_before_the_window_opens_is_refused(
    client: TestClient, make_event, make_user, auth
) -> None:
    event = make_event(registration_opens_at=utcnow() + timedelta(days=1))
    r = register(client, event.slug, auth(make_user()), discord_username="early")
    assert r.status_code == 403


def test_registering_is_not_required_to_create_a_team(
    client: TestClient, make_event, make_user, auth
) -> None:
    """The whole point of this being a roster and not a gate: a user who never
    registered can still form a team, exactly as before this feature existed."""
    event = make_event()
    headers = auth(make_user())
    r = client.post(f"/api/events/{event.slug}/teams", json={"name": "Unregistered"}, headers=headers)
    assert r.status_code == 201, r.text


def test_my_registration_is_null_before_registering(
    client: TestClient, make_event, make_user, auth
) -> None:
    event = make_event()
    r = client.get(f"/api/events/{event.slug}/registrations/me", headers=auth(make_user()))
    assert r.status_code == 200, r.text
    assert r.json() is None


def test_my_registration_reflects_a_real_registration(
    client: TestClient, make_event, make_user, auth
) -> None:
    event = make_event()
    headers = auth(make_user())
    assert register(client, event.slug, headers, discord_username="me").status_code == 201
    r = client.get(f"/api/events/{event.slug}/registrations/me", headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["discord_username"] == "me"


def test_has_team_becomes_true_after_joining_a_team(
    client: TestClient, make_event, make_user, auth
) -> None:
    event = make_event()
    user = make_user()
    headers = auth(user)
    assert register(client, event.slug, headers, discord_username="soon-teamed").status_code == 201
    assert (
        client.get(f"/api/events/{event.slug}/registrations/me", headers=headers).json()["has_team"]
        is False
    )

    client.post(f"/api/events/{event.slug}/teams", json={"name": "Now Teamed"}, headers=headers)
    assert (
        client.get(f"/api/events/{event.slug}/registrations/me", headers=headers).json()["has_team"]
        is True
    )


def test_registrations_list_is_staff_only(
    client: TestClient, make_event, make_user, auth
) -> None:
    event = make_event()
    r = register(client, event.slug, auth(make_user()), discord_username="registrant")
    assert r.status_code == 201, r.text

    assert (
        client.get(f"/api/events/{event.slug}/registrations", headers=auth(make_user())).status_code
        == 403
    )
    assert client.get(f"/api/events/{event.slug}/registrations").status_code == 403

    organizer = make_user(Role.ORGANIZER)
    listed = client.get(f"/api/events/{event.slug}/registrations", headers=auth(organizer))
    assert listed.status_code == 200, listed.text
    assert listed.json()["total"] == 1
    assert listed.json()["items"][0]["discord_username"] == "registrant"


def test_registration_is_recorded_in_the_audit_log(
    client: TestClient, make_event, make_user, auth
) -> None:
    event = make_event()
    user = make_user()
    assert register(client, event.slug, auth(user), discord_username="audited").status_code == 201

    organizer = make_user(Role.ORGANIZER)
    rows = client.get(f"/api/events/{event.slug}/audit", headers=auth(organizer)).json()["rows"]
    assert any(r["action"] == "event_registered" for r in rows)


# --------------------------------------------------------------------------- #
# Admin removal, for cause (Phase 6)
# --------------------------------------------------------------------------- #


def test_removing_a_registration_requires_a_reason(
    client: TestClient, make_event, make_user, auth
) -> None:
    event = make_event()
    user = make_user()
    reg = register(client, event.slug, auth(user), discord_username="mistaken").json()

    organizer = make_user(Role.ORGANIZER)
    response = client.post(
        f"/api/events/{event.slug}/registrations/{reg['id']}/remove",
        json={},
        headers=auth(organizer),
    )
    assert response.status_code == 422


def test_removing_a_registration_is_staff_only(
    client: TestClient, make_event, make_user, auth
) -> None:
    event = make_event()
    user = make_user()
    reg = register(client, event.slug, auth(user), discord_username="mistaken").json()
    payload = {"reason": "Duplicate signup"}

    assert (
        client.post(
            f"/api/events/{event.slug}/registrations/{reg['id']}/remove",
            json=payload,
            headers=auth(user),
        ).status_code
        == 403
    )
    assert (
        client.post(
            f"/api/events/{event.slug}/registrations/{reg['id']}/remove", json=payload
        ).status_code
        == 403
    )


def test_removing_a_registration_deletes_the_row(
    client: TestClient, make_event, make_user, auth
) -> None:
    event = make_event()
    user = make_user()
    reg = register(client, event.slug, auth(user), discord_username="mistaken").json()
    organizer = make_user(Role.ORGANIZER)

    response = client.post(
        f"/api/events/{event.slug}/registrations/{reg['id']}/remove",
        json={"reason": "Duplicate signup, kept the first one"},
        headers=auth(organizer),
    )
    assert response.status_code == 204

    listed = client.get(f"/api/events/{event.slug}/registrations", headers=auth(organizer)).json()
    assert listed["total"] == 0

    # Removing the row is not a ban -- registering again must still work.
    again = register(client, event.slug, auth(user), discord_username="mistaken")
    assert again.status_code == 201, again.text


def test_removal_of_a_registration_is_recorded_in_the_audit_log_with_the_reason(
    client: TestClient, make_event, make_user, auth
) -> None:
    event = make_event()
    user = make_user(email="dup@example.com")
    reg = register(client, event.slug, auth(user), discord_username="mistaken").json()
    organizer = make_user(Role.ORGANIZER)
    headers = auth(organizer)

    client.post(
        f"/api/events/{event.slug}/registrations/{reg['id']}/remove",
        json={"reason": "Registered under the wrong account"},
        headers=headers,
    )

    rows = client.get(f"/api/events/{event.slug}/audit", headers=headers).json()["rows"]
    entry = next(r for r in rows if r["action"] == "registration_removed")
    assert "Registered under the wrong account" in entry["summary"]
    assert "dup@example.com" in entry["summary"]


def test_a_registration_id_from_another_event_is_not_found(
    client: TestClient, make_event, make_user, auth
) -> None:
    event_a = make_event()
    event_b = make_event()
    user = make_user()
    reg = register(client, event_a.slug, auth(user), discord_username="cross-event").json()

    response = client.post(
        f"/api/events/{event_b.slug}/registrations/{reg['id']}/remove",
        json={"reason": "Wrong event"},
        headers=auth(make_user(Role.ORGANIZER)),
    )
    assert response.status_code == 404
