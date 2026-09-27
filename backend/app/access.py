"""Authorization. All of it.

Every route that touches a non-public resource calls `check_access()` before it
does anything else. Not a decorator with per-route logic, not a frontend
`if (role === 'judge')`, not a query that quietly returns fewer rows. One
function, one place to read, one place to audit.

The rule this buys us, stated once and enforced everywhere: **removing every
check from the UI must not change what the API permits.**

Role rank (`visitor < participant < judge < organizer < admin`) is a floor, not
the whole answer. The interesting cases here are relationship checks -- "is this
user on that team" -- and time checks -- "is the submission deadline past". Both
live in this file so that Phase 2 role isolation and Phase 3 hidden results are
one more branch each, not a new pass over the codebase.

Deliberate exceptions, named rather than hidden:

* Organizers and admins can edit a submission after the deadline. Someone has to
  be able to fix a broken repo link in a hurry, and pretending otherwise means it
  happens in psql instead. Phase 3 writes these to the audit log.
* There is no blanket `if admin: return True`. Admin is granted by the same
  branches as organizer, so every grant is visible at the point it is made.
* **Staff may read every ballot and may not write one.** `Action.SCORE` is the
  single place in this file where organizer privilege stops, and it is deliberate:
  an organizer who can author a judge's score can forge a result, and nothing
  downstream could tell the forgery from the judgement. Deleting an assignment
  is allowed; authoring its scores is not.
* **Admin levels refine `Role.ADMIN`, they do not replace it.** An admin is an
  `owner` (everything, and the only level that may administer accounts), a
  `manager` (everything except administering accounts) or an `auditor`
  (read-only). The rank comparisons above are untouched; the two account-
  administration predicates and one verb gate in `check_access` are where levels
  bite, and `app.deps.get_current_principal` holds an auditor to safe HTTP
  methods as the second, route-independent line.
* **Results are closed until somebody opens them.** `results_public_at` is NULL on
  every new event, so `READ_TALLY` is staff-only by default and becomes public at a
  moment an organizer chooses. The brief asks for results hidden during the voting
  window; defaulting to hidden is the version of that which cannot be got wrong by
  forgetting a step. Note that publishing the community tally does **not** publish
  the judging aggregate -- `READ_RESULTS` stays staff-only for the whole of T3.
"""

from __future__ import annotations

import enum
import uuid
from dataclasses import dataclass
from datetime import datetime

from fastapi import HTTPException
from fastapi import status as http_status

from .models import (
    AdminLevel,
    Announcement,
    Comment,
    Event,
    EventQuestion,
    Judge,
    JudgeAssignment,
    Prize,
    Role,
    RubricCriterion,
    Score,
    Submission,
    SubmissionStatus,
    Team,
    Track,
    User,
    Vote,
    Voter,
    VotingAccess,
    utcnow,
)


