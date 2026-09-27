"""Project submissions: draft, edit, submit -- and the deadline that stops all
three.

The deadline is enforced in exactly one place, `check_access`, and every write
path in this module goes through it. There is no second copy of the comparison
to drift out of sync, and no route that writes without asking.

What the deadline does *not* do is delete anything. A draft that misses the
deadline stays a draft: still readable by its team, never in the gallery, never
judged. Silence is a worse outcome than a locked form.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from ..access import Action, Principal, check_access, require_access, require_authenticated
from ..audit import quote, record
from ..db import get_db
from ..deps import get_current_principal
from ..hooks import schedule
from ..models import (
    AuditAction,
    Event,
    Role,
    Submission,
    SubmissionAnswer,
    SubmissionStatus,
    Team,
    TeamMember,
    WebhookEvent,
    utcnow,
)
from ..pagination import Page, PageParams, count_of, pages_for
from ..schemas import (
    SubmissionCreate,
    SubmissionDisqualifyIn,
    SubmissionOut,
    SubmissionUpdate,
)
from ..serializers import submission_out
from .events import readable_event
from .results import compute_tiers, effective_tiers, gather
from .teams import load_team

router = APIRouter(prefix="/api", tags=["submissions"])

_SUBMISSION_LOADERS = (
    selectinload(Submission.event).selectinload(Event.questions),
    selectinload(Submission.event).selectinload(Event.tracks),
    selectinload(Submission.team).selectinload(Team.members).selectinload(TeamMember.user),
    selectinload(Submission.answers),
)

# String fields a project must have filled in before it can leave draft.
# Deliberately short: an organizer's own custom questions carry the
# event-specific demands. `discord_usernames` is checked separately in
# `_blockers()` below -- it is a list, not a string, so it does not fit this
# tuple's `(value or "").strip()` check.
REQUIRED_TO_SUBMIT = ("name", "tagline", "description", "repo_url")


def load_submission(db: Session, submission_id: uuid.UUID) -> Submission:
    row = db.execute(
        select(Submission).options(*_SUBMISSION_LOADERS).where(Submission.id == submission_id)
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Submission not found")
    return row


def readable_submission(db: Session, submission_id: uuid.UUID, user: Principal) -> Submission:
    """A draft belonging to somebody else's team is a 404, not a 403 -- the
    gallery is the only place submissions are public, and a 403 would confirm
    that a project exists before it has been entered."""
    row = load_submission(db, submission_id)
    require_access(user, row, Action.READ, status_code=status.HTTP_404_NOT_FOUND)
    return row


def _require_writable(user: Principal, submission: Submission, action: Action) -> None:
    """`require_access` is the gate. The branch above it only improves the error
    message for the single most common denial, and can never grant anything."""
    if (
        not check_access(user, submission, action)
        and not user.role.at_least(Role.ORGANIZER)
        and user.id in submission.team.member_ids()
        and submission.event.deadline_passed()
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="The submission deadline has passed",
        )
    require_access(user, submission, action)


def _reject_if_disqualified(submission: Submission) -> None:
    """`submit`/`unsubmit`/`delete` are the team-facing lifecycle -- none of
    them know about disqualification, and none of them should be able to
    reverse or erase one by accident. Only `/disqualify` and `/reinstate`
    (staff-only, reasoned, audited) may change a `DISQUALIFIED` submission's
    status, so every other write path refuses outright rather than silently
    treating it like a draft. Applies to staff too: `_check_submission`
    exempts staff from the deadline on these routes, not from this."""
    if submission.status is SubmissionStatus.DISQUALIFIED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This project has been disqualified. Use the reinstate "
            "endpoint to restore it.",
        )


def _blockers(submission: Submission) -> list[str]:
    """Everything standing between this draft and the gallery, in one list, so
    the form can show all of it at once instead of one error per save."""
    missing: list[str] = []
    for field in REQUIRED_TO_SUBMIT:
        if not (getattr(submission, field) or "").strip():
            missing.append(f"{field} is required")
    if not submission.discord_usernames:
        missing.append("discord_usernames is required")
    if submission.event.tracks and submission.track_id is None:
        missing.append("track is required")

    answered = {a.question_id: (a.value or "").strip() for a in submission.answers}
    for question in submission.event.questions:
        if question.required and not answered.get(question.id):
            missing.append(f"answer required: {question.prompt}")
    return missing


# --------------------------------------------------------------------------- #
# Reading
# --------------------------------------------------------------------------- #


def _tier_for(db: Session, submission: Submission, principal: Principal) -> str | None:
    """Part 3's winner/community-vote label for this one submission -- `None`
    if the event does not use the split, or (for a non-staff caller) results
    are not yet public. Reuses `event.results_public()`, the same switch that
    already gates the community vote tally, rather than inventing a second
    one: once an organizer decides results are public, the tally and the
    tier label become visible together.

    Deliberately confined to this one helper, called only from the single-
    submission read route: `gather()` queries every judge assignment in the
    event, which is a cost a list of submissions must not pay per row.
    """
    event = submission.event
    if event.winner_slots <= 0 and event.community_vote_slots <= 0:
        return None
    if not principal.role.at_least(Role.ORGANIZER) and not event.results_public():
        return None
    computed, _, _ = gather(db, event, only_complete=True)
    # `effective_tiers()`: a team must see its *final* status, including any
    # Part 4 admin override -- never the pre-override computed value alone,
    # which would contradict what the override was for.
    return effective_tiers(db, event, compute_tiers(event, computed)).get(submission.id)


@router.get("/submissions/{submission_id}", response_model=SubmissionOut)
def get_submission(
    submission_id: uuid.UUID,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db),
) -> SubmissionOut:
    submission = readable_submission(db, submission_id, principal)
    out = submission_out(submission, principal)
    out.tier = _tier_for(db, submission, principal)
    return out


@router.get("/events/{slug}/submissions", response_model=Page[SubmissionOut])
def list_event_submissions(
    slug: str,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db),
    pagination: PageParams = Depends(),
) -> Page[SubmissionOut]:
    """Every submission the caller may read, drafts included, paged.

    For staff that is the organizer's working view of the event -- the one
    real caller today (`organizer/[slug]/page.tsx`), and the one that grows
    past a page at hackathon-plus scale: measured at 336KB / ~2s unpaged for
    200 submissions (ARCHITECTURE.md). For a participant it is their own
    team's entry and the published gallery -- the same predicate, filtering a
    different amount, still applied after the page is fetched. `total`/`pages`
    reflect the event's full count before that per-row check, which is exact
    for the staff case this route actually serves and a documented
    simplification for the rare non-staff one.
    """
    event = readable_event(db, slug, principal)
    stmt = select(Submission).where(Submission.event_id == event.id)
    total = count_of(db, stmt)
    rows = (
        db.execute(
            stmt.options(*_SUBMISSION_LOADERS)
            .order_by(Submission.created_at)
            .offset(pagination.offset)
            .limit(pagination.per_page)
        )
        .scalars()
        .all()
    )
    return Page(
        items=[
            submission_out(s, principal) for s in rows if check_access(principal, s, Action.READ)
        ],
        total=total,
        page=pagination.page,
        per_page=pagination.per_page,
        pages=pages_for(total, pagination.per_page),
    )


@router.get("/me/submissions", response_model=list[SubmissionOut])
def my_submissions(
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db),
) -> list[SubmissionOut]:
    """Whatever the signed-in user's teams have entered, across every event."""
    user = require_authenticated(principal)
    rows = (
        db.execute(
            select(Submission)
            .options(*_SUBMISSION_LOADERS)
            .join(Team, Team.id == Submission.team_id)
            .join(TeamMember, TeamMember.team_id == Team.id)
            .where(TeamMember.user_id == user.id)
            .order_by(Submission.updated_at.desc())
        )
        .scalars()
        .all()
    )
    return [submission_out(s, principal) for s in rows]


