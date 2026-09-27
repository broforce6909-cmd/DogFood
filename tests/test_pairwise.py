"""Pairwise comparison and the Bradley-Terry ranking recovered from it.

No database, no HTTP, no fixtures -- `app.pairwise` is pure functions over
dataclasses for the same reason `app.scoring` is: the maths here is the part of
the feature most likely to be argued with, so it is tested with numbers and
properties a reader can check by hand, independent of anything the API or the
ORM adds on top.

Three edge cases get their own section, because they are the ones a naive
Bradley-Terry implementation gets wrong and the ones this project's own mandate
named explicitly: **ties**, **a judge who never compares two specific projects**,
and **cold start** (zero comparisons, or a submission nobody has compared yet).
"""

from __future__ import annotations

import math
import sys
import uuid
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1] / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.pairwise import (  # noqa: E402
    ANCHOR_STRENGTH,
    REGULARIZATION_GAMES,
    Comparison,
    coverage,
    pick_next_pair,
    rank_submissions,
)


def ids(n: int) -> list[uuid.UUID]:
    return [uuid.uuid4() for _ in range(n)]


# --------------------------------------------------------------------------- #
# The basics: a clear winner beats a clear loser
# --------------------------------------------------------------------------- #


def test_a_project_that_always_wins_ranks_above_one_that_always_loses() -> None:
    a, b = ids(2)
    comparisons = [Comparison(a, b, winner_id=a) for _ in range(6)]
    result = rank_submissions([a, b], comparisons)

    by_id = {row.submission_id: row for row in result.submissions}
    assert by_id[a].rank == 1
    assert by_id[b].rank == 2
    assert by_id[a].rating > 0
    assert by_id[b].rating < 0
    assert result.converged
    assert result.total_comparisons == 6


def test_a_symmetric_head_to_head_ties_both_at_zero() -> None:
    """3 wins each, evenly split: nothing distinguishes them, so both land
    exactly on the anchor."""
    a, b = ids(2)
    comparisons = (
        [Comparison(a, b, winner_id=a) for _ in range(3)]
        + [Comparison(a, b, winner_id=b) for _ in range(3)]
    )
    result = rank_submissions([a, b], comparisons)
    ratings = {row.submission_id: row.rating for row in result.submissions}
    assert math.isclose(ratings[a], 0.0, abs_tol=1e-6)
    assert math.isclose(ratings[b], 0.0, abs_tol=1e-6)
    assert result.submissions[0].rank == result.submissions[1].rank == 1


def test_more_comparisons_pulls_the_rating_further_from_the_anchor() -> None:
    """Winning 2 of 2 is weaker evidence than winning 6 of 6 -- the regularizing
    anchor's pull is proportionally larger with fewer real games, so the more
    convincing record should end up with the more extreme rating."""
    a, b, c, d = ids(4)
    comparisons = (
        [Comparison(a, b, winner_id=a) for _ in range(2)]
        + [Comparison(c, d, winner_id=c) for _ in range(6)]
    )
    result = rank_submissions([a, b, c, d], comparisons)
    ratings = {row.submission_id: row.rating for row in result.submissions}
    assert ratings[c] > ratings[a] > 0
    assert ratings[d] < ratings[b] < 0


# --------------------------------------------------------------------------- #
# Ties
# --------------------------------------------------------------------------- #


def test_a_single_tie_leaves_both_projects_exactly_even() -> None:
    a, b = ids(2)
    result = rank_submissions([a, b], [Comparison(a, b, winner_id=None)])
    ratings = {row.submission_id: row.rating for row in result.submissions}
    assert ratings[a] == ratings[b]
    assert result.submissions[0].rank == 1
    assert result.submissions[1].rank == 1  # competition ranking: a tie shares one


def test_a_tie_counts_as_half_a_win_and_half_a_loss_each() -> None:
    a, b = ids(2)
    result = rank_submissions([a, b], [Comparison(a, b, winner_id=None)])
    by_id = {row.submission_id: row for row in result.submissions}
    assert by_id[a].wins == 0.5
    assert by_id[a].losses == 0.5
    assert by_id[b].wins == 0.5
    assert by_id[b].losses == 0.5
    assert by_id[a].n_comparisons == 1


def test_a_tie_plus_a_real_win_still_favours_the_winner() -> None:
    """A ties B once, then A beats B outright twice. A must still rank above B --
    the tie should not erase the two decisive wins."""
    a, b = ids(2)
    comparisons = [
        Comparison(a, b, winner_id=None),
        Comparison(a, b, winner_id=a),
        Comparison(a, b, winner_id=a),
    ]
    result = rank_submissions([a, b], comparisons)
    by_id = {row.submission_id: row for row in result.submissions}
    assert by_id[a].rating > by_id[b].rating
    assert by_id[a].rank == 1


# --------------------------------------------------------------------------- #
# A judge who never compares two specific projects
# --------------------------------------------------------------------------- #