class Action(str, enum.Enum):
    """What is being attempted.

    Intentionally verb-per-operation rather than CRUD-only: `SUBMIT` and
    `UPDATE` on a submission have different rules, and collapsing them into
    "write" is how a deadline gets bypassed.
    """

    READ = "read"
    UPDATE = "update"
    DELETE = "delete"
    MANAGE = "manage"
    # Archiving and unarchiving are their own verb, not `MANAGE`: an archived event
    # refuses `MANAGE` (and every other write), and unarchiving has to remain
    # possible or archival would be a trap.
    ARCHIVE = "archive"

    CREATE_EVENT = "create_event"
    LIST_USERS = "list_users"
    # Admin-provisioned accounts (judges, organizers, or another admin) --
    # narrower than `LIST_USERS`, which any staff member gets: this is the
    # same audience as `Action.MANAGE` on an existing `User` (role
    # assignment), because creating an account with a role already attached
    # is that same grant, just for a person who does not have an account
    # yet. See `routers/users.py`'s `create_user`.
    CREATE_USER = "create_user"
    REGISTER = "register"
    CREATE_TEAM = "create_team"
    JOIN_TEAM = "join_team"
    READ_INVITE = "read_invite"
    CREATE_SUBMISSION = "create_submission"
    SUBMIT = "submit"
    UNSUBMIT = "unsubmit"

    # Judging (Phase 2). `SCORE` is separate from `UPDATE` for the same reason
    # `SUBMIT` is: authoring a ballot and administering one are different rights,
    # and collapsing them is how an organizer ends up able to forge a result.
    MANAGE_JUDGES = "manage_judges"
    ASSIGN = "assign"
    SCORE = "score"
    READ_PROGRESS = "read_progress"
    READ_RESULTS = "read_results"

    # Pairwise judging (Phase 5). A separate verb from `SCORE` for the same
    # reason `SCORE` is separate from `UPDATE`: fetching a pair to compare and
    # answering "which is better" are a judge's own right, checked against
    # their own `Judge` row, not against the rubric-scoring machinery `SCORE`
    # protects. Reading the fitted ranking reuses `READ_RESULTS` rather than a
    # new verb -- the rule is identical (staff-only, full stop), and a Bradley-
    # Terry ranking is exactly the same kind of secret a normalized score table
    # already is.
    COMPARE = "compare"
    EXPORT = "export"

    # Public voting and comments (Phase 3). `READ_TALLY` is separate from
    # `READ_RESULTS` because they are two different secrets: an organizer may
    # publish the community vote without publishing the judges' scores.
    CLAIM_BALLOT = "claim_ballot"
    VOTE = "vote"
    READ_TALLY = "read_tally"

    # Phase 6 (Part 5): the public results dashboard -- a *third* secret,
    # separate again from `READ_TALLY` (a vote count) and `READ_RESULTS`
    # (every statistic, staff-only, always). Time-gated the same way
    # `READ_TALLY` is, by the same `results_public()` switch, because an
    # organizer flipping one to reveal the other but not this would be a
    # distinction nobody asked for.
    READ_PUBLIC_RESULTS = "read_public_results"
    # A team's own project only -- checked against that `Submission`, not the
    # `Event`, which is what makes this a different rule from the one above
    # rather than the same one reused. See `_check_submission`.
    READ_JUDGE_REPORT = "read_judge_report"

    COMMENT = "comment"
    MODERATE = "moderate"
    READ_AUDIT = "read_audit"

    # Platform-level, admin-only (Phase 5). `READ_AUDIT` above is deliberately
    # scoped to one event and open to any organizer *of that event*; this is the
    # cross-event view, and it is narrower on purpose. An organizer for event A
    # has no business reading event B's trail, so this is `Role.ADMIN` alone,
    # not `_is_staff` (organizer-or-admin). It exists because `audit_log.event_id`
    # is `ON DELETE SET NULL`: deleting an event orphans that event's history
    # rather than destroying it, and an orphaned entry has no event left to be
    # scoped under -- the per-event endpoint can no longer reach it by
    # construction, so this is the only place it is still readable.
    READ_GLOBAL_AUDIT = "read_global_audit"


@dataclass(frozen=True)
class Anonymous:
    """A request with no session. Has a role so it needs no special-casing."""

    role: Role = Role.VISITOR
    id: uuid.UUID | None = None
    is_active: bool = True

    @property
    def display_name(self) -> str:
        return "visitor"


ANONYMOUS = Anonymous()

Principal = User | Anonymous


class Platform:
    """Sentinel for actions that have no row yet -- creating an event."""

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "<Platform>"


PLATFORM = Platform()


# --------------------------------------------------------------------------- #
# Relationship helpers
# --------------------------------------------------------------------------- #


def _is_member(user: Principal, team: Team) -> bool:
    return user.id is not None and user.id in team.member_ids()


def _is_owner(user: Principal, team: Team) -> bool:
    return user.id is not None and user.id in team.owner_ids()


def _is_staff(user: Principal) -> bool:
    """Organizer or admin: the people who run the event."""
    return user.role.at_least(Role.ORGANIZER)


# What an `AUDITOR` admin may do: every verb that only reads. Anything not
# listed is refused, so a verb added later is read-only-denied by default rather
# than silently allowed. `READ_INVITE` is left out on purpose -- an invite link
# is a capability to join a team, not just information about one.
READ_ONLY_ACTIONS = frozenset(
    {
        Action.READ,
        Action.LIST_USERS,
        Action.READ_PROGRESS,
        Action.READ_RESULTS,
        Action.EXPORT,
        Action.READ_TALLY,
        Action.READ_PUBLIC_RESULTS,
        Action.READ_JUDGE_REPORT,
        Action.READ_AUDIT,
        Action.READ_GLOBAL_AUDIT,
    }
)


def admin_level(user: Principal) -> AdminLevel | None:
    """The caller's admin level, or None if they are not an admin at all."""
    if isinstance(user, User) and user.role is Role.ADMIN:
        return user.admin_level
    return None


