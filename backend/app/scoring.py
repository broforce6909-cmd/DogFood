"""The weighted rubric and cross-judge normalization.

Pure functions over plain dataclasses: no ORM, no session, no HTTP. The maths in
here is the part of the project most likely to be argued with, so it is written
to be read and tested in isolation, and `tests/test_scoring.py` exercises it with
hand-checkable numbers rather than fixtures.

The full argument, including what this does about the judge who marks everything
a 3, is in `JUDGING.md`. This module is the implementation of that document and
the two are meant to be read together.

Two deliberate choices worth naming up front:

* **Weights are divided by their own sum at read time**, never baked into stored
  scores. An organizer who edits one weight changes the ranking from that moment
  on; they do not silently rescale ballots that were already cast.
* **A judge with no spread contributes no ordering.** Not a fudge factor, not an
  epsilon in a denominator -- an explicit rule with its own branch, because
  dividing by a judge's zero standard deviation is the single most likely way
  for a normalization implementation to be quietly wrong.
"""

from __future__ import annotations

import math
import uuid
from dataclasses import dataclass, field
from datetime import datetime

# How strongly a judge's own statistics are pulled toward the global ones when
# they have only scored a handful of projects. `k` is "the number of imaginary
# average-judge observations added to every judge's record": with k = 3 a judge
# with 3 ballots is weighted half on themselves and half on the field, and a
# judge with 30 is weighted 91% on themselves. See JUDGING.md for why a constant
# rather than an estimated prior.
SHRINKAGE_K = 3.0

# Below this, a standard deviation is treated as exactly zero. Raw scores live on
# a 1-5ish scale, so 1e-9 is "the judge used one value", not "the judge was
# nearly flat".
EPSILON = 1e-9


# --------------------------------------------------------------------------- #
# Inputs
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Criterion:
    """One rubric row. `weight` is as the organizer typed it, unnormalised."""

    id: uuid.UUID
    key: str
    name: str
    weight: float
    min_score: int = 1
    max_score: int = 5


@dataclass(frozen=True)
class Ballot:
    """One judge's completed review of one submission.

    `scores` maps criterion id -> the value that judge gave. A ballot missing a
    criterion is not an error here; `raw_score` weights over whatever is present,
    so a partially completed ballot degrades rather than explodes. The routes
    refuse to mark such a ballot complete, which is where that rule belongs.
    """

    judge_id: uuid.UUID
    submission_id: uuid.UUID
    scores: dict[uuid.UUID, float]


# --------------------------------------------------------------------------- #
# Outputs
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class JudgeCalibration:
    """What we inferred about one judge's use of the scale.

    Published to organizers verbatim, because "your normalization moved my
    project down four places" deserves an answer more specific than "statistics".

    `track_id` is `None` from a plain `normalize()` call (one population, no
    track distinction to make). `normalize_by_track()` sets it to the track
    that population was scoped to -- `None` there specifically means "the
    untracked-submissions group", not "no track information available". A
    judge with no track restriction produces one `JudgeCalibration` *per track
    they actually scored in*, each with a different `track_id`: their
    calibration in one track says nothing about how they used the scale in
    another, which is the entire reason this function exists.
    """

    judge_id: uuid.UUID
    n: int
    mean: float
    sd: float
    shrunk_mean: float
    shrunk_sd: float
    flat: bool
    track_id: uuid.UUID | None = None

    @property
    def note(self) -> str:
        if self.flat:
            return "used a single value; contributes no ordering"
        if self.n <= SHRINKAGE_K:
            return f"only {self.n} ballot(s); heavily shrunk toward the field"
        return "calibrated on own distribution"


@dataclass
class SubmissionResult:
    submission_id: uuid.UUID
    n_reviews: int
    raw_mean: float
    normalized_mean: float
    per_judge: dict[uuid.UUID, float] = field(default_factory=dict)
    raw_rank: int = 0
    normalized_rank: int = 0

    @property
    def rank_delta(self) -> int:
        """Positive means normalization moved this project *up* the table."""
        return self.raw_rank - self.normalized_rank


