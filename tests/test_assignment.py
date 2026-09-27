"""Batch assignment: the four constraints, asserted as properties.

`JUDGING.md` commits to four things. Two are hard -- breaking them is a
correctness bug -- and two are targets that can conflict with each other:

1. Every submission receives exactly K reviews.            (target)
2. Judge load is balanced to within one submission.        (target)
3. A judge is never assigned a submission from their own team.   (HARD)
4. Track judges only receive submissions in their track.         (HARD)

The hard ones are checked over a generated population rather than one example,
because a conflict-of-interest bug that only shows up at 17 judges and 40 projects
is still a conflict-of-interest bug. `random.Random(seed)` keeps it reproducible;
this is property-based testing done by hand rather than with Hypothesis, which
would be a dependency for not much gain at this size.
"""

from __future__ import annotations

import random
import sys
import uuid
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1] / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.assignment import JudgeInfo, SubmissionInfo, plan_assignments  # noqa: E402


# --------------------------------------------------------------------------- #
# Population builder
# --------------------------------------------------------------------------- #


def population(
    *,
    n_judges: int,
    n_submissions: int,
    tracks: int = 1,
    judges_on_teams: bool = False,
    seed: int = 0,
):
    """A plausible event: some tracks, some judges, some entered projects.

    `judges_on_teams` puts every judge on exactly one team as well, which is the
    realistic awkward case -- a mentor who also entered -- and the one constraint 3
    exists for.
    """
    rng = random.Random(seed)
    track_ids = [uuid.uuid4() for _ in range(tracks)]

    judge_users = [uuid.uuid4() for _ in range(n_judges)]
    judges = [
        JudgeInfo(
            id=uuid.uuid4(),
            user_id=judge_users[i],
            # First judge is always all-tracks, so a single-track event is never
            # accidentally unservable.
            track_id=None if (tracks == 1 or i == 0) else rng.choice(track_ids),
        )
        for i in range(n_judges)
    ]

    submissions = []
    for i in range(n_submissions):
        members = {uuid.uuid4() for _ in range(rng.randint(1, 3))}
        if judges_on_teams and i < n_judges:
            members.add(judge_users[i])
        submissions.append(
            SubmissionInfo(
                id=uuid.uuid4(),
                track_id=rng.choice(track_ids) if tracks > 1 else track_ids[0],
                team_member_ids=frozenset(members),
            )
        )
    return judges, submissions


def counts(plan, submissions) -> dict[uuid.UUID, int]:
    per = {s.id: 0 for s in submissions}
    for _judge_id, submission_id in plan.pairs:
        per[submission_id] += 1
    return per


# --------------------------------------------------------------------------- #
# 3. Conflict of interest -- HARD
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("seed", range(8))
def test_a_judge_is_never_assigned_their_own_teams_project(seed: int) -> None:
    """The constraint that must never bend, over eight generated populations."""
    judges, submissions = population(
        n_judges=9, n_submissions=20, judges_on_teams=True, seed=seed
    )
    plan = plan_assignments(judges, submissions, reviews_per_submission=3, seed=seed)

    by_judge = {j.id: j for j in judges}
    by_submission = {s.id: s for s in submissions}
    for judge_id, submission_id in plan.pairs:
        judge = by_judge[judge_id]
        assert judge.user_id not in by_submission[submission_id].team_member_ids


def test_conflict_of_interest_beats_the_review_target() -> None:
    """One judge, one project, and the judge is on the team.

    The correct answer is zero assignments and a reported shortfall -- not a
    ballot that should not exist. A platform that quietly assigns it is worse than
    one that says it cannot.
    """
    user = uuid.uuid4()
    judge = JudgeInfo(id=uuid.uuid4(), user_id=user)
    submission = SubmissionInfo(id=uuid.uuid4(), team_member_ids=frozenset({user}))

    plan = plan_assignments([judge], [submission], reviews_per_submission=1, seed=1)
    assert plan.pairs == []
    assert len(plan.shortfalls) == 1
    assert plan.shortfalls[0].eligible == 0
    assert "no eligible judge" in plan.shortfalls[0].reason


# --------------------------------------------------------------------------- #
# 4. Track isolation -- HARD
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("seed", range(8))
def test_a_track_judge_is_never_assigned_another_track(seed: int) -> None:
    judges, submissions = population(n_judges=12, n_submissions=30, tracks=3, seed=seed)
    plan = plan_assignments(judges, submissions, reviews_per_submission=3, seed=seed)

    by_judge = {j.id: j for j in judges}
    by_submission = {s.id: s for s in submissions}
    for judge_id, submission_id in plan.pairs:
        judge = by_judge[judge_id]
        if judge.track_id is not None:
            assert judge.track_id == by_submission[submission_id].track_id


def test_an_untracked_submission_is_nobodys_track() -> None:
    """A project filed under no track is not claimable by a track judge.

    The alternative -- treating NULL as "matches anything" -- would let a track
    judge see work outside their remit through a data-entry gap.
    """
    track = uuid.uuid4()
    track_judge = JudgeInfo(id=uuid.uuid4(), user_id=uuid.uuid4(), track_id=track)
    untracked = SubmissionInfo(id=uuid.uuid4(), track_id=None)

    plan = plan_assignments([track_judge], [untracked], reviews_per_submission=1, seed=1)
    assert plan.pairs == []
    assert plan.shortfalls[0].eligible == 0


def test_an_all_track_judge_takes_anything() -> None:
    generalist = JudgeInfo(id=uuid.uuid4(), user_id=uuid.uuid4(), track_id=None)
    subs = [
        SubmissionInfo(id=uuid.uuid4(), track_id=uuid.uuid4()),
        SubmissionInfo(id=uuid.uuid4(), track_id=None),
    ]
    plan = plan_assignments([generalist], subs, reviews_per_submission=1, seed=1)
    assert len(plan.pairs) == 2


