"""The access-control matrix, tested without a database.

`check_access` is a pure function over model instances, which is deliberate: the
authorization rules can be exercised at unit-test speed, with no fixtures, no
HTTP and no Postgres, so there is no excuse for not covering them.

The table in FIG. 02 of the brief is reproduced here as executable assertions.
Phase 1 covered the rows it could; Phase 2 adds the score columns -- own scores,
peer scores, other track, aggregate -- at the bottom of this file. The audit-log
column lands with Phase 3.
"""

from __future__ import annotations

import sys
import uuid
from datetime import timedelta
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1] / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.access import (  # noqa: E402
    ANONYMOUS,
    PLATFORM,
    Action,
    check_access,
)
from app.models import (  # noqa: E402
    AssignmentStatus,
    Event,
    Judge,
    JudgeAssignment,
    Role,
    RubricCriterion,
    Score,
    Submission,
    SubmissionStatus,
    Team,
    TeamMember,
    TeamRole,
    Track,
    User,
    utcnow,
)

NOW = utcnow()


# --------------------------------------------------------------------------- #
# Builders -- transient objects, never flushed
# --------------------------------------------------------------------------- #


def user(role: Role = Role.PARTICIPANT, *, active: bool = True) -> User:
    return User(
        id=uuid.uuid4(),
        email=f"{uuid.uuid4().hex[:8]}@example.com",
        display_name="Fixture",
        password_hash="x",
        role=role,
        is_active=active,
    )


def event(*, published: bool = True, open_window: bool = True) -> Event:
    """`open_window=False` produces an event whose deadline passed yesterday."""
    if open_window:
        opens, deadline = NOW - timedelta(days=1), NOW + timedelta(days=7)
    else:
        opens, deadline = NOW - timedelta(days=10), NOW - timedelta(days=1)
    return Event(
        id=uuid.uuid4(),
        slug="fixture",
        name="Fixture Event",
        starts_at=opens,
        ends_at=deadline,
        registration_opens_at=opens,
        submission_opens_at=opens,
        submission_deadline=deadline,
        max_team_size=4,
        is_published=published,
    )


def team(ev: Event, owner: User, *members: User) -> Team:
    t = Team(id=uuid.uuid4(), event_id=ev.id, name="Fixture Team", invite_token="tok")
    t.event = ev
    t.members.append(
        TeamMember(user_id=owner.id, event_id=ev.id, team_role=TeamRole.OWNER)
    )
    for m in members:
        t.members.append(
            TeamMember(user_id=m.id, event_id=ev.id, team_role=TeamRole.MEMBER)
        )
    return t


def submission(
    ev: Event, tm: Team, *, status: SubmissionStatus = SubmissionStatus.SUBMITTED
) -> Submission:
    s = Submission(
        id=uuid.uuid4(),
        event_id=ev.id,
        team_id=tm.id,
        name="Fixture Project",
        status=status,
        submitted_at=NOW if status is SubmissionStatus.SUBMITTED else None,
        tech_tags=[],
        gallery_image_urls=[],
    )
    s.event = ev
    s.team = tm
    return s


ALL_ROLES = [Role.VISITOR, Role.PARTICIPANT, Role.JUDGE, Role.ORGANIZER, Role.ADMIN]


def actor(role: Role):
    """A principal of the given role; `visitor` means no session at all."""
    return ANONYMOUS if role is Role.VISITOR else user(role)


# --------------------------------------------------------------------------- #
# Role rank
# --------------------------------------------------------------------------- #


def test_roles_are_strictly_ordered() -> None:
    ranks = [role.rank for role in ALL_ROLES]
    assert ranks == sorted(ranks) and len(set(ranks)) == len(ranks)


def test_at_least_is_inclusive() -> None:
    assert Role.ORGANIZER.at_least(Role.ORGANIZER)
    assert Role.ADMIN.at_least(Role.ORGANIZER)
    assert not Role.JUDGE.at_least(Role.ORGANIZER)


