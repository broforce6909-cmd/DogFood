"""Organizer announcements: `visible_to_visitors` is the whole access rule.

See `models.Announcement`'s own docstring and `access._check_announcement`.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.models import Role


def post(client: TestClient, slug: str, headers, **body):
    return client.post(f"/api/events/{slug}/announcements", json=body, headers=headers)


def test_an_organizer_can_post_an_announcement(
    client: TestClient, make_event, make_user, auth
) -> None:
    event = make_event()
    organizer = make_user(Role.ORGANIZER, name="Organizer Rivera")
    response = post(
        client,
        event.slug,
        auth(organizer),
        title="Lunch moved",
        body="Lunch is now in the atrium.",
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["title"] == "Lunch moved"
    assert body["visible_to_visitors"] is False
    assert body["posted_by_display_name"] == "Organizer Rivera"


def test_posting_an_announcement_is_staff_only(
    client: TestClient, make_event, make_user, auth
) -> None:
    event = make_event()
    payload = {"title": "Hijack", "body": "Not staff"}
    assert (
        post(client, event.slug, auth(make_user(Role.PARTICIPANT)), **payload).status_code == 403
    )
    assert (
        client.post(f"/api/events/{event.slug}/announcements", json=payload).status_code == 403
    )


def test_title_and_body_cannot_be_blank(
    client: TestClient, make_event, make_user, auth
) -> None:
    event = make_event()
    headers = auth(make_user(Role.ORGANIZER))
    assert post(client, event.slug, headers, title="   ", body="Real body").status_code == 422
    assert post(client, event.slug, headers, title="Real title", body="   ").status_code == 422


def test_a_participants_only_announcement_is_hidden_from_a_visitor(
    client: TestClient, make_event, make_user, auth
) -> None:
    event = make_event()
    post(
        client,
        event.slug,
        auth(make_user(Role.ORGANIZER)),
        title="Internal note",
        body="Judging starts soon.",
    )

    visitor_view = client.get(f"/api/events/{event.slug}/announcements")
    assert visitor_view.status_code == 200
    assert visitor_view.json() == []

    participant_view = client.get(
        f"/api/events/{event.slug}/announcements", headers=auth(make_user(Role.PARTICIPANT))
    )
    assert [a["title"] for a in participant_view.json()] == ["Internal note"]


def test_a_visitor_flagged_announcement_is_shown_to_everyone(
    client: TestClient, make_event, make_user, auth
) -> None:
    event = make_event()
    post(
        client,
        event.slug,
        auth(make_user(Role.ORGANIZER)),
        title="Livestream link",
        body="Watch the finale here.",
        visible_to_visitors=True,
    )

    visitor_view = client.get(f"/api/events/{event.slug}/announcements")
    assert [a["title"] for a in visitor_view.json()] == ["Livestream link"]


def test_announcements_are_newest_first(
    client: TestClient, make_event, make_user, auth
) -> None:
    event = make_event()
    headers = auth(make_user(Role.ORGANIZER))
    post(client, event.slug, headers, title="First", body="one", visible_to_visitors=True)
    post(client, event.slug, headers, title="Second", body="two", visible_to_visitors=True)

    titles = [a["title"] for a in client.get(f"/api/events/{event.slug}/announcements").json()]
    assert titles == ["Second", "First"]


def test_posting_an_announcement_is_recorded_in_the_audit_log(
    client: TestClient, make_event, make_user, auth
) -> None:
    event = make_event()
    organizer = make_user(Role.ORGANIZER)
    headers = auth(organizer)
    post(client, event.slug, headers, title="Wifi password", body="hackathon2026")

    rows = client.get(f"/api/events/{event.slug}/audit", headers=headers).json()["rows"]
    entry = next(r for r in rows if r["action"] == "announcement_posted")
    assert "Wifi password" in entry["summary"]
    assert organizer.email in entry["summary"]


def test_an_unpublished_events_announcements_are_not_leaked_to_a_visitor(
    client: TestClient, make_event, make_user, auth
) -> None:
    """`visible_to_visitors` only ever widens who sees a *published* event's
    notice -- it is not a way to peek at a draft event that has not gone
    public yet."""
    event = make_event(published=False)
    post(
        client,
        event.slug,
        auth(make_user(Role.ORGANIZER)),
        title="Draft-event notice",
        body="Should not leak",
        visible_to_visitors=True,
    )
    assert client.get(f"/api/events/{event.slug}/announcements").status_code == 404