def _event_of(resource: object) -> Event | None:
    """The event a resource belongs to, or None for one that belongs to none.

    Used for one thing: finding out whether that event is archived. Every branch
    here is a relationship the model already has; a resource type missing from
    this list is simply not frozen by archival, which is why the list mirrors the
    dispatch in `check_access` below rather than being written independently.
    """
    if isinstance(resource, Event):
        return resource
    if isinstance(
        resource,
        (
            Track, Prize, EventQuestion, RubricCriterion,
            Judge, Announcement, Team, Submission, Voter,
        ),
    ):
        return resource.event
    if isinstance(resource, JudgeAssignment):
        return resource.submission.event
    if isinstance(resource, Score):
        return resource.assignment.submission.event
    if isinstance(resource, Vote):
        return resource.voter.event
    if isinstance(resource, Comment):
        return resource.submission.event
    return None


def _is_account_admin(user: Principal) -> bool:
    """May administer accounts: create users, change roles and levels,
    deactivate. Admin *and* owner-level -- a manager or auditor holds every
    other admin power (or none, for an auditor) but not this one."""
    return admin_level(user) is AdminLevel.OWNER


def _event_visible(user: Principal, event: Event) -> bool:
    if event.is_published:
        return True
    if _is_staff(user):
        return True
    return user.id is not None and event.created_by_id == user.id


# --------------------------------------------------------------------------- #
# The one function
# --------------------------------------------------------------------------- #


def check_access(
    user: Principal,
    resource: object,
    action: Action,
    *,
    now: datetime | None = None,
) -> bool:
    """Return True if `user` may perform `action` on `resource`.

    `now` is injectable so deadline behaviour is testable without waiting for a
    deadline; production callers leave it alone and get the server clock.
    """
    now = now or utcnow()

    # A deactivated account is not a lesser account, it is no account.
    if not user.is_active:
        return False

    # Read-only admins: refuse every verb that is not on the read list, before
    # any resource-specific branch can grant it.
    if admin_level(user) is AdminLevel.AUDITOR and action not in READ_ONLY_ACTIONS:
        return False

    # Archived events are frozen: the same read list, and nothing else, for
    # everything that belongs to one -- except ARCHIVE itself, which is how an
    # organizer un-freezes it. Sits above the per-resource branches for the same
    # reason the auditor gate does: no branch below can grant what this refuses.
    if action is not Action.ARCHIVE and action not in READ_ONLY_ACTIONS:
        owning_event = _event_of(resource)
        if owning_event is not None and owning_event.archived_at is not None:
            return False

    if isinstance(resource, Platform):
        return _check_platform(user, action)
    if isinstance(resource, Event):
        return _check_event(user, resource, action, now)
    if isinstance(resource, (Track, Prize, EventQuestion, RubricCriterion)):
        # Event configuration inherits its parent's rules exactly. The rubric is
        # configuration too: everyone who can see the event can read the criteria
        # they are being judged against, and only staff can change them.
        return _check_event_child(user, resource.event, action)
    if isinstance(resource, Judge):
        return _check_judge(user, resource, action, now)
    if isinstance(resource, JudgeAssignment):
        return _check_assignment(user, resource, action, now)
    if isinstance(resource, Score):
        # A score is not its own subject: it is readable and writable exactly
        # when the ballot it hangs off is. One rule, not two to drift apart.
        return _check_assignment(user, resource.assignment, action, now)
    if isinstance(resource, Voter):
        return _check_voter(user, resource, action, now)
    if isinstance(resource, Vote):
        # Same pattern as Score: a vote inherits its voter's rules.
        return _check_voter(user, resource.voter, action, now)
    if isinstance(resource, Comment):
        return _check_comment(user, resource, action)
    if isinstance(resource, Announcement):
        return _check_announcement(user, resource, action)
    if isinstance(resource, Team):
        return _check_team(user, resource, action, now)
    if isinstance(resource, Submission):
        return _check_submission(user, resource, action, now)
    if isinstance(resource, User):
        return _check_user(user, resource, action)

    # Unknown resource type: deny. Adding a model must not silently open a hole.
    return False


def _check_platform(user: Principal, action: Action) -> bool:
    if action in (Action.CREATE_EVENT, Action.LIST_USERS):
        return _is_staff(user)
    if action is Action.READ_GLOBAL_AUDIT:
        return user.role.at_least(Role.ADMIN)
    if action is Action.CREATE_USER:
        return _is_account_admin(user)
    return False


