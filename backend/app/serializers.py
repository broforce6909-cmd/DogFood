"""ORM row -> response model.

Built explicitly rather than by attribute-copying, for two reasons: several
response fields are computed (whether a window is open, what the caller may do
next), and explicit construction means adding a column to a model never
accidentally publishes it.

`can_edit` / `can_submit` are computed by calling `check_access` -- the same
predicate that will refuse the request. The UI uses them to hide buttons; it is
a courtesy, not a control.
"""

from __future__ import annotations

from datetime import datetime

from .access import Action, Principal, check_access
from .models import (
    Event,
    Judge,
    JudgeAssignment,
    RubricCriterion,
    Submission,
    Team,
    Track,
    utcnow,
)
from .schemas import (
    AnswerOut,
    AssignmentOut,
    CriterionOut,
    EventOut,
    EventSummary,
    JudgeOut,
    PrizeOut,
    QuestionOut,
    ScoreOut,
    SubmissionCard,
    SubmissionOut,
    TeamMemberOut,
    TeamOut,
    TrackOut,
    UserPublic,
)
from .scoring import Ballot, Criterion, raw_score


def track_out(track: Track | None) -> TrackOut | None:
    return None if track is None else TrackOut.model_validate(track)


def event_summary(event: Event) -> EventSummary:
    return EventSummary.model_validate(event)


def event_out(event: Event, *, now: datetime | None = None) -> EventOut:
    now = now or utcnow()
    return EventOut(
        id=event.id,
        slug=event.slug,
        name=event.name,
        tagline=event.tagline,
        description=event.description,
        website_url=event.website_url,
        starts_at=event.starts_at,
        ends_at=event.ends_at,
        registration_opens_at=event.registration_opens_at,
        submission_opens_at=event.submission_opens_at,
        submission_deadline=event.submission_deadline,
        judging_opens_at=event.judging_opens_at,
        judging_closes_at=event.judging_closes_at,
        max_team_size=event.max_team_size,
        is_published=event.is_published,
        archived_at=event.archived_at,
        tracks=[TrackOut.model_validate(t) for t in event.tracks],
        prizes=[PrizeOut.model_validate(p) for p in event.prizes],
        questions=[QuestionOut.model_validate(q) for q in event.questions],
        submissions_open=event.submissions_open(now),
        registration_open=event.registration_open(now),
        voting_opens_at=event.voting_opens_at,
        voting_closes_at=event.voting_closes_at,
        voting_access=event.voting_access,
        voting_method=event.voting_method,
        vote_credits=event.vote_credits,
        votes_per_voter=event.votes_per_voter,
        comments_enabled=event.comments_enabled,
        voting_open=event.voting_open(now),
        results_public_at=event.results_public_at,
        results_public=event.results_public(now),
        pairwise_enabled=event.pairwise_enabled,
        winner_slots=event.winner_slots,
        community_vote_slots=event.community_vote_slots,
    )


def team_out(team: Team) -> TeamOut:
    members = sorted(team.members, key=lambda m: (m.team_role.value, m.joined_at))
    return TeamOut(
        id=team.id,
        event_id=team.event_id,
        event_slug=team.event.slug,
        name=team.name,
        created_at=team.created_at,
        members=[
            TeamMemberOut(
                user=UserPublic.model_validate(m.user),
                team_role=m.team_role,
                joined_at=m.joined_at,
            )
            for m in members
        ],
        submission_id=team.submission.id if team.submission else None,
    )


def submission_card(submission: Submission) -> SubmissionCard:
    return SubmissionCard(
        id=submission.id,
        name=submission.name,
        tagline=submission.tagline,
        thumbnail_url=submission.thumbnail_url,
        tech_tags=list(submission.tech_tags or []),
        track=track_out(submission.track),
        team_name=submission.team.name,
        submitted_at=submission.submitted_at,
    )


