"""Webhook registration and delivery history.

Organizer-only, all of it. A hook is an outbound request made with *our* network
position, so the SSRF guard in `app.hooks` runs here at registration and again at
delivery — see that module for why twice.

The secret is returned **once**, on creation. A receiver needs it to verify
signatures; showing it again on every list would be a second exposure for no gain.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..access import Action, Principal
from ..audit import record
from ..db import get_db
from ..deps import get_current_principal
from ..hooks import check_target, deliver
from ..models import AuditAction, Webhook, WebhookDelivery, WebhookEvent
from ..pagination import Page, PageParams, count_of, pages_for
from ..schemas import (
    DeliveryOut,
    WebhookCreate,
    WebhookCreated,
    WebhookOut,
    WebhookUpdate,
)
from .judges import _staff_event

router = APIRouter(prefix="/api/events", tags=["webhooks"])


def _out(hook: Webhook) -> WebhookOut:
    return WebhookOut(
        id=hook.id,
        event_id=hook.event_id,
        url=hook.url,
        topics=list(hook.topics or []),
        is_active=hook.is_active,
        description=hook.description,
        created_at=hook.created_at,
        failure_count=hook.failure_count,
        last_delivery_at=hook.last_delivery_at,
    )


def _validate_topics(topics: list[str]) -> None:
    """A typo'd topic is a subscription that silently never fires, so it is a 422."""
    known = {t.value for t in WebhookEvent}
    unknown = [t for t in topics if t not in known]
    if unknown:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Unknown topic(s): {', '.join(unknown)}. "
                f"Available: {', '.join(sorted(known))}"
            ),
        )


def _load(db: Session, event_id: uuid.UUID, hook_id: uuid.UUID) -> Webhook:
    hook = db.execute(
        select(Webhook).where(Webhook.id == hook_id, Webhook.event_id == event_id)
    ).scalar_one_or_none()
    if hook is None:
        raise HTTPException(status_code=404, detail="Webhook not found on this event")
    return hook


@router.get("/{slug}/webhooks", response_model=list[WebhookOut])
def list_webhooks(
    slug: str,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db),
) -> list[WebhookOut]:
    event = _staff_event(db, slug, principal, Action.MANAGE)
    rows = (
        db.execute(
            select(Webhook).where(Webhook.event_id == event.id).order_by(Webhook.created_at)
        )
        .scalars()
        .all()
    )
    return [_out(h) for h in rows]


@router.post("/{slug}/webhooks", response_model=WebhookCreated, status_code=201)
def create_webhook(
    slug: str,
    payload: WebhookCreate,
    request: Request,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db),
) -> WebhookCreated:
    """Register a hook.

    The URL is checked before the row is written, so an organizer who pastes an
    internal address gets told immediately rather than discovering months later that
    nothing was ever delivered.
    """
    event = _staff_event(db, slug, principal, Action.MANAGE)
    _validate_topics(payload.topics)

    rejected = check_target(str(payload.url))
    if rejected is not None:
        raise HTTPException(status_code=422, detail=rejected.reason)

    hook = Webhook(
        event_id=event.id,
        url=str(payload.url),
        topics=payload.topics,
        description=payload.description,
        created_by_id=principal.id,
    )
    db.add(hook)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This event already has a webhook for that URL",
        ) from None

    record(
        db,
        action=AuditAction.WEBHOOK_REGISTERED,
        summary=f"{principal.email} registered a webhook for {hook.url} on {event.slug}",
        principal=principal,
        event=event,
        resource_type="webhook",
        resource_id=hook.id,
        request=request,
    )
    db.commit()
    db.refresh(hook)

    out = _out(hook)
    # The one and only time the secret is returned.
    return WebhookCreated(**out.model_dump(), secret=hook.secret)


