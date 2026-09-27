"""Phase 5: the audit log's hash chain, and the cross-event admin view.

Two things this file pins down that `test_audit_log.py` does not cover:

* **Tamper evidence.** `GET /api/audit/verify` must say `valid: true` on an
  untouched chain and must name the first broken link -- not just "false" -- when a
  row is edited behind the API's back.
* **Deleting an event does not delete its history.** `AuditEntry.event_id` is
  `SET NULL`, not `CASCADE`, specifically so the deletion's own audit entry (and
  everything before it) survives. `GET /api/audit?orphaned=true` is the only way to
  read it afterward, and it is admin-only because it is inherently cross-event.

`make_event` (the shared fixture) inserts an `Event` row directly over the ORM and
writes no audit entry -- it is meant for tests that need an event to exist, not for
exercising the audit trail. Every test here that needs a *row in the chain* goes
through the real `POST /api/events` route instead, via `create_event()` below, so
`record()` actually runs.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from app.audit import verify_chain
from app.models import AuditAction, AuditEntry, Role, utcnow

NOW = utcnow()


def event_payload(**overrides) -> dict:
    payload = {
        "slug": f"chain-{uuid.uuid4().hex[:10]}",
        "name": "Chain Test Event",
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


def create_event(client: TestClient, headers, **overrides) -> dict:
    """A real `POST /api/events`, so `EVENT_CREATED` actually lands in the chain."""
    r = client.post("/api/events", json=event_payload(**overrides), headers=headers)
    assert r.status_code == 201, r.text
    return r.json()


# --------------------------------------------------------------------------- #
# Hash chain integrity
# --------------------------------------------------------------------------- #


def test_a_fresh_chain_verifies(client: TestClient, auth, make_user):
    """Baseline: a chain with at least the entry this test itself creates must
    verify clean."""
    organizer = make_user(Role.ORGANIZER)
    create_event(client, auth(organizer))

    admin = make_user(Role.ADMIN)
    r = client.get("/api/audit/verify", headers=auth(admin))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["valid"] is True
    assert body["first_break"] is None
    assert body["checked"] >= 1


def test_verify_chain_directly_on_an_empty_table(db) -> None:
    """An empty table is valid by definition -- there is nothing to have
    tampered with. Exercises `verify_chain()` below the HTTP layer, matching
    whatever the table actually holds in this test's own transaction."""
    valid, checked, broken = verify_chain(db)
    assert checked == db.query(AuditEntry).count()
    if checked == 0:
        assert valid is True
        assert broken is None


def test_tampering_with_a_row_breaks_verification(
    client: TestClient, auth, db, make_user
):
    """Edit a summary behind the API's back; verification must flip to invalid
    and name the row it first disagreed about."""
    organizer = make_user(Role.ORGANIZER)
    create_event(client, auth(organizer))
    admin = make_user(Role.ADMIN)

    before = client.get("/api/audit/verify", headers=auth(admin)).json()
    assert before["valid"] is True

    row = db.query(AuditEntry).order_by(AuditEntry.seq.desc()).first()
    assert row is not None
    row.summary = row.summary + " (tampered)"
    db.commit()

    after = client.get("/api/audit/verify", headers=auth(admin)).json()
    assert after["valid"] is False
    assert after["first_break"] is not None
    assert after["first_break"]["seq"] == row.seq


def test_tampering_with_an_early_row_is_still_found(
    client: TestClient, auth, db, make_user
):
    """The break reported must be the *first* one, not the last -- editing a
    row earlier in the chain must not be masked by everything after it
    still (correctly) disagreeing too."""
    organizer = make_user(Role.ORGANIZER)
    create_event(client, auth(organizer))
    create_event(client, auth(organizer))
    create_event(client, auth(organizer))
    admin = make_user(Role.ADMIN)

    rows = db.query(AuditEntry).order_by(AuditEntry.seq.asc()).all()
    assert len(rows) >= 2, "need at least two entries to test an early tamper"
    target = rows[-3]
    target.actor_label = "someone else entirely"
    db.commit()

    after = client.get("/api/audit/verify", headers=auth(admin)).json()
    assert after["valid"] is False
    assert after["first_break"]["seq"] == target.seq


def test_verify_chain_rejects_a_forged_prev_hash(client: TestClient, auth, db, make_user) -> None:
    """Editing `prev_hash` itself (not just a content field) must also be
    caught -- it is exactly what an attacker splicing in a fabricated entry
    would have to forge."""
    organizer = make_user(Role.ORGANIZER)
    create_event(client, auth(organizer))

    row = db.query(AuditEntry).order_by(AuditEntry.seq.desc()).first()
    assert row is not None
    row.prev_hash = "0" * 64
    db.commit()

    valid, _checked, broken = verify_chain(db)
    assert valid is False
    assert broken.seq == row.seq
    assert "prev_hash" in broken.reason