# --------------------------------------------------------------------------- #
# The event surface
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("role", ALL_ROLES)
def test_published_events_are_readable_by_everyone(role: Role) -> None:
    assert check_access(actor(role), event(published=True), Action.READ)


@pytest.mark.parametrize("role", ALL_ROLES)
def test_unpublished_events_are_staff_only(role: Role) -> None:
    permitted = check_access(actor(role), event(published=False), Action.READ)
    assert permitted is role.at_least(Role.ORGANIZER)


def test_an_unpublished_event_is_readable_by_the_person_who_made_it() -> None:
    creator = user(Role.PARTICIPANT)
    ev = event(published=False)
    ev.created_by_id = creator.id
    assert check_access(creator, ev, Action.READ)


@pytest.mark.parametrize("role", ALL_ROLES)
def test_only_staff_create_events(role: Role) -> None:
    assert check_access(actor(role), PLATFORM, Action.CREATE_EVENT) is role.at_least(
        Role.ORGANIZER
    )


@pytest.mark.parametrize("role", ALL_ROLES)
def test_only_staff_configure_an_event(role: Role) -> None:
    ev = event()
    for action in (Action.UPDATE, Action.DELETE, Action.MANAGE):
        assert check_access(actor(role), ev, action) is role.at_least(Role.ORGANIZER)


def test_event_children_inherit_the_events_rules() -> None:
    ev = event(published=False)
    track = Track(id=uuid.uuid4(), event_id=ev.id, key="k", name="K")
    track.event = ev
    assert not check_access(ANONYMOUS, track, Action.READ)
    assert check_access(user(Role.ORGANIZER), track, Action.READ)
    assert not check_access(user(Role.PARTICIPANT), track, Action.UPDATE)
    assert check_access(user(Role.ORGANIZER), track, Action.UPDATE)


# --------------------------------------------------------------------------- #
# Teams
# --------------------------------------------------------------------------- #


def test_the_invite_token_is_not_public() -> None:
    ev = event()
    owner, member, stranger = user(), user(), user()
    t = team(ev, owner, member)

    assert check_access(owner, t, Action.READ_INVITE)
    assert check_access(member, t, Action.READ_INVITE)
    assert not check_access(stranger, t, Action.READ_INVITE)
    assert not check_access(ANONYMOUS, t, Action.READ_INVITE)
    # ...even though the team itself is public on a published event.
    assert check_access(stranger, t, Action.READ)


def test_only_the_owner_manages_a_team() -> None:
    ev = event()
    owner, member = user(), user()
    t = team(ev, owner, member)

    assert check_access(owner, t, Action.MANAGE)
    assert not check_access(member, t, Action.MANAGE)
    assert check_access(user(Role.ORGANIZER), t, Action.MANAGE)


def test_joining_closes_with_registration() -> None:
    closed = event(open_window=False)
    t = team(closed, user())
    assert not check_access(user(), t, Action.JOIN_TEAM)


def test_you_cannot_join_a_team_you_are_already_on() -> None:
    ev = event()
    owner = user()
    assert not check_access(owner, team(ev, owner), Action.JOIN_TEAM)


# --------------------------------------------------------------------------- #
# Submissions: reading
# --------------------------------------------------------------------------- #


def test_a_submitted_project_in_a_published_event_is_public() -> None:
    ev = event()
    s = submission(ev, team(ev, user()))
    assert check_access(ANONYMOUS, s, Action.READ)


def test_a_draft_is_not_public() -> None:
    ev = event()
    owner, stranger = user(), user()
    s = submission(ev, team(ev, owner), status=SubmissionStatus.DRAFT)

    assert check_access(owner, s, Action.READ)
    assert check_access(user(Role.ORGANIZER), s, Action.READ)
    assert not check_access(stranger, s, Action.READ)
    assert not check_access(ANONYMOUS, s, Action.READ)