# --------------------------------------------------------------------------- #
# 1. Exactly K reviews
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("k", [1, 2, 3, 5])
def test_every_submission_gets_exactly_k_reviews_when_that_is_possible(k: int) -> None:
    judges, submissions = population(n_judges=10, n_submissions=16, seed=k)
    plan = plan_assignments(judges, submissions, reviews_per_submission=k, seed=k)
    assert set(counts(plan, submissions).values()) == {k}
    assert plan.shortfalls == []


def test_a_shortfall_is_reported_rather_than_silently_accepted() -> None:
    """Three reviews each from two judges is impossible. Say so."""
    judges, submissions = population(n_judges=2, n_submissions=4, seed=3)
    plan = plan_assignments(judges, submissions, reviews_per_submission=3, seed=3)

    assert len(plan.shortfalls) == 4
    for shortfall in plan.shortfalls:
        assert shortfall.achieved == 2
        assert shortfall.requested == 3
        assert "only 2 eligible" in shortfall.reason


# --------------------------------------------------------------------------- #
# 2. Balanced load
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("seed", range(6))
def test_load_is_balanced_to_within_one_when_nothing_constrains_it(seed: int) -> None:
    """18 projects × 3 reviews over 9 judges is 54 ballots, 6 each, no excuse."""
    judges, submissions = population(n_judges=9, n_submissions=18, seed=seed)
    plan = plan_assignments(judges, submissions, reviews_per_submission=3, seed=seed)
    assert plan.balanced, f"spread was {plan.spread}: {sorted(plan.loads.values())}"


def test_an_uneven_division_still_lands_within_one() -> None:
    """20 ballots over 3 judges cannot be equal; it can still be 7/7/6."""
    judges, submissions = population(n_judges=3, n_submissions=10, seed=9)
    plan = plan_assignments(judges, submissions, reviews_per_submission=2, seed=9)
    assert plan.balanced
    assert sorted(plan.loads.values()) == [6, 7, 7]


def test_existing_ballots_count_toward_a_judges_load() -> None:
    """Re-running assignment must not pile everything onto whoever was light.

    The judge who already holds four ballots should be picked last, not first.
    """
    busy = JudgeInfo(id=uuid.uuid4(), user_id=uuid.uuid4(), existing=4)
    idle = JudgeInfo(id=uuid.uuid4(), user_id=uuid.uuid4(), existing=0)
    submissions = [SubmissionInfo(id=uuid.uuid4()) for _ in range(4)]

    plan = plan_assignments([busy, idle], submissions, reviews_per_submission=1, seed=2)
    assert plan.loads[idle.id] == 4
    assert plan.loads[busy.id] == 4
    # Every new ballot went to the judge who had none.
    assert all(judge_id == idle.id for judge_id, _ in plan.pairs)


# --------------------------------------------------------------------------- #
# Idempotence
# --------------------------------------------------------------------------- #


def test_already_assigned_judges_are_not_assigned_twice() -> None:
    judge = JudgeInfo(id=uuid.uuid4(), user_id=uuid.uuid4())
    other = JudgeInfo(id=uuid.uuid4(), user_id=uuid.uuid4())
    submission = SubmissionInfo(
        id=uuid.uuid4(), already_assigned=frozenset({judge.id})
    )
    plan = plan_assignments([judge, other], [submission], reviews_per_submission=2, seed=1)

    assert plan.pairs == [(other.id, submission.id)]


def test_a_submission_already_at_target_is_left_alone() -> None:
    judge = JudgeInfo(id=uuid.uuid4(), user_id=uuid.uuid4())
    submission = SubmissionInfo(id=uuid.uuid4(), already_assigned=frozenset({uuid.uuid4()}))
    plan = plan_assignments([judge], [submission], reviews_per_submission=1, seed=1)
    assert plan.pairs == []
    assert plan.shortfalls == []


# --------------------------------------------------------------------------- #
# Reproducibility
# --------------------------------------------------------------------------- #


def test_the_same_seed_gives_the_same_plan() -> None:
    """An organizer who re-runs assignment should be able to show it was not
    arbitrary, and the tests above depend on it."""
    judges, submissions = population(n_judges=7, n_submissions=15, seed=4)
    first = plan_assignments(judges, submissions, reviews_per_submission=3, seed=99)
    second = plan_assignments(judges, submissions, reviews_per_submission=3, seed=99)
    assert first.pairs == second.pairs


def test_different_seeds_generally_give_different_plans() -> None:
    judges, submissions = population(n_judges=7, n_submissions=15, seed=4)
    a = plan_assignments(judges, submissions, reviews_per_submission=3, seed=1)
    b = plan_assignments(judges, submissions, reviews_per_submission=3, seed=2)
    assert a.pairs != b.pairs


# --------------------------------------------------------------------------- #
# Degenerate input
# --------------------------------------------------------------------------- #


def test_no_judges_assigns_nothing_and_reports_everything() -> None:
    _judges, submissions = population(n_judges=1, n_submissions=3, seed=1)
    plan = plan_assignments([], submissions, reviews_per_submission=2, seed=1)
    assert plan.pairs == []
    assert len(plan.shortfalls) == 3
    assert plan.balanced  # vacuously: there are no loads to be unbalanced


def test_no_submissions_is_not_an_error() -> None:
    judges, _ = population(n_judges=3, n_submissions=1, seed=1)
    plan = plan_assignments(judges, [], reviews_per_submission=3, seed=1)
    assert plan.pairs == []
    assert plan.shortfalls == []