def _check_event(user: Principal, event: Event, action: Action, now: datetime) -> bool:
    if action is Action.READ:
        return _event_visible(user, event)

    if action in (Action.UPDATE, Action.DELETE, Action.MANAGE, Action.ARCHIVE):
        return _is_staff(user)

    if action is Action.CLAIM_BALLOT:
        # Who may hold a ballot at all. The narrowest mode wins: an event set to
        # AUTHENTICATED does not hand ballots to visitors however they ask.
        if not event.is_published:
            return False
        # The winner/community-vote split, when an event uses it, narrows
        # further still -- to a rule `voting_access` cannot express at all,
        # since it is about *when* an account showed up, not what kind of
        # account it is. This overrides `voting_access` rather than adding to
        # it: `OPEN_LINK`/`EMAIL_GATED` ballots have no registration to check
        # a cutoff against, so the community-tier round is registered-and-
        # authenticated only, full stop, whatever this event's general
        # `voting_access` says.
        if event.community_vote_slots > 0:
            return event.community_vote_eligible(user.id)
        if event.voting_access is VotingAccess.AUTHENTICATED:
            return user.role.at_least(Role.PARTICIPANT)
        return True

    if action is Action.COMMENT:
        # Authenticated only, and that is anti-abuse rather than an oversight: an
        # anonymous comment box on a public gallery is a spam endpoint.
        return (
            event.is_published
            and event.comments_enabled
            and user.role.at_least(Role.PARTICIPANT)
        )

    if action is Action.READ_TALLY:
        # One of two time-gated public reads in the system -- see
        # `READ_PUBLIC_RESULTS` below for the other.
        return _is_staff(user) or event.results_public(now)

    if action is Action.READ_PUBLIC_RESULTS:
        return _is_staff(user) or event.results_public(now)

    if action in (
        Action.MANAGE_JUDGES,
        Action.ASSIGN,
        Action.READ_PROGRESS,
        Action.READ_RESULTS,
        Action.EXPORT,
        Action.MODERATE,
        Action.READ_AUDIT,
    ):
        # FIG. 02: a judge sees neither the aggregate nor who else is judging.
        # During T2 the results of an event are staff-only, full stop; T3 adds a
        # public reveal once the voting window closes.
        return _is_staff(user)

    if action in (Action.REGISTER, Action.CREATE_TEAM):
        # Registering and forming a team share one window and one rule --
        # `Event.registration_open()` is the single admin-configured "is
        # registration open" answer, not two that could drift apart.
        if _is_staff(user):
            return True
        return (
            user.role.at_least(Role.PARTICIPANT)
            and event.is_published
            and event.registration_open(now)
        )

    return False


def _check_event_child(user: Principal, event: Event, action: Action) -> bool:
    if action is Action.READ:
        return _event_visible(user, event)
    return _is_staff(user)


def _check_team(user: Principal, team: Team, action: Action, now: datetime) -> bool:
    if action is Action.READ:
        # Team names are public on a published event, as they are everywhere
        # else in this category. The invite token is not -- see READ_INVITE.
        return _event_visible(user, team.event) and (
            team.event.is_published or _is_member(user, team) or _is_staff(user)
        )

    if action is Action.READ_INVITE:
        # Any member may invite; that is what the link is for.
        return _is_member(user, team) or _is_staff(user)

    if action in (Action.UPDATE, Action.DELETE, Action.MANAGE):
        return _is_owner(user, team) or _is_staff(user)

    if action is Action.JOIN_TEAM:
        if _is_member(user, team):
            return False  # already in; joining again is not a thing
        if _is_staff(user):
            return True
        return (
            user.role.at_least(Role.PARTICIPANT)
            and team.event.is_published
            and team.event.registration_open(now)
        )

    if action is Action.CREATE_SUBMISSION:
        if _is_staff(user):
            return True
        return _is_member(user, team) and team.event.submissions_open(now)

    return False


def _check_submission(
    user: Principal, submission: Submission, action: Action, now: datetime
) -> bool:
    team = submission.team
    event = submission.event

    if action is Action.READ:
        if submission.status is SubmissionStatus.SUBMITTED and event.is_published:
            return True  # this is the gallery
        return _is_member(user, team) or _is_staff(user)

    if action is Action.MANAGE:
        return _is_staff(user)

    if action in (Action.UPDATE, Action.SUBMIT, Action.UNSUBMIT, Action.DELETE):
        # The documented exception: staff are not bound by the deadline.
        if _is_staff(user):
            return True
        if not _is_member(user, team):
            return False
        # The deadline. One comparison, one place, every write path.
        if not event.submissions_open(now):
            return False
        if action is Action.DELETE:
            return submission.status is SubmissionStatus.DRAFT
        return True

    if action is Action.READ_JUDGE_REPORT:
        # A team's own project only -- checked against membership, not
        # against the gallery-visibility branch `READ` uses above, so a
        # stranger who could browse this project in a published gallery
        # still cannot read its judges' feedback. Staff bypass for the same
        # reason they bypass everywhere else: reviewing what a team was
        # shown is part of running the event.
        if _is_staff(user):
            return True
        return _is_member(user, team) and event.results_public(now)

    return False