# --------------------------------------------------------------------------- #
# Writing
# --------------------------------------------------------------------------- #


@router.post("/submissions", response_model=SubmissionOut, status_code=status.HTTP_201_CREATED)
def create_submission(
    payload: SubmissionCreate,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db),
) -> SubmissionOut:
    """Starts a draft. One per team, enforced by a unique index on `team_id`."""
    require_authenticated(principal)
    team = load_team(db, payload.team_id)
    require_access(principal, team, Action.CREATE_SUBMISSION)

    submission = Submission(
        event_id=team.event_id,
        team_id=team.id,
        name=payload.name.strip(),
        status=SubmissionStatus.DRAFT,
    )
    db.add(submission)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This team already has a submission",
        ) from None
    db.commit()
    return submission_out(load_submission(db, submission.id), principal)


@router.patch("/submissions/{submission_id}", response_model=SubmissionOut)
def update_submission(
    submission_id: uuid.UUID,
    payload: SubmissionUpdate,
    request: Request,
    background: BackgroundTasks,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db),
) -> SubmissionOut:
    """Draft-and-edit. Also edits a *submitted* project, up to the deadline --
    that is what "edit until the deadline" means, and pretending otherwise just
    teaches teams to submit at the last second."""
    submission = readable_submission(db, submission_id, principal)
    _require_writable(principal, submission, Action.UPDATE)

    fields = payload.model_dump(exclude_unset=True)
    answers = fields.pop("answers", None)

    if "track_id" in fields and fields["track_id"] is not None:
        valid = {t.id for t in submission.event.tracks}
        if fields["track_id"] not in valid:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="That track does not belong to this event",
            )

    for field, value in fields.items():
        setattr(submission, field, value)

    if answers is not None:
        _apply_answers(db, submission, answers)

    # The documented exception, made visible. An organizer editing an entry after
    # the deadline is legitimate and is also exactly the kind of thing a team will
    # later ask about, so it goes in the log naming who, what and which fields.
    if submission.event.deadline_passed() and principal.role.at_least(Role.ORGANIZER):
        touched = ", ".join(sorted(fields)) or "nothing"
        record(
            db,
            action=AuditAction.SUBMISSION_EDITED_AFTER_DEADLINE,
            summary=(
                f"{principal.email} edited {quote(submission.name)} "
                f"({touched}) after the submission deadline on "
                f"{submission.event.slug}"
            ),
            principal=principal,
            event=submission.event,
            resource_type="submission",
            resource_id=submission.id,
            request=request,
        )

    db.commit()

    # `submission.updated` was a documented, subscribable webhook topic
    # (`GET /api/events/{slug}/webhooks/topics` lists it) that nothing ever
    # scheduled -- found in the same self-audit that closed the equivalent gap
    # in the audit log's own enum. An organizer who subscribed to it got a hook
    # that silently never fired. Only for a *submitted* project: a draft's edits
    # are exactly the noise an integration would want to filter back out, so
    # firing for every keystroke on an unsubmitted draft would make the topic
    # worse than useless.
    if fields and submission.status is SubmissionStatus.SUBMITTED:
        schedule(
            background,
            db,
            submission.event_id,
            WebhookEvent.SUBMISSION_UPDATED,
            {
                "event": submission.event.slug,
                "submission_id": str(submission.id),
                "name": submission.name,
                "fields_changed": sorted(fields),
            },
        )
    return submission_out(load_submission(db, submission.id), principal)


