"""Reading the audit trail.

The brief's requirement is about the reader: _"an audit trail an organizer can read
without a database client."_ So this endpoint serves whole sentences, newest first,
filterable by action and actor, and there is a CSV of the same thing in
`exports.py`.

There is **no write route here, and no update or delete route anywhere.** The log
is appended to by `app.audit.record()` inside the transaction of the thing it
records. A log an organizer can edit is not a log, and one they can write by hand
is worse than none.

Two endpoints, two audiences:

* `GET /api/events/{slug}/audit` -- any organizer of *that* event, scoped to it.
* `GET /api/audit` -- admin only, across every event. It exists because
  `AuditEntry.event_id` is `ON DELETE SET NULL`: deleting an event orphans its
  history rather than destroying it (found and fixed while wiring `EVENT_DELETED`
  in Phase 5 -- the obvious alternative, `CASCADE`, would have made the
  deletion's own audit entry vanish along with everything else). An orphaned
  entry has no event left to be scoped under, so the per-event endpoint can no
  longer reach it by construction; this is the only place it is still readable.
  It is admin-only rather than organizer-accessible because it is, by
  definition, cross-event -- an organizer for event A has no business reading
  event B's trail.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..access import PLATFORM, Action, Principal, require_access
from ..audit import verify_chain
from ..db import get_db
from ..deps import get_current_principal
from ..models import AuditAction, AuditEntry
from ..schemas import AuditPage, AuditRow, ChainBreak, ChainVerification
from .judges import _staff_event

router = APIRouter(prefix="/api/events", tags=["audit"])
global_router = APIRouter(prefix="/api", tags=["audit"])

MAX_PER_PAGE = 200


def _row(entry: AuditEntry) -> AuditRow:
    return AuditRow(
        id=entry.id,
        created_at=entry.created_at,
        event_slug=entry.event_slug,
        action=entry.action,
        actor_label=entry.actor_label,
        actor_role=entry.actor_role,
        resource_type=entry.resource_type,
        resource_id=entry.resource_id,
        summary=entry.summary,
        ip_address=entry.ip_address,
    )


def _page(
    db: Session,
    stmt,
    *,
    action: AuditAction | None,
    actor: str | None,
    page: int,
    per_page: int,
    event_slug_field: str | None,
) -> AuditPage:
    if action is not None:
        stmt = stmt.where(AuditEntry.action == action)
    if actor:
        stmt = stmt.where(AuditEntry.actor_label.ilike(f"%{actor.strip()}%"))

    total = db.execute(select(func.count()).select_from(stmt.subquery())).scalar_one()
    rows = (
        db.execute(
            # `seq` alone, not `created_at` -- ties on a timestamp used to break on
            # `id`, a random UUID that carries no order at all (the model's own
            # docstring says exactly this about why `seq` exists). `seq` is written
            # under the same advisory lock that stamps `created_at`, so ordering by
            # it alone already agrees with `created_at` order and needs no second
            # column.
            stmt.order_by(AuditEntry.seq.desc())
            .offset((page - 1) * per_page)
            .limit(per_page)
        )
        .scalars()
        .all()
    )
    return AuditPage(
        event_slug=event_slug_field,
        total=total,
        page=page,
        per_page=per_page,
        pages=max(1, -(-total // per_page)),
        rows=[_row(r) for r in rows],
    )


@router.get("/{slug}/audit", response_model=AuditPage)
def read_audit(
    slug: str,
    action: AuditAction | None = Query(default=None, description="Filter to one verb"),
    actor: str | None = Query(default=None, description="Substring match on the actor"),
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=50, ge=1, le=MAX_PER_PAGE),
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db),
) -> AuditPage:
    """Organizer-only, newest first.

    Newest first because the question an organizer brings to this page is almost
    always "what just happened", not "what happened first".
    """
    event = _staff_event(db, slug, principal, Action.READ_AUDIT)
    stmt = select(AuditEntry).where(AuditEntry.event_id == event.id)
    return _page(
        db, stmt, action=action, actor=actor, page=page, per_page=per_page,
        event_slug_field=event.slug,
    )


@router.get("/{slug}/audit/actions", response_model=list[str])
def audit_actions(
    slug: str,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db),
) -> list[str]:
    """The verbs present in this event's log, for building a filter control.

    The enum's full list would include verbs this event has never produced, which
    makes a filter that returns nothing look broken.
    """
    event = _staff_event(db, slug, principal, Action.READ_AUDIT)
    rows = db.execute(
        select(AuditEntry.action)
        .where(AuditEntry.event_id == event.id)
        .distinct()
        .order_by(AuditEntry.action)
    ).scalars()
    return [r.value for r in rows]


@global_router.get("/audit", response_model=AuditPage)
def read_global_audit(
    event: str | None = Query(default=None, description="Filter to one event slug"),
    orphaned: bool = Query(
        default=False, description="Only entries whose event has been deleted"
    ),
    action: AuditAction | None = Query(default=None, description="Filter to one verb"),
    actor: str | None = Query(default=None, description="Substring match on the actor"),
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=50, ge=1, le=MAX_PER_PAGE),
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db),
) -> AuditPage:
    """The cross-event trail. Admin only -- see the module docstring for why.

    `orphaned=true` is the specific reason this endpoint exists: it is the only
    way to read the history of an event that has since been deleted, including
    the entry recording the deletion itself.
    """
    require_access(principal, PLATFORM, Action.READ_GLOBAL_AUDIT)
    stmt = select(AuditEntry)
    if orphaned:
        stmt = stmt.where(AuditEntry.event_id.is_(None))
    elif event:
        stmt = stmt.where(AuditEntry.event_slug == event)
    return _page(
        db, stmt, action=action, actor=actor, page=page, per_page=per_page,
        event_slug_field=event if event else None,
    )


@global_router.get("/audit/verify", response_model=ChainVerification)
def verify_audit_chain(
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db),
) -> ChainVerification:
    """Recompute every entry's hash and confirm it against the last. Admin only.

    Chain integrity is a whole-table property -- one entry's hash covers the
    entry before it, across every event -- so "verify just my event" cannot
    mean anything on its own; a tampered row in someone else's event would
    still break the link an organizer's own entries chain through. This is why
    verification lives here rather than on the per-event endpoint.
    """
    require_access(principal, PLATFORM, Action.READ_GLOBAL_AUDIT)
    valid, checked, broken = verify_chain(db)
    return ChainVerification(
        valid=valid,
        checked=checked,
        first_break=(
            ChainBreak(seq=broken.seq, id=broken.id, reason=broken.reason)
            if broken is not None
            else None
        ),
    )