def test_a_submitted_project_in_an_unpublished_event_is_not_public() -> None:
    ev = event(published=False)
    s = submission(ev, team(ev, user()))
    assert not check_access(ANONYMOUS, s, Action.READ)
    assert not check_access(user(), s, Action.READ)


# --------------------------------------------------------------------------- #
# Submissions: the deadline
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "action", [Action.UPDATE, Action.SUBMIT, Action.UNSUBMIT, Action.DELETE]
)
def test_the_deadline_closes_every_write_path(action: Action) -> None:
    """Not "the edit form is hidden": every write verb, refused by the predicate."""
    closed = event(open_window=False)
    owner = user()
    s = submission(closed, team(closed, owner), status=SubmissionStatus.DRAFT)
    assert not check_access(owner, s, action)


@pytest.mark.parametrize("action", [Action.UPDATE, Action.SUBMIT])
def test_writes_are_permitted_before_the_deadline(action: Action) -> None:
    ev = event(open_window=True)
    owner = user()
    s = submission(ev, team(ev, owner), status=SubmissionStatus.DRAFT)
    assert check_access(owner, s, action)


def test_the_deadline_is_evaluated_against_the_clock_we_pass_in() -> None:
    """The same submission, two different moments, two different answers --
    which is what makes the deadline a property of the system and not of the
    moment the test happened to run."""
    ev = event(open_window=True)
    owner = user()
    s = submission(ev, team(ev, owner), status=SubmissionStatus.DRAFT)

    assert check_access(owner, s, Action.UPDATE, now=ev.submission_deadline - timedelta(seconds=1))
    assert not check_access(owner, s, Action.UPDATE, now=ev.submission_deadline)
    assert not check_access(owner, s, Action.UPDATE, now=ev.submission_deadline + timedelta(days=1))


def test_staff_may_edit_after_the_deadline_and_that_is_on_purpose() -> None:
    closed = event(open_window=False)
    s = submission(closed, team(closed, user()))
    assert check_access(user(Role.ORGANIZER), s, Action.UPDATE)
    assert check_access(user(Role.ADMIN), s, Action.UPDATE)


def test_a_stranger_cannot_edit_an_open_submission() -> None:
    ev = event()
    s = submission(ev, team(ev, user()), status=SubmissionStatus.DRAFT)
    assert not check_access(user(), s, Action.UPDATE)
    assert not check_access(ANONYMOUS, s, Action.UPDATE)


def test_a_judge_is_not_a_participant_with_extra_powers() -> None:
    """Rank is a floor, not a licence: a judge outranks a participant globally
    and still cannot touch somebody else's project."""
    ev = event()
    s = submission(ev, team(ev, user()), status=SubmissionStatus.DRAFT)
    assert not check_access(user(Role.JUDGE), s, Action.UPDATE)


def test_a_draft_cannot_be_deleted_once_submitted() -> None:
    ev = event()
    owner = user()
    s = submission(ev, team(ev, owner), status=SubmissionStatus.SUBMITTED)
    assert not check_access(owner, s, Action.DELETE)


# --------------------------------------------------------------------------- #
# Accounts
# --------------------------------------------------------------------------- #


def test_a_deactivated_account_can_do_nothing_at_all() -> None:
    ev = event()
    owner = user(active=False)
    t = team(ev, owner)
    s = submission(ev, t, status=SubmissionStatus.DRAFT)

    assert not check_access(owner, ev, Action.READ)  # not even a published event
    assert not check_access(owner, t, Action.READ)
    assert not check_access(owner, s, Action.UPDATE)


def test_only_an_admin_changes_roles() -> None:
    target = user()
    assert not check_access(user(Role.ORGANIZER), target, Action.MANAGE)
    assert check_access(user(Role.ADMIN), target, Action.MANAGE)