def _check_judge(user: Principal, judge: Judge, action: Action, now: datetime) -> bool:
    """A judge record. Who *is* judging is staff information.

    A judge may read their own record -- that is how the UI knows whether they
    are a track judge -- and nothing about their peers. `MANAGE_JUDGES` on the
    event is what invites and removes them.
    """
    is_self = user.id is not None and user.id == judge.user_id

    if action is Action.READ:
        return is_self or _is_staff(user)

    if action is Action.COMPARE:
        # Mirrors `_check_assignment`'s SCORE branch closely on purpose: same
        # three conditions (own it, be active, be inside the window), even
        # though pairwise has no per-row `JudgeAssignment` to check them
        # against -- the judge's own row and the event's judging window are the
        # whole of it. Staff privilege stops here too, for the reason
        # `_check_assignment` names: comparing on a judge's behalf would make a
        # forged ranking as easy as a forged score.
        if not (is_self and judge.is_active):
            return False
        if not judge.event.pairwise_enabled:
            return False
        return judge.event.judging_open(now)

    return _is_staff(user)


def _check_assignment(
    user: Principal, assignment: JudgeAssignment, action: Action, now: datetime
) -> bool:
    """The ballot. This function is the whole of T2 role isolation.

    Three conditions have to hold for a judge, and they are separate on purpose:

    1. **Ownership.** The assignment points at a `judges` row whose `user_id` is
       this caller. A peer's ballot fails here -- the answer is 403, and the
       acceptance suite is documented as expecting exactly that.
    2. **The judge is still active.** A revoked judge is not a judge.
    3. **Track.** A track judge may only touch work inside their track. The
       assignment algorithm already refuses to create a cross-track ballot, so
       this is the belt to that braces: if such a row ever existed, reading it
       would still be denied.
    """
    judge = assignment.judge
    owns_it = user.id is not None and judge is not None and judge.user_id == user.id
    in_track = judge is not None and judge.judges_track(assignment.submission.track_id)

    if action is Action.READ:
        if _is_staff(user):
            return True
        return owns_it and judge.is_active and in_track

    if action is Action.SCORE:
        # The one place staff privilege stops. See the module docstring.
        if not (owns_it and judge.is_active and in_track):
            return False
        # Scoring a project that was never submitted is not a thing, and neither
        # is scoring outside the window the organizer published.
        if assignment.submission.status is not SubmissionStatus.SUBMITTED:
            return False
        return assignment.submission.event.judging_open(now)

    if action in (Action.UPDATE, Action.DELETE, Action.MANAGE):
        return _is_staff(user)

    return False


def _check_voter(
    user: Principal, voter: Voter, action: Action, now: datetime
) -> bool:
    """A ballot belongs to exactly one voter.

    Note what is *not* here: any route that lets one voter read another's ballot.
    An organizer can read the tally and the audit log, and that is the whole of
    their visibility into voting -- deliberately, because a vote is secret in a way
    a judge's ballot is not. An organizer who can see who voted for what can lean
    on people, and the tally plus the audit trail is enough to investigate abuse
    without that.
    """
    event = voter.event

    if action is Action.READ:
        # Your own ballot, or staff for abuse investigation. Staff see the rows,
        # which the audit log would show them anyway.
        if _is_staff(user):
            return True
        return _owns_ballot(user, voter)

    if action is Action.VOTE:
        if not _owns_ballot(user, voter):
            return False
        if isinstance(user, User) and not user.is_active:
            return False
        # Same override as `CLAIM_BALLOT`: re-checked here too, not just at
        # claim time, because a ballot claimed before `community_vote_slots`
        # was ever set (or before this voter's eligibility would have been
        # re-evaluated) must not go on writing votes forever on the strength
        # of a ballot row that already exists.
        if event.community_vote_slots > 0 and not event.community_vote_eligible(user.id):
            return False
        # The window. One comparison, one place, every write path -- the same shape
        # as the submission deadline.
        return event.is_published and event.voting_open(now)

    if action in (Action.UPDATE, Action.DELETE, Action.MANAGE):
        return _is_staff(user)

    return False


