"""The organizer's judging surface: rubric, judges, assignment, progress.

Everything here is staff-only, and all of it is decided by `check_access` against
the *event* -- `MANAGE_JUDGES`, `ASSIGN`, `READ_PROGRESS`. A judge calling any of
it gets a 403, which is the FIG. 02 row for "other track" and "aggregate" both.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, status
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from ..access import Action, Principal, require_access
from ..assignment import JudgeInfo, SubmissionInfo, plan_assignments
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
    Role,
    RubricCriterion,
    Score,
    Submission,
    SubmissionStatus,
    Team,
    User,
    WebhookEvent,
    utcnow,
)
from ..pagination import Page, PageParams, count_of, pages_for
from ..schemas import (
    AssignmentOut,
    AssignRequest,
    AssignResult,
    CriterionIn,
    CriterionOut,
    CriterionUpdate,
    JudgeIn,
    JudgeOut,
    JudgeProgressRow,
    JudgeRemovalIn,
    JudgeUpdate,
    ProgressOut,
    RecusalIn,
    RecusalOut,
    ShortfallOut,
)
from ..security import normalize_email
from ..serializers import assignment_out, criterion_out, judge_out
from .events import readable_event

router = APIRouter(prefix="/api/events", tags=["judging-admin"])


def _staff_event(db: Session, slug: str, user: Principal, action: Action) -> Event:
    """Load the event, then demand the specific judging right.

    `readable_event` 404s an event whose existence is not public; the action check
    then 403s. Order matters: a stranger probing slugs learns nothing new.
    """
    event = readable_event(db, slug, user)
    require_access(user, event, action)
    return event


# --------------------------------------------------------------------------- #
# Rubric
# --------------------------------------------------------------------------- #


@router.get("/{slug}/criteria", response_model=list[CriterionOut])
def list_criteria(
    slug: str,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db),
) -> list[CriterionOut]:
    """Public to anyone who can see the event, on purpose.

    Participants are entitled to know what they are being judged against, and
    publishing the rubric costs nothing an organizer wanted to keep. Only staff
    can change it.
    """
    event = readable_event(db, slug, principal)
    rows = (
        db.execute(
            select(RubricCriterion)
            .where(RubricCriterion.event_id == event.id)
            .order_by(RubricCriterion.position, RubricCriterion.key)
        )
        .scalars()
        .all()
    )
    return [criterion_out(c) for c in rows]


@router.post("/{slug}/criteria", response_model=CriterionOut, status_code=201)
def add_criterion(
    slug: str,
    payload: CriterionIn,
    request: Request,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db),
) -> CriterionOut:
    event = _staff_event(db, slug, principal, Action.MANAGE)
    criterion = RubricCriterion(
        event_id=event.id,
        key=payload.key,
        name=payload.name,
        description=payload.description,
        weight=payload.weight,
        min_score=payload.min_score,
        max_score=payload.max_score,
        position=payload.position,
    )
    db.add(criterion)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"This event already has a criterion keyed '{payload.key}'",
        ) from None
    record(
        db,
        action=AuditAction.RUBRIC_CHANGED,
        summary=(
            f"{principal.email} added rubric criterion {quote(criterion.name)} "
            f"(weight {criterion.weight}) on {event.slug}"
        ),
        principal=principal,
        event=event,
        resource_type="rubric_criterion",
        resource_id=criterion.id,
        request=request,
    )
    db.commit()
    db.refresh(criterion)
    return criterion_out(criterion)


@router.patch("/{slug}/criteria/{criterion_id}", response_model=CriterionOut)
def edit_criterion(
    slug: str,
    criterion_id: uuid.UUID,
    payload: CriterionUpdate,
    request: Request,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db),
) -> CriterionOut:
    """Editing a weight re-ranks the event from here on.

    It does **not** rewrite any stored score: weights are applied at read time,
    so the ballots already cast stay exactly as the judges left them. That is the
    whole reason weights are not baked in.
    """
    event = _staff_event(db, slug, principal, Action.MANAGE)
    criterion = _load_criterion(db, event, criterion_id)

    data = payload.model_dump(exclude_unset=True)
    for field, value in data.items():
        setattr(criterion, field, value)
    if criterion.max_score <= criterion.min_score:
        raise HTTPException(status_code=422, detail="max_score must be greater than min_score")
    if data:
        record(
            db,
            action=AuditAction.RUBRIC_CHANGED,
            summary=(
                f"{principal.email} edited rubric criterion {quote(criterion.name)} "
                f"({', '.join(sorted(data))}) on {event.slug}"
            ),
            principal=principal,
            event=event,
            resource_type="rubric_criterion",
            resource_id=criterion.id,
            request=request,
        )
    db.commit()
    db.refresh(criterion)
    return criterion_out(criterion)


@router.delete("/{slug}/criteria/{criterion_id}", status_code=204)
def remove_criterion(
    slug: str,
    criterion_id: uuid.UUID,
    request: Request,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db),
) -> None:
    """Deleting a criterion deletes the scores recorded against it.

    Cascade rather than a soft delete, because a rubric row nobody scores against
    is indistinguishable from one that was never there -- but it does mean this is
    destructive, and the count is reported so an organizer can see what it cost.
    """
    event = _staff_event(db, slug, principal, Action.MANAGE)
    criterion = _load_criterion(db, event, criterion_id)
    scored = len(db.execute(select(Score).where(Score.criterion_id == criterion.id)).all())
    record(
        db,
        action=AuditAction.RUBRIC_CHANGED,
        summary=(
            f"{principal.email} removed rubric criterion {quote(criterion.name)} "
            f"on {event.slug} ({scored} recorded score(s) went with it)"
        ),
        principal=principal,
        event=event,
        resource_type="rubric_criterion",
        resource_id=criterion.id,
        request=request,
    )
    db.delete(criterion)
    db.commit()


def _load_criterion(db: Session, event: Event, criterion_id: uuid.UUID) -> RubricCriterion:
    criterion = db.execute(
        select(RubricCriterion).where(
            RubricCriterion.id == criterion_id, RubricCriterion.event_id == event.id
        )
    ).scalar_one_or_none()
    if criterion is None:
        raise HTTPException(status_code=404, detail="Criterion not found on this event")
    return criterion


# --------------------------------------------------------------------------- #
# Judges
# --------------------------------------------------------------------------- #


@router.get("/{slug}/judges", response_model=list[JudgeOut])
def list_judges(
    slug: str,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db),
) -> list[JudgeOut]:
    """Staff only. Who else is judging is not a judge's business."""
    event = _staff_event(db, slug, principal, Action.MANAGE_JUDGES)
    return [judge_out(j) for j in _judges_of(db, event)]


