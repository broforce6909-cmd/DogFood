"""Event creation and configuration: dates, tracks, prizes, custom questions."""

from __future__ import annotations

from datetime import timedelta

from fastapi.testclient import TestClient

from app.models import Role, utcnow

NOW = utcnow()


def event_payload(**overrides) -> dict:
    payload = {
        "slug": "new-event",
        "name": "A New Event",
        "tagline": "Configurable dates, tracks and prizes",
        "starts_at": (NOW + timedelta(days=1)).isoformat(),
        "ends_at": (NOW + timedelta(days=3)).isoformat(),
        "registration_opens_at": NOW.isoformat(),
        "submission_opens_at": (NOW + timedelta(days=1)).isoformat(),
        "submission_deadline": (NOW + timedelta(days=3)).isoformat(),
        "judging_opens_at": (NOW + timedelta(days=3)).isoformat(),
        "judging_closes_at": (NOW + timedelta(days=5)).isoformat(),
        "max_team_size": 4,
        "is_published": True,
    }
    payload.update(overrides)
    return payload


def test_an_organizer_can_create_an_event(client: TestClient, make_user, auth) -> None:
    organizer = make_user(Role.ORGANIZER)
    response = client.post("/api/events", json=event_payload(), headers=auth(organizer))
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["slug"] == "new-event"
    assert body["max_team_size"] == 4
    assert body["tracks"] == [] and body["prizes"] == [] and body["questions"] == []


def test_participants_and_judges_cannot_create_events(
    client: TestClient, make_user, auth
) -> None:
    for role in (Role.PARTICIPANT, Role.JUDGE):
        response = client.post(
            "/api/events", json=event_payload(), headers=auth(make_user(role))
        )
        assert response.status_code == 403, role


def test_a_visitor_cannot_create_an_event(client: TestClient) -> None:
    assert client.post("/api/events", json=event_payload()).status_code == 403


def test_out_of_order_windows_are_refused(client: TestClient, make_user, auth) -> None:
    organizer = make_user(Role.ORGANIZER)
    response = client.post(
        "/api/events",
        json=event_payload(submission_deadline=(NOW - timedelta(days=30)).isoformat()),
        headers=auth(organizer),
    )
    assert response.status_code == 422


def test_a_duplicate_slug_is_a_conflict(client: TestClient, make_user, auth) -> None:
    headers = auth(make_user(Role.ORGANIZER))
    assert client.post("/api/events", json=event_payload(), headers=headers).status_code == 201
    assert client.post("/api/events", json=event_payload(), headers=headers).status_code == 409


def test_an_unpublished_event_does_not_exist_as_far_as_a_visitor_knows(
    client: TestClient, make_event, make_user, auth
) -> None:
    event = make_event(slug="hidden", published=False)

    assert client.get(f"/api/events/{event.slug}").status_code == 404
    assert (
        client.get(f"/api/events/{event.slug}", headers=auth(make_user())).status_code == 404
    )
    assert (
        client.get(
            f"/api/events/{event.slug}", headers=auth(make_user(Role.ORGANIZER))
        ).status_code
        == 200
    )


def test_the_event_list_shows_each_caller_exactly_what_they_could_open(
    client: TestClient, make_event, make_user, auth
) -> None:
    make_event(slug="public-one", published=True)
    make_event(slug="hidden-one", published=False)

    assert [e["slug"] for e in client.get("/api/events").json()] == ["public-one"]
    staff = client.get("/api/events", headers=auth(make_user(Role.ORGANIZER))).json()
    assert sorted(e["slug"] for e in staff) == ["hidden-one", "public-one"]


def test_the_api_reports_whether_its_own_windows_are_open(
    client: TestClient, make_event
) -> None:
    """So the UI never has to decide whether a deadline has passed."""
    open_event = make_event(slug="open-now")
    closed = make_event(slug="closed-now", deadline=NOW - timedelta(hours=1))

    assert client.get(f"/api/events/{open_event.slug}").json()["submissions_open"] is True
    assert client.get(f"/api/events/{closed.slug}").json()["submissions_open"] is False


def test_the_slug_is_not_editable(client: TestClient, make_event, make_user, auth) -> None:
    event = make_event(slug="permanent")
    response = client.patch(
        f"/api/events/{event.slug}",
        json={"slug": "renamed", "name": "Renamed"},
        headers=auth(make_user(Role.ORGANIZER)),
    )
    assert response.status_code == 200
    assert response.json()["slug"] == "permanent"
    assert response.json()["name"] == "Renamed"


# --------------------------------------------------------------------------- #
# Tracks, prizes, questions
# --------------------------------------------------------------------------- #


def test_tracks_are_organizer_configured(
    client: TestClient, make_event, make_user, auth
) -> None:
    event = make_event()
    organizer = auth(make_user(Role.ORGANIZER))
    participant = auth(make_user())

    assert (
        client.post(
            f"/api/events/{event.slug}/tracks",
            json={"key": "ai-agents", "name": "AI & Agents"},
            headers=participant,
        ).status_code
        == 403
    )

    created = client.post(
        f"/api/events/{event.slug}/tracks",
        json={"key": "ai-agents", "name": "AI & Agents", "position": 0},
        headers=organizer,
    )
    assert created.status_code == 201, created.text
    # ...and a visitor can read it, because tracks are how a gallery filters.
    assert client.get(f"/api/events/{event.slug}/tracks").json()[0]["key"] == "ai-agents"


def test_track_keys_are_unique_within_an_event_and_free_across_events(
    client: TestClient, make_event, make_user, auth
) -> None:
    headers = auth(make_user(Role.ORGANIZER))
    first, second = make_event(slug="one"), make_event(slug="two")
    body = {"key": "general", "name": "General"}

    assert client.post(f"/api/events/one/tracks", json=body, headers=headers).status_code == 201
    assert client.post(f"/api/events/one/tracks", json=body, headers=headers).status_code == 409
    assert client.post(f"/api/events/two/tracks", json=body, headers=headers).status_code == 201


def test_a_prize_cannot_point_at_another_events_track(
    client: TestClient, make_event, make_track, make_user, auth
) -> None:
    """The composite foreign key, surfacing as a 422 rather than a 500."""
    headers = auth(make_user(Role.ORGANIZER))
    home, elsewhere = make_event(slug="home"), make_event(slug="elsewhere")
    foreign_track = make_track(elsewhere, key="theirs")

    response = client.post(
        "/api/events/home/prizes",
        json={"title": "Best Of", "value": "$100", "track_id": str(foreign_track.id)},
        headers=headers,
    )
    assert response.status_code == 422


def test_custom_questions_round_trip(
    client: TestClient, make_event, make_user, auth
) -> None:
    event = make_event()
    headers = auth(make_user(Role.ORGANIZER))

    created = client.post(
        f"/api/events/{event.slug}/questions",
        json={
            "prompt": "What did you cut?",
            "kind": "textarea",
            "required": True,
            "position": 0,
        },
        headers=headers,
    )
    assert created.status_code == 201, created.text

    detail = client.get(f"/api/events/{event.slug}").json()
    assert detail["questions"][0]["prompt"] == "What did you cut?"
    assert detail["questions"][0]["required"] is True


def test_a_select_question_needs_options(
    client: TestClient, make_event, make_user, auth
) -> None:
    event = make_event()
    response = client.post(
        f"/api/events/{event.slug}/questions",
        json={"prompt": "Pick one", "kind": "select"},
        headers=auth(make_user(Role.ORGANIZER)),
    )
    assert response.status_code == 422