def submission_out(
    submission: Submission, user: Principal, *, now: datetime | None = None
) -> SubmissionOut:
    now = now or utcnow()
    answers = {a.question_id: a.value for a in submission.answers}
    return SubmissionOut(
        id=submission.id,
        event_id=submission.event_id,
        event_slug=submission.event.slug,
        team_id=submission.team_id,
        team_name=submission.team.name,
        name=submission.name,
        tagline=submission.tagline,
        description=submission.description,
        thumbnail_url=submission.thumbnail_url,
        gallery_image_urls=list(submission.gallery_image_urls or []),
        demo_video_url=submission.demo_video_url,
        repo_url=submission.repo_url,
        live_url=submission.live_url,
        linkedin_url=submission.linkedin_url,
        tech_tags=list(submission.tech_tags or []),
        discord_usernames=list(submission.discord_usernames or []),
        track=track_out(submission.track),
        status=submission.status,
        submitted_at=submission.submitted_at,
        created_at=submission.created_at,
        updated_at=submission.updated_at,
        answers=[
            AnswerOut(
                question_id=q.id,
                prompt=q.prompt,
                value=answers.get(q.id),
            )
            for q in submission.event.questions
        ],
        members=[UserPublic.model_validate(m.user) for m in submission.team.members],
        can_edit=check_access(user, submission, Action.UPDATE, now=now),
        can_submit=check_access(user, submission, Action.SUBMIT, now=now),
    )


# --------------------------------------------------------------------------- #
# Judging (Phase 2)
# --------------------------------------------------------------------------- #


def criterion_out(criterion: RubricCriterion) -> CriterionOut:
    return CriterionOut(
        id=criterion.id,
        key=criterion.key,
        name=criterion.name,
        description=criterion.description,
        # Numeric -> float at the boundary. Decimal is right for storage and
        # wrong for JSON, where it would serialise as a string.
        weight=float(criterion.weight),
        min_score=criterion.min_score,
        max_score=criterion.max_score,
        position=criterion.position,
    )


def judge_out(judge: Judge) -> JudgeOut:
    return JudgeOut(
        id=judge.id,
        event_id=judge.event_id,
        event_slug=judge.event.slug,
        pairwise_enabled=judge.event.pairwise_enabled,
        user=UserPublic.model_validate(judge.user),
        email=judge.user.email,
        track=track_out(judge.track),
        is_active=judge.is_active,
        invited_at=judge.invited_at,
        accepted_at=judge.accepted_at,
    )


def assignment_out(
    assignment: JudgeAssignment, user: Principal, *, now: datetime | None = None
) -> AssignmentOut:
    """One ballot.

    The scores on it are the ones belonging to *this* ballot and no other, which
    is the entire isolation property: there is no code path here that could load
    a peer's scores, because an assignment only has its own.
    """
    now = now or utcnow()
    criteria = assignment.submission.event.criteria
    by_id = {c.id: c for c in criteria}
    scores = [
        ScoreOut(
            criterion_id=s.criterion_id,
            criterion_key=by_id[s.criterion_id].key if s.criterion_id in by_id else "unknown",
            value=s.value,
            comment=s.comment,
        )
        for s in sorted(
            assignment.scores,
            key=lambda s: by_id[s.criterion_id].position if s.criterion_id in by_id else 0,
        )
    ]
    return AssignmentOut(
        id=assignment.id,
        event_id=assignment.event_id,
        event_slug=assignment.submission.event.slug,
        judge_id=assignment.judge_id,
        judge_name=assignment.judge.user.display_name,
        status=assignment.status,
        comment=assignment.comment,
        is_adjudication=assignment.is_adjudication,
        assigned_at=assignment.assigned_at,
        completed_at=assignment.completed_at,
        submission=submission_card(assignment.submission),
        scores=scores,
        raw_score=_raw_for(assignment, criteria),
        can_score=check_access(user, assignment, Action.SCORE, now=now),
    )


def _raw_for(assignment: JudgeAssignment, criteria: list[RubricCriterion]) -> float | None:
    """This ballot's own weighted score, so a judge can see what they just said."""
    if not assignment.scores:
        return None
    ballot = Ballot(
        judge_id=assignment.judge_id,
        submission_id=assignment.submission_id,
        scores={s.criterion_id: s.value for s in assignment.scores},
    )
    return raw_score(ballot, [scoring_criterion(c) for c in criteria])


def scoring_criterion(criterion: RubricCriterion) -> Criterion:
    """ORM row -> the plain dataclass `app.scoring` works in.

    The maths module deliberately knows nothing about SQLAlchemy; this is the
    one-line bridge, and it lives here with the other translations.
    """
    return Criterion(
        id=criterion.id,
        key=criterion.key,
        name=criterion.name,
        weight=float(criterion.weight),
        min_score=criterion.min_score,
        max_score=criterion.max_score,
    )
