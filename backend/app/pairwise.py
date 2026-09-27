"""Pairwise comparisons, and the ranking recovered from them by Bradley-Terry.

The Gavel approach, named as the "more interesting answer" in JUDGING.md's list of
what this project does not do: instead of asking a judge to place a number on a 1-5
scale (the whole subject of `scoring.py`'s normalization), show them two projects
and ask which is better. A judge who is bad at using a 1-5 scale consistently is
usually still fine at saying "this one, not that one" -- pairwise comparison
sidesteps the calibration problem `scoring.py` corrects for, rather than correcting
for it.

Pure functions over plain dataclasses, exactly like `scoring.py` and for the same
reason: the maths here is the part of the feature most likely to be argued with, so
it is written to be read and tested in isolation from the ORM and the HTTP layer.

## The model

Bradley-Terry: each submission `i` has a latent strength `π_i > 0`, and the
probability that `i` beats `j` in one comparison is `π_i / (π_i + π_j)`. Fitted by
the MM (minorization-maximization) algorithm from Hunter, which is simpler
to implement correctly than gradient-based MLE and, unlike a closed form, exists
for this model: there is no closed-form Bradley-Terry solution once there are more
than two items.

## Three edge cases a naive implementation gets wrong, and how this one handles them

1. **A judge who never compares two specific projects.** Not a problem -- Bradley-
   Terry does not need every pair compared, only enough comparisons overall to
   connect the graph (see #3). A judge's individual coverage is irrelevant; the
   model aggregates every comparison from every judge into one win/loss count per
   pair and fits one set of strengths.
2. **Ties.** Gavel-style comparison allows "I can't decide". Treated as **half a
   win for each side** (`w_ij += 0.5, w_ji += 0.5`) rather than discarded: throwing
   a tie away would tell the model less than it actually knows (that the two are
   close), and the alternative -- the Davidson model with an explicit tie
   parameter -- is real added complexity for a distinction ("close" vs "no
   information") this project's judging scale does not need to make. Named here
   rather than silently chosen.
3. **Disconnected components / cold start.** If submission A is only ever compared
   within one cluster of projects and submission B only within another, or a
   submission has been submitted but never compared to anything, raw Bradley-Terry
   has no way to say which cluster is stronger, or where an uncompared item
   belongs -- the likelihood is flat along whatever direction would compare them,
   and the fit does not converge to a finite answer. The fix is the same one
   logistic regression uses for perfect separation: **regularize toward a fixed
   anchor.** Every real submission is given one virtual win and one virtual loss
   against a phantom "average project" whose strength is pinned at 1 and never
   updated. This guarantees every item is transitively connected to every other
   item through the anchor, so the MM iteration always converges to finite,
   globally comparable strengths -- at the cost of pulling a thinly-compared item's
   rating toward the middle, which is the honest, visible version of "we do not
   have enough signal on this one yet" rather than a crash or a `nan`.

`REGULARIZATION_GAMES` controls how strongly: 1.0 means each submission's rating is
anchored as if it had already played (and split) one game against an average
opponent, which is a small nudge relative to a submission with a real handful of
comparisons and a dominant one for a submission with zero.
"""

from __future__ import annotations

import math
import random
import uuid
from dataclasses import dataclass

# One virtual win *and* one virtual loss against a fixed, average-strength phantom
# opponent, for every submission. See the module docstring, point 3: this is what
# keeps a disconnected or entirely uncompared submission from breaking the fit
# instead of just ranking near the middle with a wide implicit uncertainty.
REGULARIZATION_GAMES = 1.0

# The anchor's own strength. Fixed, never updated -- it is what pins the whole
# scale down to one solution rather than a family of them (Bradley-Terry strengths
# are only ever meaningful up to a common multiplicative constant; anchoring one
# phantom item removes that remaining freedom).
ANCHOR_STRENGTH = 1.0

MAX_ITERATIONS = 500

# MM has converged when the largest relative change in any strength between
# iterations drops below this. 1e-9 is far tighter than any ranking decision could
# turn on; it exists so "converged" means converged, not "stopped".
CONVERGENCE_EPSILON = 1e-9


# --------------------------------------------------------------------------- #
# Inputs
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Comparison:
    """One judge's answer to "which is better". `winner_id` is `None` for a tie.

    Precondition, enforced by the caller (the route, which controls how a pair is
    ever offered): `winner_id` is one of `a_id`, `b_id`, or `None`, and `a_id !=
    b_id`. This module trusts that rather than re-checking it, the same way
    `scoring.raw_score` trusts a `Ballot`'s shape.
    """

    a_id: uuid.UUID
    b_id: uuid.UUID
    winner_id: uuid.UUID | None


# --------------------------------------------------------------------------- #
# Outputs
# --------------------------------------------------------------------------- #