@dataclass(frozen=True)
class Results:
    submissions: list[SubmissionResult]
    calibrations: list[JudgeCalibration]
    global_mean: float
    global_sd: float

    def by_id(self, submission_id: uuid.UUID) -> SubmissionResult | None:
        for row in self.submissions:
            if row.submission_id == submission_id:
                return row
        return None


# --------------------------------------------------------------------------- #
# Inter-rater reliability
# --------------------------------------------------------------------------- #

# A submission is flagged when its judges' *normalized* scores differ, on
# average pairwise, by more than this many points on the rubric's own 1-5ish
# scale. Normalized rather than raw, deliberately: two judges giving raw 2 and
# 4 might just be one harsh and one generous judge agreeing perfectly about
# which project is better, and normalization already exists to say so. If they
# still differ by this much *after* removing each judge's own scale, that is
# disagreement about the project, not about the scale -- the signal an
# organizer actually wants "needs a third opinion" to mean.
#
# A fixed constant, not a percentile of this event's own spread: a percentile
# always flags *something* (there is always a top decile), which is the wrong
# shape for a signal that should say "this event has no real disagreement"
# when that happens to be true. 1.5 points is a little under a third of a
# typical 1-5 range and is exactly what JUDGING.md's own worked example checks
# against with the seeded fixture's own harsh/generous/flat judges.
DISAGREEMENT_THRESHOLD = 1.5


@dataclass(frozen=True)
class DisagreementRow:
    """One submission's judges, and how much they actually disagreed.

    `mean_abs_pairwise_diff` is the metric an organizer can explain to a judge
    who asks "why was I flagged" without a statistics degree: the average, over
    every pair of judges who reviewed this project, of how many points apart
    their normalized scores were. A population standard deviation would answer
    the same question with a number nobody outside this codebase would
    recognise; mean absolute pairwise difference is intelligible on its own,
    which is worth more here than being a more "standard" statistic that would
    need translating back into English on every results page anyway.
    """

    submission_id: uuid.UUID
    n_reviews: int
    sd: float
    mean_abs_pairwise_diff: float
    needs_review: bool


@dataclass(frozen=True)
class JudgeDeviation:
    """How far one judge's *normalized* scores sit from the consensus on the
    projects they reviewed -- a different question from `JudgeCalibration`,
    deliberately.

    Calibration describes how a judge uses the 1-5 scale (harsh, generous,
    flat) -- exactly the thing normalization exists to correct for, and a high
    calibration spread is not a problem, it is the input the method consumes.
    This describes something normalization cannot fix: a judge whose
    *normalized* scores -- already corrected for their own scale -- still sit
    far from what their co-reviewers said about the same projects. That is
    disagreement with the room, not a scale quirk, and it survives exactly the
    correction that makes calibration differences disappear.
    """

    judge_id: uuid.UUID
    n_reviews: int
    mean_abs_deviation: float


# --------------------------------------------------------------------------- #
# 1. The weighted rubric
# --------------------------------------------------------------------------- #


def raw_score(ballot: Ballot, criteria: list[Criterion]) -> float | None:
    """Weighted mean of one ballot's criterion scores.

        raw = Σ_c  value(c) × weight(c)  /  Σ_c weight(c)

    Dividing by the sum of the weights that were *actually scored* keeps the
    result on the same scale as the criteria themselves, so a 1-5 rubric produces
    a 1-5 number whatever the weights are. Returns None for a ballot with nothing
    on it, which is different from a ballot that scored zero.
    """
    total = 0.0
    weight_sum = 0.0
    for criterion in criteria:
        value = ballot.scores.get(criterion.id)
        if value is None:
            continue
        total += float(value) * criterion.weight
        weight_sum += criterion.weight
    if weight_sum <= 0:
        return None
    return total / weight_sum