def test_a_user_may_read_themselves_and_not_their_neighbour() -> None:
    a, b = user(), user()
    assert check_access(a, a, Action.READ)
    assert not check_access(a, b, Action.READ)
    assert check_access(user(Role.ORGANIZER), b, Action.READ)


# --------------------------------------------------------------------------- #
# Fail closed
# --------------------------------------------------------------------------- #


def test_an_unknown_resource_type_is_denied() -> None:
    """Adding a model must not open a hole by default."""

    class Whatever:
        pass

    assert not check_access(user(Role.ADMIN), Whatever(), Action.READ)


def test_an_action_a_resource_does_not_define_is_denied() -> None:
    assert not check_access(user(Role.ADMIN), event(), Action.SUBMIT)
    assert not check_access(user(Role.ADMIN), PLATFORM, Action.READ)


# --------------------------------------------------------------------------- #
# Judging (Phase 2) -- FIG. 02, the rows Phase 1 could not claim
# --------------------------------------------------------------------------- #


def judging_event(*, judging_open: bool = True) -> Event:
    ev = event()
    ev.judging_opens_at = NOW - timedelta(hours=1) if judging_open else NOW + timedelta(days=1)
    ev.judging_closes_at = NOW + timedelta(days=3)
    return ev


def judge_row(ev: Event, u: User, *, track: Track | None = None, active: bool = True) -> Judge:
    j = Judge(
        id=uuid.uuid4(),
        event_id=ev.id,
        user_id=u.id,
        track_id=track.id if track else None,
        is_active=active,
    )
    j.event = ev
    j.user = u
    j.track = track
    return j


def ballot(j: Judge, s: Submission, *, scores: int = 0) -> JudgeAssignment:
    a = JudgeAssignment(
        id=uuid.uuid4(),
        event_id=j.event_id,
        judge_id=j.id,
        submission_id=s.id,
        status=AssignmentStatus.PENDING,
    )
    a.judge = j
    a.submission = s
    return a


def track_row(ev: Event, key: str) -> Track:
    return Track(id=uuid.uuid4(), event_id=ev.id, key=key, name=key.title(), position=0)


# -- the aggregate is staff-only ------------------------------------------- #


@pytest.mark.parametrize(
    "action",
    [
        Action.MANAGE_JUDGES,
        Action.ASSIGN,
        Action.READ_PROGRESS,
        Action.READ_RESULTS,
        Action.EXPORT,
    ],
)
@pytest.mark.parametrize("role", [Role.VISITOR, Role.PARTICIPANT, Role.JUDGE])
def test_nobody_below_organizer_touches_the_judging_administration(
    role: Role, action: Action
) -> None:
    """FIG. 02: a judge sees neither the aggregate nor the audit surface."""
    assert check_access(actor(role), judging_event(), action) is False


@pytest.mark.parametrize(
    "action",
    [
        Action.MANAGE_JUDGES,
        Action.ASSIGN,
        Action.READ_PROGRESS,
        Action.READ_RESULTS,
        Action.EXPORT,
    ],
)
@pytest.mark.parametrize("role", [Role.ORGANIZER, Role.ADMIN])
def test_staff_administer_judging(role: Role, action: Action) -> None:
    assert check_access(actor(role), judging_event(), action) is True


# -- own ballot vs a peer's ------------------------------------------------ #


def test_a_judge_reads_their_own_ballot_and_not_a_peers() -> None:
    """The single most important assertion in this file."""
    ev = judging_event()
    tm = team(ev, user())
    sub = submission(ev, tm)

    mine, theirs = user(Role.JUDGE), user(Role.JUDGE)
    my_ballot = ballot(judge_row(ev, mine), sub)
    their_ballot = ballot(judge_row(ev, theirs), sub)

    assert check_access(mine, my_ballot, Action.READ) is True
    assert check_access(mine, their_ballot, Action.READ) is False