# --------------------------------------------------------------------------- #
# Disqualification (Phase 6, admin)
# --------------------------------------------------------------------------- #


@router.post("/submissions/{submission_id}/disqualify", response_model=SubmissionOut)
def disqualify_submission(
    submission_id: uuid.UUID,
    payload: SubmissionDisqualifyIn,
    request: Request,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db),
) -> SubmissionOut:
    """Remove a project from the gallery, judging assignment, voting,
    certificates and results, for cause, without deleting it. The row, every
    ballot already scored against it, and every vote already cast for it are
    untouched -- only the status changes, the same soft-removal shape as
    `remove_judge`. See `SubmissionStatus`'s own docstring for the full
    reasoning; `/reinstate` below is how this is undone."""
    submission = readable_submission(db, submission_id, principal)
    require_access(principal, submission, Action.MANAGE)

    if submission.status is SubmissionStatus.DISQUALIFIED:
        return submission_out(submission, principal)

    submission.status = SubmissionStatus.DISQUALIFIED
    record(
        db,
        action=AuditAction.SUBMISSION_DISQUALIFIED,
        summary=(
            f"{principal.email} disqualified {quote(submission.name)} "
            f"from {submission.event.slug}: {quote(payload.reason)}"
        ),
        principal=principal,
        event=submission.event,
        resource_type="submission",
        resource_id=submission.id,
        request=request,
    )
    db.commit()
    return submission_out(load_submission(db, submission.id), principal)