@router.post("/{slug}/judges", response_model=JudgeOut, status_code=201)
def invite_judge(
    slug: str,
    payload: JudgeIn,
    request: Request,
    background: BackgroundTasks,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db),
) -> JudgeOut:
    """Invite an existing account to judge this event.

    The account has to exist: minting users from an invite form is a spam vector
    and an account-takeover vector, and there is no email to send the invitation
    with anyway. Their global role is raised to `judge` if it was lower, which is
    the floor that lets them reach the judging routes -- this row is what says
    which event.
    """
    event = _staff_event(db, slug, principal, Action.MANAGE_JUDGES)
    email = normalize_email(payload.email)

    user = db.execute(select(User).where(User.email == email)).scalar_one_or_none()
    if user is None:
        raise HTTPException(
            status_code=404,
            detail=f"No account for {email}. They need to register before being invited to judge.",
        )
    if not user.is_active:
        raise HTTPException(status_code=409, detail="That account is deactivated")

    if payload.track_id is not None:
        _require_own_track(event, payload.track_id)

    judge = Judge(
        event_id=event.id,
        user_id=user.id,
        track_id=payload.track_id,
        # No email to accept through, so an invite by an organizer is taken as
        # accepted. When Phase 4 adds notifications this becomes a real handshake.
        accepted_at=utcnow(),
    )
    if not user.role.at_least(Role.JUDGE):
        user.role = Role.JUDGE

    db.add(judge)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"{email} is already a judge on this event",
        ) from None
    record(
        db,
        action=AuditAction.JUDGE_INVITED,
        summary=(
            f"{principal.email} invited {user.email} to judge {event.slug}"
            + (" (track judge)" if payload.track_id else " (all tracks)")
        ),
        principal=principal,
        event=event,
        resource_type="judge",
        resource_id=judge.id,
        request=request,
    )
    db.commit()
    db.refresh(judge)

    # `judge.invited` was a documented, subscribable webhook topic that nothing
    # ever scheduled. Same gap, same fix as `team.created` and
    # `submission.updated` -- found by checking every listed topic against
    # every actual `schedule()` call site rather than trusting the list.
    schedule(
        background,
        db,
        event.id,
        WebhookEvent.JUDGE_INVITED,
        {
            "event": event.slug,
            "judge_id": str(judge.id),
            "email": user.email,
            "track": judge.track.key if judge.track else None,
        },
    )
    return judge_out(judge)


