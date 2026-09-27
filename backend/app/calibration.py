"""Judge calibration: are this judge's scores systematically harsher or more
generous than the organizer's own?

Pure functions over plain numbers -- no ORM, no session, no HTTP -- for the same
reason `app/scoring.py` is: the flag is the part somebody will argue with, so it
should be checkable by hand.

**The method, kept deliberately small.** Before real judging, each judge scores a
few *practice projects* whose expected scores an organizer fixed in advance. For
every criterion of every practice project the judge fully scored, the deviation
is `(judge - expected) / (criterion max - criterion min)` -- a fraction of that
criterion's own range, so a 1-5 criterion and a 0-10 one contribute on the same
scale. A judge's **mean signed deviation** is the average of those. At or below
`-CALIBRATION_THRESHOLD` they are flagged `harsh`, at or above it `generous`,
otherwise `aligned`. The **mean absolute deviation** is reported alongside as a
separate number, because a judge who scores one project far too high and another
far too low can average to zero and still be uncalibrated.

**What this does not claim.** It is a flag for an organizer to read, never a
correction: nothing here changes a judge's real scores, and calibration scores
never reach `gather()` or the results. `normalize()` already corrects for a
harsh or generous judge *after the fact* by standardising within each judge; this
is the earlier, coarser question of who to talk to *before* judging starts. With
one or two practice projects the estimate is noisy, and the threshold is a
starting point an organizer should feel free to distrust, not a finding.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

# 0.10 of a criterion's range: 0.4 points on a 1-5 rubric. Large enough that one
# rounded score does not trip it, small enough that a judge who is consistently
# a whole point off on a 1-5 rubric (0.25) is unmistakable.
CALIBRATION_THRESHOLD = 0.10


@dataclass(frozen=True)
class CalibrationCriterion:
    id: uuid.UUID
    min_score: float
    max_score: float


@dataclass(frozen=True)
class Assessment:
    projects_total: int
    projects_scored: int
    mean_signed_deviation: float | None
    mean_abs_deviation: float | None
    verdict: str  # not_started | incomplete | harsh | generous | aligned


def assess(
    *,
    project_ids: list[uuid.UUID],
    criteria: list[CalibrationCriterion],
    expected: dict[tuple[uuid.UUID, uuid.UUID], float],
    given: dict[tuple[uuid.UUID, uuid.UUID], float],
    threshold: float = CALIBRATION_THRESHOLD,
) -> Assessment:
    """One judge's calibration. Keys are `(project_id, criterion_id)`.

    A project counts only if the judge scored *every* criterion of it: half a
    practice ballot is not a calibration sample any more than half a real one is
    a judgement. The verdict is `incomplete` until every practice project is
    counted, so a flag is never issued off a judge who has only scored the one
    project they found easiest.
    """
    signed: list[float] = []
    scored = 0
    for project_id in project_ids:
        deviations = []
        for c in criteria:
            key = (project_id, c.id)
            if key not in given or key not in expected:
                deviations = []
                break
            span = c.max_score - c.min_score
            deviations.append((given[key] - expected[key]) / span)
        if deviations:
            scored += 1
            signed.extend(deviations)

    total = len(project_ids)
    if scored == 0:
        return Assessment(total, 0, None, None, "not_started")

    mean_signed = sum(signed) / len(signed)
    mean_abs = sum(abs(d) for d in signed) / len(signed)
    if scored < total:
        verdict = "incomplete"
    elif mean_signed <= -threshold:
        verdict = "harsh"
    elif mean_signed >= threshold:
        verdict = "generous"
    else:
        verdict = "aligned"
    return Assessment(total, scored, mean_signed, mean_abs, verdict)
