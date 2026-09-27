"""Pairwise judging: the Gavel-style alternative to the scored rubric.

Named as "the more interesting answer" in JUDGING.md's list of what this project
did not do -- instead of asking a judge to place a number on a scale, show them two
projects and ask which is better, then recover a ranking with Bradley-Terry. See
`app/pairwise.py` for the model itself; this module is the HTTP surface around it.

Additive, not a replacement: `Event.pairwise_enabled` is its own toggle, and an
organizer may run both a scored rubric and pairwise comparison on the same event.
Both share the judging window (`judging_opens_at`/`judging_closes_at`) -- they are
two different mechanisms for the same phase of the event, not two separate phases.

Three routes:

* `GET /{slug}/pairwise/next` -- a judge's next pair to compare. Computed fresh
  from the comparisons already on file, not a persisted assignment the way a
  scored ballot is; see `app/pairwise.pick_next_pair` for why nothing needs to be
  written until the judge actually answers.
* `POST /{slug}/pairwise/compare` -- a judge's answer, written once.
* `GET /{slug}/pairwise/results` -- the fitted ranking. Staff-only, same rule as
  `READ_RESULTS` on the scored side and for the same reason: a ranking is exactly
  the kind of secret a normalized score table already is.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from ..access import Action, Principal, require_access, require_authenticated
from ..audit import quote, record
from ..db import get_db
from ..deps import get_current_principal
from ..models import (
    AuditAction,
    Judge,
    JudgeRecusal,
    PairwiseComparison,
    Submission,
    SubmissionStatus,
    Team,
    TeamMember,
)
from ..pairwise import Comparison, coverage, pick_next_pair, rank_submissions
from ..schemas import (
    PairOut,
    PairwiseComparisonOut,
    PairwiseCoverageOut,
    PairwiseJudgeProgressRow,
    PairwiseResultRow,
    PairwiseResultsOut,
    PairwiseVoteIn,
)
from ..serializers import submission_out
from .events import readable_event

router = APIRouter(prefix="/api/events", tags=["pairwise"])


def _own_judge(db: Session, event_id: uuid.UUID, user_id: uuid.UUID) -> Judge:
    """This caller's own `Judge` row on this event, or a 404.

    404, not 403: whether a stranger happens to also be a judge somewhere else
    is not information this route leaks, and "you are not a judge here" reads
    the same as "no such judge" from the outside either way.
    """
    judge = db.execute(
        select(Judge)
        .options(selectinload(Judge.track))
        .where(Judge.event_id == event_id, Judge.user_id == user_id)
    ).scalar_one_or_none()
    if judge is None:
        raise HTTPException(status_code=404, detail="You are not a judge on this event")
    return judge


def _eligible_submission_ids(db: Session, event_id: uuid.UUID, judge: Judge) -> list[uuid.UUID]:
    """Submitted projects this judge may be shown: in their track if they have
    one, never their own team's -- the same conflict-of-interest rule
    `app/assignment.py` enforces for scored assignment, applied here at query
    time since pairwise has no assignment row to enforce it through -- and
    never one they are individually recused from (`models.JudgeRecusal`),
    the manually-recorded conflict on top of the structural, team-based one.
    """
    own_team_ids = set(
        db.execute(
            select(Team.id)
            .join(TeamMember, TeamMember.team_id == Team.id)
            .where(Team.event_id == event_id, TeamMember.user_id == judge.user_id)
        ).scalars()
    )
    recused_submission_ids = set(
        db.execute(
            select(JudgeRecusal.submission_id).where(JudgeRecusal.judge_id == judge.id)
        ).scalars()
    )
    stmt = select(Submission.id).where(
        Submission.event_id == event_id, Submission.status == SubmissionStatus.SUBMITTED
    )
    if judge.track_id is not None:
        stmt = stmt.where(Submission.track_id == judge.track_id)
    if own_team_ids:
        stmt = stmt.where(Submission.team_id.notin_(own_team_ids))
    if recused_submission_ids:
        stmt = stmt.where(Submission.id.notin_(recused_submission_ids))
    return list(db.execute(stmt).scalars())


def _load_submission_pair(
    db: Session, event_id: uuid.UUID, ids: tuple[uuid.UUID, uuid.UUID]
) -> tuple[Submission, Submission]:
    rows = {
        s.id: s
        for s in db.execute(
            select(Submission)
            .options(
                selectinload(Submission.team),
                selectinload(Submission.track),
                selectinload(Submission.answers),
            )
            .where(Submission.event_id == event_id, Submission.id.in_(ids))
        ).scalars()
    }
    return rows[ids[0]], rows[ids[1]]


@router.get("/{slug}/pairwise/next", response_model=PairOut)
def next_pair(
    slug: str,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db),
) -> PairOut:
    """The next two projects for this judge to compare.

    409, not an empty response, when fewer than two eligible projects exist:
    that is a real state (an event with one submission, or a track judge whose
    entire track has one project) and a caller needs to be able to tell it
    apart from "you are not allowed here" or "loading".
    """
    event = readable_event(db, slug, principal)
    user = require_authenticated(principal)
    judge = _own_judge(db, event.id, user.id)
    require_access(principal, judge, Action.COMPARE)

    eligible = _eligible_submission_ids(db, event.id, judge)
    if len(eligible) < 2:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Not enough eligible projects to compare yet",
        )

    counts: dict[uuid.UUID, int] = {sid: 0 for sid in eligible}
    for a_id, b_id in db.execute(
        select(PairwiseComparison.submission_a_id, PairwiseComparison.submission_b_id).where(
            PairwiseComparison.event_id == event.id
        )
    ):
        if a_id in counts:
            counts[a_id] += 1
        if b_id in counts:
            counts[b_id] += 1

    seen: set[frozenset[uuid.UUID]] = {
        frozenset((a_id, b_id))
        for a_id, b_id in db.execute(
            select(
                PairwiseComparison.submission_a_id, PairwiseComparison.submission_b_id
            ).where(
                PairwiseComparison.event_id == event.id, PairwiseComparison.judge_id == judge.id
            )
        )
    }

    pair = pick_next_pair(eligible, total_comparisons=counts, already_seen_by_this_judge=seen)
    assert pair is not None  # `eligible` has >= 2 entries, checked above

    a, b = _load_submission_pair(db, event.id, pair)
    return PairOut(
        submission_a=submission_out(a, principal),
        submission_b=submission_out(b, principal),
    )


@router.post("/{slug}/pairwise/compare", response_model=PairwiseComparisonOut, status_code=201)
def compare(
    slug: str,
    payload: PairwiseVoteIn,
    request: Request,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db),
) -> PairwiseComparisonOut:
    """Record this judge's answer. Every field is re-checked against the
    database, not just against the Pydantic model: a client that fabricated a
    pair it was never shown must not be able to write a comparison for it.
    """
    event = readable_event(db, slug, principal)
    user = require_authenticated(principal)
    judge = _own_judge(db, event.id, user.id)
    require_access(principal, judge, Action.COMPARE)

    eligible = set(_eligible_submission_ids(db, event.id, judge))
    if payload.submission_a_id not in eligible or payload.submission_b_id not in eligible:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "One or both of these projects are not eligible for this judge to "
                "compare -- not submitted, outside this judge's track, or one of "
                "this judge's own team's projects"
            ),
        )

    entry = PairwiseComparison(
        event_id=event.id,
        judge_id=judge.id,
        submission_a_id=payload.submission_a_id,
        submission_b_id=payload.submission_b_id,
        winner_id=payload.winner_id,
    )
    db.add(entry)
    db.flush()

    a, b = _load_submission_pair(db, event.id, (payload.submission_a_id, payload.submission_b_id))
    if payload.winner_id == payload.submission_a_id:
        verb = f"preferred {quote(a.name)} over {quote(b.name)}"
    elif payload.winner_id == payload.submission_b_id:
        verb = f"preferred {quote(b.name)} over {quote(a.name)}"
    else:
        verb = f"could not decide between {quote(a.name)} and {quote(b.name)}"
    record(
        db,
        action=AuditAction.PAIRWISE_COMPARED,
        summary=f"{user.email} {verb} on {event.slug}",
        principal=principal,
        event=event,
        resource_type="pairwise_comparison",
        resource_id=entry.id,
        request=request,
    )
    db.commit()
    db.refresh(entry)
    return PairwiseComparisonOut(
        id=entry.id,
        submission_a_id=entry.submission_a_id,
        submission_b_id=entry.submission_b_id,
        winner_id=entry.winner_id,
        created_at=entry.created_at,
    )


@router.get("/{slug}/pairwise/results", response_model=PairwiseResultsOut)
def pairwise_results(
    slug: str,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db),
) -> PairwiseResultsOut:
    """The fitted ranking. Every submitted project appears, including one with
    zero comparisons yet -- see `app/pairwise.py` on why cold start is ranked
    at the anchor rather than omitted."""
    event = readable_event(db, slug, principal)
    require_access(principal, event, Action.READ_RESULTS)

    submissions = list(
        db.execute(
            select(Submission)
            .options(selectinload(Submission.team), selectinload(Submission.track))
            .where(Submission.event_id == event.id, Submission.status == SubmissionStatus.SUBMITTED)
        ).scalars()
    )
    by_id = {s.id: s for s in submissions}

    rows = db.execute(
        select(
            PairwiseComparison.submission_a_id,
            PairwiseComparison.submission_b_id,
            PairwiseComparison.winner_id,
            PairwiseComparison.judge_id,
        ).where(PairwiseComparison.event_id == event.id)
    ).all()
    comparisons = [Comparison(a_id=a, b_id=b, winner_id=w) for a, b, w, _j in rows]

    fitted = rank_submissions([s.id for s in submissions], comparisons)

    result_rows = [
        PairwiseResultRow(
            submission_id=row.submission_id,
            submission_name=by_id[row.submission_id].name,
            team_name=by_id[row.submission_id].team.name,
            track=by_id[row.submission_id].track.name if by_id[row.submission_id].track else None,
            rank=row.rank,
            rating=row.rating,
            strength=row.strength,
            n_comparisons=row.n_comparisons,
            wins=row.wins,
            losses=row.losses,
            win_rate=row.win_rate,
        )
        for row in fitted.submissions
    ]

    per_judge: dict[uuid.UUID, int] = {}
    for _a, _b, _w, judge_id in rows:
        per_judge[judge_id] = per_judge.get(judge_id, 0) + 1
    judges = list(
        db.execute(
            select(Judge).options(selectinload(Judge.user)).where(Judge.event_id == event.id)
        ).scalars()
    )
    judge_rows = [
        PairwiseJudgeProgressRow(
            judge_id=j.id, judge_name=j.user.display_name, n_comparisons=per_judge.get(j.id, 0)
        )
        for j in judges
    ]

    cov = coverage(fitted)

    return PairwiseResultsOut(
        event_slug=event.slug,
        method="Bradley-Terry (MM), anchor-regularized -- see app/pairwise.py",
        total_comparisons=fitted.total_comparisons,
        converged=fitted.converged,
        coverage=PairwiseCoverageOut(
            total_submissions=cov.total_submissions,
            total_comparisons=cov.total_comparisons,
            min_comparisons=cov.min_comparisons,
            max_comparisons=cov.max_comparisons,
            mean_comparisons=round(cov.mean_comparisons, 2),
            coverage_pct=round(cov.coverage_pct, 1),
        ),
        rows=result_rows,
        judges=judge_rows,
    )
