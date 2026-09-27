"""Multi-level admins: owner, manager, auditor.

What is pinned down here, in the order it matters:

* **Nothing changed for existing admins.** A plain `Role.ADMIN` account is an
  `owner` and still does everything an admin always could.
* **A manager is an admin minus account administration.** It runs events, reads
  the global audit, and cannot create users, change roles or deactivate anyone.
* **An auditor is read-only, and not because each route remembered.** It reads
  everything an admin reads; every unsafe request is refused before any handler
  runs, and `check_access` independently refuses every non-read verb.
* **The last owner cannot lock everyone out.**
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from app.access import PLATFORM, Action, check_access
from app.models import AdminLevel, AuditAction, AuditEntry, Role, User, utcnow

NOW = utcnow()


@pytest.fixture()
def make_admin(make_user, db):
    def _make(level: AdminLevel) -> User:
        user = make_user(Role.ADMIN)
        user.admin_level = level
        db.commit()
        db.refresh(user)
        return user

    return _make


def event_payload() -> dict:
    return {
        "slug": f"lvl-{uuid.uuid4().hex[:10]}",
        "name": "Level Test Event",
        "starts_at": (NOW + timedelta(days=1)).isoformat(),
        "ends_at": (NOW + timedelta(days=3)).isoformat(),
        "registration_opens_at": NOW.isoformat(),
        "submission_opens_at": (NOW + timedelta(days=1)).isoformat(),
        "submission_deadline": (NOW + timedelta(days=3)).isoformat(),
        "max_team_size": 4,
        "is_published": True,
    }


# --------------------------------------------------------------------------- #
# Existing admins are unchanged
# --------------------------------------------------------------------------- #


def test_a_plain_admin_is_an_owner_and_can_still_do_everything(
    client: TestClient, auth, make_user
) -> None:
    admin = make_user(Role.ADMIN)
    assert admin.admin_level is AdminLevel.OWNER

    r = client.post(
        "/api/users",
        json={"email": "new.judge@example.com", "display_name": "New Judge", "role": "judge"},
        headers=auth(admin),
    )
    assert r.status_code == 201, r.text

    target = make_user(Role.PARTICIPANT)
    r = client.patch(f"/api/users/{target.id}/role", json={"role": "organizer"}, headers=auth(admin))
    assert r.status_code == 200, r.text


def test_only_admins_report_a_level(client: TestClient, auth, make_user, make_admin) -> None:
    owner = make_admin(AdminLevel.OWNER)
    participant = make_user(Role.PARTICIPANT)

    r = client.get(f"/api/users/{participant.id}", headers=auth(owner))
    assert r.json()["admin_level"] is None
    r = client.get(f"/api/users/{owner.id}", headers=auth(owner))
    assert r.json()["admin_level"] == "owner"


# --------------------------------------------------------------------------- #
# Creating and promoting admins
# --------------------------------------------------------------------------- #


def test_a_new_admin_defaults_to_manager_not_owner(
    client: TestClient, auth, make_admin
) -> None:
    owner = make_admin(AdminLevel.OWNER)
    r = client.post(
        "/api/users",
        json={"email": "fresh.admin@example.com", "display_name": "Fresh", "role": "admin"},
        headers=auth(owner),
    )
    assert r.status_code == 201, r.text
    assert r.json()["admin_level"] == "manager"

    r = client.post(
        "/api/users",
        json={
            "email": "second.owner@example.com",
            "display_name": "Second",
            "role": "admin",
            "admin_level": "owner",
        },
        headers=auth(owner),
    )
    assert r.json()["admin_level"] == "owner"


def test_a_level_on_a_non_admin_role_is_refused_not_ignored(
    client: TestClient, auth, make_admin
) -> None:
    owner = make_admin(AdminLevel.OWNER)
    r = client.post(
        "/api/users",
        json={
            "email": "j@example.com",
            "display_name": "J",
            "role": "judge",
            "admin_level": "owner",
        },
        headers=auth(owner),
    )
    assert r.status_code == 422


def test_promoting_to_admin_defaults_to_manager_and_a_level_can_be_changed(
    client: TestClient, auth, make_user, make_admin
) -> None:
    owner = make_admin(AdminLevel.OWNER)
    target = make_user(Role.ORGANIZER)

    r = client.patch(f"/api/users/{target.id}/role", json={"role": "admin"}, headers=auth(owner))
    assert r.json()["admin_level"] == "manager"

    r = client.patch(
        f"/api/users/{target.id}/role",
        json={"role": "admin", "admin_level": "auditor"},
        headers=auth(owner),
    )
    assert r.status_code == 200 and r.json()["admin_level"] == "auditor"

    # Omitting the level on a later change keeps it.
    r = client.patch(f"/api/users/{target.id}/role", json={"role": "admin"}, headers=auth(owner))
    assert r.json()["admin_level"] == "auditor"


def test_a_level_change_is_written_to_the_audit_log(
    client: TestClient, auth, make_user, make_admin, db
) -> None:
    owner = make_admin(AdminLevel.OWNER)
    target = make_admin(AdminLevel.MANAGER)
    client.patch(
        f"/api/users/{target.id}/role",
        json={"role": "admin", "admin_level": "auditor"},
        headers=auth(owner),
    )
    entry = (
        db.query(AuditEntry)
        .filter(AuditEntry.action == AuditAction.ROLE_CHANGED)
        .order_by(AuditEntry.seq.desc())
        .first()
    )
    assert entry is not None
    assert "admin (manager) to admin (auditor)" in entry.summary


# --------------------------------------------------------------------------- #
# Manager: an admin minus account administration
# --------------------------------------------------------------------------- #


def test_a_manager_runs_events_and_reads_the_global_audit(
    client: TestClient, auth, make_admin
) -> None:
    manager = make_admin(AdminLevel.MANAGER)
    r = client.post("/api/events", json=event_payload(), headers=auth(manager))
    assert r.status_code == 201, r.text
    assert client.get("/api/audit", headers=auth(manager)).status_code == 200
    assert client.get("/api/audit/verify", headers=auth(manager)).status_code == 200
    assert client.get("/api/users", headers=auth(manager)).status_code == 200


def test_a_manager_cannot_administer_accounts(
    client: TestClient, auth, make_user, make_admin
) -> None:
    manager = make_admin(AdminLevel.MANAGER)
    target = make_user(Role.PARTICIPANT)

    r = client.post(
        "/api/users",
        json={"email": "x@example.com", "display_name": "X", "role": "judge"},
        headers=auth(manager),
    )
    assert r.status_code == 403
    r = client.patch(f"/api/users/{target.id}/role", json={"role": "organizer"}, headers=auth(manager))
    assert r.status_code == 403
    r = client.post(f"/api/users/{target.id}/deactivate", headers=auth(manager))
    assert r.status_code == 403

    # In particular a manager cannot mint an owner -- or promote itself to one.
    r = client.patch(
        f"/api/users/{manager.id}/role",
        json={"role": "admin", "admin_level": "owner"},
        headers=auth(manager),
    )
    assert r.status_code == 403


# --------------------------------------------------------------------------- #
# Auditor: read-only, everywhere
# --------------------------------------------------------------------------- #


def test_an_auditor_can_read_what_an_admin_reads(
    client: TestClient, auth, make_admin
) -> None:
    auditor = make_admin(AdminLevel.AUDITOR)
    for path in ("/api/users", "/api/audit", "/api/audit/verify", "/api/events"):
        r = client.get(path, headers=auth(auditor))
        assert r.status_code == 200, (path, r.text)


def test_an_auditor_cannot_change_anything(
    client: TestClient, auth, make_user, make_admin
) -> None:
    owner = make_admin(AdminLevel.OWNER)
    auditor = make_admin(AdminLevel.AUDITOR)
    event = client.post("/api/events", json=event_payload(), headers=auth(owner)).json()
    target = make_user(Role.PARTICIPANT)

    attempts = [
        ("post", "/api/events", event_payload()),
        ("delete", f"/api/events/{event['slug']}", None),
        ("patch", f"/api/users/{target.id}/role", {"role": "organizer"}),
        ("post", f"/api/users/{target.id}/deactivate", None),
        ("post", "/api/users", {"email": "a@example.com", "display_name": "A", "role": "judge"}),
        ("put", f"/api/events/{event['slug']}/votes", {"votes": []}),
    ]
    for method, path, body in attempts:
        kwargs = {"json": body} if body is not None else {}
        r = getattr(client, method)(path, headers=auth(auditor), **kwargs)
        assert r.status_code == 403, (method, path, r.status_code, r.text)
        assert "read-only" in r.text


def test_an_auditor_may_still_manage_its_own_session(
    client: TestClient, auth, make_admin
) -> None:
    auditor = make_admin(AdminLevel.AUDITOR)
    r = client.post("/api/auth/logout", headers=auth(auditor))
    assert r.status_code == 204, r.text


def test_the_auditor_gate_holds_in_check_access_too(make_admin) -> None:
    """Independent of the HTTP layer: every verb outside the read list is
    refused for an auditor, and every read verb an admin has is kept."""
    auditor = make_admin(AdminLevel.AUDITOR)
    owner = make_admin(AdminLevel.OWNER)
    event = object()  # any resource type: the gate fires before dispatch

    for action in Action:
        if action in (
            Action.READ,
            Action.LIST_USERS,
            Action.READ_PROGRESS,
            Action.READ_RESULTS,
            Action.EXPORT,
            Action.READ_TALLY,
            Action.READ_PUBLIC_RESULTS,
            Action.READ_JUDGE_REPORT,
            Action.READ_AUDIT,
            Action.READ_GLOBAL_AUDIT,
        ):
            continue
        assert check_access(auditor, event, action) is False, action
        assert check_access(auditor, PLATFORM, action) is False, action

    assert check_access(auditor, PLATFORM, Action.READ_GLOBAL_AUDIT) is True
    assert check_access(auditor, PLATFORM, Action.LIST_USERS) is True
    assert check_access(owner, PLATFORM, Action.CREATE_USER) is True
    assert check_access(auditor, PLATFORM, Action.CREATE_USER) is False


# --------------------------------------------------------------------------- #
# Lockout protection
# --------------------------------------------------------------------------- #


def test_the_last_owner_cannot_demote_themselves(
    client: TestClient, auth, make_admin
) -> None:
    only_owner = make_admin(AdminLevel.OWNER)
    for body in ({"role": "admin", "admin_level": "manager"}, {"role": "organizer"}):
        r = client.patch(f"/api/users/{only_owner.id}/role", json=body, headers=auth(only_owner))
        assert r.status_code == 409, (body, r.text)
        assert "last owner" in r.text


def test_an_owner_can_step_down_once_another_owner_exists(
    client: TestClient, auth, make_admin
) -> None:
    first = make_admin(AdminLevel.OWNER)
    make_admin(AdminLevel.OWNER)
    r = client.patch(
        f"/api/users/{first.id}/role",
        json={"role": "admin", "admin_level": "manager"},
        headers=auth(first),
    )
    assert r.status_code == 200, r.text
    assert r.json()["admin_level"] == "manager"


def test_a_deactivated_owner_does_not_count_as_another_owner(
    client: TestClient, auth, make_admin, db
) -> None:
    active = make_admin(AdminLevel.OWNER)
    gone = make_admin(AdminLevel.OWNER)
    gone.is_active = False
    db.commit()
    r = client.patch(
        f"/api/users/{active.id}/role",
        json={"role": "admin", "admin_level": "manager"},
        headers=auth(active),
    )
    assert r.status_code == 409, r.text


def test_an_in_memory_admin_gets_the_documented_default_and_none_fails_closed() -> None:
    """A `User` means the same thing before and after it is stored, and a level
    that is genuinely absent is never read as a privilege."""
    assert User(email="a@example.com", display_name="A", password_hash="x", role=Role.ADMIN).admin_level is AdminLevel.OWNER

    levelless = User(email="b@example.com", display_name="B", password_hash="x", role=Role.ADMIN)
    levelless.admin_level = None
    assert check_access(levelless, PLATFORM, Action.CREATE_USER) is False