@dataclass
class SubmissionStrength:
    submission_id: uuid.UUID
    rating: float  # ln(strength). Zero is "exactly as strong as the anchor".
    strength: float  # exp(rating) -- the Bradley-Terry π. Always > 0.
    n_comparisons: int
    wins: float
    losses: float
    rank: int = 0

    @property
    def win_rate(self) -> float | None:
        """Plain, not model-fitted -- the number an organizer sanity-checks the
        rating against. `None` with zero real comparisons rather than a
        division by zero standing in for "no data"."""
        if self.n_comparisons == 0:
            return None
        return self.wins / self.n_comparisons


@dataclass(frozen=True)
class CoverageStats:
    """How evenly comparisons are spread across submissions -- the numbers
    that answer "is this pairwise round actually balanced" without reading the
    per-submission table row by row.

    `min`/`max` over zero submissions would be undefined; `total_submissions
    == 0` is the caller's signal to not trust `min`/`max`/`mean` at all, the
    same shape `SubmissionStrength.win_rate` uses for zero comparisons.
    """

    total_submissions: int
    total_comparisons: int
    min_comparisons: int
    max_comparisons: int
    mean_comparisons: float
    # The fraction of submissions with at least one comparison. Not "at least
    # the mean" or some other threshold: a submission with zero comparisons is
    # the failure `rank_submissions`' anchor regularization exists to survive
    # gracefully, not one this stat should treat as merely "below average".
    coverage_pct: float


def coverage(results: PairwiseResults) -> CoverageStats:
    counts = [row.n_comparisons for row in results.submissions]
    total = len(counts)
    if total == 0:
        return CoverageStats(0, results.total_comparisons, 0, 0, 0.0, 0.0)
    covered = sum(1 for c in counts if c > 0)
    return CoverageStats(
        total_submissions=total,
        total_comparisons=results.total_comparisons,
        min_comparisons=min(counts),
        max_comparisons=max(counts),
        mean_comparisons=sum(counts) / total,
        coverage_pct=100.0 * covered / total,
    )


@dataclass(frozen=True)
class PairwiseResults:
    submissions: list[SubmissionStrength]
    total_comparisons: int
    iterations: int
    converged: bool


def _rank(rows: list[SubmissionStrength]) -> None:
    """Competition ranking: equal ratings share a rank, and the next rank skips.

    Ties broken by submission id purely for a stable, reproducible order across
    runs -- identical to `scoring._rank`, which this deliberately mirrors.
    """
    ordered = sorted(rows, key=lambda r: (-r.rating, r.submission_id.hex))
    previous: float | None = None
    rank = 0
    for index, row in enumerate(ordered, start=1):
        if previous is None or abs(row.rating - previous) > 1e-9:
            rank = index
            previous = row.rating
        row.rank = rank


# --------------------------------------------------------------------------- #
# Fitting
# --------------------------------------------------------------------------- #


