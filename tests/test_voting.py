"""The voting arithmetic and the ballot ordering.

No database, no HTTP. `app.voting` is pure functions for the same reason
`app.scoring` is: quadratic voting is the part of T3 most likely to be argued
with, and the cost curve should be checkable by reading a test.
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1] / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.models import VotingMethod  # noqa: E402
from app.voting import (  # noqa: E402
    Choice,
    ballot_order,
    check_ballot,
    cost_of,
    credits_spent,
    new_ordering_seed,
)

QUADRATIC = VotingMethod.QUADRATIC
SINGLE = VotingMethod.SINGLE


def ids(n: int) -> list[uuid.UUID]:
    return [uuid.uuid4() for _ in range(n)]


# --------------------------------------------------------------------------- #
# The cost curve
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "votes,cost", [(1, 1), (2, 4), (3, 9), (5, 25), (10, 100), (11, 121)]
)
def test_quadratic_cost_is_votes_squared(votes: int, cost: int) -> None:
    assert cost_of(votes, QUADRATIC) == cost


def test_the_marginal_vote_gets_more_expensive() -> None:
    """The property that makes the method work: the nth vote costs 2n-1.

    Influence is linear in votes, price is quadratic in them, so intensity is
    expressible but never cheap. This is the whole mechanism, asserted directly.
    """
    for n in range(1, 12):
        marginal = cost_of(n, QUADRATIC) - cost_of(n - 1, QUADRATIC)
        assert marginal == 2 * n - 1


def test_single_mode_cost_is_linear() -> None:
    assert cost_of(1, SINGLE) == 1
    assert cost_of(3, SINGLE) == 3


def test_credits_spent_sums_the_curve() -> None:
    a, b = ids(2)
    choices = [Choice(a, 3), Choice(b, 4)]
    assert credits_spent(choices, QUADRATIC) == 9 + 16


# --------------------------------------------------------------------------- #
# Budget validation
# --------------------------------------------------------------------------- #


def test_an_affordable_ballot_is_allowed() -> None:
    a, b = ids(2)
    problem = check_ballot(
        [Choice(a, 5), Choice(b, 5)],
        method=QUADRATIC,
        budget=100,
        max_projects=100,
        votable_ids=frozenset({a, b}),
    )
    assert problem is None


def test_an_unaffordable_ballot_names_the_numbers() -> None:
    """A refusal a voter can act on: what it cost, and what they had."""
    a = ids(1)[0]
    problem = check_ballot(
        [Choice(a, 11)], method=QUADRATIC, budget=100, max_projects=100,
        votable_ids=frozenset({a}),
    )
    assert problem is not None
    assert "121" in problem.detail
    assert "100" in problem.detail


def test_the_budget_binds_across_projects_not_per_project() -> None:
    """Three 6-vote choices are 108 credits. No single one looks expensive."""
    a, b, c = ids(3)
    problem = check_ballot(
        [Choice(a, 6), Choice(b, 6), Choice(c, 6)],
        method=QUADRATIC,
        budget=100,
        max_projects=100,
        votable_ids=frozenset({a, b, c}),
    )
    assert problem is not None and "108" in problem.detail


def test_exactly_on_budget_is_allowed() -> None:
    """Off-by-one at the boundary is the classic way to annoy a voter."""
    a = ids(1)[0]
    assert (
        check_ballot(
            [Choice(a, 10)], method=QUADRATIC, budget=100, max_projects=100,
            votable_ids=frozenset({a}),
        )
        is None
    )


def test_an_empty_ballot_is_a_withdrawal_not_an_error() -> None:
    assert check_ballot([], method=QUADRATIC, budget=100, max_projects=3) is None


def test_a_duplicate_project_is_refused() -> None:
    """Attack: list a project twice so two small costs beat one big one."""
    a = ids(1)[0]
    problem = check_ballot(
        [Choice(a, 3), Choice(a, 3)], method=QUADRATIC, budget=100, max_projects=100,
        votable_ids=frozenset({a}),
    )
    assert problem is not None and "twice" in problem.detail


def test_zero_or_negative_votes_are_refused() -> None:
    a = ids(1)[0]
    for credits in (0, -3):
        problem = check_ballot(
            [Choice(a, credits)], method=QUADRATIC, budget=100, max_projects=100,
            votable_ids=frozenset({a}),
        )
        assert problem is not None, credits


def test_your_own_project_is_refused() -> None:
    mine, theirs = ids(2)
    problem = check_ballot(
        [Choice(mine, 2)],
        method=QUADRATIC,
        budget=100,
        max_projects=100,
        own_submission_ids=frozenset({mine}),
        votable_ids=frozenset({mine, theirs}),
    )
    assert problem is not None and "own" in problem.detail.lower()


def test_a_project_outside_the_gallery_is_refused() -> None:
    """Attack: vote for a draft, or for another event's project, by id."""
    inside, outside = ids(2)
    problem = check_ballot(
        [Choice(outside, 1)],
        method=QUADRATIC,
        budget=100,
        max_projects=100,
        votable_ids=frozenset({inside}),
    )
    assert problem is not None and "gallery" in problem.detail.lower()