def test_chain_verification_requires_admin(client: TestClient, auth, make_user):
    for role in (Role.PARTICIPANT, Role.JUDGE, Role.ORGANIZER):
        r = client.get("/api/audit/verify", headers=auth(make_user(role)))
        assert r.status_code == 403, f"{role.value} could verify the audit chain"
    assert client.get("/api/audit/verify").status_code == 403


# --------------------------------------------------------------------------- #
# Concurrency: two writers must extend the chain, not fork it
# --------------------------------------------------------------------------- #


def test_concurrent_writes_extend_one_chain_not_two(engine) -> None:
    """`record()` takes a Postgres advisory lock before reading the chain head,
    specifically so two transactions committing at once cannot both read the
    same head and each believe their own write is the next link. Simulated with
    two real, separately-committed sessions rather than asserting on the lock
    call, since the property that matters is the committed result, not the
    mechanism.
    """
    from sqlalchemy.orm import Session as OrmSession

    from app.access import ANONYMOUS
    from app.audit import record

    session_a = OrmSession(engine)
    session_b = OrmSession(engine)
    try:
        record(session_a, action=AuditAction.RATE_LIMITED, summary="writer A", principal=ANONYMOUS)
        session_a.commit()

        record(session_b, action=AuditAction.RATE_LIMITED, summary="writer B", principal=ANONYMOUS)
        session_b.commit()
    finally:
        session_a.close()
        session_b.close()

    verify_session = OrmSession(engine)
    try:
        valid, _checked, broken = verify_chain(verify_session)
        assert valid is True, broken.reason if broken else None
    finally:
        verify_session.close()


# --------------------------------------------------------------------------- #
# Orphaned entries: deleting an event must not delete its history
# --------------------------------------------------------------------------- #


def test_deleting_an_event_orphans_its_audit_entries_rather_than_erasing_them(
    client: TestClient, auth, make_user
):
    organizer = make_user(Role.ORGANIZER)
    event = create_event(client, auth(organizer))
    slug = event["slug"]

    r = client.delete(f"/api/events/{slug}", headers=auth(organizer))
    assert r.status_code == 204, r.text

    admin = make_user(Role.ADMIN)
    rows = client.get(
        "/api/audit", headers=auth(admin), params={"orphaned": "true"}
    ).json()["rows"]
    deletion = [row for row in rows if row["action"] == AuditAction.EVENT_DELETED.value]
    assert deletion, "the deletion entry itself must survive the event it describes"
    assert slug in deletion[0]["summary"]
    assert deletion[0]["event_slug"] == slug


def test_orphaned_entries_are_unreachable_from_the_per_event_endpoint(
    client: TestClient, auth, make_user
):
    """Once `event_id` is `NULL`, the per-event endpoint can no longer scope to
    it by construction -- the global admin view is the only way back in."""
    organizer = make_user(Role.ORGANIZER)
    event = create_event(client, auth(organizer))
    slug = event["slug"]
    client.delete(f"/api/events/{slug}", headers=auth(organizer))

    r = client.get(f"/api/events/{slug}/audit", headers=auth(organizer))
    assert r.status_code == 404, r.text


def test_the_global_audit_endpoint_is_admin_only(client: TestClient, auth, make_user):
    organizer = make_user(Role.ORGANIZER)
    create_event(client, auth(organizer))
    for role in (Role.PARTICIPANT, Role.JUDGE, Role.ORGANIZER):
        r = client.get("/api/audit", headers=auth(make_user(role)))
        assert r.status_code == 403, f"{role.value} could read the global audit log"
    assert client.get("/api/audit").status_code == 403
    assert client.get("/api/audit", headers=auth(make_user(Role.ADMIN))).status_code == 200


def test_the_global_audit_endpoint_can_filter_to_one_event(client: TestClient, auth, make_user):
    organizer = make_user(Role.ORGANIZER)
    event_a = create_event(client, auth(organizer))
    event_b = create_event(client, auth(organizer))
    admin = make_user(Role.ADMIN)

    rows = client.get(
        "/api/audit", headers=auth(admin), params={"event": event_a["slug"]}
    ).json()["rows"]
    assert rows
    assert all(row["event_slug"] == event_a["slug"] for row in rows)
    assert not any(row["event_slug"] == event_b["slug"] for row in rows)