@router.patch("/{slug}/judges/{judge_id}", response_model=JudgeOut)
def edit_judge(
    slug: str,
    judge_id: uuid.UUID,
    payload: JudgeUpdate,
    request: Request,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db),
) -> JudgeOut:
    """Re-track or deactivate a judge.

    Narrowing a judge to a track does not retract ballots they already hold on
    other tracks -- but `check_access` will refuse to serve those ballots from
    this moment on, so the effect is immediate even though the rows survive. The
    count of now-unreachable assignments is worth an organizer's attention, so
    `/assignments` reports it.
    """
    event = _staff_event(db, slug, principal, Action.MANAGE_JUDGES)
    judge = _load_judge(db, event, judge_id)
    was_active = judge.is_active

    if payload.clear_track:
        judge.track_id = None
    elif payload.track_id is not None:
        _require_own_track(event, payload.track_id)
        judge.track_id = payload.track_id
    if payload.is_active is not None:
        judge.is_active = payload.is_active

    if payload.is_active is False and was_active:
        record(
            db,
            action=AuditAction.JUDGE_DEACTIVATED,
            summary=f"{principal.email} deactivated judge {judge.user.email} on {event.slug}",
            principal=principal,
            event=event,
            resource_type="judge",
            resource_id=judge.id,
            request=request,
        )
    elif payload.clear_track or payload.track_id is not None:
        record(
            db,
            action=AuditAction.JUDGE_INVITED,
            summary=(
                f"{principal.email} changed judge {judge.user.email}'s track "
                f"scope on {event.slug}"
            ),
            principal=principal,
            event=event,
            resource_type="judge",
            resource_id=judge.id,
            request=request,
        )

    db.commit()
    db.refresh(judge)
    return judge_out(judge)


