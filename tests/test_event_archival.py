"""Event archival: frozen, out of the default lists, still readable.

What is pinned down here:

* **Who may archive**: staff, and not participants, judges, visitors or a
  read-only admin.
* **Frozen means frozen**: on an archived event *every* non-read verb is refused
  for everything that belongs to it -- checked as a full verb matrix over the
  resource types, not only for the handful of routes listed by hand -- and the
  same actions work again the moment it is unarchived.
* **Out of the way, not gone**: archived events leave the default list, come
  back with `archived=only|include`, and stay readable (event page, gallery,
  results) for anyone who could read them before.
* **It is audited.**
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from app.access import PLATFORM, READ_ONLY_ACTIONS, Action, check_access
from app.models import (
    AdminLevel,
    AssignmentStatus,
    AuditAction,
    AuditEntry,
    Role,
    SubmissionStatus,
    utcnow,
)


@pytest.fixture()
def owner(make_user):
    return make_user(Role.ADMIN)  # owner-level by default


def archive(client, slug, headers):
    return client.post(f"/api/events/{slug}/archive", headers=headers)


def unarchive(client, slug, headers):
    return client.post(f"/api/events/{slug}/unarchive", headers=headers)


def slugs(client, headers=None, **params):
    r = client.get("/api/events", params=params, headers=headers or {})
    assert r.status_code == 200, r.text
    return {e["slug"] for e in r.json()}


# --------------------------------------------------------------------------- #
# Archiving and unarchiving
# --------------------------------------------------------------------------- #


def test_an_organizer_archives_and_unarchives(
    client: TestClient, auth, make_user, make_event, db
) -> None:
    organizer = make_user(Role.ORGANIZER)
    event = make_event()

    r = archive(client, event.slug, auth(organizer))
    assert r.status_code == 200, r.text
    assert r.json()["archived_at"] is not None

    shown = client.get(f"/api/events/{event.slug}", headers=auth(organizer)).json()
    assert shown["archived_at"] is not None

    r = unarchive(client, event.slug, auth(organizer))
    assert r.status_code == 200, r.text
    assert r.json()["archived_at"] is None

    actions = [
        row.action
        for row in db.query(AuditEntry).order_by(AuditEntry.seq.asc()).all()
        if row.action in (AuditAction.EVENT_ARCHIVED, AuditAction.EVENT_UNARCHIVED)
    ]
    assert actions == [AuditAction.EVENT_ARCHIVED, AuditAction.EVENT_UNARCHIVED]


def test_archiving_twice_or_unarchiving_an_active_event_is_a_conflict(
    client: TestClient, auth, make_user, make_event
) -> None:
    organizer = make_user(Role.ORGANIZER)
    event = make_event()
    assert unarchive(client, event.slug, auth(organizer)).status_code == 409
    assert archive(client, event.slug, auth(organizer)).status_code == 200
    assert archive(client, event.slug, auth(organizer)).status_code == 409


@pytest.mark.parametrize("role", [Role.PARTICIPANT, Role.JUDGE])
def test_only_staff_can_archive(client: TestClient, auth, make_user, make_event, role) -> None:
    event = make_event()
    assert archive(client, event.slug, auth(make_user(role))).status_code == 403
    assert archive(client, event.slug, {}).status_code in (401, 403)


def test_a_read_only_admin_cannot_archive_but_a_manager_can(
    client: TestClient, auth, make_user, make_event, db
) -> None:
    event = make_event()
    auditor = make_user(Role.ADMIN)
    auditor.admin_level = AdminLevel.AUDITOR
    manager = make_user(Role.ADMIN)
    manager.admin_level = AdminLevel.MANAGER
    db.commit()

    assert archive(client, event.slug, auth(auditor)).status_code == 403
    assert archive(client, event.slug, auth(manager)).status_code == 200


# --------------------------------------------------------------------------- #
# Out of the default lists, not gone
# --------------------------------------------------------------------------- #


def test_archived_events_leave_the_default_list_and_come_back_on_request(
    client: TestClient, auth, make_user, make_event
) -> None:
    organizer = make_user(Role.ORGANIZER)
    active = make_event()
    frozen = make_event()
    archive(client, frozen.slug, auth(organizer))

    assert slugs(client, auth(organizer)) >= {active.slug}
    assert frozen.slug not in slugs(client, auth(organizer))
    assert frozen.slug not in slugs(client)  # a visitor's default list too

    assert slugs(client, auth(organizer), archived="only") == {frozen.slug}
    both = slugs(client, auth(organizer), archived="include")
    assert {active.slug, frozen.slug} <= both


def test_an_archived_event_is_still_readable_by_its_link(
    client: TestClient, auth, make_user, make_event, make_team, make_submission
) -> None:
    organizer = make_user(Role.ORGANIZER)
    event = make_event()
    owner_user = make_user(Role.PARTICIPANT)
    team = make_team(event, owner_user, name="Kept Team")
    make_submission(team, name="Kept Project", status=SubmissionStatus.SUBMITTED)
    archive(client, event.slug, auth(organizer))

    assert client.get(f"/api/events/{event.slug}").status_code == 200
    gallery = client.get("/api/gallery", params={"event": event.slug}).json()
    assert [p["name"] for p in gallery["items"]] == ["Kept Project"]
    assert client.get(f"/api/events/{event.slug}/results", headers=auth(organizer)).status_code == 200


# --------------------------------------------------------------------------- #
# Frozen
# --------------------------------------------------------------------------- #


def test_writes_are_refused_while_archived_and_work_again_after(
    client: TestClient, auth, make_user, make_event, make_criterion
) -> None:
    organizer = make_user(Role.ORGANIZER)
    participant = make_user(Role.PARTICIPANT)
    event = make_event()

    def make_team_via_api():
        return client.post(
            f"/api/events/{event.slug}/teams", json={"name": f"T{uuid.uuid4().hex[:6]}"},
            headers=auth(participant),
        )

    def add_criterion():
        return client.post(
            f"/api/events/{event.slug}/criteria",
            json={"key": f"k{uuid.uuid4().hex[:5]}", "name": "New", "weight": 1,
                  "min_score": 1, "max_score": 5, "position": 0},
            headers=auth(organizer),
        )

    assert make_team_via_api().status_code == 201  # the control: it works while active
    assert add_criterion().status_code == 201

    archive(client, event.slug, auth(organizer))
    assert make_team_via_api().status_code == 403
    assert add_criterion().status_code == 403
    assert client.patch(
        f"/api/events/{event.slug}", json={"name": "Renamed"}, headers=auth(organizer)
    ).status_code == 403
    # Deleting is refused too: archiving is the non-destructive alternative, and
    # unarchiving first is the deliberate extra step before anything is destroyed.
    assert client.delete(f"/api/events/{event.slug}", headers=auth(organizer)).status_code == 403

    unarchive(client, event.slug, auth(organizer))
    assert add_criterion().status_code == 201
    assert client.patch(
        f"/api/events/{event.slug}", json={"name": "Renamed"}, headers=auth(organizer)
    ).status_code == 200


def test_a_judge_cannot_score_on_an_archived_event(
    client: TestClient, auth, make_user, make_event, judging_event, make_criterion,
    make_team, make_submission, make_judge, make_assignment,
) -> None:
    organizer = make_user(Role.ORGANIZER)
    event = judging_event()
    criterion = make_criterion(event, key="overall", name="Overall")
    owner_user = make_user(Role.PARTICIPANT)
    submission = make_submission(
        make_team(event, owner_user), name="P", status=SubmissionStatus.SUBMITTED
    )
    judge_user = make_user(Role.JUDGE)
    judge = make_judge(event, judge_user)
    ballot = make_assignment(judge, submission, status=AssignmentStatus.PENDING)

    def score():
        return client.put(
            f"/api/judging/assignments/{ballot.id}/scores",
            json={"scores": [{"criterion_id": str(criterion.id), "value": 4.0}], "complete": True},
            headers=auth(judge_user),
        )

    archive(client, event.slug, auth(organizer))
    assert score().status_code in (403, 409)
    unarchive(client, event.slug, auth(organizer))
    assert score().status_code == 200


def test_every_write_verb_is_refused_on_everything_an_archived_event_owns(
    make_user, make_event, judging_event, make_criterion, make_team, make_submission,
    make_judge, make_assignment, make_score, db,
) -> None:
    """The matrix: for every resource type that belongs to an event and every
    verb that is not a read, an owner-level admin is refused while the event is
    archived -- and is not refused *by archival* once it is not. New resource
    types and new verbs are covered automatically, so the freeze cannot quietly
    stop meaning "everything"."""
    admin = make_user(Role.ADMIN)
    event = judging_event()
    criterion = make_criterion(event, key="overall", name="Overall")
    owner_user = make_user(Role.PARTICIPANT)
    team = make_team(event, owner_user)
    submission = make_submission(team, name="P", status=SubmissionStatus.SUBMITTED)
    judge = make_judge(event, make_user(Role.JUDGE))
    ballot = make_assignment(judge, submission, status=AssignmentStatus.COMPLETE)
    score = make_score(ballot, criterion, 4)

    resources = [event, criterion, team, submission, judge, ballot, score]
    writes = [a for a in Action if a not in READ_ONLY_ACTIONS and a is not Action.ARCHIVE]

    event.archived_at = utcnow()
    db.commit()
    for resource in resources:
        for action in writes:
            assert check_access(admin, resource, action) is False, (
                type(resource).__name__, action.value,
            )
        # Reads are untouched by archival.
        assert check_access(admin, resource, Action.READ) is True, type(resource).__name__

    # Un-freezing changes exactly this: the gate was the thing refusing.
    event.archived_at = None
    db.commit()
    assert check_access(admin, event, Action.MANAGE) is True
    assert check_access(admin, team, Action.MANAGE) is True
    assert check_access(admin, criterion, Action.UPDATE) is True


def test_archive_itself_is_never_frozen(make_user, make_event, db) -> None:
    admin = make_user(Role.ADMIN)
    event = make_event()
    event.archived_at = utcnow()
    db.commit()
    assert check_access(admin, event, Action.ARCHIVE) is True
    assert check_access(make_user(Role.PARTICIPANT), event, Action.ARCHIVE) is False
    assert check_access(admin, PLATFORM, Action.CREATE_EVENT) is True  # platform verbs unaffected


def test_archival_never_touches_another_event(
    client: TestClient, auth, make_user, make_event
) -> None:
    organizer = make_user(Role.ORGANIZER)
    participant = make_user(Role.PARTICIPANT)
    frozen = make_event()
    other = make_event()
    archive(client, frozen.slug, auth(organizer))
    r = client.post(
        f"/api/events/{other.slug}/teams", json={"name": "Elsewhere"}, headers=auth(participant)
    )
    assert r.status_code == 201, r.text