# --------------------------------------------------------------------------- #
# 2. Descriptive statistics
# --------------------------------------------------------------------------- #


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _sd(values: list[float]) -> float:
    """Population standard deviation (ddof=0).

    We are describing the spread of the ballots a judge actually cast, not
    inferring the variance of a population they were sampled from, so dividing by
    n is the honest choice. It also means a judge with one ballot has sd 0 rather
    than undefined, and the flat-judge rule below then handles them.
    """
    if len(values) < 2:
        return 0.0
    mu = _mean(values)
    return math.sqrt(sum((v - mu) ** 2 for v in values) / len(values))


def _shrink(own: float, global_value: float, n: int, k: float | None = None) -> float:
    """Pull a judge's statistic toward the field in proportion to how little we
    know about them:

        shrunk = (n × own + k × global) / (n + k)

    n → ∞ recovers `own`; n = 0 gives `global`; n = k splits the difference.

    `k` defaults to the module constant, read at call time rather than bound as a
    default argument, so a test can turn shrinkage off and exercise the z-score
    on its own.
    """
    if k is None:
        k = SHRINKAGE_K
    return (n * own + k * global_value) / (n + k)


# --------------------------------------------------------------------------- #
# 3. Cross-judge normalization
# --------------------------------------------------------------------------- #


def normalize(
    ballots: list[Ballot], criteria: list[Criterion], *, track_id: uuid.UUID | None = None
) -> Results:
    """Raw scores in, normalized scores and rankings out.

    The method is per-judge z-score standardisation with shrinkage, rescaled back
    into the original units:

        z(s, j)    = ( raw(s, j) − μ̂_j ) / σ̂_j
        norm(s, j) = μ_G + σ_G × z(s, j)

    where μ̂_j and σ̂_j are judge j's mean and standard deviation shrunk toward
    the global ones, and μ_G, σ_G are those global statistics. A submission's
    score is the mean of `norm` over the judges who reviewed it.

    Rescaling by μ_G and σ_G is cosmetic but not pointless: it puts the output
    back on the 1-5 scale the rubric is written in, so an organizer reading
    "4.1" knows what it means. Rankings are identical either way.
    """
    per_judge_raw: dict[uuid.UUID, list[float]] = {}
    per_submission_raw: dict[uuid.UUID, dict[uuid.UUID, float]] = {}

    for ballot in ballots:
        value = raw_score(ballot, criteria)
        if value is None:
            continue
        per_judge_raw.setdefault(ballot.judge_id, []).append(value)
        per_submission_raw.setdefault(ballot.submission_id, {})[ballot.judge_id] = value

    pooled = [v for values in per_judge_raw.values() for v in values]
    global_mean = _mean(pooled)
    global_sd = _sd(pooled)

    # -- per-judge calibration -------------------------------------------- #
    calibrations: dict[uuid.UUID, JudgeCalibration] = {}
    for judge_id, values in per_judge_raw.items():
        n = len(values)
        own_mean, own_sd = _mean(values), _sd(values)
        # The judge who marks everything a 3. Their ballots are internally
        # indistinguishable, so they carry no information about which project is
        # better -- and that, not a division by something small, is what we
        # record. Note this is the judge's *own* sd, before shrinkage: borrowing
        # the field's spread would invent an ordering they never expressed.
        flat = own_sd < EPSILON
        calibrations[judge_id] = JudgeCalibration(
            judge_id=judge_id,
            n=n,
            mean=own_mean,
            sd=own_sd,
            shrunk_mean=_shrink(own_mean, global_mean, n),
            shrunk_sd=_shrink(own_sd, global_sd, n),
            flat=flat,
            track_id=track_id,
        )

    # -- normalized score per (submission, judge) -------------------------- #
    results: list[SubmissionResult] = []
    for submission_id, by_judge in per_submission_raw.items():
        normalized: dict[uuid.UUID, float] = {}
        for judge_id, raw in by_judge.items():
            cal = calibrations[judge_id]
            if cal.flat or cal.shrunk_sd < EPSILON:
                z = 0.0
            else:
                z = (raw - cal.shrunk_mean) / cal.shrunk_sd
            normalized[judge_id] = global_mean + global_sd * z
        results.append(
            SubmissionResult(
                submission_id=submission_id,
                n_reviews=len(by_judge),
                raw_mean=_mean(list(by_judge.values())),
                normalized_mean=_mean(list(normalized.values())),
                per_judge=normalized,
            )
        )

    _rank(results, key=lambda r: r.raw_mean, assign="raw_rank")
    _rank(results, key=lambda r: r.normalized_mean, assign="normalized_rank")
    results.sort(key=lambda r: r.normalized_rank)

    return Results(
        submissions=results,
        calibrations=sorted(calibrations.values(), key=lambda c: c.judge_id.hex),
        global_mean=global_mean,
        global_sd=global_sd,
    )