@router.post("/submissions/{submission_id}/reinstate", response_model=SubmissionOut)
def reinstate_submission(
    submission_id: uuid.UUID,
    payload: SubmissionDisqualifyIn,
    request: Request,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db),
) -> SubmissionOut:
    """Undo a disqualification. Returns to `SUBMITTED` if the project had a
    `submitted_at` (it was a real entry when disqualified), `DRAFT`
    otherwise -- the same rule the CHECK constraint already enforces, made
    explicit here instead of left to fall out of it."""
    submission = readable_submission(db, submission_id, principal)
    require_access(principal, submission, Action.MANAGE)

    if submission.status is not SubmissionStatus.DISQUALIFIED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This project is not disqualified",
        )

    submission.status = (
        SubmissionStatus.SUBMITTED
        if submission.submitted_at is not None
        else SubmissionStatus.DRAFT
    )
    record(
        db,
        action=AuditAction.SUBMISSION_REINSTATED,
        summary=(
            f"{principal.email} reinstated {quote(submission.name)} "
            f"on {submission.event.slug}: {quote(payload.reason)}"
        ),
        principal=principal,
        event=submission.event,
        resource_type="submission",
        resource_id=submission.id,
        request=request,
    )
    db.commit()
    return submission_out(load_submission(db, submission.id), principal)


def _apply_answers(
    db: Session, submission: Submission, answers: dict[uuid.UUID, str]
) -> None:
    known = {q.id for q in submission.event.questions}
    unknown = set(answers) - known
    if unknown:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="One or more answers refer to a question this event does not ask",
        )
    existing = {a.question_id: a for a in submission.answers}
    for question_id, value in answers.items():
        if question_id in existing:
            existing[question_id].value = value
        else:
            db.add(
                SubmissionAnswer(
                    submission_id=submission.id, question_id=question_id, value=value
                )
            )


@router.post("/submissions/{submission_id}/submit", response_model=SubmissionOut)
def submit(
    submission_id: uuid.UUID,
    background: BackgroundTasks,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db),
) -> SubmissionOut:
    """Draft -> submitted. Idempotent, and refuses on an incomplete project with
    the whole list of what is missing rather than the first item."""
    submission = readable_submission(db, submission_id, principal)
    _require_writable(principal, submission, Action.SUBMIT)
    _reject_if_disqualified(submission)

    if submission.status is SubmissionStatus.SUBMITTED:
        return submission_out(submission, principal)

    missing = _blockers(submission)
    if missing:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"message": "This project is not ready to submit", "missing": missing},
        )

    submission.status = SubmissionStatus.SUBMITTED
    submission.submitted_at = utcnow()
    db.commit()

    # The most useful topic an integration can subscribe to: a project just entered.
    # Scheduled after the commit, so a broken relay can never be the reason an entry
    # was not saved.
    schedule(
        background,
        db,
        submission.event_id,
        WebhookEvent.SUBMISSION_SUBMITTED,
        {
            "event": submission.event.slug,
            "submission_id": str(submission.id),
            "name": submission.name,
            "team": submission.team.name,
            "track": submission.track.key if submission.track else None,
            "repo_url": submission.repo_url,
            "submitted_at": submission.submitted_at.isoformat(),
        },
    )
    return submission_out(load_submission(db, submission.id), principal)


@router.post("/submissions/{submission_id}/unsubmit", response_model=SubmissionOut)
def unsubmit(
    submission_id: uuid.UUID,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db),
) -> SubmissionOut:
    """Back to draft, and out of the gallery. Only until the deadline -- after
    that a submitted project is entered, and un-entering it is an organizer's
    decision to make and to answer for."""
    submission = readable_submission(db, submission_id, principal)
    _require_writable(principal, submission, Action.UNSUBMIT)
    _reject_if_disqualified(submission)

    submission.status = SubmissionStatus.DRAFT
    submission.submitted_at = None
    db.commit()
    return submission_out(load_submission(db, submission.id), principal)


@router.delete("/submissions/{submission_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_submission(
    submission_id: uuid.UUID,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db),
) -> None:
    submission = readable_submission(db, submission_id, principal)
    _require_writable(principal, submission, Action.DELETE)
    _reject_if_disqualified(submission)
    db.delete(submission)
    db.commit()
