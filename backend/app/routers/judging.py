"""The judge's own surface: their queue, their ballots, their scores.

Everything in this module is scoped to the caller by `check_access`, never by a
`WHERE judge_id = me` that happens to be correct. The distinction matters: a
filtered query is a guess that the filter is right, and the acceptance suite
tests the guess by pointing at a peer's ballot id directly.

So the shape of every route here is the same as everywhere else in this codebase:

    load the resource -> require_access(...) -> do the work

The queue is the one collection, and it pairs a SQL filter with an assertion that
the filter and the predicate agree -- the same treatment the public gallery gets.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from ..access import (
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
from ..models import (
    AssignmentStatus,
    AuditAction,
    Event,
    Judge,
    JudgeAssignment,
    JudgeRecusal,
    RubricCriterion,
    Score,
    Submission,
    SubmissionStatus,
    WebhookEvent,
    utcnow,
)
from ..schemas import AssignmentOut, BallotIn, JudgeOut, RecusalOut, SelfRecusalIn
from ..serializers import assignment_out, judge_out

router = APIRouter(prefix="/api/judging", tags=["judging"])

_BALLOT_LOADERS = (
    selectinload(JudgeAssignment.judge).selectinload(Judge.user),
    selectinload(JudgeAssignment.judge).selectinload(Judge.track),
    selectinload(JudgeAssignment.scores),
    selectinload(JudgeAssignment.submission).selectinload(Submission.team),
    selectinload(JudgeAssignment.submission).selectinload(Submission.track),
    selectinload(JudgeAssignment.submission)
    .selectinload(Submission.event)
    .selectinload(Event.criteria),
)


def load_assignment(db: Session, assignment_id: uuid.UUID) -> JudgeAssignment:
    row = db.execute(
        select(JudgeAssignment)
        .options(*_BALLOT_LOADERS)
        .where(JudgeAssignment.id == assignment_id)
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Assignment not found")
    return row


def readable_assignment(
    db: Session, assignment_id: uuid.UUID, user: Principal
) -> JudgeAssignment:
    """403, not 404, for a ballot that is not yours.

    Deliberately different from an unsubmitted draft. That a peer judge holds a
    ballot is not a secret -- the organizer's dashboard shows the whole grid --
    so there is nothing to protect by pretending the row does not exist, and
    ARCHITECTURE.md commits to 403 here because that is what the acceptance suite
    checks for.
    """
    row = load_assignment(db, assignment_id)
    require_access(user, row, Action.READ)
    return row


# --------------------------------------------------------------------------- #
# Who am I, as a judge
# --------------------------------------------------------------------------- #


@router.get("/me", response_model=list[JudgeOut])
def my_judge_records(
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db),
) -> list[JudgeOut]:
    """The events this caller judges, and on which track.

    A judge needs this to know whether they are a track judge; it is also how the
    UI decides whether to show a judging nav item at all.
    """
    user = require_authenticated(principal)
    rows = (
        db.execute(
            select(Judge)
            .options(selectinload(Judge.user), selectinload(Judge.track))
            .where(Judge.user_id == user.id)
            .order_by(Judge.invited_at)
        )
        .scalars()
        .all()
    )
    return [judge_out(j) for j in rows if check_access(user, j, Action.READ)]


# --------------------------------------------------------------------------- #
# The queue
# --------------------------------------------------------------------------- #


@router.get("/queue", response_model=list[AssignmentOut])
def my_queue(
    event: str | None = Query(default=None, description="Restrict to one event slug"),
    outstanding: bool = Query(default=False, description="Hide ballots already completed"),
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db),
) -> list[AssignmentOut]:
    """Every ballot this judge holds, and nothing else.

    The `join` restricts to the caller's own judge rows, and the `check_access`
    filter afterwards is not redundant: it is what makes the SQL a performance
    optimisation rather than the access control. If the two ever disagree, the
    predicate wins and `test_queue_filter_matches_the_predicate` fails.
    """
    user = require_authenticated(principal)
    now = utcnow()

    stmt = (
        select(JudgeAssignment)
        .options(*_BALLOT_LOADERS)
        .join(Judge, Judge.id == JudgeAssignment.judge_id)
        .where(Judge.user_id == user.id, Judge.is_active.is_(True))
    )
    if event:
        stmt = stmt.join(Event, Event.id == JudgeAssignment.event_id).where(Event.slug == event)
    if outstanding:
        stmt = stmt.where(JudgeAssignment.status != AssignmentStatus.COMPLETE)

    rows = db.execute(stmt.order_by(JudgeAssignment.assigned_at)).scalars().all()
    return [
        assignment_out(row, user, now=now)
        for row in rows
        if check_access(user, row, Action.READ, now=now)
    ]


# --------------------------------------------------------------------------- #
# One ballot
# --------------------------------------------------------------------------- #


@router.get("/assignments/{assignment_id}", response_model=AssignmentOut)
def get_ballot(
    assignment_id: uuid.UUID,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db),
) -> AssignmentOut:
    assignment = readable_assignment(db, assignment_id, principal)
    return assignment_out(assignment, principal)


@router.put("/assignments/{assignment_id}/scores", response_model=AssignmentOut)
def put_scores(
    assignment_id: uuid.UUID,
    payload: BallotIn,
    request: Request,
    background: BackgroundTasks,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db),
) -> AssignmentOut:
    """Write this judge's ballot. Idempotent: re-scoring updates in place.

    Note what this route does *not* accept: a judge id. There is no way to
    express "score on behalf of" in the API surface, which is a stronger
    guarantee than checking that you did not.
    """
    assignment = load_assignment(db, assignment_id)
    now = utcnow()

    # Read access first, so a caller who may not see the ballot learns nothing
    # from the shape of the error they get for writing it.
    require_access(principal, assignment, Action.READ)

    if not check_access(principal, assignment, Action.SCORE, now=now):
        # Separate the "you are not allowed, ever" case from the "not right now"
        # case: a judge hitting a closed window deserves to be told that, and a
        # 403 would send them looking for a permissions problem.
        event = assignment.submission.event
        if not event.judging_open(now):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    "Judging is not open for this event"
                    if event.judging_opens_at
                    else "Judging has not been scheduled for this event"
                ),
            )
        if assignment.submission.status is SubmissionStatus.DISQUALIFIED:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="That project has been disqualified",
            )
        if assignment.submission.status is not SubmissionStatus.SUBMITTED:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="That project was never submitted",
            )
        require_access(principal, assignment, Action.SCORE, now=now)

    criteria = {c.id: c for c in assignment.submission.event.criteria}
    if not criteria:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This event has no rubric yet; an organizer must add criteria first",
        )

    _validate_against_rubric(payload, criteria)

    existing = {s.criterion_id: s for s in assignment.scores}
    for incoming in payload.scores:
        row = existing.get(incoming.criterion_id)
        if row is None:
            db.add(
                Score(
                    event_id=assignment.event_id,
                    assignment_id=assignment.id,
                    criterion_id=incoming.criterion_id,
                    value=incoming.value,
                    comment=incoming.comment,
                )
            )
        else:
            row.value = incoming.value
            row.comment = incoming.comment

    if payload.comment is not None:
        assignment.comment = payload.comment


    # A ballot is complete when the judge says so *and* every criterion is
    # answered. Claiming completion with half a rubric filled in would corrupt
    # the progress dashboard and the normalization both.
    was_complete = assignment.status is AssignmentStatus.COMPLETE
    scored_ids = {s.criterion_id for s in payload.scores} | set(existing)
    fully_scored = scored_ids >= set(criteria)
    if payload.complete and fully_scored:
        assignment.status = AssignmentStatus.COMPLETE
        assignment.completed_at = assignment.completed_at or now
    else:
        assignment.status = AssignmentStatus.IN_PROGRESS
        assignment.completed_at = None

    # The audit entry is written in the same transaction as the scores it records,
    # so an organizer reading the log is reading what actually committed.
    scored = ", ".join(
        f"{criteria[s.criterion_id].key} {s.value}"
        for s in payload.scores
        if s.criterion_id in criteria
    )
    record(
        db,
        action=AuditAction.BALLOT_SCORED,
        summary=(
            f"{principal.email} scored {quote(assignment.submission.name)} "
            f"({scored}) on {assignment.submission.event.slug}"
            + ("" if assignment.status is AssignmentStatus.COMPLETE else " [in progress]")
        ),
        principal=principal,
        event=assignment.submission.event,
        resource_type="assignment",
        resource_id=assignment.id,
        request=request,
    )
    try:
        db.commit()
    except IntegrityError:
        # Two requests for the same ballot both read "no row yet" for a
        # criterion and both tried to INSERT -- a double-click or a client
        # retry racing the original request, not a different judge (the
        # ballot is already ownership-checked above). The loser's rollback is
        # not a lost score: the winner's row holds the same value for the same
        # criterion, so telling the caller to resend converts cleanly into the
        # UPDATE branch instead of a second INSERT.
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This ballot was updated by a concurrent request. Please resend your scores.",
        ) from None

    # `ballot.completed` was a documented, subscribable webhook topic that
    # nothing ever scheduled -- same gap as the other topics fixed in this
    # pass. Fired only on the transition into COMPLETE, not on every re-save of
    # an already-complete ballot (a judge revisiting a finished ballot to tweak
    # a comment should not re-notify an integration that already heard about
    # this ballot finishing).
    if assignment.status is AssignmentStatus.COMPLETE and not was_complete:
        schedule(
            background,
            db,
            assignment.event_id,
            WebhookEvent.BALLOT_COMPLETED,
            {
                "event": assignment.submission.event.slug,
                "assignment_id": str(assignment.id),
                "submission_id": str(assignment.submission_id),
                "judge_id": str(assignment.judge_id),
            },
        )
    db.refresh(assignment)
    return assignment_out(assignment, principal, now=now)


# --------------------------------------------------------------------------- #
# Conflict of interest: recusing yourself
# --------------------------------------------------------------------------- #


@router.post(
    "/assignments/{assignment_id}/recuse", response_model=RecusalOut, status_code=201
)
def recuse_self(
    assignment_id: uuid.UUID,
    payload: SelfRecusalIn,
    request: Request,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db),
) -> RecusalOut:
    """A judge declaring their own conflict of interest on a ballot they hold.

    The structural conflict -- reviewing your own team's project -- cannot
    reach this route at all, because it is never assigned in the first place
    (see `app/assignment.py`). This is for the conflict only the judge
    themself would know about: someone they know personally, a financial
    stake, anything that makes them the wrong reviewer for this one project.
    Refused once the ballot is `COMPLETE` -- that is a scored, historical
    record, and un-scoring it is an organizer's call
    (`DELETE /api/events/{slug}/assignments/{id}`), not a unilateral one from
    the judge who just realised the conflict.
    """
    assignment = load_assignment(db, assignment_id)
    require_access(principal, assignment, Action.READ)
    user = require_authenticated(principal)
    if assignment.judge.user_id != user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the assigned judge may recuse themselves from their own ballot",
        )
    if assignment.status is AssignmentStatus.COMPLETE:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "This ballot is already complete. Ask an organizer to remove the "
                "assignment if you have a conflict of interest you did not catch "
                "in time."
            ),
        )

    judge = assignment.judge
    submission = assignment.submission
    recusal = JudgeRecusal(
        event_id=assignment.event_id,
        judge_id=judge.id,
        submission_id=submission.id,
        reason=payload.reason,
    )
    db.add(recusal)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="You are already recused from this project",
        ) from None

    db.delete(assignment)
    record(
        db,
        action=AuditAction.JUDGE_RECUSED,
        summary=(
            f"{principal.email} recused themselves from {quote(submission.name)} "
            f"on {submission.event.slug}"
        ),
        principal=principal,
        event=submission.event,
        resource_type="judge_recusal",
        resource_id=recusal.id,
        request=request,
    )
    db.commit()
    db.refresh(recusal)
    return RecusalOut(
        id=recusal.id,
        judge_id=judge.id,
        judge_name=judge.user.display_name,
        submission_id=submission.id,
        submission_name=submission.name,
        reason=recusal.reason,
        created_at=recusal.created_at,
    )


def _validate_against_rubric(
    payload: BallotIn, criteria: dict[uuid.UUID, RubricCriterion]
) -> None:
    """The rubric's own bounds, enforced per criterion.

    `ScoreIn` caps values at 0..100 because Pydantic cannot know the event's
    rubric; this is where the organizer's actual range applies. A 422 naming the
    criterion is more use than a generic one.
    """
    for incoming in payload.scores:
        criterion = criteria.get(incoming.criterion_id)
        if criterion is None:
            raise HTTPException(
                status_code=422,
                detail=f"Criterion {incoming.criterion_id} does not belong to this event",
            )
        if not criterion.min_score <= incoming.value <= criterion.max_score:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"{criterion.name}: {incoming.value} is outside "
                    f"{criterion.min_score}-{criterion.max_score}"
                ),
            )
