"""Judge calibration: practice projects with expected scores, and who they flag.

The smallest version of the idea that still works end to end. An organizer adds
one or two practice projects with the scores they expect for each criterion; each
judge scores them before real judging; the organizer reads a report of who ran
harsh or generous. The maths and its honest limits are in `app/calibration.py`.

Two audiences, two routers, and one rule that holds across both: **a judge never
sees an expected score.** The judge-facing shape (`CalibrationPracticeOut`) has no
field that could carry one, so there is no query to get wrong. Calibration scores
also live in their own tables and never touch `gather()`, so practising cannot move
a real result.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from ..access import Action, Principal, require_authenticated
from ..audit import quote, record
from ..calibration import CALIBRATION_THRESHOLD, CalibrationCriterion, assess
from ..db import get_db
from ..deps import get_current_principal
from ..models import (
    AuditAction,
    CalibrationExpected,
    CalibrationProject,
    CalibrationScore,
    Event,
    Judge,
    RubricCriterion,
)
from ..schemas import (
    CalibrationJudgeRow,
    CalibrationPracticeOut,
    CalibrationProjectIn,
    CalibrationProjectOut,
    CalibrationReportOut,
    CalibrationScoresIn,
    CalibrationValueIn,
    CriterionOut,
)
from .judges import _staff_event

router = APIRouter(prefix="/api/events", tags=["calibration"])
judge_router = APIRouter(prefix="/api/judging", tags=["calibration"])


def _check_values(
    values: list[CalibrationValueIn],
    criteria: dict[uuid.UUID, RubricCriterion],
    *,
    require_all: bool,
) -> None:
    """The event's own rubric, applied: right criteria, in range, no repeats."""
    seen: set[uuid.UUID] = set()
    for v in values:
        criterion = criteria.get(v.criterion_id)
        if criterion is None:
            raise HTTPException(
                status_code=422,
                detail=f"Criterion {v.criterion_id} does not belong to this event",
            )
        if v.criterion_id in seen:
            raise HTTPException(status_code=422, detail=f"{criterion.name} appears twice")
        seen.add(v.criterion_id)
        if not criterion.min_score <= v.value <= criterion.max_score:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"{criterion.name}: {v.value} is outside "
                    f"{criterion.min_score}-{criterion.max_score}"
                ),
            )
    if require_all and seen != set(criteria):
        raise HTTPException(
            status_code=422, detail="Give an expected score for every criterion"
        )


def _event_criteria(db: Session, event: Event) -> dict[uuid.UUID, RubricCriterion]:
    rows = db.execute(
        select(RubricCriterion).where(RubricCriterion.event_id == event.id)
    ).scalars()
    return {c.id: c for c in rows}


def _project_out(project: CalibrationProject, slug: str) -> CalibrationProjectOut:
    return CalibrationProjectOut(
        id=project.id,
        event_slug=slug,
        name=project.name,
        description=project.description,
        expected=[
            CalibrationValueIn(criterion_id=e.criterion_id, value=float(e.value))
            for e in project.expected
        ],
        created_at=project.created_at,
    )


# --------------------------------------------------------------------------- #
# Organizer: practice projects and the report
# --------------------------------------------------------------------------- #


@router.post(
    "/{slug}/calibration/projects",
    response_model=CalibrationProjectOut,
    status_code=status.HTTP_201_CREATED,
)
def add_project(
    slug: str,
    payload: CalibrationProjectIn,
    request: Request,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db),
) -> CalibrationProjectOut:
    event = _staff_event(db, slug, principal, Action.MANAGE)
    criteria = _event_criteria(db, event)
    if not criteria:
        raise HTTPException(
            status_code=409, detail="Add rubric criteria before adding practice projects"
        )
    _check_values(payload.expected, criteria, require_all=True)

    project = CalibrationProject(
        event_id=event.id, name=payload.name.strip(), description=payload.description
    )
    project.expected = [
        CalibrationExpected(
            criterion_id=v.criterion_id, event_id=event.id, value=Decimal(str(v.value))
        )
        for v in payload.expected
    ]
    db.add(project)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=409, detail="A practice project with that name already exists"
        ) from None

    record(
        db,
        action=AuditAction.CALIBRATION_PROJECT_ADDED,
        summary=f"{principal.email} added practice project {quote(project.name)} on {event.slug}",
        principal=principal,
        event=event,
        resource_type="calibration_project",
        resource_id=project.id,
        request=request,
    )
    db.commit()
    db.refresh(project)
    return _project_out(project, event.slug)