@router.post("/{slug}/judges/{judge_id}/remove", response_model=JudgeOut)
def remove_judge(
    slug: str,
    judge_id: uuid.UUID,
    payload: JudgeRemovalIn,
    request: Request,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db),
) -> JudgeOut:
    """Remove a judge from this event, for cause.

    Soft, not hard: earlier this row was deleted outright, which cascaded
    away every score the judge had ever written -- including `COMPLETE`
    ballots, which is exactly the historical record normalization for every
    *other* judge on the event depends on staying put. That directly
    contradicted this feature's own requirement the first time it was
    exercised for real, so the mechanism changed rather than the requirement:
    the `Judge` row survives, deactivated (`is_active=false`, the same flag
    `PATCH .../judges/{id}` already flips for a plain deactivation), so a
    `COMPLETE` assignment's `judge_id` still resolves and every score they
    wrote stays exactly as they left it.

    Only *incomplete* assignments (`pending`/`in_progress`) are dropped: a
    ballot nobody finished carries no scoring signal worth keeping, and an
    organizer who wants that submission back at full review strength re-runs
    batch assignment afterward -- the same idempotent top-up `run_assignment`
    already does for any other coverage gap, not a new reassignment
    mechanism built for this one case.
    """
    event = _staff_event(db, slug, principal, Action.MANAGE_JUDGES)
    judge = _load_judge(db, event, judge_id)

    dropped_ids = list(
        db.execute(
            delete(JudgeAssignment)
            .where(
                JudgeAssignment.judge_id == judge.id,
                JudgeAssignment.status != AssignmentStatus.COMPLETE,
            )
            .returning(JudgeAssignment.id)
        ).scalars()
    )
    completed = db.execute(
        select(func.count())
        .select_from(JudgeAssignment)
        .where(
            JudgeAssignment.judge_id == judge.id,
            JudgeAssignment.status == AssignmentStatus.COMPLETE,
        )
    ).scalar_one()

    judge.is_active = False
    record(
        db,
        action=AuditAction.JUDGE_REMOVED,
        summary=(
            f"{principal.email} removed judge {judge.user.email} from {event.slug}: "
            f"{quote(payload.reason)} ({len(dropped_ids)} incomplete ballot(s) dropped, "
            f"{completed} completed ballot(s) and their scores kept)"
        ),
        principal=principal,
        event=event,
        resource_type="judge",
        resource_id=judge.id,
        request=request,
    )
    db.commit()
    db.refresh(judge)
    return judge_out(judge)


def _judges_of(db: Session, event: Event) -> list[Judge]:
    return list(
        db.execute(
            select(Judge)
            .options(selectinload(Judge.user), selectinload(Judge.track))
            .where(Judge.event_id == event.id)
            .order_by(Judge.invited_at)
        )
        .scalars()
        .all()
    )


def _load_judge(db: Session, event: Event, judge_id: uuid.UUID) -> Judge:
    judge = db.execute(
        select(Judge)
        .options(selectinload(Judge.user), selectinload(Judge.track))
        .where(Judge.id == judge_id, Judge.event_id == event.id)
    ).scalar_one_or_none()
    if judge is None:
        raise HTTPException(status_code=404, detail="Judge not found on this event")
    return judge


def _require_own_track(event: Event, track_id: uuid.UUID) -> None:
    if track_id not in {t.id for t in event.tracks}:
        raise HTTPException(status_code=422, detail="That track belongs to another event")


# --------------------------------------------------------------------------- #
# Conflict of interest: recusals
# --------------------------------------------------------------------------- #


def _recusal_out(recusal: JudgeRecusal, judge: Judge, submission_name: str) -> RecusalOut:
    return RecusalOut(
        id=recusal.id,
        judge_id=judge.id,
        judge_name=judge.user.display_name,
        submission_id=recusal.submission_id,
        submission_name=submission_name,
        reason=recusal.reason,
        created_at=recusal.created_at,
    )