def normalize_by_track(
    ballots: list[Ballot],
    criteria: list[Criterion],
    track_of: dict[uuid.UUID, uuid.UUID | None],
) -> Results:
    """`normalize()`, scoped to a track's own judge pool -- the fix for the gap
    named since Phase 2 and finally closed here: JUDGING.md §3 (and
    ARCHITECTURE.md's "Open questions") named that a track judge's z-score was
    computed against a global mean pooling in tracks they never saw, which
    biases the result if tracks differ in true quality rather than just judge
    harshness -- normalization cannot tell "this track's projects are weaker"
    from "this judge is harsh" if it is shown both tracks' scores at once.

    **The fix**: partition ballots by the *submission's* track (not the
    judge's -- a judge with no track restriction reviews across tracks, and
    each of their ballots belongs to whichever track that submission is in),
    and run the exact same `normalize()` independently within each partition.
    A judge is now shrunk toward, and z-scored against, only the judges who
    scored the *same track* -- exactly the population they are actually
    comparable to.

    **What this does not solve, named plainly**: each track's rescaled output
    uses that track's *own* mean/sd, so "4.1" in one track and "4.1" in
    another are not the same claim -- there is no way to make them the same
    claim without assuming the tracks are of identical true quality, which is
    exactly the assumption this function exists to stop making. Ranks are
    still comparable (rank 1 beat rank 2 *within a track's own comparison
    basis*), and that is what a combined list below sorts by. An organizer who
    wants directly comparable numbers across tracks wants one track, or one
    prize per track -- both already-supported shapes.

    Submissions with no track (`track_of[id] is None`, or an event with no
    tracks at all) form their own group, so an event with zero or one track
    behaves identically to `normalize()` -- this is a strict generalisation,
    not a new code path for the common case.
    """
    groups: dict[uuid.UUID | None, list[Ballot]] = {}
    for ballot in ballots:
        track_id = track_of.get(ballot.submission_id)
        groups.setdefault(track_id, []).append(ballot)

    all_submissions: list[SubmissionResult] = []
    all_calibrations: list[JudgeCalibration] = []
    # A judge who is track-restricted appears in exactly one group; a judge who
    # sees every track appears once per track they actually scored in, with a
    # separate `JudgeCalibration` for each -- their calibration in Track A says
    # nothing about how they used the scale in Track B, which is the point.
    for track_id, group_ballots in groups.items():
        result = normalize(group_ballots, criteria, track_id=track_id)
        all_submissions.extend(result.submissions)
        all_calibrations.extend(result.calibrations)

    # One combined ranking over every track's already-normalized scores, so the
    # API and the results page still have a single list to render -- ranked by
    # what each project earned within its own track's comparison basis.
    _rank(all_submissions, key=lambda r: r.raw_mean, assign="raw_rank")
    _rank(all_submissions, key=lambda r: r.normalized_mean, assign="normalized_rank")
    all_submissions.sort(key=lambda r: r.normalized_rank)

    pooled = [v for ballot in ballots for v in [raw_score(ballot, criteria)] if v is not None]
    return Results(
        submissions=all_submissions,
        calibrations=sorted(all_calibrations, key=lambda c: c.judge_id.hex),
        global_mean=_mean(pooled),
        global_sd=_sd(pooled),
    )


