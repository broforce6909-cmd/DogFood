"""Comments on gallery projects.

Two decisions shape this module, both anti-abuse:

**Authenticated only.** An anonymous comment box on a public gallery is a spam
endpoint, and the brief asks for anti-abuse that means something. Posting needs an
account; reading does not.

**Hidden, never deleted.** Moderation sets `is_hidden` and records who did it and
why. A deleted row cannot be reviewed, reversed, or audited, and "the organizer
removed a comment" is exactly the kind of action somebody will later dispute.
Hidden text is withheld from everybody except staff -- including its own author,
so a moderated comment cannot simply be read back and re-posted verbatim.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from .. import ratelimit
from ..access import (
    PUBLIC_SUBMISSION_CRITERIA,
    Action,
    Principal,
    check_access,
    require_access,
    require_authenticated,
)
from ..audit import quote, record
from ..db import get_db
from ..deps import get_current_principal
from ..hooks import schedule
from ..models import AuditAction, Comment, Event, Submission, WebhookEvent
from ..pagination import Page, PageParams, count_of, pages_for
from ..schemas import CommentIn, CommentOut, HideIn

router = APIRouter(prefix="/api", tags=["comments"])

_LOADERS = (
    selectinload(Comment.author),
    selectinload(Comment.submission).selectinload(Submission.event),
)


def _public_submission(db: Session, submission_id: uuid.UUID) -> Submission:
    """A project anybody may comment on: submitted, in a published event.

    Reuses `PUBLIC_SUBMISSION_CRITERIA` -- the same tuple the gallery filters by and
    the one `check_access` is tested to agree with. A second definition of "public"
    here is how the two would drift apart.
    """
    row = db.execute(
        select(Submission)
        .options(selectinload(Submission.event), selectinload(Submission.team))
        .join(Event, Event.id == Submission.event_id)
        .where(Submission.id == submission_id, *PUBLIC_SUBMISSION_CRITERIA)
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not in the gallery")
    return row


def _out(comment: Comment, user: Principal) -> CommentOut:
    return CommentOut(
        id=comment.id,
        submission_id=comment.submission_id,
        author=comment.author,
        body=comment.body,
        created_at=comment.created_at,
        is_hidden=comment.is_hidden,
        hidden_reason=comment.hidden_reason,
        can_remove=check_access(user, comment, Action.DELETE),
    )


@router.get("/gallery/{submission_id}/comments", response_model=Page[CommentOut])
def list_comments(
    submission_id: uuid.UUID,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db),
    pagination: PageParams = Depends(),
) -> Page[CommentOut]:
    """Public, paged. Hidden comments are filtered by the predicate, not by the
    query.

    The `WHERE` could exclude them and would be faster, but then the rule would
    live in two places. This loads and filters through `check_access`, which is
    the same shape as every other collection in this codebase -- and the reason
    `total`/`pages` count every comment on the thread, hidden ones included, for
    a non-staff caller: exact per the SQL page, approximate per what that caller
    actually sees rendered, the same honestly-documented tradeoff as the other
    paged, post-filtered collections (`/submissions`, `/teams`).
    """
    submission = _public_submission(db, submission_id)
    stmt = select(Comment).where(Comment.submission_id == submission.id)
    total = count_of(db, stmt)
    rows = (
        db.execute(
            stmt.options(*_LOADERS)
            .order_by(Comment.created_at)
            .offset(pagination.offset)
            .limit(pagination.per_page)
        )
        .scalars()
        .all()
    )
    return Page(
        items=[_out(c, principal) for c in rows if check_access(principal, c, Action.READ)],
        total=total,
        page=pagination.page,
        per_page=pagination.per_page,
        pages=pages_for(total, pagination.per_page),
    )


@router.post("/gallery/{submission_id}/comments", response_model=CommentOut, status_code=201)
def post_comment(
    submission_id: uuid.UUID,
    payload: CommentIn,
    request: Request,
    background: BackgroundTasks,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db),
) -> CommentOut:
    """Post a comment. Authenticated, rate-limited, audited."""
    user = require_authenticated(principal)
    submission = _public_submission(db, submission_id)
    event = submission.event

    if not check_access(user, event, Action.COMMENT):
        if not event.comments_enabled:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Comments are switched off for this event",
            )
        require_access(user, event, Action.COMMENT)

    # Identity and address, separately -- see the same reasoning in voting.py.
    # The previous version folded the two into one combined key
    # (`f"{user.id}:{ip}"`), which is weaker than either key alone: a scripted
    # account rotating its address got a fresh counter on every request, because
    # the *combination* had never been seen before.
    ratelimit.enforce_dual(
        db,
        ratelimit.POST_COMMENT,
        f"user:{user.id}",
        ratelimit.client_key(request),
        request=request,
        event=event,
    )

    comment = Comment(
        event_id=event.id,
        submission_id=submission.id,
        author_id=user.id,
        body=payload.body.strip(),
    )
    db.add(comment)
    record(
        db,
        action=AuditAction.COMMENT_POSTED,
        summary=f"{user.email} commented on {quote(submission.name)} in {event.slug}",
        principal=user,
        event=event,
        resource_type="submission",
        resource_id=submission.id,
        request=request,
    )
    db.commit()
    db.refresh(comment)

    schedule(
        background,
        db,
        event.id,
        WebhookEvent.COMMENT_POSTED,
        {
            "event": event.slug,
            "project": submission.name,
            "author": user.display_name,
            "comment_id": str(comment.id),
        },
    )
    return _out(comment, user)


def _load(db: Session, comment_id: uuid.UUID) -> Comment:
    row = db.execute(
        select(Comment).options(*_LOADERS).where(Comment.id == comment_id)
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Comment not found")
    return row


@router.post("/comments/{comment_id}/hide", response_model=CommentOut)
def hide_comment(
    comment_id: uuid.UUID,
    payload: HideIn,
    request: Request,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db),
) -> CommentOut:
    """Moderate a comment. Staff only, reason required, audited.

    The reason is mandatory because an organizer removing a comment without one is
    the thing a participant will ask about, and "no reason recorded" is not an
    answer the log should be able to give.
    """
    comment = _load(db, comment_id)
    require_access(principal, comment, Action.MODERATE)
    user = require_authenticated(principal)

    comment.is_hidden = True
    comment.hidden_by_id = user.id
    comment.hidden_reason = payload.reason.strip()
    record(
        db,
        action=AuditAction.COMMENT_HIDDEN,
        summary=(
            f"{user.email} hid a comment by {comment.author.email} on "
            f"{quote(comment.submission.name)}: {payload.reason.strip()}"
        ),
        principal=user,
        event=comment.submission.event,
        resource_type="comment",
        resource_id=comment.id,
        request=request,
    )
    db.commit()
    db.refresh(comment)
    return _out(comment, user)


@router.delete("/comments/{comment_id}", status_code=204)
def remove_comment(
    comment_id: uuid.UUID,
    request: Request,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db),
) -> None:
    """Retract your own comment, or remove anybody's as staff.

    An author retracting their own words is a real delete -- they wrote it, and
    keeping it against their wishes is not moderation. A staff removal goes through
    `/hide` instead, which is reviewable; this route exists for the author.
    """
    comment = _load(db, comment_id)
    require_access(principal, comment, Action.DELETE)
    is_author = principal.id is not None and principal.id == comment.author_id

    record(
        db,
        action=AuditAction.COMMENT_HIDDEN,
        summary=(
            f"{comment.author.email} retracted their own comment on "
            f"{quote(comment.submission.name)}"
            if is_author
            else (
                f"{getattr(principal, 'email', 'staff')} deleted a comment by "
                f"{comment.author.email} on {quote(comment.submission.name)}"
            )
        ),
        principal=principal,
        event=comment.submission.event,
        resource_type="comment",
        resource_id=comment.id,
        request=request,
    )
    db.delete(comment)
    db.commit()
