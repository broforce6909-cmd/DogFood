"""Results: the weighted rubric and the normalization, applied.

Staff-only for the whole of T2. The brief requires results hidden from everyone
but organizers during the voting window, and the honest way to land that is to
start closed and open it deliberately in T3 rather than to ship it open and
remember to shut it.

The numbers are computed on the fly from `scores` rather than stored. At hackathon
scale that is a few hundred rows and a millisecond, and it means there is no
cached aggregate to go stale when a judge edits a ballot or an organizer changes a
weight. If that ever stops being true, the fix is a materialised view, not a
denormalised column nobody refreshes.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from ..access import Action, Principal, require_access, require_authenticated
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
    OverrideTier,
    ResultOverride,
    RubricCriterion,
    Submission,
    SubmissionStatus,
    Team,
    WebhookEvent,
)
from ..schemas import (
    CalibrationOut,
    ConsistencyFlagOut,
    ConsistencyOut,
    CriterionOut,
    DisagreementOut,
    DisagreementRowOut,
    JudgeDeviationOut,
    JudgeReportOut,
    JudgeReportRow,
    PublicResultRow,
    PublicResultsOut,
    ResultOverrideClearIn,
    ResultOverrideIn,
    ResultRow,
    ResultsOut,
    ScoringGridCell,
    ScoringGridJudgeRow,
    ScoringGridOut,
    ThirdReviewResult,
)
from ..scoring import (
    DISAGREEMENT_THRESHOLD,
    SHRINKAGE_K,
    AssignmentTiming,
    Ballot,
    Results,
    disagreement,
    fast_completion_flags,
    judge_deviation,
    near_identical_score_flags,
    normalize_by_track,
    raw_score,
)
from ..serializers import scoring_criterion
from .events import readable_event
from .judges import _judges_of, _staff_event

router = APIRouter(prefix="/api/events", tags=["results"])

METHOD = (
    "per-judge z-score standardisation with shrinkage toward the global mean "
    f"(k={SHRINKAGE_K}); a judge who used a single value contributes no ordering. "
    "See JUDGING.md."
)


def gather(
    db: Session, event: Event, *, only_complete: bool = True
) -> tuple[Results, dict, list[RubricCriterion]]:
    """Pull the ballots for one event and run the maths over them.

    Shared with the CSV export so the spreadsheet and the dashboard can never
    disagree -- which they would, eventually, if each computed its own.

    `only_complete=True` is the default and the right one for a published result:
    a half-filled ballot is not a judgement. An organizer chasing progress can ask
    for the provisional picture instead.
    """
    stmt = (
        select(JudgeAssignment)
        .join(Submission, Submission.id == JudgeAssignment.submission_id)
        .options(
            selectinload(JudgeAssignment.scores),
            selectinload(JudgeAssignment.judge).selectinload(Judge.user),
            selectinload(JudgeAssignment.submission).selectinload(Submission.team),
            selectinload(JudgeAssignment.submission).selectinload(Submission.track),
        )
        .where(JudgeAssignment.event_id == event.id)
        # A disqualified project's ballots stay in the database -- see
        # `SubmissionStatus`'s own docstring -- but never in a computed result.
        # This was the one status-sensitive query site in the codebase with no
        # filter at all: every other `SubmissionStatus.SUBMITTED` equality
        # check elsewhere excludes a third status for free, but this table
        # never checked status to begin with, since it predates there being a
        # third one.
        .where(Submission.status != SubmissionStatus.DISQUALIFIED)
    )
    if only_complete:
        stmt = stmt.where(JudgeAssignment.status == AssignmentStatus.COMPLETE)

    assignments = list(db.execute(stmt).scalars().all())
    criteria = sorted(event.criteria, key=lambda c: (c.position, c.key))

    ballots = [
        Ballot(
            judge_id=a.judge_id,
            submission_id=a.submission_id,
            scores={s.criterion_id: float(s.value) for s in a.scores},
        )
        for a in assignments
    ]
    # Track-scoped, not one global population: JUDGING.md §3's named gap. A
    # judge is normalized only against judges who scored the *same* track, so
    # a track that happens to run harsher or draw stronger projects cannot be
    # mistaken for a harsh judge. See `normalize_by_track`'s own docstring for
    # exactly what this fixes and what it still cannot promise.
    track_of = {a.submission_id: a.submission.track_id for a in assignments}
    results = normalize_by_track(ballots, [scoring_criterion(c) for c in criteria], track_of)

    track_names = {
        a.submission.track_id: a.submission.track.name
        for a in assignments
        if a.submission.track
    }
    meta = {
        "submissions": {
            a.submission_id: (
                a.submission.name,
                a.submission.team.name,
                a.submission.track.name if a.submission.track else None,
            )
            for a in assignments
        },
        "judges": {a.judge_id: a.judge.user.display_name for a in assignments},
        "track_names": track_names,
    }
    return results, meta, criteria


def compute_tiers(event: Event, results: Results) -> dict[uuid.UUID, str]:
    """Which of `winner`/`community_tier` each submission falls into, by
    `normalized_rank` -- the top `winner_slots` ranks are outright winners,
    the next `community_vote_slots` move into the separate community-voting
    round `app/routers/voting.py` scopes to exactly this subset (see its
    `_votable()`). Nothing cached: called fresh off whatever `gather()` +
    `normalize_by_track()` return, the same "live, not stored" property
    `winner_slots`/`community_vote_slots` themselves already have (see
    DATA-MODEL.md).

    Empty when the event does not use the split -- both slots default to 0,
    so every submission is simply unlabelled, exactly as before this
    feature existed.
    """
    tiers: dict[uuid.UUID, str] = {}
    if event.winner_slots <= 0 and event.community_vote_slots <= 0:
        return tiers
    for row in results.submissions:
        if row.normalized_rank <= event.winner_slots:
            tiers[row.submission_id] = "winner"
        elif row.normalized_rank <= event.winner_slots + event.community_vote_slots:
            tiers[row.submission_id] = "community_tier"
    return tiers


def _overrides_by_submission(db: Session, event: Event) -> dict[uuid.UUID, ResultOverride]:
    return {
        o.submission_id: o
        for o in db.execute(
            select(ResultOverride)
            .options(selectinload(ResultOverride.overridden_by))
            .where(ResultOverride.event_id == event.id)
        ).scalars()
    }


def effective_tiers(
    db: Session,
    event: Event,
    computed_tiers: dict[uuid.UUID, str],
    *,
    overrides: dict[uuid.UUID, ResultOverride] | None = None,
) -> dict[uuid.UUID, str]:
    """Part 4: `compute_tiers()`'s output with any admin overrides applied on
    top -- the actual final tier every other part of the system (community
    vote eligibility, a team's own `GET /submissions/{id}`) must use.

    Never mutates `computed_tiers`, and never the reverse: an override
    replaces this submission's entry in a *copy*, so the original judge-
    computed ranking stays exactly what `compute_tiers()` says -- shown
    alongside this on `GET /{slug}/results` for staff, never silently lost.

    `overrides` is fetched here if not already on hand; pass it through when
    a caller (like `results()` below) already queried it, so the two never
    fetch the same rows twice in one request.
    """
    if overrides is None:
        overrides = _overrides_by_submission(db, event)
    if not overrides:
        return computed_tiers
    result = dict(computed_tiers)
    for submission_id, override in overrides.items():
        if override.tier is OverrideTier.NEITHER:
            result.pop(submission_id, None)
        else:
            result[submission_id] = override.tier.value
    return result


@router.get("/{slug}/results", response_model=ResultsOut)
def results(
    slug: str,
    provisional: bool = Query(
        default=False,
        description="Include ballots that are not finished. Never use for a published result.",
    ),
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db),
) -> ResultsOut:
    """Raw and normalized scores, with the rank movement between them.

    `rank_delta` is published because it is the number an organizer will be asked
    about: "normalization moved this project up three places" is a claim somebody
    will want to check, and hiding it would not make it less true.
    """
    event = _staff_event(db, slug, principal, Action.READ_RESULTS)
    computed, meta, _ = gather(db, event, only_complete=not provisional)
    computed_tiers = compute_tiers(event, computed)
    overrides = _overrides_by_submission(db, event)
    final_tiers = effective_tiers(db, event, computed_tiers, overrides=overrides)

    rows = []
    for row in computed.submissions:
        name, team, track = meta["submissions"].get(row.submission_id, ("?", "?", None))
        override = overrides.get(row.submission_id)
        rows.append(
            ResultRow(
                submission_id=row.submission_id,
                submission_name=name,
                team_name=team,
                track=track,
                n_reviews=row.n_reviews,
                raw_mean=round(row.raw_mean, 4),
                normalized_mean=round(row.normalized_mean, 4),
                raw_rank=row.raw_rank,
                normalized_rank=row.normalized_rank,
                rank_delta=row.rank_delta,
                computed_tier=computed_tiers.get(row.submission_id),
                tier=final_tiers.get(row.submission_id),
                override_reason=override.reason if override else None,
                overridden_by=(
                    override.overridden_by.display_name
                    if override and override.overridden_by
                    else None
                ),
                overridden_at=override.updated_at if override else None,
            )
        )

    return ResultsOut(
        event_slug=event.slug,
        method=METHOD,
        global_mean=round(computed.global_mean, 4),
        global_sd=round(computed.global_sd, 4),
        shrinkage_k=SHRINKAGE_K,
        rows=rows,
        calibrations=[
            CalibrationOut(
                judge_id=cal.judge_id,
                judge_name=meta["judges"].get(cal.judge_id, "?"),
                track=meta["track_names"].get(cal.track_id),
                n=cal.n,
                mean=round(cal.mean, 4),
                sd=round(cal.sd, 4),
                shrunk_mean=round(cal.shrunk_mean, 4),
                shrunk_sd=round(cal.shrunk_sd, 4),
                flat=cal.flat,
                note=cal.note,
            )
            for cal in computed.calibrations
        ],
    )


def _load_submission_in_event(db: Session, event: Event, submission_id: uuid.UUID) -> Submission:
    submission = db.execute(
        select(Submission).where(
            Submission.id == submission_id, Submission.event_id == event.id
        )
    ).scalar_one_or_none()
    if submission is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Submission not found")
    return submission


@router.put("/{slug}/results/overrides/{submission_id}", response_model=ResultRow)
def set_override(
    slug: str,
    submission_id: uuid.UUID,
    payload: ResultOverrideIn,
    request: Request,
    background: BackgroundTasks,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db),
) -> ResultRow:
    """Set or change this submission's final tier, for cause.

    Never touches a ballot, a score, or `compute_tiers()`'s own output --
    the judge-computed ranking is unaffected and stays visible on
    `GET /{slug}/results` alongside whatever this sets. `PUT` rather than
    `POST`, because setting an override that already exists is a change to
    the same one thing, not a second one: `uq_result_overrides_one_per_
    submission` is the guarantee, this is the route that respects it.
    """
    event = _staff_event(db, slug, principal, Action.MANAGE)
    user = require_authenticated(principal)
    submission = _load_submission_in_event(db, event, submission_id)

    existing = db.execute(
        select(ResultOverride).where(
            ResultOverride.event_id == event.id, ResultOverride.submission_id == submission_id
        )
    ).scalar_one_or_none()
    tier = OverrideTier(payload.tier)
    previous = existing.tier.value if existing else "computed"

    if existing is None:
        existing = ResultOverride(
            event_id=event.id,
            submission_id=submission_id,
            tier=tier,
            reason=payload.reason,
            overridden_by_id=user.id,
        )
        db.add(existing)
    else:
        existing.tier = tier
        existing.reason = payload.reason
        existing.overridden_by_id = user.id

    record(
        db,
        action=AuditAction.RESULT_OVERRIDDEN,
        summary=(
            f"{user.email} set {quote(submission.name)}'s final result on {event.slug} "
            f"to {tier.value} (was {previous}): {quote(payload.reason)}"
        ),
        principal=user,
        event=event,
        resource_type="submission",
        resource_id=submission.id,
        request=request,
    )
    db.commit()
    db.refresh(existing)

    schedule(
        background,
        db,
        event.id,
        WebhookEvent.RESULTS_FINALIZED,
        {
            "event": event.slug,
            "submission_id": str(submission.id),
            "tier": tier.value,
        },
    )

    computed, meta, _ = gather(db, event, only_complete=True)
    computed_tiers = compute_tiers(event, computed)
    row = computed.by_id(submission_id)
    name, team, track = meta["submissions"].get(
        submission_id, (submission.name, submission.team.name, None)
    )
    return ResultRow(
        submission_id=submission_id,
        submission_name=name,
        team_name=team,
        track=track,
        n_reviews=row.n_reviews if row else 0,
        raw_mean=round(row.raw_mean, 4) if row else 0.0,
        normalized_mean=round(row.normalized_mean, 4) if row else 0.0,
        raw_rank=row.raw_rank if row else 0,
        normalized_rank=row.normalized_rank if row else 0,
        rank_delta=row.rank_delta if row else 0,
        computed_tier=computed_tiers.get(submission_id),
        tier=None if tier is OverrideTier.NEITHER else tier.value,
        override_reason=existing.reason,
        overridden_by=user.display_name,
        overridden_at=existing.updated_at,
    )


@router.post("/{slug}/results/overrides/{submission_id}/clear", response_model=ResultRow)
def clear_override(
    slug: str,
    submission_id: uuid.UUID,
    payload: ResultOverrideClearIn,
    request: Request,
    background: BackgroundTasks,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db),
) -> ResultRow:
    """Undo an override -- back to whatever `compute_tiers()` says on its
    own. A reason is required here too: reverting a correction is its own
    decision, just as auditable as making one."""
    event = _staff_event(db, slug, principal, Action.MANAGE)
    user = require_authenticated(principal)
    submission = _load_submission_in_event(db, event, submission_id)

    existing = db.execute(
        select(ResultOverride).where(
            ResultOverride.event_id == event.id, ResultOverride.submission_id == submission_id
        )
    ).scalar_one_or_none()
    if existing is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This submission has no override to clear",
        )
    previous = existing.tier.value
    db.delete(existing)

    record(
        db,
        action=AuditAction.RESULT_OVERRIDE_CLEARED,
        summary=(
            f"{user.email} cleared {quote(submission.name)}'s override on {event.slug} "
            f"(was {previous}, now the computed result stands): {quote(payload.reason)}"
        ),
        principal=user,
        event=event,
        resource_type="submission",
        resource_id=submission.id,
        request=request,
    )
    db.commit()

    schedule(
        background,
        db,
        event.id,
        WebhookEvent.RESULTS_FINALIZED,
        {"event": event.slug, "submission_id": str(submission.id), "tier": None},
    )

    computed, meta, _ = gather(db, event, only_complete=True)
    computed_tiers = compute_tiers(event, computed)
    row = computed.by_id(submission_id)
    name, team, track = meta["submissions"].get(
        submission_id, (submission.name, submission.team.name, None)
    )
    return ResultRow(
        submission_id=submission_id,
        submission_name=name,
        team_name=team,
        track=track,
        n_reviews=row.n_reviews if row else 0,
        raw_mean=round(row.raw_mean, 4) if row else 0.0,
        normalized_mean=round(row.normalized_mean, 4) if row else 0.0,
        raw_rank=row.raw_rank if row else 0,
        normalized_rank=row.normalized_rank if row else 0,
        rank_delta=row.rank_delta if row else 0,
        computed_tier=computed_tiers.get(submission_id),
        tier=computed_tiers.get(submission_id),
        override_reason=None,
        overridden_by=None,
        overridden_at=None,
    )


@router.get("/{slug}/results/{submission_id}/scoring", response_model=ScoringGridOut)
def scoring_grid(
    slug: str,
    submission_id: uuid.UUID,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db),
) -> ScoringGridOut:
    """Every judge's ballot for one project, side by side: raw per-criterion
    scores, each judge's normalized contribution, and the final aggregate.

    Read-only -- nothing reachable from this response writes to a `Score`
    row, and a ballot is exactly as immutable through this route as it is
    everywhere else. This is the organizer's own tool for "why did this
    project end up where it did", one project at a time, without a database
    client.
    """
    event = _staff_event(db, slug, principal, Action.READ_RESULTS)
    submission = _load_submission_in_event(db, event, submission_id)
    criteria = sorted(event.criteria, key=lambda c: (c.position, c.key))
    scoring_criteria = [scoring_criterion(c) for c in criteria]

    assignments = list(
        db.execute(
            select(JudgeAssignment)
            .options(
                selectinload(JudgeAssignment.scores),
                selectinload(JudgeAssignment.judge).selectinload(Judge.user),
            )
            .where(
                JudgeAssignment.event_id == event.id,
                JudgeAssignment.submission_id == submission_id,
            )
        )
        .scalars()
        .all()
    )

    computed, _, _ = gather(db, event, only_complete=True)
    row = computed.by_id(submission_id)
    per_judge_normalized = row.per_judge if row else {}

    judge_rows = []
    for assignment in assignments:
        scores_by_criterion = {s.criterion_id: float(s.value) for s in assignment.scores}
        ballot = Ballot(
            judge_id=assignment.judge_id,
            submission_id=assignment.submission_id,
            scores=scores_by_criterion,
        )
        judge_rows.append(
            ScoringGridJudgeRow(
                judge_id=assignment.judge_id,
                judge_name=assignment.judge.user.display_name,
                status=assignment.status.value,
                is_adjudication=assignment.is_adjudication,
                scores=[
                    ScoringGridCell(criterion_id=cid, value=value)
                    for cid, value in scores_by_criterion.items()
                ],
                comment=assignment.comment,
                raw_mean=raw_score(ballot, scoring_criteria),
                normalized_mean=per_judge_normalized.get(assignment.judge_id),
            )
        )

    return ScoringGridOut(
        submission_id=submission.id,
        submission_name=submission.name,
        criteria=[CriterionOut.model_validate(c) for c in criteria],
        judges=judge_rows,
        n_reviews=row.n_reviews if row else 0,
        final_raw_mean=round(row.raw_mean, 4) if row else None,
        final_normalized_mean=round(row.normalized_mean, 4) if row else None,
    )


@router.get("/{slug}/results/{submission_id}/report", response_model=JudgeReportOut)
def judge_report(
    slug: str,
    submission_id: uuid.UUID,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db),
) -> JudgeReportOut:
    """A team's own project, judge feedback included -- and nothing else.

    This is the participant-facing counterpart to `scoring_grid` above, and
    the two differ in exactly the ways that matter: this one is scoped to
    the caller's own team (`Action.READ_JUDGE_REPORT` on the `Submission`,
    not `Action.READ_RESULTS` on the `Event`), gated behind
    `event.results_public()` for anyone who is not staff, and anonymizes
    every judge to `"Judge 1"`, `"Judge 2"`, ... in assignment-id order --
    consistent with `access.py` treating who is judging as staff-only
    information everywhere else. Every query below is filtered by this one
    `submission_id`; there is no code path here that can return another
    project's data.
    """
    event = readable_event(db, slug, principal)
    submission = _load_submission_in_event(db, event, submission_id)
    require_access(principal, submission, Action.READ_JUDGE_REPORT)

    criteria = sorted(event.criteria, key=lambda c: (c.position, c.key))
    scoring_criteria = [scoring_criterion(c) for c in criteria]

    assignments = sorted(
        db.execute(
            select(JudgeAssignment)
            .options(selectinload(JudgeAssignment.scores))
            .where(
                JudgeAssignment.event_id == event.id,
                JudgeAssignment.submission_id == submission_id,
                JudgeAssignment.status == AssignmentStatus.COMPLETE,
            )
        )
        .scalars()
        .all(),
        key=lambda a: a.id,
    )

    computed, _, _ = gather(db, event, only_complete=True)
    row = computed.by_id(submission_id)
    per_judge_normalized = row.per_judge if row else {}
    tiers = effective_tiers(db, event, compute_tiers(event, computed))

    judge_rows = []
    for index, assignment in enumerate(assignments, start=1):
        scores_by_criterion = {s.criterion_id: float(s.value) for s in assignment.scores}
        ballot = Ballot(
            judge_id=assignment.judge_id,
            submission_id=assignment.submission_id,
            scores=scores_by_criterion,
        )
        judge_rows.append(
            JudgeReportRow(
                label=f"Judge {index}",
                scores=[
                    ScoringGridCell(criterion_id=cid, value=value)
                    for cid, value in scores_by_criterion.items()
                ],
                comment=assignment.comment,
                raw_mean=raw_score(ballot, scoring_criteria),
                normalized_mean=per_judge_normalized.get(assignment.judge_id),
            )
        )

    return JudgeReportOut(
        submission_id=submission.id,
        submission_name=submission.name,
        criteria=[CriterionOut.model_validate(c) for c in criteria],
        judges=judge_rows,
        n_reviews=row.n_reviews if row else 0,
        final_raw_mean=round(row.raw_mean, 4) if row else None,
        final_normalized_mean=round(row.normalized_mean, 4) if row else None,
        tier=tiers.get(submission_id),
    )


@router.get("/{slug}/results/public", response_model=PublicResultsOut)
def public_results(
    slug: str,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db),
) -> PublicResultsOut:
    """The public results dashboard: every submitted project, not just
    winners, with rank, score, tier and its links -- distinct from the
    community-vote tally (a vote count) and from the gallery (no rank or
    score at all). Gated by the same `results_public()` switch as the tally.
    """
    event = readable_event(db, slug, principal)
    require_access(principal, event, Action.READ_PUBLIC_RESULTS)

    computed, _, _ = gather(db, event, only_complete=True)
    computed_tiers = compute_tiers(event, computed)
    tiers = effective_tiers(db, event, computed_tiers)
    by_id = {r.submission_id: r for r in computed.submissions}

    all_submitted = (
        db.execute(
            select(Submission)
            .options(selectinload(Submission.team), selectinload(Submission.track))
            .where(
                Submission.event_id == event.id,
                Submission.status == SubmissionStatus.SUBMITTED,
            )
        )
        .scalars()
        .all()
    )

    rows = []
    for submission in all_submitted:
        computed_row = by_id.get(submission.id)
        rows.append(
            PublicResultRow(
                submission_id=submission.id,
                submission_name=submission.name,
                team_name=submission.team.name,
                track=submission.track.name if submission.track else None,
                tier=tiers.get(submission.id),
                normalized_rank=computed_row.normalized_rank if computed_row else None,
                normalized_score=(
                    round(computed_row.normalized_mean, 4) if computed_row else None
                ),
                thumbnail_url=submission.thumbnail_url,
                repo_url=submission.repo_url,
                live_url=submission.live_url,
                demo_video_url=submission.demo_video_url,
            )
        )
    rows.sort(
        key=lambda r: (
            r.normalized_rank is None,
            r.normalized_rank or 0,
            r.submission_name,
        )
    )
    return PublicResultsOut(event_slug=event.slug, rows=rows)


@router.get("/{slug}/results/disagreement", response_model=DisagreementOut)
def results_disagreement(
    slug: str,
    provisional: bool = Query(
        default=False,
        description="Include ballots that are not finished. Never use for a published result.",
    ),
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db),
) -> DisagreementOut:
    """Inter-rater reliability: which projects need a third opinion, and which
    judges are furthest from consensus.

    Staff-only for the same reason `/results` is: this is the aggregate, not
    any one judge's own ballot. Built on the same `gather()` the dashboard and
    the CSV export use, so this cannot disagree with either about what the
    normalized scores were -- only about what to do with them next.
    """
    event = _staff_event(db, slug, principal, Action.READ_RESULTS)
    computed, meta, _ = gather(db, event, only_complete=not provisional)

    submission_rows = []
    for row in disagreement(computed):
        name, team, track = meta["submissions"].get(row.submission_id, ("?", "?", None))
        submission_rows.append(
            DisagreementRowOut(
                submission_id=row.submission_id,
                submission_name=name,
                team_name=team,
                track=track,
                n_reviews=row.n_reviews,
                sd=round(row.sd, 4),
                mean_abs_pairwise_diff=round(row.mean_abs_pairwise_diff, 4),
                needs_review=row.needs_review,
            )
        )

    judge_rows = [
        JudgeDeviationOut(
            judge_id=row.judge_id,
            judge_name=meta["judges"].get(row.judge_id, "?"),
            n_reviews=row.n_reviews,
            mean_abs_deviation=round(row.mean_abs_deviation, 4),
        )
        for row in judge_deviation(computed)
    ]

    return DisagreementOut(
        threshold=DISAGREEMENT_THRESHOLD,
        needs_review_count=sum(1 for r in submission_rows if r.needs_review),
        submissions=submission_rows,
        judges=judge_rows,
    )


@router.get("/{slug}/results/consistency", response_model=ConsistencyOut)
def results_consistency(
    slug: str,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db),
) -> ConsistencyOut:
    """Suspicious-pattern flags for organizer review only -- never a verdict.

    Two independent signals, both named plainly in `app/scoring.py`: a judge
    whose raw scores barely move across many ballots (`near_identical_score_flags`),
    and a judge who finished one ballot and then another implausibly fast
    (`fast_completion_flags`). Staff-only, same rule and same reason as
    `/results` and `/results/disagreement` -- this is the aggregate, not any
    one judge's own ballot, and it is never surfaced to the judge it is about.
    """
    event = _staff_event(db, slug, principal, Action.READ_RESULTS)
    computed, meta, _ = gather(db, event, only_complete=True)

    timings = [
        AssignmentTiming(judge_id=judge_id, submission_id=submission_id, completed_at=completed_at)
        for judge_id, submission_id, completed_at in db.execute(
            select(
                JudgeAssignment.judge_id,
                JudgeAssignment.submission_id,
                JudgeAssignment.completed_at,
            ).where(
                JudgeAssignment.event_id == event.id,
                JudgeAssignment.status == AssignmentStatus.COMPLETE,
                JudgeAssignment.completed_at.is_not(None),
            )
        )
    ]

    flags = [
        ConsistencyFlagOut(
            judge_id=f.judge_id,
            judge_name=meta["judges"].get(f.judge_id, "?"),
            reason=f.reason,
            detail=f.detail,
        )
        for f in [*near_identical_score_flags(computed), *fast_completion_flags(timings)]
    ]
    return ConsistencyOut(flags=flags)


@router.post(
    "/{slug}/results/disagreement/{submission_id}/third-review",
    response_model=ThirdReviewResult,
    status_code=201,
)
def assign_third_review(
    slug: str,
    submission_id: uuid.UUID,
    request: Request,
    background: BackgroundTasks,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db),
) -> ThirdReviewResult:
    """Route a flagged submission to one more, previously uninvolved judge.

    Reuses `plan_assignments` for the actual pick -- the same eligibility
    rules (team membership, track scope, no duplicate reviewer, no recused
    judge; see `app/assignment.py`) and the same least-loaded tie-break
    already proven correct for batch assignment, asked for exactly one more
    reviewer on exactly one submission instead of a whole event. Purely
    additive: the ballots that disagreed enough to flag this submission are
    never touched, changed, or deleted by this call -- see
    `JudgeAssignment.is_adjudication`, the only mark this leaves on them, and
    `normalize()`, which will simply average in the third ballot like any
    other once it is complete. "No auto-overwrite" is not a rule this route
    enforces; it is a consequence of the fact that there is nothing here that
    writes over an existing score.
    """
    event = _staff_event(db, slug, principal, Action.ASSIGN)
    submission = db.execute(
        select(Submission)
        .options(selectinload(Submission.team).selectinload(Team.members))
        .where(Submission.id == submission_id, Submission.event_id == event.id)
    ).scalar_one_or_none()
    if submission is None:
        raise HTTPException(status_code=404, detail="Submission not found on this event")

    judges = [j for j in _judges_of(db, event) if j.is_active]
    if not judges:
        raise HTTPException(status_code=409, detail="This event has no active judges yet")

    existing_judge_ids = set(
        db.execute(
            select(JudgeAssignment.judge_id).where(
                JudgeAssignment.submission_id == submission.id
            )
        ).scalars()
    )
    recused = set(
        db.execute(
            select(JudgeRecusal.judge_id).where(JudgeRecusal.submission_id == submission.id)
        ).scalars()
    )
    load: dict[uuid.UUID, int] = {j.id: 0 for j in judges}
    for judge_id, count in db.execute(
        select(JudgeAssignment.judge_id, func.count())
        .where(JudgeAssignment.event_id == event.id)
        .group_by(JudgeAssignment.judge_id)
    ):
        if judge_id in load:
            load[judge_id] = count

    plan = plan_assignments(
        [
            JudgeInfo(id=j.id, user_id=j.user_id, track_id=j.track_id, existing=load.get(j.id, 0))
            for j in judges
        ],
        [
            SubmissionInfo(
                id=submission.id,
                track_id=submission.track_id,
                team_member_ids=frozenset(m.user_id for m in submission.team.members),
                already_assigned=frozenset(existing_judge_ids),
                recused_judge_ids=frozenset(recused),
            )
        ],
        reviews_per_submission=len(existing_judge_ids) + 1,
    )
    if not plan.pairs:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "No eligible judge left for a third review -- every active judge is "
                "already assigned to this submission, on the submitting team, "
                "recused, or outside this project's track."
            ),
        )
    judge_id, _ = plan.pairs[0]
    judge = next(j for j in judges if j.id == judge_id)

    assignment = JudgeAssignment(
        event_id=event.id,
        judge_id=judge.id,
        submission_id=submission.id,
        status=AssignmentStatus.PENDING,
        is_adjudication=True,
    )
    db.add(assignment)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="That judge already holds a ballot for this submission. Re-run to pick another.",
        ) from None

    record(
        db,
        action=AuditAction.THIRD_REVIEW_ASSIGNED,
        summary=(
            f"{principal.email} routed {quote(submission.name)} to {judge.user.email} "
            f"for a third review on {event.slug} (flagged disagreement)"
        ),
        principal=principal,
        event=event,
        resource_type="assignment",
        resource_id=assignment.id,
        request=request,
    )
    db.commit()
    db.refresh(assignment)

    schedule(
        background,
        db,
        event.id,
        WebhookEvent.ASSIGNMENTS_CREATED,
        {
            "event": event.slug,
            "created": 1,
            "judges": 1,
            "submissions": 1,
            "reviews_per_submission": len(existing_judge_ids) + 1,
            "third_review": True,
        },
    )
    return ThirdReviewResult(
        assignment_id=assignment.id,
        judge_id=judge.id,
        judge_name=judge.user.display_name,
        submission_id=submission.id,
    )