@router.post("/{slug}/judges/{judge_id}/recusals", response_model=RecusalOut, status_code=201)
def add_recusal(
    slug: str,
    judge_id: uuid.UUID,
    payload: RecusalIn,
    request: Request,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db),
) -> RecusalOut:
    """Record that this judge must never review this specific project.

    On top of the structural team-membership conflict `plan_assignments`
    already enforces, unconditionally -- this is for the conflict structure
    cannot see: a judge who personally knows a team, has a stake in it, or
    otherwise should not be the one scoring this project. Any *incomplete*
    assignment already linking the two is removed along with it, the same
    treatment `remove_judge` gives a departing judge's unfinished ballots; a
    `COMPLETE` ballot is a historical record and is left in place -- delete
    the assignment separately (`DELETE /{slug}/assignments/{id}`) if it
    should be discarded too.
    """
    event = _staff_event(db, slug, principal, Action.MANAGE_JUDGES)
    judge = _load_judge(db, event, judge_id)
    submission = db.execute(
        select(Submission).where(
            Submission.id == payload.submission_id, Submission.event_id == event.id
        )
    ).scalar_one_or_none()
    if submission is None:
        raise HTTPException(status_code=404, detail="Submission not found on this event")

    recusal = JudgeRecusal(
        event_id=event.id,
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
            detail=f"{judge.user.email} is already recused from {submission.name}",
        ) from None

    stale = db.execute(
        select(JudgeAssignment).where(
            JudgeAssignment.judge_id == judge.id,
            JudgeAssignment.submission_id == submission.id,
            JudgeAssignment.status != AssignmentStatus.COMPLETE,
        )
    ).scalar_one_or_none()
    if stale is not None:
        db.delete(stale)

    record(
        db,
        action=AuditAction.JUDGE_RECUSED,
        summary=(
            f"{principal.email} recorded a conflict of interest: {judge.user.email} "
            f"will not review {quote(submission.name)} on {event.slug}"
        ),
        principal=principal,
        event=event,
        resource_type="judge_recusal",
        resource_id=recusal.id,
        request=request,
    )
    db.commit()
    db.refresh(recusal)
    return _recusal_out(recusal, judge, submission.name)


@router.get("/{slug}/judges/{judge_id}/recusals", response_model=list[RecusalOut])
def list_recusals(
    slug: str,
    judge_id: uuid.UUID,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db),
) -> list[RecusalOut]:
    event = _staff_event(db, slug, principal, Action.MANAGE_JUDGES)
    judge = _load_judge(db, event, judge_id)
    rows = (
        db.execute(
            select(JudgeRecusal)
            .options(selectinload(JudgeRecusal.submission))
            .where(JudgeRecusal.judge_id == judge.id)
            .order_by(JudgeRecusal.created_at)
        )
        .scalars()
        .all()
    )
    return [_recusal_out(r, judge, r.submission.name) for r in rows]


@router.delete("/{slug}/recusals/{recusal_id}", status_code=204)
def remove_recusal(
    slug: str,
    recusal_id: uuid.UUID,
    request: Request,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db),
) -> None:
    """Undo a recorded conflict -- usually a correction of a mistaken entry.

    Does not restore any assignment `add_recusal` removed: re-running batch
    assignment (or a fresh third-review call) will consider this judge
    eligible again from here on, which is the same "topped up, not replayed"
    idempotence `run_assignment` already promises everywhere else.
    """
    event = _staff_event(db, slug, principal, Action.MANAGE_JUDGES)
    row = db.execute(
        select(JudgeRecusal)
        .options(
            selectinload(JudgeRecusal.judge).selectinload(Judge.user),
            selectinload(JudgeRecusal.submission),
        )
        .where(JudgeRecusal.id == recusal_id, JudgeRecusal.event_id == event.id)
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Recusal not found on this event")
    record(
        db,
        action=AuditAction.JUDGE_RECUSED,
        summary=(
            f"{principal.email} removed {row.judge.user.email}'s recusal from "
            f"{quote(row.submission.name)} on {event.slug}"
        ),
        principal=principal,
        event=event,
        resource_type="judge_recusal",
        resource_id=row.id,
        request=request,
    )
    db.delete(row)
    db.commit()


# --------------------------------------------------------------------------- #
# Assignment
# --------------------------------------------------------------------------- #


@router.get("/{slug}/assignments", response_model=Page[AssignmentOut])
def list_assignments(
    slug: str,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db),
    pagination: PageParams = Depends(),
) -> Page[AssignmentOut]:
    """The whole grid, staff only, paged. This is the view a judge must never get.

    A dedicated query rather than `_assignments_of` with a slice tacked on:
    that helper also backs `run_assignment`'s load-balancing read, which needs
    every existing assignment to compute correctly, not one page of them.
    Measured unpaged at 1000 assignments (1000 judges x submissions at 200
    submissions, 5 reviews each): ~1MB, ~1.7s -- see ARCHITECTURE.md.
    """
    event = _staff_event(db, slug, principal, Action.READ_PROGRESS)
    stmt = select(JudgeAssignment).where(JudgeAssignment.event_id == event.id)
    total = count_of(db, stmt)
    rows = (
        db.execute(
            stmt.options(
                selectinload(JudgeAssignment.judge).selectinload(Judge.user),
                selectinload(JudgeAssignment.judge).selectinload(Judge.track),
                selectinload(JudgeAssignment.scores),
                selectinload(JudgeAssignment.submission).selectinload(Submission.team),
                selectinload(JudgeAssignment.submission).selectinload(Submission.track),
                selectinload(JudgeAssignment.submission)
                .selectinload(Submission.event)
                .selectinload(Event.criteria),
            )
            .order_by(JudgeAssignment.assigned_at)
            .offset(pagination.offset)
            .limit(pagination.per_page)
        )
        .scalars()
        .all()
    )
    now = utcnow()
    return Page(
        items=[assignment_out(a, principal, now=now) for a in rows],
        total=total,
        page=pagination.page,
        per_page=pagination.per_page,
        pages=pages_for(total, pagination.per_page),
    )