def _rank(rows: list[SubmissionResult], *, key, assign: str) -> None:
    """Competition ranking: equal scores share a rank, and the next rank skips.

    Ties are resolved by submission id purely so the output is stable across runs
    -- it is not a tie-break in any meaningful sense, and equal scores really do
    get equal ranks.
    """
    ordered = sorted(rows, key=lambda r: (-key(r), r.submission_id.hex))
    previous: float | None = None
    rank = 0
    for index, row in enumerate(ordered, start=1):
        value = key(row)
        if previous is None or abs(value - previous) > EPSILON:
            rank = index
            previous = value
        setattr(row, assign, rank)


def disagreement(
    results: Results, *, threshold: float = DISAGREEMENT_THRESHOLD
) -> list[DisagreementRow]:
    """One row per submission with two or more reviews, ranked most-disagreed
    first so the organizer's "needs review" list reads top to bottom in
    priority order rather than needing a second sort.

    A submission with fewer than two reviews cannot disagree with itself --
    `sd` and `mean_abs_pairwise_diff` are both 0.0 for it, `needs_review` is
    always `False`, and it still appears (an organizer checking coverage wants
    to see "only one review yet", not have the row silently vanish).
    """
    rows = []
    for sub in results.submissions:
        values = list(sub.per_judge.values())
        if len(values) < 2:
            rows.append(
                DisagreementRow(
                    submission_id=sub.submission_id,
                    n_reviews=len(values),
                    sd=0.0,
                    mean_abs_pairwise_diff=0.0,
                    needs_review=False,
                )
            )
            continue
        pairs = [
            abs(a - b) for i, a in enumerate(values) for b in values[i + 1 :]
        ]
        mad = _mean(pairs)
        rows.append(
            DisagreementRow(
                submission_id=sub.submission_id,
                n_reviews=len(values),
                sd=_sd(values),
                mean_abs_pairwise_diff=mad,
                needs_review=mad > threshold,
            )
        )
    rows.sort(key=lambda r: (-r.mean_abs_pairwise_diff, r.submission_id.hex))
    return rows


def judge_deviation(results: Results) -> list[JudgeDeviation]:
    """One row per (judge, track) calibration in `results`, measuring how far
    that judge's normalized scores sit from the consensus on the projects they
    actually shared with other judges.

    Only submissions with two or more reviews count -- a judge's sole review of
    a project has no co-reviewer to disagree with, so it says nothing about
    this judge specifically (it already shows up, correctly, in `disagreement`
    as a low-coverage row instead). Ranked most-deviated first, the same reason
    `disagreement` is: this is a priority list, not a directory.
    """
    per_judge_deviations: dict[uuid.UUID, list[float]] = {}
    for sub in results.submissions:
        if len(sub.per_judge) < 2:
            continue
        consensus = _mean(list(sub.per_judge.values()))
        for judge_id, value in sub.per_judge.items():
            per_judge_deviations.setdefault(judge_id, []).append(abs(value - consensus))

    rows = [
        JudgeDeviation(judge_id=judge_id, n_reviews=len(devs), mean_abs_deviation=_mean(devs))
        for judge_id, devs in per_judge_deviations.items()
    ]
    rows.sort(key=lambda r: (-r.mean_abs_deviation, r.judge_id.hex))
    return rows


# --------------------------------------------------------------------------- #
# Judge consistency: signals for organizer review, never proof of anything
# --------------------------------------------------------------------------- #

# A judge with at least this many complete ballots, whose raw scores vary by
# less than this across every one of them, reads as suspiciously uniform --
# not "a calm grader" (that reading needs far fewer ballots to happen by
# chance), but a pattern with enough evidence behind it to be worth an
# organizer's attention. Deliberately not the same rule as `flat` above:
# `flat` fires on a single ballot's own sd because normalization needs to
# know a judge contributed no ordering *at all*, however little evidence that
# rests on; this fires only once there is enough evidence that "coincidence"
# stops being the likely explanation.
CONSISTENCY_MIN_REVIEWS = 4
CONSISTENCY_SD_THRESHOLD = 0.25