# -- single mode ----------------------------------------------------------- #


def test_single_mode_refuses_stacked_votes() -> None:
    a = ids(1)[0]
    problem = check_ballot(
        [Choice(a, 4)], method=SINGLE, budget=3, max_projects=3,
        votable_ids=frozenset({a}),
    )
    assert problem is not None and "one vote" in problem.detail.lower()


def test_single_mode_caps_the_project_count() -> None:
    picks = ids(4)
    problem = check_ballot(
        [Choice(p, 1) for p in picks],
        method=SINGLE,
        budget=3,
        max_projects=3,
        votable_ids=frozenset(picks),
    )
    assert problem is not None and "at most 3" in problem.detail


def test_single_mode_at_the_cap_is_allowed() -> None:
    picks = ids(3)
    assert (
        check_ballot(
            [Choice(p, 1) for p in picks],
            method=SINGLE,
            budget=3,
            max_projects=3,
            votable_ids=frozenset(picks),
        )
        is None
    )


# --------------------------------------------------------------------------- #
# Ballot ordering
# --------------------------------------------------------------------------- #


def test_the_order_is_a_permutation() -> None:
    """Shuffling must not drop or duplicate a project."""
    projects = ids(25)
    ordered = ballot_order(projects, seed=12345)
    assert sorted(ordered, key=str) == sorted(projects, key=str)


def test_the_same_seed_gives_the_same_order() -> None:
    """Stable across requests, which is what stops the page moving under a voter
    halfway through deciding."""
    projects = ids(20)
    assert ballot_order(projects, 7) == ballot_order(projects, 7)


def test_the_order_does_not_depend_on_the_input_order() -> None:
    """A voter's permutation is a property of their seed, not of however the
    database happened to return the rows."""
    projects = ids(20)
    assert ballot_order(projects, 7) == ballot_order(list(reversed(projects)), 7)


def test_different_seeds_give_different_orders() -> None:
    projects = ids(20)
    orders = {tuple(ballot_order(projects, seed)) for seed in range(12)}
    assert len(orders) > 1


def test_position_bias_is_spread_across_voters() -> None:
    """The point of the whole exercise: over many voters, no single project owns
    the top slot.

    With 8 projects and 400 voters, a fair shuffle puts each project first about 50
    times. Asserting "no project is first more than a quarter of the time" is loose
    enough never to flake and tight enough to catch an ordering that is not
    actually varying.
    """
    projects = ids(8)
    firsts: dict[uuid.UUID, int] = {p: 0 for p in projects}
    for seed in range(400):
        firsts[ballot_order(projects, seed)[0]] += 1
    assert max(firsts.values()) < 100, firsts
    assert min(firsts.values()) > 10, firsts


def test_ordering_seeds_are_positive_and_varied() -> None:
    """The column is a plain `integer`, so the seed has to fit in 31 bits."""
    seeds = {new_ordering_seed() for _ in range(200)}
    assert len(seeds) > 190
    assert all(0 <= s < 2**31 for s in seeds)


def test_one_project_is_not_a_special_case() -> None:
    single = ids(1)
    assert ballot_order(single, 3) == single
    assert ballot_order([], 3) == []