@router.post("/{slug}/assignments", response_model=AssignResult)
def run_assignment(
    slug: str,
    payload: AssignRequest,
    request: Request,
    background: BackgroundTasks,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db),
) -> AssignResult:
    """Batch-assign judges to submitted projects.

    Idempotent and additive: existing ballots count toward both the per-submission
    review target and each judge's load, so running this again after inviting a
    judge tops the event up instead of doubling it.

    `dry_run=true` computes and reports the plan without writing it, which is the
    only honest way to let an organizer see what a seed does before committing.
    """
    event = _staff_event(db, slug, principal, Action.ASSIGN)

    judges = [j for j in _judges_of(db, event) if j.is_active]
    submissions = _submitted_with_teams(db, event)
    if not judges:
        raise HTTPException(status_code=409, detail="This event has no active judges yet")
    if not submissions:
        raise HTTPException(status_code=409, detail="This event has no submitted projects yet")

    existing = _assignments_of(db, event)
    by_submission: dict[uuid.UUID, set[uuid.UUID]] = {}
    load: dict[uuid.UUID, int] = {j.id: 0 for j in judges}
    for row in existing:
        by_submission.setdefault(row.submission_id, set()).add(row.judge_id)
        if row.judge_id in load:
            load[row.judge_id] += 1

    recusals: dict[uuid.UUID, set[uuid.UUID]] = {}
    for judge_id, submission_id in db.execute(
        select(JudgeRecusal.judge_id, JudgeRecusal.submission_id).where(
            JudgeRecusal.event_id == event.id
        )
    ):
        recusals.setdefault(submission_id, set()).add(judge_id)

    plan = plan_assignments(
        [
            JudgeInfo(
                id=j.id, user_id=j.user_id, track_id=j.track_id, existing=load.get(j.id, 0)
            )
            for j in judges
        ],
        [
            SubmissionInfo(
                id=s.id,
                track_id=s.track_id,
                team_member_ids=frozenset(m.user_id for m in s.team.members),
                already_assigned=frozenset(by_submission.get(s.id, set())),
                recused_judge_ids=frozenset(recusals.get(s.id, set())),
            )
            for s in submissions
        ],
        reviews_per_submission=payload.reviews_per_submission,
        seed=payload.seed,
    )

    if not payload.dry_run:
        for judge_id, submission_id in plan.pairs:
            db.add(
                JudgeAssignment(
                    event_id=event.id,
                    judge_id=judge_id,
                    submission_id=submission_id,
                    status=AssignmentStatus.PENDING,
                )
            )
        record(
            db,
            action=AuditAction.ASSIGNMENT_RUN,
            summary=(
                f"{principal.email} ran batch assignment on {event.slug}: "
                f"{len(plan.pairs)} ballot(s) created across {len(judges)} judge(s) "
                f"and {len(submissions)} project(s), target {payload.reviews_per_submission} "
                f"review(s) each"
                + (f", {len(plan.shortfalls)} short" if plan.shortfalls else "")
            ),
            principal=principal,
            event=event,
            resource_type="event",
            resource_id=event.id,
            request=request,
        )
        try:
            db.commit()
        except IntegrityError:
            # Two runs read the same "who already has what" snapshot before
            # either wrote, and both planned the same judge onto the same
            # submission -- a double-click on "Run assignment", or two
            # organizers running it at once. `uq_judge_assignments_once` is
            # the constraint that would otherwise let both writes land as a
            # single judge silently reviewing one project twice. The other
            # run's assignments already committed; re-reading and re-running
            # (idempotent, per this route's own docstring) tops up whatever
            # is actually still missing rather than double-booking it.
            db.rollback()
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    "Another assignment run committed at the same time. "
                    "Re-run assignment to top up whatever is still missing."
                ),
            ) from None

        # `assignments.created` was a documented, subscribable webhook topic
        # that nothing ever scheduled -- same gap as the other topics fixed in
        # this pass. Not fired on a dry run: nothing was actually created for
        # an integration to hear about.
        if plan.pairs:
            schedule(
                background,
                db,
                event.id,
                WebhookEvent.ASSIGNMENTS_CREATED,
                {
                    "event": event.slug,
                    "created": len(plan.pairs),
                    "judges": len(judges),
                    "submissions": len(submissions),
                    "reviews_per_submission": payload.reviews_per_submission,
                },
            )

    names = {s.id: s.name for s in submissions}
    judge_names = {j.id: j.user.display_name for j in judges}
    return AssignResult(
        created=len(plan.pairs),
        reviews_per_submission=payload.reviews_per_submission,
        submissions=len(submissions),
        judges=len(judges),
        balanced=plan.balanced,
        spread=plan.spread,
        loads={judge_names.get(jid, str(jid)): n for jid, n in plan.loads.items()},
        shortfalls=[
            ShortfallOut(
                submission_id=s.submission_id,
                submission_name=names.get(s.submission_id, "?"),
                requested=s.requested,
                achieved=s.achieved,
                eligible=s.eligible,
                reason=s.reason,
            )
            for s in plan.shortfalls
        ],
        dry_run=payload.dry_run,
    )


