"""Community voting: the budget rules and the ballot ordering.

Pure functions over plain values, like `scoring` and `assignment`, so the rules
can be tested without a database and argued with without reading a route.

Two things live here:

* **What a ballot costs**, which is the whole of quadratic voting, and
* **What order a voter sees the projects in**, which is the whole of the
  position-bias defence.

The abuse argument -- what each access mode actually buys, and what it does not --
is in `THREAT-MODEL.md`. This module is where the arithmetic is.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass

from .models import VotingMethod

# A quadratic budget has to be big enough that the curve is expressible. With 100
# credits a voter can shout once at 10 votes, or spread 5 votes across four
# projects for 20 votes of influence -- the trade the method exists to offer. With
# 9 credits the only honest ballots are 1, 2 or 3 votes and the curve is invisible.
MIN_SENSIBLE_CREDITS = 16


@dataclass(frozen=True)
class Choice:
    submission_id: uuid.UUID
    credits: int


@dataclass(frozen=True)
class BudgetError:
    """Why a ballot was refused, in terms a voter can act on.

    `detail` is rendered straight into the 422, so it names the numbers: "that
    ballot costs 121 credits and you have 100" is actionable, and "invalid vote"
    is not.
    """

    detail: str


def cost_of(credits: int, method: VotingMethod) -> int:
    """What one project's votes cost that voter.

    Quadratic: `n` votes cost `n²`. This is the standard formulation -- the
    *marginal* cost of the nth vote is `2n-1`, so influence is linear in votes and
    price is quadratic in them. The brief's phrasing ("n votes costs the square
    root of n in influence") is the same curve read from the other end.

    Single: one vote, one credit, and `check_ballot` refuses anything else.
    """
    if method is VotingMethod.QUADRATIC:
        return credits * credits
    return credits


def check_ballot(
    choices: list[Choice],
    *,
    method: VotingMethod,
    budget: int,
    max_projects: int,
    own_submission_ids: frozenset[uuid.UUID] = frozenset(),
    votable_ids: frozenset[uuid.UUID] | None = None,
) -> BudgetError | None:
    """Validate a whole ballot. Returns None if it is allowed.

    The ballot is checked as a unit rather than per project, because in quadratic
    mode the budget is global: three individually modest choices can be
    collectively unaffordable, and a per-project check would let them through.

    Refused whole, never trimmed. Quietly reducing somebody's vote to fit is worse
    than telling them it does not fit.
    """
    if not choices:
        return None  # an empty ballot is a withdrawal, not an error

    seen: set[uuid.UUID] = set()
    for choice in choices:
        if choice.submission_id in seen:
            return BudgetError("A project appears twice on that ballot; list it once.")
        seen.add(choice.submission_id)

        if choice.credits < 1:
            return BudgetError("A vote has to be at least 1. Leave a project off instead.")

        if choice.submission_id in own_submission_ids:
            return BudgetError("You cannot vote for your own team's project.")

        if votable_ids is not None and choice.submission_id not in votable_ids:
            return BudgetError("That project is not in this event's gallery.")

    if method is VotingMethod.SINGLE:
        if any(choice.credits != 1 for choice in choices):
            return BudgetError(
                "This event uses one vote per project. Pick more projects instead of "
                "stacking votes on one."
            )
        if len(choices) > max_projects:
            return BudgetError(
                f"You may back at most {max_projects} project(s); that ballot has "
                f"{len(choices)}."
            )
        return None

    spend = sum(cost_of(choice.credits, method) for choice in choices)
    if spend > budget:
        breakdown = " + ".join(
            f"{c.credits}²={cost_of(c.credits, method)}" for c in choices
        )
        return BudgetError(
            f"That ballot costs {spend} credits ({breakdown}) and you have {budget}."
        )
    return None


def credits_spent(choices: list[Choice], method: VotingMethod) -> int:
    return sum(cost_of(choice.credits, method) for choice in choices)


# --------------------------------------------------------------------------- #
# Ballot ordering
# --------------------------------------------------------------------------- #


def ballot_order(
    submission_ids: list[uuid.UUID], seed: int
) -> list[uuid.UUID]:
    """Order projects for one voter: random between voters, stable for each.

    Position bias is real -- whatever is at the top of a long list gets more votes
    than it earned -- so every voter gets their own permutation. But re-shuffling
    on every page load would move a project the voter was halfway through
    considering, so the permutation is a pure function of the voter's stored seed
    and is therefore identical on a refresh.

    Implemented as a sort on `sha256(seed:id)` rather than `random.shuffle`: no
    global RNG state, no dependence on Python's shuffle staying stable across
    versions, and the same answer on any machine.
    """

    def key(submission_id: uuid.UUID) -> str:
        return hashlib.sha256(f"{seed}:{submission_id}".encode()).hexdigest()

    return sorted(submission_ids, key=key)


def new_ordering_seed() -> int:
    """A per-voter seed. 31 bits so it fits a plain `integer` column."""
    return int.from_bytes(hashlib.sha256(uuid.uuid4().bytes).digest()[:4], "big") >> 1
