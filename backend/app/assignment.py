"""Batch judge assignment.

Pure functions over plain dataclasses, for the same reason as `scoring`: the
properties this has to satisfy are worth asserting directly, and
`tests/test_assignment.py` does so on generated populations rather than fixtures.

The four constraints, from `JUDGING.md`:

1. Every submission receives exactly K reviews.
2. Judge load is balanced to within one submission.
3. A judge is never assigned a submission from their own team.
4. Track judges only receive submissions in their track.

(3) and (4) are hard constraints -- violating either is a correctness bug, and
the algorithm will report a shortfall rather than break one. (1) and (2) are
targets that can be impossible simultaneously: six submissions needing three
reviews each from four judges is 18 slots over 4 judges, which cannot be both
exact and balanced. When they conflict, **(1) is satisfied and (2) is reported**,
because an unreviewed project is a worse outcome than an unevenly loaded judge.

Why greedy rather than a matching algorithm: the constraint graph here is small
(tens of judges, tens of projects) and the objective -- balanced load under two
hard filters -- is exactly what least-loaded-first greedy optimises. A flow
formulation would be more impressive and produce the same answer.
"""

from __future__ import annotations

import random
import uuid
from dataclasses import dataclass, field


@dataclass(frozen=True)
class JudgeInfo:
    """A judge as the assigner sees them.

    `existing` is how many ballots they already hold, so re-running assignment
    tops up an event rather than starting from zero and double-loading whoever
    was assigned first.
    """

    id: uuid.UUID
    user_id: uuid.UUID
    track_id: uuid.UUID | None = None
    existing: int = 0

    def judges_track(self, track_id: uuid.UUID | None) -> bool:
        if self.track_id is None:
            return True
        return track_id is not None and self.track_id == track_id


@dataclass(frozen=True)
class SubmissionInfo:
    id: uuid.UUID
    track_id: uuid.UUID | None = None
    team_member_ids: frozenset[uuid.UUID] = frozenset()
    already_assigned: frozenset[uuid.UUID] = frozenset()
    # Judges recorded as conflicted on this specific submission (see
    # `models.JudgeRecusal`), on top of the structural team-membership
    # conflict already checked below. Same treatment as team membership:
    # excluded from eligibility, whatever the load balance costs.
    recused_judge_ids: frozenset[uuid.UUID] = frozenset()


@dataclass(frozen=True)
class Shortfall:
    """A submission that could not be given its full K reviews, and why.

    Surfaced to the organizer rather than swallowed: "three projects in the
    hardware track have only one reviewer because you only invited one hardware
    judge" is actionable, and silently assigning two reviews is not.
    """

    submission_id: uuid.UUID
    requested: int
    achieved: int
    eligible: int

    @property
    def reason(self) -> str:
        if self.eligible == 0:
            return "no eligible judge (every judge is on this team, or no judge covers this track)"
        return f"only {self.eligible} eligible judge(s) for {self.requested} reviews"


@dataclass
class Plan:
    pairs: list[tuple[uuid.UUID, uuid.UUID]] = field(default_factory=list)
    shortfalls: list[Shortfall] = field(default_factory=list)
    loads: dict[uuid.UUID, int] = field(default_factory=dict)

    @property
    def balanced(self) -> bool:
        """True when no judge carries more than one ballot above the lightest."""
        if not self.loads:
            return True
        return max(self.loads.values()) - min(self.loads.values()) <= 1

    @property
    def spread(self) -> int:
        if not self.loads:
            return 0
        return max(self.loads.values()) - min(self.loads.values())


def _eligible(judge: JudgeInfo, submission: SubmissionInfo) -> bool:
    """The two hard constraints, in one place.

    Conflict of interest first: a judge on the team that entered a project never
    reviews it, whatever the load balance costs.
    """
    if judge.user_id in submission.team_member_ids:
        return False
    if not judge.judges_track(submission.track_id):
        return False
    if judge.id in submission.already_assigned:
        return False
    if judge.id in submission.recused_judge_ids:
        return False
    return True


def plan_assignments(
    judges: list[JudgeInfo],
    submissions: list[SubmissionInfo],
    *,
    reviews_per_submission: int = 3,
    seed: int | None = None,
) -> Plan:
    """Assign `reviews_per_submission` judges to every submission.

    `seed` makes a run reproducible, which matters twice: the tests need it, and
    an organizer who re-runs assignment after inviting one more judge should be
    able to show that the result was not arbitrary.

    Most-constrained-first: submissions are processed in order of how few judges
    are eligible for them, so the hardware project with two possible reviewers is
    served before the general-track project that anyone can take. Doing it the
    other way round is how a greedy assigner paints itself into a corner.
    """
    rng = random.Random(seed)
    loads: dict[uuid.UUID, int] = {j.id: j.existing for j in judges}
    plan = Plan(loads=loads)

    eligibility: dict[uuid.UUID, list[JudgeInfo]] = {
        s.id: [j for j in judges if _eligible(j, s)] for s in submissions
    }

    ordered = sorted(
        submissions,
        key=lambda s: (len(eligibility[s.id]), s.id.hex),
    )

    for submission in ordered:
        candidates = list(eligibility[submission.id])
        rng.shuffle(candidates)  # breaks load ties without a bias toward insertion order
        wanted = reviews_per_submission - len(submission.already_assigned)
        if wanted <= 0:
            continue

        # Least-loaded first. The shuffle above is what makes equal loads fair
        # rather than alphabetical.
        candidates.sort(key=lambda j: loads[j.id])
        chosen = candidates[:wanted]

        for judge in chosen:
            plan.pairs.append((judge.id, submission.id))
            loads[judge.id] += 1

        achieved = len(chosen) + len(submission.already_assigned)
        if achieved < reviews_per_submission:
            plan.shortfalls.append(
                Shortfall(
                    submission_id=submission.id,
                    requested=reviews_per_submission,
                    achieved=achieved,
                    eligible=len(eligibility[submission.id]),
                )
            )

    return plan