def test_a_judge_writes_their_own_ballot_and_not_a_peers() -> None:
    ev = judging_event()
    sub = submission(ev, team(ev, user()))
    mine, theirs = user(Role.JUDGE), user(Role.JUDGE)
    my_ballot = ballot(judge_row(ev, mine), sub)
    their_ballot = ballot(judge_row(ev, theirs), sub)

    assert check_access(mine, my_ballot, Action.SCORE) is True
    assert check_access(mine, their_ballot, Action.SCORE) is False


def test_staff_read_every_ballot() -> None:
    ev = judging_event()
    sub = submission(ev, team(ev, user()))
    a = ballot(judge_row(ev, user(Role.JUDGE)), sub)
    for role in (Role.ORGANIZER, Role.ADMIN):
        assert check_access(actor(role), a, Action.READ) is True


def test_staff_may_not_author_a_ballot() -> None:
    """The one place organizer privilege stops, and it is deliberate.

    An organizer who can write a ballot can forge a result, and nothing
    downstream could tell the forgery from the judgement.
    """
    ev = judging_event()
    sub = submission(ev, team(ev, user()))
    a = ballot(judge_row(ev, user(Role.JUDGE)), sub)
    for role in (Role.ORGANIZER, Role.ADMIN):
        assert check_access(actor(role), a, Action.SCORE) is False


def test_staff_may_delete_a_ballot_they_cannot_write() -> None:
    """Destroying a ballot is administration; authoring one is judging."""
    ev = judging_event()
    sub = submission(ev, team(ev, user()))
    a = ballot(judge_row(ev, user(Role.JUDGE)), sub)
    assert check_access(actor(Role.ORGANIZER), a, Action.DELETE) is True


@pytest.mark.parametrize("role", [Role.VISITOR, Role.PARTICIPANT])
def test_a_participant_never_reads_a_ballot(role: Role) -> None:
    ev = judging_event()
    sub = submission(ev, team(ev, user()))
    a = ballot(judge_row(ev, user(Role.JUDGE)), sub)
    assert check_access(actor(role), a, Action.READ) is False


# -- track isolation ------------------------------------------------------- #


def test_a_track_judge_cannot_touch_another_track() -> None:
    ev = judging_event()
    alpha, beta = track_row(ev, "alpha"), track_row(ev, "beta")
    ev.tracks = [alpha, beta]

    tm = team(ev, user())
    in_beta = submission(ev, tm)
    in_beta.track_id = beta.id

    judge_user = user(Role.JUDGE)
    alpha_judge = judge_row(ev, judge_user, track=alpha)
    # Their own ballot, but on the wrong track: still denied.
    a = ballot(alpha_judge, in_beta)

    assert check_access(judge_user, a, Action.READ) is False
    assert check_access(judge_user, a, Action.SCORE) is False


def test_an_all_track_judge_reads_any_track() -> None:
    ev = judging_event()
    beta = track_row(ev, "beta")
    ev.tracks = [beta]
    sub = submission(ev, team(ev, user()))
    sub.track_id = beta.id

    judge_user = user(Role.JUDGE)
    a = ballot(judge_row(ev, judge_user, track=None), sub)
    assert check_access(judge_user, a, Action.READ) is True


def test_a_track_judge_cannot_read_an_untracked_project() -> None:
    """NULL is not a wildcard. Treating it as one would leak work through a
    data-entry gap."""
    ev = judging_event()
    alpha = track_row(ev, "alpha")
    ev.tracks = [alpha]
    sub = submission(ev, team(ev, user()))  # track_id stays None

    judge_user = user(Role.JUDGE)
    a = ballot(judge_row(ev, judge_user, track=alpha), sub)
    assert check_access(judge_user, a, Action.READ) is False


# -- the judging window and the submission state --------------------------- #