def rank_submissions(
    submission_ids: list[uuid.UUID], comparisons: list[Comparison]
) -> PairwiseResults:
    """Fit Bradley-Terry strengths by MM and return every submission, ranked.

    `submission_ids` is the *complete* roster to rank, not just the ones that have
    been compared -- a submitted project with zero comparisons yet is exactly the
    cold-start case point 3 above exists for, and it must still appear in the
    output (tied with every other uncompared project, at the anchor's own
    strength) rather than silently vanish from the results page.

    The MM update, for one real item `i` against every other item `j` (including
    the fixed anchor as an extra, unindexed opponent of everyone):

        π_i ← W_i / Σ_j (w_ij + w_ji) / (π_i + π_j)

    where `W_i` is `i`'s total win count (real plus the regularization win against
    the anchor) and the sum runs over every other real item plus the anchor. This
    is Hunter's generalized Bradley-Terry MM update; the anchor is folded in
    as one more opponent whose own strength is simply never updated in the loop
    below.
    """
    ids = list(submission_ids)
    index = {sid: i for i, sid in enumerate(ids)}
    n = len(ids)

    # wins[i][j] = total wins of i over j, real comparisons plus the ties-as-half
    # rule -- excludes the anchor, which is tracked separately since it is not one
    # of `ids` and never has its own row.
    wins = [[0.0] * n for _ in range(n)]
    n_comparisons = [0] * n
    real_wins = [0.0] * n
    real_losses = [0.0] * n

    counted = 0
    for cmp in comparisons:
        i, j = index.get(cmp.a_id), index.get(cmp.b_id)
        if i is None or j is None:
            # A comparison naming a submission outside this roster (a different
            # event's data leaking in, say) is a caller bug, not a crash here --
            # skipped rather than raised, matching this module's stance of
            # trusting shape but not silently exploding on a mismatch either.
            continue
        counted += 1
        n_comparisons[i] += 1
        n_comparisons[j] += 1
        if cmp.winner_id == cmp.a_id:
            wins[i][j] += 1.0
            real_wins[i] += 1.0
            real_losses[j] += 1.0
        elif cmp.winner_id == cmp.b_id:
            wins[j][i] += 1.0
            real_wins[j] += 1.0
            real_losses[i] += 1.0
        else:  # a tie
            wins[i][j] += 0.5
            wins[j][i] += 0.5
            real_wins[i] += 0.5
            real_wins[j] += 0.5
            real_losses[i] += 0.5
            real_losses[j] += 0.5

    if n == 0:
        return PairwiseResults(submissions=[], total_comparisons=0, iterations=0, converged=True)

    # Every item's total win count, including its guaranteed win against the
    # anchor -- this is the MM update's numerator and it is why the anchor makes
    # every item's denominator finite even with zero real comparisons.
    total_wins = [real_wins[i] + REGULARIZATION_GAMES for i in range(n)]

    strength = [1.0] * n  # π_i, all equal at the start: no information yet.
    iterations = 0
    converged = False

    for iterations in range(1, MAX_ITERATIONS + 1):  # noqa: B007 -- read after the loop, below
        new_strength = [0.0] * n
        max_relative_change = 0.0
        for i in range(n):
            denominator = 0.0
            for j in range(n):
                if j == i:
                    continue
                games = wins[i][j] + wins[j][i]
                if games:
                    denominator += games / (strength[i] + strength[j])
            # The anchor: a guaranteed win and a guaranteed loss for i, at a
            # fixed, never-updated strength. Always present, so denominator is
            # never zero even for an item with no real comparisons at all.
            denominator += (2 * REGULARIZATION_GAMES) / (strength[i] + ANCHOR_STRENGTH)

            new_strength[i] = total_wins[i] / denominator
            if strength[i] > 0:
                max_relative_change = max(
                    max_relative_change,
                    abs(new_strength[i] - strength[i]) / strength[i],
                )
        strength = new_strength
        if max_relative_change < CONVERGENCE_EPSILON:
            converged = True
            break

    rows = [
        SubmissionStrength(
            submission_id=sid,
            rating=_safe_log(strength[i]),
            strength=strength[i],
            n_comparisons=n_comparisons[i],
            wins=real_wins[i],
            losses=real_losses[i],
        )
        for i, sid in enumerate(ids)
    ]
    _rank(rows)
    rows.sort(key=lambda r: r.rank)

    return PairwiseResults(
        submissions=rows,
        total_comparisons=counted,
        iterations=iterations,
        converged=converged,
    )


def _safe_log(value: float) -> float:
    return math.log(value) if value > 0 else float("-inf")


# --------------------------------------------------------------------------- #
# Picking the next pair to show a judge
# --------------------------------------------------------------------------- #


def pick_next_pair(
    eligible_ids: list[uuid.UUID],
    *,
    total_comparisons: dict[uuid.UUID, int],
    already_seen_by_this_judge: set[frozenset[uuid.UUID]],
    rng: random.Random | None = None,
) -> tuple[uuid.UUID, uuid.UUID] | None:
    """Which two submissions to show this judge next. `None` if fewer than two
    are eligible at all.

    Two goals, in priority order:

    1. **Coverage.** Every submission needs enough comparisons for the fit to say
       anything about it -- the cold-start case `rank_submissions` handles
       gracefully is still worse information than a real comparison. Preferring
       the least-compared eligible submissions overall (ties broken randomly, so
       the earliest-submitted projects do not always go first) spreads
       comparisons out rather than letting an early pair dominate the count
       forever.
    2. **Novelty for *this* judge.** Two different judges comparing the same pair
       is useful (it is exactly what lets `rank_submissions` combine everyone's
       opinions); the same judge comparing the same pair twice teaches the model
       nothing new. So among the least-compared submissions, the first pair this
       judge has not already answered wins.

    If this judge has already compared every possible pair among the eligible
    set -- realistic in a small event once judging has run a while -- goal 2
    cannot be satisfied, and this falls back to the two least-compared
    submissions regardless of who has seen them. A repeat is weaker signal than
    a fresh pair, not wrong signal, and refusing to serve *anything* once a small
    event's pairs run out would be a worse failure than a redundant comparison.
    """
    if len(eligible_ids) < 2:
        return None
    rng = rng or random.Random()

    ordered = sorted(
        eligible_ids, key=lambda sid: (total_comparisons.get(sid, 0), rng.random())
    )
    for i in range(len(ordered)):
        for j in range(i + 1, len(ordered)):
            pair = frozenset((ordered[i], ordered[j]))
            if pair not in already_seen_by_this_judge:
                return ordered[i], ordered[j]
    return ordered[0], ordered[1]