def _owns_ballot(user: Principal, voter: Voter) -> bool:
    """Does this principal hold this ballot?

    Only the authenticated case can be decided here. An open-link or email-gated
    ballot is proven by a token the request carries, which the route checks before
    it ever gets here -- `check_access` sees a `Voter` the route has already
    matched to a token, so ownership for those modes is established upstream and
    this returns True for the row the route resolved.
    """
    if voter.user_id is not None:
        return user.id is not None and user.id == voter.user_id
    # Token-identified ballot: the route resolved it from the token it was given.
    return True


def _check_comment(user: Principal, comment: Comment, action: Action) -> bool:
    """Comments are public once posted, and hidden ones are not.

    Hiding rather than deleting means moderation is reversible and auditable, and
    it means this predicate has to keep hidden text away from everybody except
    staff -- including its own author, so that a moderated comment cannot be
    re-read and re-posted verbatim.
    """
    is_author = user.id is not None and user.id == comment.author_id

    if action is Action.READ:
        if comment.is_hidden:
            return _is_staff(user)
        return comment.submission.event.is_published or _is_staff(user)

    if action is Action.MODERATE:
        return _is_staff(user)

    if action in (Action.UPDATE, Action.DELETE):
        # An author may retract their own comment; staff may remove anybody's.
        return (is_author and not comment.is_hidden) or _is_staff(user)

    return False


def _check_announcement(user: Principal, announcement: Announcement, action: Action) -> bool:
    """`visible_to_visitors` is the whole rule: unset, only somebody at
    participant rank or above (or staff) sees it; set, anybody who can see
    the event at all does, the same as a gallery entry. Posting is gated on
    the event itself (`Action.MANAGE`), not here -- there is no row yet at
    that point.
    """
    if action is Action.READ:
        if not _event_visible(user, announcement.event):
            return False
        if announcement.visible_to_visitors:
            return True
        return user.role.at_least(Role.PARTICIPANT) or _is_staff(user)

    return False


def _check_user(user: Principal, target: User, action: Action) -> bool:
    is_self = user.id is not None and user.id == target.id

    if action is Action.READ:
        return is_self or _is_staff(user)
    if action is Action.UPDATE:
        return is_self or _is_account_admin(user)
    if action in (Action.MANAGE, Action.DELETE):
        # Role assignment and deactivation are the account-admin's alone. An
        # organizer who could mint organizers is an admin with extra steps --
        # and so is a manager who could mint owners, which is why this is the
        # owner level rather than "any admin".
        return _is_account_admin(user)
    return False


# --------------------------------------------------------------------------- #
# Route-facing wrappers
# --------------------------------------------------------------------------- #


# The gallery is the only collection served to callers who were never
# authorized for its members individually, so its SQL filter lives here, beside
# the predicate it has to agree with. `test_gallery_query_matches_predicate`
# asserts the two cannot drift: every row this filter admits is a row
# `check_access(ANONYMOUS, row, READ)` permits, and no row it excludes is.
PUBLIC_SUBMISSION_CRITERIA = (
    Submission.status == SubmissionStatus.SUBMITTED,
    Event.is_published.is_(True),
)


def require_access(
    user: Principal,
    resource: object,
    action: Action,
    *,
    status_code: int = http_status.HTTP_403_FORBIDDEN,
    now: datetime | None = None,
) -> None:
    """Raise unless permitted. Called before the handler does any work, so a
    denied request never reads the data it was denied.

    `status_code=404` is for resources whose *existence* is not public -- an
    unpublished event, another team's draft. A 403 there would answer a question
    the caller was not allowed to ask.
    """
    if check_access(user, resource, action, now=now):
        return
    if status_code == http_status.HTTP_404_NOT_FOUND:
        raise HTTPException(status_code=status_code, detail="Not found")
    raise HTTPException(
        status_code=status_code,
        detail=f"Not permitted: {action.value} on {type(resource).__name__.lower()}",
    )


def require_authenticated(user: Principal) -> User:
    """For routes that need a real account before a resource is even loaded."""
    if isinstance(user, User) and user.is_active:
        return user
    raise HTTPException(
        status_code=http_status.HTTP_401_UNAUTHORIZED, detail="Authentication required"
    )