def test_ranking_does_not_require_every_pair_to_have_been_compared() -> None:
    """A, B and C: only (A,B) and (B,C) were ever shown to any judge. A and C
    were never placed side by side by anybody, and the model must still place
    all three on one ranking by reasoning through B."""
    a, b, c = ids(3)
    comparisons = [
        Comparison(a, b, winner_id=a),
        Comparison(a, b, winner_id=a),
        Comparison(b, c, winner_id=b),
        Comparison(b, c, winner_id=b),
    ]
    result = rank_submissions([a, b, c], comparisons)
    order = [row.submission_id for row in result.submissions]
    assert order == [a, b, c], "A > B > C should be inferred transitively through B"


def test_which_judge_made_which_comparison_is_irrelevant_to_the_fit() -> None:
    """The model only ever sees aggregated win/loss counts -- two different
    judges each casting half of an identical set of comparisons must fit
    identically to one judge casting all of them."""
    a, b = ids(2)
    one_judge = [Comparison(a, b, winner_id=a) for _ in range(4)]
    two_judges = [Comparison(a, b, winner_id=a) for _ in range(2)] * 2

    r1 = rank_submissions([a, b], one_judge)
    r2 = rank_submissions([a, b], two_judges)
    ratings1 = {row.submission_id: row.rating for row in r1.submissions}
    ratings2 = {row.submission_id: row.rating for row in r2.submissions}
    assert math.isclose(ratings1[a], ratings2[a], abs_tol=1e-9)
    assert math.isclose(ratings1[b], ratings2[b], abs_tol=1e-9)


# --------------------------------------------------------------------------- #
# Cold start and disconnected components
# --------------------------------------------------------------------------- #


def test_zero_comparisons_ranks_everything_tied_at_the_anchor() -> None:
    a, b, c = ids(3)
    result = rank_submissions([a, b, c], [])
    assert result.total_comparisons == 0
    assert all(row.rating == 0.0 for row in result.submissions)
    assert all(row.rank == 1 for row in result.submissions)
    assert all(row.n_comparisons == 0 for row in result.submissions)
    assert all(row.win_rate is None for row in result.submissions)
    assert result.converged


def test_an_uncompared_submission_lands_between_a_winner_and_a_loser() -> None:
    """The realistic cold-start shape: most projects have been compared a few
    times, one was just submitted and has not been shown to anyone yet. It
    must still appear in the ranking, sitting at the neutral anchor rather
    than at the bottom (which would unfairly read as "worst") or vanishing
    from the page entirely."""
    a, b, uncompared = ids(3)
    comparisons = [Comparison(a, b, winner_id=a) for _ in range(4)]
    result = rank_submissions([a, b, uncompared], comparisons)
    by_id = {row.submission_id: row for row in result.submissions}

    assert by_id[uncompared].n_comparisons == 0
    assert by_id[uncompared].rating == 0.0
    assert by_id[a].rating > by_id[uncompared].rating > by_id[b].rating


def test_disconnected_components_still_produce_one_comparable_ranking() -> None:
    """Two clusters of projects that were never compared against each other
    (a plausible outcome of random pairing at small scale) must still end up
    on one ranking, anchored through the same phantom opponent rather than
    two separate, incomparable scales."""
    a, b, c, d = ids(4)
    comparisons = (
        [Comparison(a, b, winner_id=a) for _ in range(3)]
        + [Comparison(c, d, winner_id=c) for _ in range(3)]
    )
    result = rank_submissions([a, b, c, d], comparisons)
    ratings = {row.submission_id: row.rating for row in result.submissions}

    # Identical won/lost records in each isolated cluster -> identical ratings.
    assert math.isclose(ratings[a], ratings[c], abs_tol=1e-6)
    assert math.isclose(ratings[b], ratings[d], abs_tol=1e-6)
    assert ratings[a] > 0 > ratings[b]
    assert result.converged


def test_a_lopsided_disconnected_pair_still_out_ranks_a_weaker_one() -> None:
    """Not just symmetric toy cases: cluster (A beats B 5-0) should rank A
    above cluster (C beats D 1-0) even though A and C were never compared,
    because A's record is more convincing -- and by the same logic, B's more
    decisive loss should sit further below the anchor than D's single loss."""
    a, b, c, d = ids(4)
    comparisons = (
        [Comparison(a, b, winner_id=a) for _ in range(5)]
        + [Comparison(c, d, winner_id=c) for _ in range(1)]
    )
    result = rank_submissions([a, b, c, d], comparisons)
    ratings = {row.submission_id: row.rating for row in result.submissions}
    assert ratings[a] > ratings[c] > 0
    assert 0 > ratings[d] > ratings[b]


def test_an_empty_roster_returns_an_empty_result() -> None:
    result = rank_submissions([], [])
    assert result.submissions == []
    assert result.total_comparisons == 0
    assert result.converged is True


def test_comparisons_naming_an_id_outside_the_roster_are_ignored_not_raised() -> None:
    """A defensive property, not a documented feature: this module trusts its
    caller's shape but must not explode if a comparison somehow names a
    submission from outside the event being ranked."""
    a, b, stray = ids(3)
    comparisons = [Comparison(a, b, winner_id=a), Comparison(a, stray, winner_id=a)]
    result = rank_submissions([a, b], comparisons)
    assert result.total_comparisons == 1
    by_id = {row.submission_id: row for row in result.submissions}
    assert set(by_id) == {a, b}