def test_scoring_is_closed_before_judging_opens() -> None:
    ev = judging_event(judging_open=False)
    sub = submission(ev, team(ev, user()))
    judge_user = user(Role.JUDGE)
    a = ballot(judge_row(ev, judge_user), sub)

    assert check_access(judge_user, a, Action.READ) is True, "reading is not writing"
    assert check_access(judge_user, a, Action.SCORE) is False


def test_an_unscheduled_judging_window_is_closed_not_open() -> None:
    """The safe default for a window nobody configured is shut."""
    ev = event()  # judging_opens_at is None
    assert ev.judging_open(NOW) is False
    sub = submission(ev, team(ev, user()))
    judge_user = user(Role.JUDGE)
    assert check_access(judge_user, ballot(judge_row(ev, judge_user), sub), Action.SCORE) is False


def test_a_draft_is_never_scorable() -> None:
    ev = judging_event()
    sub = submission(ev, team(ev, user()), status=SubmissionStatus.DRAFT)
    judge_user = user(Role.JUDGE)
    a = ballot(judge_row(ev, judge_user), sub)
    assert check_access(judge_user, a, Action.SCORE) is False


def test_a_deactivated_judge_record_is_not_a_judge() -> None:
    ev = judging_event()
    sub = submission(ev, team(ev, user()))
    judge_user = user(Role.JUDGE)
    a = ballot(judge_row(ev, judge_user, active=False), sub)

    assert check_access(judge_user, a, Action.READ) is False
    assert check_access(judge_user, a, Action.SCORE) is False


# -- a score inherits its ballot's rules ----------------------------------- #


def test_a_score_is_governed_by_its_ballot() -> None:
    """One rule, not two to drift apart."""
    ev = judging_event()
    sub = submission(ev, team(ev, user()))
    mine, theirs = user(Role.JUDGE), user(Role.JUDGE)

    my_ballot = ballot(judge_row(ev, mine), sub)
    their_ballot = ballot(judge_row(ev, theirs), sub)
    my_score = Score(id=uuid.uuid4(), event_id=ev.id, assignment_id=my_ballot.id,
                     criterion_id=uuid.uuid4(), value=4)
    my_score.assignment = my_ballot
    their_score = Score(id=uuid.uuid4(), event_id=ev.id, assignment_id=their_ballot.id,
                        criterion_id=uuid.uuid4(), value=4)
    their_score.assignment = their_ballot

    assert check_access(mine, my_score, Action.READ) is True
    assert check_access(mine, their_score, Action.READ) is False


# -- the judge record itself ----------------------------------------------- #


def test_a_judge_reads_their_own_record_and_not_a_peers() -> None:
    """How the UI knows whether this judge is a track judge -- and no more."""
    ev = judging_event()
    mine, theirs = user(Role.JUDGE), user(Role.JUDGE)
    assert check_access(mine, judge_row(ev, mine), Action.READ) is True
    assert check_access(mine, judge_row(ev, theirs), Action.READ) is False


# -- the rubric is public, its editing is not ------------------------------ #


def test_the_rubric_is_readable_by_anyone_who_can_see_the_event() -> None:
    """You are entitled to know what you are being judged against."""
    ev = judging_event()
    criterion = RubricCriterion(
        id=uuid.uuid4(), event_id=ev.id, key="impact", name="Impact", weight=1,
        min_score=1, max_score=5, position=0,
    )
    criterion.event = ev
    for role in ALL_ROLES:
        assert check_access(actor(role), criterion, Action.READ) is True


@pytest.mark.parametrize("role", [Role.VISITOR, Role.PARTICIPANT, Role.JUDGE])
def test_only_staff_edit_the_rubric(role: Role) -> None:
    ev = judging_event()
    criterion = RubricCriterion(
        id=uuid.uuid4(), event_id=ev.id, key="impact", name="Impact", weight=1,
        min_score=1, max_score=5, position=0,
    )
    criterion.event = ev
    assert check_access(actor(role), criterion, Action.UPDATE) is False