@router.delete("/{slug}/assignments/{assignment_id}", status_code=204)
def delete_assignment(
    slug: str,
    assignment_id: uuid.UUID,
    request: Request,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db),
) -> None:
    """Staff may destroy a ballot. They may not author one -- see `app.access`."""
    event = _staff_event(db, slug, principal, Action.ASSIGN)
    row = db.execute(
        select(JudgeAssignment)
        .options(selectinload(JudgeAssignment.judge).selectinload(Judge.user))
        .where(
            JudgeAssignment.id == assignment_id, JudgeAssignment.event_id == event.id
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Assignment not found on this event")
    record(
        db,
        action=AuditAction.ASSIGNMENT_DELETED,
        summary=(
            f"{principal.email} deleted {row.judge.user.email}'s assignment "
            f"(status was {row.status.value}) on {event.slug}"
        ),
        principal=principal,
        event=event,
        resource_type="assignment",
        resource_id=row.id,
        request=request,
    )
    db.delete(row)
    db.commit()


def _assignments_of(db: Session, event: Event) -> list[JudgeAssignment]:
    return list(
        db.execute(
            select(JudgeAssignment)
            .options(
                selectinload(JudgeAssignment.judge).selectinload(Judge.user),
                selectinload(JudgeAssignment.judge).selectinload(Judge.track),
                selectinload(JudgeAssignment.scores),
                selectinload(JudgeAssignment.submission).selectinload(Submission.team),
                selectinload(JudgeAssignment.submission).selectinload(Submission.track),
                selectinload(JudgeAssignment.submission)
                .selectinload(Submission.event)
                .selectinload(Event.criteria),
            )
            .where(JudgeAssignment.event_id == event.id)
            .order_by(JudgeAssignment.assigned_at)
        )
        .scalars()
        .all()
    )


def _submitted_with_teams(db: Session, event: Event) -> list[Submission]:
    """Only submitted projects are judged. A draft is not an entry."""
    return list(
        db.execute(
            select(Submission)
            .options(
                selectinload(Submission.team).selectinload(Team.members),
                selectinload(Submission.track),
            )
            .where(
                Submission.event_id == event.id,
                Submission.status == SubmissionStatus.SUBMITTED,
            )
            .order_by(Submission.submitted_at)
        )
        .scalars()
        .all()
    )


# --------------------------------------------------------------------------- #
# Progress
# --------------------------------------------------------------------------- #


@router.get("/{slug}/judging/progress", response_model=ProgressOut)
def progress(
    slug: str,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db),
) -> ProgressOut:
    """Who has not started, who is mid-way, and how close the event is to done.

    Aggregated in the database rather than by loading every ballot: this is the
    page an organizer refreshes every two minutes near the deadline.
    """
    event = _staff_event(db, slug, principal, Action.READ_PROGRESS)
    now = utcnow()

    counts = db.execute(
        select(
            JudgeAssignment.judge_id,
            JudgeAssignment.status,
            func.count().label("n"),
        )
        .where(JudgeAssignment.event_id == event.id)
        .group_by(JudgeAssignment.judge_id, JudgeAssignment.status)
    ).all()

    tally: dict[uuid.UUID, dict[AssignmentStatus, int]] = {}
    for judge_id, assignment_status, n in counts:
        tally.setdefault(judge_id, {})[assignment_status] = n

    rows: list[JudgeProgressRow] = []
    not_started: list[str] = []
    for judge in _judges_of(db, event):
        per = tally.get(judge.id, {})
        complete = per.get(AssignmentStatus.COMPLETE, 0)
        in_progress = per.get(AssignmentStatus.IN_PROGRESS, 0)
        pending = per.get(AssignmentStatus.PENDING, 0)
        assigned = complete + in_progress + pending
        rows.append(
            JudgeProgressRow(
                judge_id=judge.id,
                judge_name=judge.user.display_name,
                track=judge.track.name if judge.track else None,
                assigned=assigned,
                complete=complete,
                in_progress=in_progress,
                pending=pending,
            )
        )
        if assigned and complete + in_progress == 0:
            not_started.append(judge.user.display_name)

    assignments_total = sum(r.assigned for r in rows)
    assignments_complete = sum(r.complete for r in rows)

    # "Fully reviewed" means every ballot on the project is in. A project with
    # two of three reviews back is not done, and averaging it as if it were is
    # how a partial result leaks into a ranking.
    per_submission = db.execute(
        select(
            JudgeAssignment.submission_id,
            func.count().label("total"),
            # FILTER rather than SUM(CAST(...)): Postgres has the clause, and it
            # says what it means.
            func.count()
            .filter(JudgeAssignment.status == AssignmentStatus.COMPLETE)
            .label("done"),
        )
        .where(JudgeAssignment.event_id == event.id)
        .group_by(JudgeAssignment.submission_id)
    ).all()
    fully = sum(1 for _, total, done in per_submission if total and done == total)

    submissions_total = db.execute(
        select(func.count())
        .select_from(Submission)
        .where(
            Submission.event_id == event.id,
            Submission.status == SubmissionStatus.SUBMITTED,
        )
    ).scalar_one()

    return ProgressOut(
        event_slug=event.slug,
        judging_opens_at=event.judging_opens_at,
        judging_closes_at=event.judging_closes_at,
        judging_open=event.judging_open(now),
        judges=rows,
        submissions_total=submissions_total,
        submissions_fully_reviewed=fully,
        assignments_total=assignments_total,
        assignments_complete=assignments_complete,
        percent_complete=(
            round(100.0 * assignments_complete / assignments_total, 1)
            if assignments_total
            else 0.0
        ),
        not_started=sorted(not_started),
    )