@router.patch("/{slug}/webhooks/{hook_id}", response_model=WebhookOut)
def update_webhook(
    slug: str,
    hook_id: uuid.UUID,
    payload: WebhookUpdate,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db),
) -> WebhookOut:
    """Re-point, re-subscribe, or re-enable after a fix.

    Re-enabling resets `failure_count`: the counter exists to stop retrying a dead
    endpoint, and an organizer saying "I fixed it" is exactly the signal to try again.
    """
    event = _staff_event(db, slug, principal, Action.MANAGE)
    hook = _load(db, event.id, hook_id)

    if payload.url is not None:
        rejected = check_target(str(payload.url))
        if rejected is not None:
            raise HTTPException(status_code=422, detail=rejected.reason)
        hook.url = str(payload.url)
    if payload.topics is not None:
        _validate_topics(payload.topics)
        hook.topics = payload.topics
    if payload.description is not None:
        hook.description = payload.description
    if payload.is_active is not None:
        hook.is_active = payload.is_active
        if payload.is_active:
            hook.failure_count = 0

    db.commit()
    db.refresh(hook)
    return _out(hook)


@router.delete("/{slug}/webhooks/{hook_id}", status_code=204)
def delete_webhook(
    slug: str,
    hook_id: uuid.UUID,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db),
) -> None:
    event = _staff_event(db, slug, principal, Action.MANAGE)
    hook = _load(db, event.id, hook_id)
    record(
        db,
        action=AuditAction.WEBHOOK_REMOVED,
        summary=f"{principal.email} removed the webhook for {hook.url} on {event.slug}",
        principal=principal,
        event=event,
        resource_type="webhook",
        resource_id=hook.id,
    )
    db.delete(hook)
    db.commit()


@router.post("/{slug}/webhooks/{hook_id}/test", response_model=DeliveryOut)
def test_webhook(
    slug: str,
    hook_id: uuid.UUID,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db),
) -> DeliveryOut:
    """Send a `ping` and report exactly what happened.

    An organizer should be able to check their endpoint without waiting for a real
    submission, and see the status code rather than guessing.
    """
    event = _staff_event(db, slug, principal, Action.MANAGE)
    hook = _load(db, event.id, hook_id)
    delivery = deliver(db, hook, "ping", {"message": "This is a test delivery."})
    return _delivery_out(delivery)


@router.get("/{slug}/webhooks/{hook_id}/deliveries", response_model=Page[DeliveryOut])
def list_deliveries(
    slug: str,
    hook_id: uuid.UUID,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db),
    pagination: PageParams = Depends(),
) -> Page[DeliveryOut]:
    """Delivery history, newest first, paged.

    Stored rather than logged, because "did my integration receive that submission"
    is a question an organizer asks mid-incident and a log line cannot answer -- and an
    event running for weeks with retries (see `deliver()`) can accumulate enough
    of these that "the last 50" (the previous, unpaged version of this route)
    would hide anything older. No frontend calls this yet, so widening the shape
    to `Page[DeliveryOut]` here is a clean break rather than a migration.
    """
    event = _staff_event(db, slug, principal, Action.MANAGE)
    _load(db, event.id, hook_id)
    stmt = select(WebhookDelivery).where(WebhookDelivery.webhook_id == hook_id)
    total = count_of(db, stmt)
    rows = (
        db.execute(
            stmt.order_by(WebhookDelivery.created_at.desc())
            .offset(pagination.offset)
            .limit(pagination.per_page)
        )
        .scalars()
        .all()
    )
    return Page(
        items=[_delivery_out(d) for d in rows],
        total=total,
        page=pagination.page,
        per_page=pagination.per_page,
        pages=pages_for(total, pagination.per_page),
    )


def _delivery_out(delivery: WebhookDelivery) -> DeliveryOut:
    return DeliveryOut(
        id=delivery.id,
        topic=delivery.topic,
        status=delivery.status,
        attempts=delivery.attempts,
        response_code=delivery.response_code,
        error=delivery.error,
        created_at=delivery.created_at,
        delivered_at=delivery.delivered_at,
    )


@router.get("/{slug}/webhooks/topics", response_model=list[str])
def topics() -> list[str]:
    """Every topic that can be subscribed to. Public: it is documentation."""
    return [t.value for t in WebhookEvent]