@router.get("/{slug}/calibration/projects", response_model=list[CalibrationProjectOut])
def list_projects(
    slug: str,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db),
) -> list[CalibrationProjectOut]:
    event = _staff_event(db, slug, principal, Action.READ_RESULTS)
    projects = (
        db.execute(
            select(CalibrationProject)
            .options(selectinload(CalibrationProject.expected))
            .where(CalibrationProject.event_id == event.id)
            .order_by(CalibrationProject.created_at)
        )
        .scalars()
        .all()
    )
    return [_project_out(p, event.slug) for p in projects]


@router.delete("/{slug}/calibration/projects/{project_id}", status_code=204)
def remove_project(
    slug: str,
    project_id: uuid.UUID,
    request: Request,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db),
) -> Response:
    event = _staff_event(db, slug, principal, Action.MANAGE)
    project = db.execute(
        select(CalibrationProject).where(
            CalibrationProject.id == project_id, CalibrationProject.event_id == event.id
        )
    ).scalar_one_or_none()
    if project is None:
        raise HTTPException(status_code=404, detail="Practice project not found")
    name = project.name
    db.delete(project)
    record(
        db,
        action=AuditAction.CALIBRATION_PROJECT_REMOVED,
        summary=f"{principal.email} removed practice project {quote(name)} on {event.slug}",
        principal=principal,
        event=event,
        resource_type="calibration_project",
        resource_id=project_id,
        request=request,
    )
    db.commit()
    return Response(status_code=204)


@router.get("/{slug}/calibration/report", response_model=CalibrationReportOut)
def report(
    slug: str,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db),
) -> CalibrationReportOut:
    """Every active judge on the event, with their calibration verdict."""
    event = _staff_event(db, slug, principal, Action.READ_RESULTS)
    criteria = _event_criteria(db, event)
    cal_criteria = [
        CalibrationCriterion(c.id, c.min_score, c.max_score)
        for c in sorted(criteria.values(), key=lambda c: (c.position, c.key))
    ]

    project_ids = list(
        db.execute(
            select(CalibrationProject.id).where(CalibrationProject.event_id == event.id)
        ).scalars()
    )
    expected = {
        (e.project_id, e.criterion_id): float(e.value)
        for e in db.execute(
            select(CalibrationExpected).where(CalibrationExpected.event_id == event.id)
        ).scalars()
    }
    given_by_judge: dict[uuid.UUID, dict[tuple[uuid.UUID, uuid.UUID], float]] = {}
    for s in db.execute(
        select(CalibrationScore).where(CalibrationScore.event_id == event.id)
    ).scalars():
        given_by_judge.setdefault(s.judge_id, {})[(s.project_id, s.criterion_id)] = float(s.value)

    judges = (
        db.execute(
            select(Judge)
            .options(selectinload(Judge.user))
            .where(Judge.event_id == event.id, Judge.is_active.is_(True))
            .order_by(Judge.invited_at)
        )
        .scalars()
        .all()
    )
    rows = []
    for judge in judges:
        a = assess(
            project_ids=project_ids,
            criteria=cal_criteria,
            expected=expected,
            given=given_by_judge.get(judge.id, {}),
        )
        rows.append(
            CalibrationJudgeRow(
                judge_id=judge.id,
                judge_name=judge.user.display_name,
                projects_scored=a.projects_scored,
                projects_total=a.projects_total,
                mean_signed_deviation=(
                    None if a.mean_signed_deviation is None else round(a.mean_signed_deviation, 4)
                ),
                mean_abs_deviation=(
                    None if a.mean_abs_deviation is None else round(a.mean_abs_deviation, 4)
                ),
                verdict=a.verdict,
            )
        )
    return CalibrationReportOut(
        event_slug=event.slug,
        threshold=CALIBRATION_THRESHOLD,
        projects_total=len(project_ids),
        rows=rows,
    )


# --------------------------------------------------------------------------- #
# Judge: score the practice projects
# --------------------------------------------------------------------------- #