# Two ballots finished this close together, by the same judge, read as "did
# not spend meaningfully different time on the second project relative to
# the first" -- a rough, honest proxy, not a claim about what happened in
# between. `judge_assignments.completed_at` is the only timestamp this
# system records for "when did a ballot land", so that is what this checks.
FAST_COMPLETION_SECONDS = 30.0


@dataclass(frozen=True)
class ConsistencyFlag:
    """A pattern worth an organizer's attention, never a verdict.

    Deliberately not named or worded as an accusation anywhere in this
    module: `reason` and `detail` are both written to be read by the judge
    being flagged as well as the organizer reading the dashboard, and both
    describe a *pattern in the data*, never a conclusion about the judge's
    intent.
    """

    judge_id: uuid.UUID
    reason: str  # "near_identical_scores" | "fast_completion"
    detail: str


def near_identical_score_flags(
    results: Results,
    *,
    min_reviews: int = CONSISTENCY_MIN_REVIEWS,
    sd_threshold: float = CONSISTENCY_SD_THRESHOLD,
) -> list[ConsistencyFlag]:
    """Judges whose *raw* scores barely move across many independent ballots.

    Raw, not normalized: `normalize()` already treats sd == 0 as `flat` and
    routes around it for scoring purposes, which says nothing about *why* a
    judge was flat, or how close to flat without quite reaching it. A judge
    with 8 ballots and a raw sd of 0.1 used the scale as if it barely
    existed; this is the signal that says so, gated on `min_reviews` so a
    judge with two similar-looking early ballots is not flagged on
    essentially no evidence -- reuses `results.calibrations`, which
    `gather()` already computes, rather than re-deriving per-judge
    statistics a second time.
    """
    return [
        ConsistencyFlag(
            judge_id=cal.judge_id,
            reason="near_identical_scores",
            detail=(
                f"{cal.n} ballots, raw score standard deviation {cal.sd:.2f} "
                f"(mean {cal.mean:.2f})"
            ),
        )
        for cal in results.calibrations
        if cal.n >= min_reviews and cal.sd < sd_threshold
    ]


@dataclass(frozen=True)
class AssignmentTiming:
    """The one fact about a completed ballot this module needs in order to
    reason about speed: who finished it, and when. Everything else -- the
    score values themselves -- is `Ballot`'s job."""

    judge_id: uuid.UUID
    submission_id: uuid.UUID
    completed_at: datetime


def fast_completion_flags(
    timings: list[AssignmentTiming], *, threshold_seconds: float = FAST_COMPLETION_SECONDS
) -> list[ConsistencyFlag]:
    """Judges who finished one ballot and then another within `threshold_seconds`.

    Measured between *consecutive* completions for the same judge, not from
    assignment to completion: a ballot can sit assigned for days before a
    judge opens it, so that gap says nothing about how long they spent on it.
    The gap between finishing one ballot and finishing the next is not
    perfect either -- a judge might read several projects before submitting
    any of them, and one legitimately fast pair proves nothing on its own --
    but it is the only timestamp this system actually records, and it is
    named as an approximation for exactly that reason rather than presented
    as a precise measurement of review time.
    """
    by_judge: dict[uuid.UUID, list[AssignmentTiming]] = {}
    for t in timings:
        by_judge.setdefault(t.judge_id, []).append(t)

    flags: list[ConsistencyFlag] = []
    for judge_id, rows in by_judge.items():
        ordered = sorted(rows, key=lambda r: r.completed_at)
        for prev, curr in zip(ordered, ordered[1:], strict=False):
            gap = (curr.completed_at - prev.completed_at).total_seconds()
            if 0 <= gap < threshold_seconds:
                flags.append(
                    ConsistencyFlag(
                        judge_id=judge_id,
                        reason="fast_completion",
                        detail=(
                            f"completed a ballot {gap:.0f}s after the previous one "
                            f"(under {threshold_seconds:.0f}s)"
                        ),
                    )
                )
    return flags