# --------------------------------------------------------------------------- #
# Regularization constants are sane
# --------------------------------------------------------------------------- #


def test_regularization_constants_are_positive() -> None:
    """The MM update divides by a sum that includes these; either at zero or
    negative would make every rating collapse to zero or diverge."""
    assert REGULARIZATION_GAMES > 0
    assert ANCHOR_STRENGTH > 0


# --------------------------------------------------------------------------- #
# Picking the next pair to show a judge
# --------------------------------------------------------------------------- #


def test_fewer_than_two_eligible_returns_none() -> None:
    a = ids(1)[0]
    assert pick_next_pair([], total_comparisons={}, already_seen_by_this_judge=set()) is None
    assert (
        pick_next_pair([a], total_comparisons={}, already_seen_by_this_judge=set()) is None
    )


def test_the_least_compared_submissions_are_preferred() -> None:
    """A has been compared 5 times, B and C never. The next pair must be drawn
    from {B, C} -- coverage of the untouched ones matters more than more data
    on the one that already has plenty."""
    a, b, c = ids(3)
    pair = pick_next_pair(
        [a, b, c],
        total_comparisons={a: 5, b: 0, c: 0},
        already_seen_by_this_judge=set(),
    )
    assert pair is not None
    assert set(pair) == {b, c}


def test_a_pair_this_judge_already_saw_is_skipped_in_favour_of_a_fresh_one() -> None:
    """B and C are tied for least-compared, but this judge already compared
    that exact pair. A fresh pair -- even one involving the more-compared A --
    teaches the model something B-vs-C would not."""
    a, b, c = ids(3)
    pair = pick_next_pair(
        [a, b, c],
        total_comparisons={a: 1, b: 0, c: 0},
        already_seen_by_this_judge={frozenset((b, c))},
    )
    assert pair is not None
    assert set(pair) != {b, c}


def test_once_every_pair_is_seen_it_falls_back_to_the_least_compared_pair() -> None:
    """A small event where this judge has exhausted every combination: serving
    nothing would be a worse failure than a repeat, so the least-compared pair
    is served again rather than refusing."""
    a, b = ids(2)
    pair = pick_next_pair(
        [a, b],
        total_comparisons={a: 3, b: 3},
        already_seen_by_this_judge={frozenset((a, b))},
    )
    assert pair is not None
    assert set(pair) == {a, b}


# --------------------------------------------------------------------------- #
# Coverage stats: is a round actually balanced
# --------------------------------------------------------------------------- #


def test_coverage_of_an_empty_roster_is_all_zero() -> None:
    result = rank_submissions([], [])
    stats = coverage(result)
    assert stats.total_submissions == 0
    assert stats.total_comparisons == 0
    assert stats.min_comparisons == 0
    assert stats.max_comparisons == 0
    assert stats.mean_comparisons == 0.0
    assert stats.coverage_pct == 0.0


def test_coverage_of_zero_comparisons_is_zero_percent_but_full_roster() -> None:
    a, b, c = ids(3)
    result = rank_submissions([a, b, c], [])
    stats = coverage(result)
    assert stats.total_submissions == 3
    assert stats.total_comparisons == 0
    assert stats.min_comparisons == 0
    assert stats.max_comparisons == 0
    assert stats.mean_comparisons == 0.0
    assert stats.coverage_pct == 0.0


def test_coverage_reports_min_max_mean_and_full_coverage_when_everyone_has_been_seen() -> None:
    """A beats B four times, B beats C twice: each comparison touches two
    submissions, so A has 4, C has 2, and B (in every comparison) has 6.
    Every submission has at least one, so coverage is 100%."""
    a, b, c = ids(3)
    comparisons = [Comparison(a, b, winner_id=a) for _ in range(4)] + [
        Comparison(b, c, winner_id=b) for _ in range(2)
    ]
    result = rank_submissions([a, b, c], comparisons)
    stats = coverage(result)
    assert stats.total_submissions == 3
    assert stats.total_comparisons == 6
    assert stats.min_comparisons == 2  # c appears in only the 2 b-c comparisons
    assert stats.max_comparisons == 6  # b appears in every comparison
    assert stats.mean_comparisons == pytest.approx((4 + 6 + 2) / 3)
    assert stats.coverage_pct == 100.0


def test_coverage_flags_an_uncompared_submission_as_a_gap() -> None:
    """One project submitted but never shown to any judge yet: coverage must
    say so numerically (below 100%), not just leave it tied at the anchor in
    the ranking table where a quick glance could miss it."""
    a, b, uncompared = ids(3)
    comparisons = [Comparison(a, b, winner_id=a) for _ in range(3)]
    result = rank_submissions([a, b, uncompared], comparisons)
    stats = coverage(result)
    assert stats.total_submissions == 3
    assert stats.min_comparisons == 0
    assert stats.max_comparisons == 3
    assert stats.coverage_pct == pytest.approx(2 / 3 * 100)