def _practice_out(
    project: CalibrationProject,
    slug: str,
    criteria: list[RubricCriterion],
    mine: dict[uuid.UUID, float],
) -> CalibrationPracticeOut:
    return CalibrationPracticeOut(
        id=project.id,
        event_slug=slug,
        name=project.name,
        description=project.description,
        criteria=[CriterionOut.model_validate(c) for c in criteria],
        my_scores=[CalibrationValueIn(criterion_id=cid, value=v) for cid, v in mine.items()],
        complete=all(c.id in mine for c in criteria),
    )


@judge_router.get("/calibration", response_model=list[CalibrationPracticeOut])
def my_practice_projects(
    event: str | None = Query(default=None, description="Restrict to one event slug"),
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db),
) -> list[CalibrationPracticeOut]:
    """The practice projects for every event this caller actively judges."""
    user = require_authenticated(principal)
    stmt = (
        select(Judge)
        .options(selectinload(Judge.event))
        .where(Judge.user_id == user.id, Judge.is_active.is_(True))
    )
    out: list[CalibrationPracticeOut] = []
    for judge in db.execute(stmt).scalars().all():
        if event and judge.event.slug != event:
            continue
        criteria = sorted(
            _event_criteria(db, judge.event).values(), key=lambda c: (c.position, c.key)
        )
        projects = db.execute(
            select(CalibrationProject)
            .where(CalibrationProject.event_id == judge.event_id)
            .order_by(CalibrationProject.created_at)
        ).scalars()
        mine: dict[uuid.UUID, dict[uuid.UUID, float]] = {}
        for s in db.execute(
            select(CalibrationScore).where(CalibrationScore.judge_id == judge.id)
        ).scalars():
            mine.setdefault(s.project_id, {})[s.criterion_id] = float(s.value)
        for project in projects:
            out.append(_practice_out(project, judge.event.slug, criteria, mine.get(project.id, {})))
    return out


@judge_router.put("/calibration/{project_id}/scores", response_model=CalibrationPracticeOut)
def put_practice_scores(
    project_id: uuid.UUID,
    payload: CalibrationScoresIn,
    request: Request,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db),
) -> CalibrationPracticeOut:
    """Idempotent: re-scoring a practice project updates in place. The caller is
    resolved to *their own* judge record for the project's event -- there is no
    way to name a judge, so there is no way to score as someone else."""
    user = require_authenticated(principal)
    project = db.execute(
        select(CalibrationProject).where(CalibrationProject.id == project_id)
    ).scalar_one_or_none()
    judge = None
    if project is not None:
        judge = db.execute(
            select(Judge)
            .options(selectinload(Judge.event))
            .where(
                Judge.event_id == project.event_id,
                Judge.user_id == user.id,
                Judge.is_active.is_(True),
            )
        ).scalar_one_or_none()
    if project is None or judge is None:
        # Same answer for "no such project" and "not your event", so a judge
        # cannot probe for other events' practice projects.
        raise HTTPException(status_code=404, detail="Practice project not found")

    criteria = _event_criteria(db, judge.event)
    _check_values(payload.scores, criteria, require_all=False)

    existing = {
        s.criterion_id: s
        for s in db.execute(
            select(CalibrationScore).where(
                CalibrationScore.project_id == project.id,
                CalibrationScore.judge_id == judge.id,
            )
        ).scalars()
    }
    for v in payload.scores:
        row = existing.get(v.criterion_id)
        if row is None:
            row = CalibrationScore(
                event_id=project.event_id,
                project_id=project.id,
                judge_id=judge.id,
                criterion_id=v.criterion_id,
                value=Decimal(str(v.value)),
            )
            db.add(row)
            existing[v.criterion_id] = row
        else:
            row.value = Decimal(str(v.value))

    record(
        db,
        action=AuditAction.CALIBRATION_SCORED,
        summary=(
            f"{user.email} scored practice project {quote(project.name)} "
            f"on {judge.event.slug}"
        ),
        principal=principal,
        event=judge.event,
        resource_type="calibration_project",
        resource_id=project.id,
        request=request,
    )
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=409, detail="Those scores were updated by a concurrent request. Resend."
        ) from None

    ordered = sorted(criteria.values(), key=lambda c: (c.position, c.key))
    return _practice_out(
        project, judge.event.slug, ordered, {cid: float(r.value) for cid, r in existing.items()}
    )
