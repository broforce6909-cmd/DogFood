"""ORM models.

Tables land with the tier that needs them; this module is the schema source of
truth. `alembic/env.py` imports it to register every table on `Base.metadata`
before diffing or generating a migration -- see `alembic/versions/` for the
schema as actually applied, rather than trusting this file to describe it exactly
(a migration not yet written is a schema change that has not happened).

Phase 1 (T1 Core) tables:

    users, sessions, events, tracks, prizes, event_questions,
    teams, team_members, submissions, submission_answers

Phase 2 (T2 Judging) tables:

    judges, rubric_criteria, judge_assignments, scores

Phase 3 (T3 Public) tables:

    voters, votes, comments, audit_log, rate_limits

Phase 4 (T4 Stretch) tables:

    webhooks, webhook_deliveries, certificates

Two conventions run through the whole file and are worth reading once:

* **Timestamps are `timestamptz`, always UTC.** Deadline enforcement compares
  against the server clock, and a naive timestamp is a deadline-gaming vector.
* **Cross-entity consistency is a database constraint, not a convention.** A
  submission cannot point at a team from another event, and a user cannot join
  two teams in one event, because composite foreign keys and unique indexes say
  so -- not because every handler remembers to check.
"""

from __future__ import annotations

import enum
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Identity,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy import (
    Enum as SAEnum,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base

# The Postgres-dialect ARRAY, not the generic one: `tech_tags @> ARRAY['rust']`
# is how the gallery filters, and containment is the operator the GIN index on
# that column exists to serve. Postgres is a stated dependency of this project,
# so a type that only works there is a choice rather than an accident.

# --------------------------------------------------------------------------- #
# Enums
# --------------------------------------------------------------------------- #


class Role(str, enum.Enum):
    """Global role. Ordered: higher roles are strictly more privileged.

    Ordering is a *floor*, never the whole answer -- "a judge may read their own
    scores but not a peer's" is a relationship check, not a rank comparison.
    See `app.access`.
    """

    VISITOR = "visitor"
    PARTICIPANT = "participant"
    JUDGE = "judge"
    ORGANIZER = "organizer"
    ADMIN = "admin"

    @property
    def rank(self) -> int:
        return _ROLE_RANK[self]

    def at_least(self, other: Role) -> bool:
        """`role.at_least(Role.ORGANIZER)` -- reads better than an operator."""
        return self.rank >= other.rank


_ROLE_RANK: dict[Role, int] = {
    Role.VISITOR: 0,
    Role.PARTICIPANT: 1,
    Role.JUDGE: 2,
    Role.ORGANIZER: 3,
    Role.ADMIN: 4,
}


class AdminLevel(str, enum.Enum):
    """How much of `Role.ADMIN` an admin actually holds.

    `Role.ADMIN` stays one rung on the role ladder -- every `at_least(ADMIN)`
    check in the codebase keeps meaning what it meant -- and this refines it,
    only for accounts whose role is ADMIN:

    * `OWNER` -- everything an admin could always do. The only level that may
      administer accounts (create users, change roles or levels, deactivate).
    * `MANAGER` -- everything an owner can do *except* administer accounts.
    * `AUDITOR` -- read-only, everywhere. See `app.deps.get_current_principal`
      and `app.access.check_access` for the two places that hold it to that.

    Existing admins are `OWNER`, so introducing levels changed nothing for them.
    """

    OWNER = "owner"
    MANAGER = "manager"
    AUDITOR = "auditor"


class TeamRole(str, enum.Enum):
    OWNER = "owner"
    MEMBER = "member"


class SubmissionStatus(str, enum.Enum):
    """Two states in T1, a third in Phase 6.

    `DISQUALIFIED` is reached only through an admin action
    (`POST /submissions/{id}/disqualify`) and is deliberately not a fourth
    state alongside `DRAFT`/`SUBMITTED` in the ordinary edit/submit/unsubmit
    lifecycle those two describe -- see `_require_writable` in
    `routers/submissions.py` for why the team-facing routes refuse to touch
    it at all. The row, every ballot, and every vote already cast against it
    are never deleted; disqualifying only removes it from the gallery,
    judging assignment, voting, certificates and results from that point on,
    the same way `DRAFT` already does. See `app/routers/submissions.py`'s
    `disqualify`/`reinstate` routes.
    """

    DRAFT = "draft"
    SUBMITTED = "submitted"
    DISQUALIFIED = "disqualified"


class QuestionKind(str, enum.Enum):
    TEXT = "text"
    TEXTAREA = "textarea"
    URL = "url"
    SELECT = "select"
    CHECKBOX = "checkbox"


class VotingAccess(str, enum.Enum):
    """Who may vote, configured per event.

    The three the brief names. They are ordered by how much they cost an attacker,
    and `OPEN_LINK` is cheap on purpose: an organizer running a friendly internal
    demo should not have to stand up accounts for it.
    """

    OPEN_LINK = "open_link"
    EMAIL_GATED = "email_gated"
    AUTHENTICATED = "authenticated"

class VotingMethod(str, enum.Enum):
    """`SINGLE` is one-person-one-vote across a handful of picks. `QUADRATIC`
    gives each voter a credit budget where n votes on one project costs n²,
    so intensity is expressible but expensive. See JUDGING.md."""

    SINGLE = "single"
    QUADRATIC = "quadratic"

class AuditAction(str, enum.Enum):
    """The verbs the audit log records.

    A closed enum rather than free text: an organizer filtering the log needs the
    values to be finite, and a typo'd action string is a silently unsearchable
    entry. Adding a verb is a deliberate change, which is the point.
    """

    VOTE_CAST = "vote_cast"
    VOTE_CHANGED = "vote_changed"
    VOTE_WITHDRAWN = "vote_withdrawn"
    BALLOT_SCORED = "ballot_scored"
    COMMENT_POSTED = "comment_posted"
    COMMENT_HIDDEN = "comment_hidden"
    JUDGE_INVITED = "judge_invited"
    JUDGE_DEACTIVATED = "judge_deactivated"
    ASSIGNMENT_RUN = "assignment_run"
    ASSIGNMENT_DELETED = "assignment_deleted"
    RUBRIC_CHANGED = "rubric_changed"
    RESULTS_PUBLISHED = "results_published"
    SUBMISSION_EDITED_AFTER_DEADLINE = "submission_edited_after_deadline"
    ROLE_CHANGED = "role_changed"
    RATE_LIMITED = "rate_limited"
    # T4
    WEBHOOK_REGISTERED = "webhook_registered"
    WEBHOOK_REMOVED = "webhook_removed"
    CERTIFICATE_ISSUED = "certificate_issued"
    CERTIFICATE_REVOKED = "certificate_revoked"
    BULK_IMPORT = "bulk_import"
    BULK_IMPORT_UNDONE = "bulk_import_undone"
    # T5: judging integrity
    THIRD_REVIEW_ASSIGNED = "third_review_assigned"
    JUDGE_RECUSED = "judge_recused"

    # Phase 5: the audit log itself was audited, and these were the actions
    # that mutated something with no record of it -- despite five of the verbs
    # above (JUDGE_INVITED, JUDGE_DEACTIVATED, ASSIGNMENT_RUN,
    # ASSIGNMENT_DELETED, RUBRIC_CHANGED, ROLE_CHANGED) having been *defined*
    # since Phase 2 or 3 and never actually wired to a route. An enum member
    # nobody constructs is not an audit trail, it is a promise. These are that
    # promise kept, plus the events, teams and account-lifecycle actions that
    # were missing the same way.
    EVENT_CREATED = "event_created"
    EVENT_CHANGED = "event_changed"
    EVENT_DELETED = "event_deleted"
    EVENT_CONFIG_CHANGED = "event_config_changed"
    TEAM_CREATED = "team_created"
    TEAM_CHANGED = "team_changed"
    TEAM_DELETED = "team_deleted"
    TEAM_JOINED = "team_joined"
    TEAM_MEMBER_REMOVED = "team_member_removed"
    INVITE_ROTATED = "invite_rotated"
    JUDGE_REMOVED = "judge_removed"
    ACCOUNT_DEACTIVATED = "account_deactivated"
    ACCOUNT_REACTIVATED = "account_reactivated"

    # Pairwise judging. Its own verb, not folded into BALLOT_SCORED, because a
    # comparison is not a ballot -- there is no `JudgeAssignment` row and no
    # rubric involved, just an answer to "which is better".
    PAIRWISE_COMPARED = "pairwise_compared"

    # Phase 6: per-event registration, ahead of team formation.
    EVENT_REGISTERED = "event_registered"

    # Phase 6: admin disqualification, for cause -- see `SubmissionStatus`'s
    # own docstring for why this is a third status rather than a delete.
    SUBMISSION_DISQUALIFIED = "submission_disqualified"
    SUBMISSION_REINSTATED = "submission_reinstated"

    # Phase 6: removing a registration row itself. Unlike a judge or a team
    # member, a registration carries no ballots, scores or submission -- it is
    # a record of intent to participate and nothing downstream depends on it
    # existing, so this is a real (audited) hard delete rather than a status
    # flip like the two above.
    REGISTRATION_REMOVED = "registration_removed"

    # Phase 6: organizer announcements.
    ANNOUNCEMENT_POSTED = "announcement_posted"

    # Phase 6: admin override of a submission's final winner/community-tier
    # status. See `models.ResultOverride`.
    RESULT_OVERRIDDEN = "result_overridden"
    RESULT_OVERRIDE_CLEARED = "result_override_cleared"

    # Phase 6: admin-provisioned account (a judge or organizer created
    # directly, never through self-registration). See `routers/users.py`'s
    # `create_user`.
    USER_CREATED = "user_created"

    # Event archival: freeze an event and take it out of the default lists.
    EVENT_ARCHIVED = "event_archived"
    EVENT_UNARCHIVED = "event_unarchived"

    # Judge calibration: practice projects and the judges' scores on them.
    CALIBRATION_PROJECT_ADDED = "calibration_project_added"
    CALIBRATION_PROJECT_REMOVED = "calibration_project_removed"
    CALIBRATION_SCORED = "calibration_scored"


class WebhookEvent(str, enum.Enum):
    """What an organizer can subscribe to.

    A closed enum, for the same reason `AuditAction` is one: a consumer needs the
    set to be finite and documented, and a typo'd topic string is a subscription
    that silently never fires.
    """

    SUBMISSION_SUBMITTED = "submission.submitted"
    SUBMISSION_UPDATED = "submission.updated"
    TEAM_CREATED = "team.created"
    JUDGE_INVITED = "judge.invited"
    ASSIGNMENTS_CREATED = "assignments.created"
    BALLOT_COMPLETED = "ballot.completed"
    VOTE_CAST = "vote.cast"
    COMMENT_POSTED = "comment.posted"
    RESULTS_PUBLISHED = "results.published"
    CERTIFICATE_ISSUED = "certificate.issued"
    REGISTRATION_CREATED = "registration.created"
    ANNOUNCEMENT_POSTED = "announcement.posted"
    # Deliberately distinct from `RESULTS_PUBLISHED`, which means "the
    # community vote tally changed visibility" -- this means "a judge-
    # computed final result changed" (an admin set or cleared an override).
    # The two were nearly the same name; keeping them different topics is
    # what stops an integration subscribed to one silently missing the other.
    RESULTS_FINALIZED = "results.finalized"

class DeliveryStatus(str, enum.Enum):
    PENDING = "pending"
    DELIVERED = "delivered"
    FAILED = "failed"
    # Kept distinct from FAILED: an organizer needs to tell "your endpoint said no"
    # from "we refused to call that address at all".
    BLOCKED = "blocked"

class CertificateKind(str, enum.Enum):
    PARTICIPATION = "participation"
    JUDGING = "judging"
    PLACEMENT = "placement"
    # Phase 6: admin-issued, on demand -- unlike the three above, never
    # created by the bulk `POST /events/{slug}/certificates/issue` pass, and
    # unlike PARTICIPATION/PLACEMENT it names a role on the event, not a
    # project. See `routers/certificates.py`'s `issue_for_user`.
    ORGANIZING = "organizing"


def _pg_enum(python_enum: type[enum.Enum], name: str) -> SAEnum:
    """Native Postgres enum storing the lowercase *values*, not the member names."""
    return SAEnum(
        python_enum,
        name=name,
        values_callable=lambda members: [m.value for m in members],
        validate_strings=True,
    )


# --------------------------------------------------------------------------- #
# Column helpers
# --------------------------------------------------------------------------- #

URL_LEN = 2048

# Part 3's own eligibility rule for the winner/community-vote split: a voter
# must have registered for the event at least this many days before it
# started to take part in the community-tier round. Keeps a brand-new
# account, created the day of the event specifically to vote, from swinging a
# round that exists to let people who were actually around pick among the
# near-misses.
COMMUNITY_VOTE_REGISTRATION_CUTOFF_DAYS = 7


def _pk() -> Mapped[uuid.UUID]:
    # Generated in Python rather than by the server so that seed fixtures and
    # invite links can be built before the row is flushed.
    return mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)


def _created_at() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


def _updated_at() -> Mapped[datetime]:
    return mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


def utcnow() -> datetime:
    """The one clock. Everything that compares against a deadline uses it."""
    return datetime.now(UTC)


def new_token() -> str:
    """43-character URL-safe token: invite links and session tokens."""
    return secrets.token_urlsafe(32)


# --------------------------------------------------------------------------- #
# Identity
# --------------------------------------------------------------------------- #


class User(Base):
    """A person. Email is stored lower-cased so the unique index is the whole
    duplicate-account defence."""

    __tablename__ = "users"

    id: Mapped[uuid.UUID] = _pk()
    email: Mapped[str] = mapped_column(String(320), nullable=False, unique=True)
    display_name: Mapped[str] = mapped_column(String(120), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[Role] = mapped_column(
        _pg_enum(Role, "user_role"), nullable=False, default=Role.PARTICIPANT
    )
    # Meaningful only while `role` is ADMIN; see `AdminLevel`. Defaults to OWNER
    # so every admin that existed before levels did keeps every power it had.
    admin_level: Mapped[AdminLevel] = mapped_column(
        _pg_enum(AdminLevel, "admin_level"),
        nullable=False,
        default=AdminLevel.OWNER,
        server_default=AdminLevel.OWNER.value,
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()

    sessions: Mapped[list[UserSession]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    memberships: Mapped[list[TeamMember]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )

    __table_args__ = (
        CheckConstraint("email = lower(email)", name="ck_users_email_lowercase"),
        Index("ix_users_role", "role"),
    )

    def __init__(self, **kwargs: object) -> None:
        # The column default only fires at INSERT, so an in-memory `User` would
        # otherwise carry `admin_level=None` -- and `app.access.admin_level()`
        # reads None as "no level", which is deliberately fail-closed. Applying
        # the documented default here means a User means the same thing before
        # and after it is stored.
        kwargs.setdefault("admin_level", AdminLevel.OWNER)
        super().__init__(**kwargs)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<User {self.email} {self.role.value}>"


class UserSession(Base):
    """An opaque server-side session.

    The cookie carries a random token; the table stores only its HMAC. Logging
    out is a `DELETE`, and rotating `SESSION_SECRET` invalidates every live
    session at once because no stored digest can be reproduced.
    """

    __tablename__ = "sessions"

    id: Mapped[uuid.UUID] = _pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    created_at: Mapped[datetime] = _created_at()
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = _created_at()
    user_agent: Mapped[str | None] = mapped_column(String(400))
    ip_address: Mapped[str | None] = mapped_column(String(45))

    user: Mapped[User] = relationship(back_populates="sessions")

    __table_args__ = (Index("ix_sessions_user_id", "user_id"),)

    def is_live(self, now: datetime | None = None) -> bool:
        return (now or utcnow()) < self.expires_at


# --------------------------------------------------------------------------- #
# Event configuration
# --------------------------------------------------------------------------- #


class Event(Base):
    """A hackathon. Owns its own dates, tracks, prizes and custom questions.

    The windows are separate on purpose: registration, submission and judging do
    not open and close together, and an organizer who has to fake one with
    another will fake it badly.
    """

    __tablename__ = "events"

    id: Mapped[uuid.UUID] = _pk()
    slug: Mapped[str] = mapped_column(String(80), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    tagline: Mapped[str | None] = mapped_column(String(300))
    description: Mapped[str | None] = mapped_column(Text)
    website_url: Mapped[str | None] = mapped_column(String(URL_LEN))

    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    registration_opens_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    submission_opens_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    submission_deadline: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # Judging is configured here in Phase 1 and enforced in Phase 2; the dates
    # are event configuration either way, so they live with the other dates.
    judging_opens_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    judging_closes_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    max_team_size: Mapped[int] = mapped_column(Integer, nullable=False, default=4)
    is_published: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # -- public voting (Phase 3) ------------------------------------------- #
    voting_opens_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    voting_closes_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    voting_access: Mapped[VotingAccess] = mapped_column(
        _pg_enum(VotingAccess, "voting_access_mode"),
        nullable=False,
        default=VotingAccess.AUTHENTICATED,
    )
    voting_method: Mapped[VotingMethod] = mapped_column(
        _pg_enum(VotingMethod, "voting_method"),
        nullable=False,
        default=VotingMethod.SINGLE,
    )
    # Quadratic budget. 100 credits buys one 10-vote shout or ten 3-vote nods
    # (9 credits each, so eleven would not fit) -- the trade the method exists for.
    vote_credits: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    # Single-vote mode: how many distinct projects one voter may back.
    votes_per_voter: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    # When the public may see results. NULL means never -- organizers only, which
    # is the safe default and the state every event starts in.
    results_public_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    comments_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    # -- archival ------------------------------------------------------------ #
    # NULL is an active event. A timestamp freezes it: every non-read verb on the
    # event or anything that belongs to it is refused by `app.access.check_access`
    # until it is unarchived, and it drops out of the default event lists. It is
    # deliberately not a state on `is_published` or a deletion: an archived event
    # keeps its gallery, results and certificates for anyone with the link.
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # -- pairwise judging (Phase 5) ----------------------------------------- #
    # The Gavel approach, additive rather than a replacement for the rubric: an
    # organizer may run both, e.g. pairwise to shortlist and a scored rubric for
    # the finalists, so this is its own toggle rather than a value on whatever
    # enum already describes judging. Off by default -- a rubric with zero
    # criteria is a visible, obviously-broken judging setup; pairwise mode with
    # zero comparisons looks identical to "nobody has judged yet", so it must be
    # switched on deliberately rather than defaulting on. Reuses
    # `judging_opens_at`/`judging_closes_at` rather than its own window: both are
    # "judging", just two different mechanisms for it, and a second pair of dates
    # an organizer has to remember to set would be its own bug waiting to happen.
    pairwise_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # -- winner / community-vote split (Phase 6) ---------------------------- #
    # The top `winner_slots` ranks by judge score are outright winners; the
    # next `community_vote_slots` ranks move into a separate community-voting
    # round scoped to just that subset (see `app/routers/voting.py` and
    # JUDGING.md). Both default to 0, which is "this event does not use the
    # split" -- every event that predates this feature, and any event that
    # simply wants a plain judge-ranked result, behaves exactly as before.
    winner_slots: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    community_vote_slots: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    created_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()

    tracks: Mapped[list[Track]] = relationship(
        back_populates="event",
        cascade="all, delete-orphan",
        order_by="Track.position",
    )
    prizes: Mapped[list[Prize]] = relationship(
        back_populates="event",
        cascade="all, delete-orphan",
        order_by="Prize.position",
    )
    questions: Mapped[list[EventQuestion]] = relationship(
        back_populates="event",
        cascade="all, delete-orphan",
        order_by="EventQuestion.position",
    )
    registrations: Mapped[list[EventRegistration]] = relationship(
        back_populates="event", cascade="all, delete-orphan"
    )
    announcements: Mapped[list[Announcement]] = relationship(
        back_populates="event",
        cascade="all, delete-orphan",
        order_by="Announcement.posted_at.desc()",
    )
    teams: Mapped[list[Team]] = relationship(back_populates="event", cascade="all, delete-orphan")
    submissions: Mapped[list[Submission]] = relationship(
        back_populates="event", cascade="all, delete-orphan"
    )
    criteria: Mapped[list[RubricCriterion]] = relationship(
        back_populates="event",
        cascade="all, delete-orphan",
        order_by="RubricCriterion.position",
    )
    judges: Mapped[list[Judge]] = relationship(
        back_populates="event", cascade="all, delete-orphan"
    )
    voters: Mapped[list[Voter]] = relationship(
        back_populates="event", cascade="all, delete-orphan"
    )
    webhooks: Mapped[list[Webhook]] = relationship(
        back_populates="event", cascade="all, delete-orphan"
    )
    certificates: Mapped[list[Certificate]] = relationship(
        back_populates="event", cascade="all, delete-orphan"
    )
    pairwise_comparisons: Mapped[list[PairwiseComparison]] = relationship(
        back_populates="event", cascade="all, delete-orphan"
    )

    __table_args__ = (
        CheckConstraint("ends_at >= starts_at", name="ck_events_dates_ordered"),
        CheckConstraint(
            "submission_deadline >= submission_opens_at", name="ck_events_submission_window"
        ),
        CheckConstraint("max_team_size >= 1", name="ck_events_team_size_positive"),
        CheckConstraint("winner_slots >= 0", name="ck_events_winner_slots_non_negative"),
        CheckConstraint(
            "community_vote_slots >= 0", name="ck_events_community_vote_slots_non_negative"
        ),
        Index("ix_events_published", "is_published"),
    )

    # -- windows ----------------------------------------------------------- #

    def registration_open(self, now: datetime | None = None) -> bool:
        now = now or utcnow()
        return self.registration_opens_at <= now <= self.submission_deadline

    def submissions_open(self, now: datetime | None = None) -> bool:
        """True while drafts may be created and edited."""
        now = now or utcnow()
        return self.submission_opens_at <= now < self.submission_deadline

    def deadline_passed(self, now: datetime | None = None) -> bool:
        return (now or utcnow()) >= self.submission_deadline

    def voting_open(self, now: datetime | None = None) -> bool:
        """True while the public may cast votes.

        Same shape and same default as `judging_open`: an event with no
        `voting_opens_at` has no voting, rather than voting that was quietly live
        from the moment the event was created.
        """
        if self.voting_opens_at is None:
            return False
        now = now or utcnow()
        if now < self.voting_opens_at:
            return False
        return self.voting_closes_at is None or now < self.voting_closes_at

    def community_vote_eligible(self, user_id: uuid.UUID | None) -> bool:
        """The community-tier round's own eligibility rule, on top of (not
        instead of) `voting_open()`: registered for this event at least
        `COMMUNITY_VOTE_REGISTRATION_CUTOFF_DAYS` days before it started.
        Only ever checked when `community_vote_slots > 0` -- see
        `access._check_event`'s `CLAIM_BALLOT` branch and `_check_voter`'s
        `VOTE` branch, the two places this is actually enforced. Scans the
        loaded `registrations` relationship the same way `Team.member_ids()`
        scans `members` -- one row per event per user, so this is never more
        than a handful of comparisons.
        """
        if user_id is None:
            return False
        cutoff = self.starts_at - timedelta(days=COMMUNITY_VOTE_REGISTRATION_CUTOFF_DAYS)
        return any(
            r.user_id == user_id and r.registered_at <= cutoff for r in self.registrations
        )

    def results_public(self, now: datetime | None = None) -> bool:
        """True once the public may see results.

        `results_public_at` is NULL until an organizer deliberately sets it, so the
        default for every event is organizers-only. The brief requires results
        hidden during the voting window; starting closed and opening on purpose is
        the version of that which cannot be got wrong by forgetting a step.
        """
        if self.results_public_at is None:
            return False
        return (now or utcnow()) >= self.results_public_at

    def judging_open(self, now: datetime | None = None) -> bool:
        """True while judges may write scores.

        An event with no `judging_opens_at` is judged **closed**, not open. The
        safe default for a window nobody has configured is shut: an organizer who
        forgets to schedule judging gets an obvious 409 rather than a scoring
        surface that was quietly live since the event was created.
        """
        if self.judging_opens_at is None:
            return False
        now = now or utcnow()
        if now < self.judging_opens_at:
            return False
        return self.judging_closes_at is None or now < self.judging_closes_at


class Track(Base):
    """A judging track. A real row rather than a string on the event, because
    Phase 2 track judges and Phase 1 gallery filters both need it to have an
    identity that survives a rename."""

    __tablename__ = "tracks"

    id: Mapped[uuid.UUID] = _pk()
    event_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("events.id", ondelete="CASCADE"), nullable=False
    )
    key: Mapped[str] = mapped_column(String(60), nullable=False)
    name: Mapped[str] = mapped_column(String(140), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    event: Mapped[Event] = relationship(back_populates="tracks")

    __table_args__ = (
        UniqueConstraint("event_id", "key", name="uq_tracks_event_key"),
        # Composite target: lets submissions and prizes prove, in the database,
        # that the track they point at belongs to the same event they do.
        UniqueConstraint("id", "event_id", name="uq_tracks_id_event"),
    )


class Prize(Base):
    __tablename__ = "prizes"

    id: Mapped[uuid.UUID] = _pk()
    event_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("events.id", ondelete="CASCADE"), nullable=False
    )
    title: Mapped[str] = mapped_column(String(160), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    # Free text, not a number: "$800", "500 EUR" and "Mentorship + credits" are
    # all things organizers actually award.
    value: Mapped[str | None] = mapped_column(String(80))
    track_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True))
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    event: Mapped[Event] = relationship(back_populates="prizes")
    track: Mapped[Track | None] = relationship(
        primaryjoin="Prize.track_id == Track.id", foreign_keys=[track_id], viewonly=True
    )

    __table_args__ = (
        # A track-specific prize must belong to a track of *this* event.
        ForeignKeyConstraint(
            ["track_id", "event_id"],
            ["tracks.id", "tracks.event_id"],
            name="fk_prizes_track_same_event",
            ondelete="SET NULL",
        ),
        Index("ix_prizes_event_id", "event_id"),
    )


class EventQuestion(Base):
    """An organizer-defined custom question shown on the submission form."""

    __tablename__ = "event_questions"

    id: Mapped[uuid.UUID] = _pk()
    event_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("events.id", ondelete="CASCADE"), nullable=False
    )
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    help_text: Mapped[str | None] = mapped_column(Text)
    kind: Mapped[QuestionKind] = mapped_column(
        _pg_enum(QuestionKind, "question_kind"), nullable=False, default=QuestionKind.TEXTAREA
    )
    options: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    required: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    event: Mapped[Event] = relationship(back_populates="questions")

    __table_args__ = (Index("ix_event_questions_event_id", "event_id"),)


# --------------------------------------------------------------------------- #
# Teams
# --------------------------------------------------------------------------- #


class EventRegistration(Base):
    """A participant's sign-up for one event, ahead of forming or joining a
    team.

    Captures exactly what is knowable at this stage -- an address to reach
    them at, their Discord handle, and (for whoever intends to start a team)
    the name they want to lead under -- and nothing about teammates, who are
    not decided yet. Team member names/details are captured later, at
    submission time, on `Submission`/`Team` themselves.

    Additive, not a gate: this table is the event's own roster and the
    trigger for the registration confirmation email (see `app/email.py`), not
    a new authorization check layered onto team creation or joining. Reuses
    `Event.registration_open()` for its own window, the same one
    `Action.CREATE_TEAM`/`Action.JOIN_TEAM` already use in `app/access.py` --
    one admin-configured window for "registration is open", not a second one
    to keep in sync.
    """

    __tablename__ = "event_registrations"

    id: Mapped[uuid.UUID] = _pk()
    event_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("events.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    # Defaults to the account's own email at registration time but editable --
    # some participants want event mail at an address other than their login
    # identity. Not kept in sync with `User.email` afterward; a stale copy
    # here is the participant's own choice, not a bug.
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    discord_username: Mapped[str] = mapped_column(String(64), nullable=False)
    is_team_leader: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    leader_name: Mapped[str | None] = mapped_column(String(160))
    registered_at: Mapped[datetime] = _created_at()

    event: Mapped[Event] = relationship(back_populates="registrations")
    user: Mapped[User] = relationship()

    __table_args__ = (
        # One registration per person per event -- re-registering is an edit,
        # not a second row.
        UniqueConstraint("event_id", "user_id", name="uq_event_registrations_event_user"),
    )


class Team(Base):
    """A team within one event. Joined by invite link; the token is rotatable so
    a leaked link can be killed without deleting the team."""

    __tablename__ = "teams"

    id: Mapped[uuid.UUID] = _pk()
    event_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("events.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(140), nullable=False)
    invite_token: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True, default=new_token
    )
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()

    event: Mapped[Event] = relationship(back_populates="teams")
    # The composite foreign keys are the database's guarantee; the ORM joins on
    # `id` alone, which is sufficient (it is the primary key) and keeps
    # `event_id` owned by exactly one relationship on each child table.
    members: Mapped[list[TeamMember]] = relationship(
        back_populates="team",
        cascade="all, delete-orphan",
        primaryjoin="Team.id == TeamMember.team_id",
        foreign_keys="TeamMember.team_id",
    )
    submission: Mapped[Submission | None] = relationship(
        back_populates="team",
        uselist=False,
        cascade="all, delete-orphan",
        primaryjoin="Team.id == Submission.team_id",
        foreign_keys="Submission.team_id",
    )

    __table_args__ = (
        UniqueConstraint("event_id", "name", name="uq_teams_event_name"),
        # Composite target for team_members and submissions.
        UniqueConstraint("id", "event_id", name="uq_teams_id_event"),
    )

    def member_ids(self) -> set[uuid.UUID]:
        return {m.user_id for m in self.members}

    def owner_ids(self) -> set[uuid.UUID]:
        return {m.user_id for m in self.members if m.team_role is TeamRole.OWNER}


class TeamMember(Base):
    """User-to-team membership.

    `event_id` is carried here so two constraints can exist that otherwise could
    not: the composite FK proves the team really is in that event, and the
    unique index proves a user is on at most one team per event.
    """

    __tablename__ = "team_members"

    # Only the composite foreign key below links this table to `teams`. A second
    # single-column FK would be redundant -- and would leave two join paths for
    # the ORM to choose between.
    team_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    event_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    team_role: Mapped[TeamRole] = mapped_column(
        _pg_enum(TeamRole, "team_role"), nullable=False, default=TeamRole.MEMBER
    )
    joined_at: Mapped[datetime] = _created_at()

    team: Mapped[Team] = relationship(
        back_populates="members",
        primaryjoin="Team.id == TeamMember.team_id",
        foreign_keys="TeamMember.team_id",
    )
    user: Mapped[User] = relationship(back_populates="memberships")

    __table_args__ = (
        ForeignKeyConstraint(
            ["team_id", "event_id"],
            ["teams.id", "teams.event_id"],
            name="fk_team_members_team_same_event",
            ondelete="CASCADE",
        ),
        UniqueConstraint("event_id", "user_id", name="uq_team_members_one_team_per_event"),
    )


# --------------------------------------------------------------------------- #
# Submissions
# --------------------------------------------------------------------------- #


class Submission(Base):
    """A project entry.

    The field set is the one that is stable across every platform in the
    category: name, tagline, long description, thumbnail, image gallery, demo
    video, repository, live link, LinkedIn, tech tags, Discord handle(s),
    track -- plus the organizer's own custom questions, which live in
    `submission_answers`.
    """

    __tablename__ = "submissions"

    id: Mapped[uuid.UUID] = _pk()
    event_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("events.id", ondelete="CASCADE"), nullable=False
    )
    # One project per team. A team that wants to enter twice makes a team twice.
    # Linked to `teams` by the composite foreign key below, not by a second
    # single-column FK -- one path in the schema, one path for the ORM.
    team_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), nullable=False, unique=True
    )
    track_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True))

    name: Mapped[str] = mapped_column(String(160), nullable=False)
    tagline: Mapped[str | None] = mapped_column(String(240))
    description: Mapped[str | None] = mapped_column(Text)
    thumbnail_url: Mapped[str | None] = mapped_column(String(URL_LEN))
    gallery_image_urls: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, default=list
    )
    demo_video_url: Mapped[str | None] = mapped_column(String(URL_LEN))
    repo_url: Mapped[str | None] = mapped_column(String(URL_LEN))
    live_url: Mapped[str | None] = mapped_column(String(URL_LEN))
    linkedin_url: Mapped[str | None] = mapped_column(String(URL_LEN))
    tech_tags: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, default=list)
    # One per team member submitting -- a solo team has one entry, a team of
    # four has up to four. Required to submit (see `REQUIRED_TO_SUBMIT` in
    # `routers/submissions.py`); team *member* names/details are deliberately
    # not collected here or anywhere else -- only this handle, per the brief.
    discord_usernames: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, default=list)

    status: Mapped[SubmissionStatus] = mapped_column(
        _pg_enum(SubmissionStatus, "submission_status"),
        nullable=False,
        default=SubmissionStatus.DRAFT,
    )
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()

    event: Mapped[Event] = relationship(back_populates="submissions")
    team: Mapped[Team] = relationship(
        back_populates="submission",
        primaryjoin="Team.id == Submission.team_id",
        foreign_keys="Submission.team_id",
    )
    track: Mapped[Track | None] = relationship(
        primaryjoin="Submission.track_id == Track.id", foreign_keys=[track_id], viewonly=True
    )
    answers: Mapped[list[SubmissionAnswer]] = relationship(
        back_populates="submission", cascade="all, delete-orphan"
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["team_id", "event_id"],
            ["teams.id", "teams.event_id"],
            name="fk_submissions_team_same_event",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["track_id", "event_id"],
            ["tracks.id", "tracks.event_id"],
            name="fk_submissions_track_same_event",
            ondelete="SET NULL",
        ),
        CheckConstraint(
            "(status = 'draft' AND submitted_at IS NULL)"
            " OR (status = 'submitted' AND submitted_at IS NOT NULL)"
            # `disqualified` carries whatever `submitted_at` it already had --
            # a disqualified draft has none, a disqualified entry keeps the
            # timestamp it was actually submitted at, and disqualifying
            # changes neither on its own.
            " OR (status = 'disqualified')",
            name="ck_submissions_submitted_at_matches_status",
        ),
        Index("ix_submissions_event_status", "event_id", "status"),
        Index("ix_submissions_tech_tags", "tech_tags", postgresql_using="gin"),
        # Composite target for judge_assignments: an assignment cannot pair a
        # judge with a submission from a different event.
        UniqueConstraint("id", "event_id", name="uq_submissions_id_event"),
    )


class SubmissionAnswer(Base):
    """One answer to one organizer-defined question."""

    __tablename__ = "submission_answers"

    id: Mapped[uuid.UUID] = _pk()
    submission_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("submissions.id", ondelete="CASCADE"), nullable=False
    )
    question_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("event_questions.id", ondelete="CASCADE"), nullable=False
    )
    value: Mapped[str | None] = mapped_column(Text)

    submission: Mapped[Submission] = relationship(back_populates="answers")
    question: Mapped[EventQuestion] = relationship()

    __table_args__ = (
        UniqueConstraint("submission_id", "question_id", name="uq_submission_answers_one_each"),
    )


# --------------------------------------------------------------------------- #
# Judging (Phase 2)
# --------------------------------------------------------------------------- #


class AssignmentStatus(str, enum.Enum):
    """Where one judge's review of one project has got to.

    `IN_PROGRESS` exists so the organizer dashboard can answer "who has started
    but not finished", which is a different question from "who has not started"
    and the one that actually predicts whether judging lands on time.
    """

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETE = "complete"


class Judge(Base):
    """A judge, scoped to one event, optionally to one track.

    Judging is a per-event job, not a global identity: the same person may judge
    one event and enter another. `users.role == judge` is the floor that lets
    them reach the judging endpoints at all; this row says *which* event and
    *which* track, and it is the row every isolation rule reads.
    """

    __tablename__ = "judges"

    id: Mapped[uuid.UUID] = _pk()
    event_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("events.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    # NULL means "all tracks". A non-NULL track is the whole of track isolation:
    # this judge is never assigned, and may never read, anything outside it.
    track_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True))
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    invited_at: Mapped[datetime] = _created_at()
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    event: Mapped[Event] = relationship(back_populates="judges")
    user: Mapped[User] = relationship()
    track: Mapped[Track | None] = relationship(
        primaryjoin="Judge.track_id == Track.id", foreign_keys=[track_id], viewonly=True
    )
    assignments: Mapped[list[JudgeAssignment]] = relationship(
        back_populates="judge",
        cascade="all, delete-orphan",
        primaryjoin="Judge.id == JudgeAssignment.judge_id",
        foreign_keys="JudgeAssignment.judge_id",
    )

    __table_args__ = (
        # One judge record per person per event.
        UniqueConstraint("event_id", "user_id", name="uq_judges_event_user"),
        # A track judge's track must belong to their own event.
        ForeignKeyConstraint(
            ["track_id", "event_id"],
            ["tracks.id", "tracks.event_id"],
            name="fk_judges_track_same_event",
            ondelete="SET NULL",
        ),
        # Composite target for judge_assignments.
        UniqueConstraint("id", "event_id", name="uq_judges_id_event"),
        Index("ix_judges_event_id", "event_id"),
    )

    def judges_track(self, track_id: uuid.UUID | None) -> bool:
        """True if this judge is allowed to see work in `track_id`.

        An all-tracks judge sees everything. A track judge sees only their own
        track -- and an untracked submission is *nobody's* track, so it is not
        theirs either.
        """
        if self.track_id is None:
            return True
        return track_id is not None and self.track_id == track_id


class JudgeRecusal(Base):
    """A judge, marked out of reviewing one specific submission.

    Team membership already keeps a judge off their own team's project (see
    `app/assignment._eligible` and `pairwise._eligible_submission_ids`) -- that
    conflict is structural and needs no organizer input. This table is for the
    conflict that structure cannot see: a judge who personally knows a team, has
    a financial stake, or otherwise should not be the one scoring a particular
    project. Either the judge or an organizer can record one; once recorded, both
    scored assignment (`plan_assignments`) and pairwise eligibility exclude that
    pairing the same way team membership does, and any of that judge's existing
    *incomplete* ballots on the submission are removed rather than left orphaned
    to confuse the progress dashboard. A ballot already `COMPLETE` is a historical
    record, not un-scored by a recusal filed after the fact -- see the router for
    why that case is refused rather than silently discarded.
    """

    __tablename__ = "judge_recusals"

    id: Mapped[uuid.UUID] = _pk()
    event_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("events.id", ondelete="CASCADE"), nullable=False
    )
    judge_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    submission_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    reason: Mapped[str | None] = mapped_column(String(300))
    created_at: Mapped[datetime] = _created_at()

    judge: Mapped[Judge] = relationship(
        primaryjoin="Judge.id == JudgeRecusal.judge_id",
        foreign_keys=[judge_id],
        viewonly=True,
    )
    submission: Mapped[Submission] = relationship(
        primaryjoin="Submission.id == JudgeRecusal.submission_id",
        foreign_keys=[submission_id],
        viewonly=True,
    )

    __table_args__ = (
        UniqueConstraint("judge_id", "submission_id", name="uq_judge_recusals_once"),
        ForeignKeyConstraint(
            ["judge_id", "event_id"],
            ["judges.id", "judges.event_id"],
            name="fk_judge_recusals_judge_same_event",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["submission_id", "event_id"],
            ["submissions.id", "submissions.event_id"],
            name="fk_judge_recusals_submission_same_event",
            ondelete="CASCADE",
        ),
    )


class RubricCriterion(Base):
    """One row of the organizer's rubric.

    Criteria and weights are *data*, not constants in code. The brief is pointed
    about this: the market leader cannot weight criteria at all, and its closest
    rival ships one fixed rubric that every event on the platform shares.
    """

    __tablename__ = "rubric_criteria"

    id: Mapped[uuid.UUID] = _pk()
    event_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("events.id", ondelete="CASCADE"), nullable=False
    )
    key: Mapped[str] = mapped_column(String(60), nullable=False)
    name: Mapped[str] = mapped_column(String(140), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    # Stored as the organizer typed it. Division by the sum of weights happens at
    # read time, so editing one weight does not silently rescale every score
    # already recorded against the others.
    weight: Mapped[Decimal] = mapped_column(Numeric(8, 4), nullable=False, default=1)
    min_score: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    max_score: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    event: Mapped[Event] = relationship(back_populates="criteria")

    __table_args__ = (
        UniqueConstraint("event_id", "key", name="uq_rubric_criteria_event_key"),
        CheckConstraint("weight > 0", name="ck_rubric_criteria_weight_positive"),
        CheckConstraint("max_score > min_score", name="ck_rubric_criteria_range"),
        # Composite target for scores.
        UniqueConstraint("id", "event_id", name="uq_rubric_criteria_id_event"),
    )


class JudgeAssignment(Base):
    """One judge's review of one submission: the ballot.

    This row -- not the submission, not the user -- is the unit of isolation.
    Every score hangs off an assignment, so "a judge must never see another
    judge's scores" reduces to one ownership check on this table.
    """

    __tablename__ = "judge_assignments"

    id: Mapped[uuid.UUID] = _pk()
    event_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("events.id", ondelete="CASCADE"), nullable=False
    )
    judge_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    submission_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    status: Mapped[AssignmentStatus] = mapped_column(
        _pg_enum(AssignmentStatus, "assignment_status"),
        nullable=False,
        default=AssignmentStatus.PENDING,
    )
    # The written feedback that goes to the team whether they placed or not.
    comment: Mapped[str | None] = mapped_column(Text)
    # True for a ballot created specifically to adjudicate a flagged
    # disagreement (see `app/scoring.disagreement`) -- a third, previously
    # uninvolved judge added on top of the two who disagreed. Never replaces
    # or hides the ballots it was added to review: this is additive, so a
    # normalized result always reflects every complete ballot on record, and
    # an organizer can see, from this flag alone, which scores came from the
    # original assignment and which were called in to settle a dispute.
    is_adjudication: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    assigned_at: Mapped[datetime] = _created_at()
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    judge: Mapped[Judge] = relationship(
        back_populates="assignments",
        primaryjoin="Judge.id == JudgeAssignment.judge_id",
        foreign_keys="JudgeAssignment.judge_id",
    )
    submission: Mapped[Submission] = relationship(
        primaryjoin="Submission.id == JudgeAssignment.submission_id",
        foreign_keys="JudgeAssignment.submission_id",
    )
    scores: Mapped[list[Score]] = relationship(
        back_populates="assignment",
        cascade="all, delete-orphan",
        primaryjoin="JudgeAssignment.id == Score.assignment_id",
        foreign_keys="Score.assignment_id",
    )

    __table_args__ = (
        # A judge reviews a given submission at most once, so re-running batch
        # assignment tops up rather than duplicating ballots.
        UniqueConstraint("judge_id", "submission_id", name="uq_judge_assignments_once"),
        ForeignKeyConstraint(
            ["judge_id", "event_id"],
            ["judges.id", "judges.event_id"],
            name="fk_judge_assignments_judge_same_event",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["submission_id", "event_id"],
            ["submissions.id", "submissions.event_id"],
            name="fk_judge_assignments_submission_same_event",
            ondelete="CASCADE",
        ),
        # Composite target for scores.
        UniqueConstraint("id", "event_id", name="uq_judge_assignments_id_event"),
        Index("ix_judge_assignments_event_status", "event_id", "status"),
        Index("ix_judge_assignments_submission", "submission_id"),
    )


class Score(Base):
    """One judge's score for one criterion on one submission.

    Per-criterion rather than one number per ballot: the weighted rubric needs
    the components in order to weight them, and normalization needs a judge's
    whole distribution in order to estimate their calibration from it.
    """

    __tablename__ = "scores"

    id: Mapped[uuid.UUID] = _pk()
    event_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("events.id", ondelete="CASCADE"), nullable=False
    )
    assignment_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    criterion_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    # Continuous, not discrete: a judge scores in 0.1 steps within the
    # criterion's min/max, same reasoning as `RubricCriterion.weight` below --
    # `Numeric` for an exact stored value, cast to `float` at the boundary into
    # `app.scoring`'s pure-math module (see `serializers.py`).
    value: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False)
    comment: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()

    assignment: Mapped[JudgeAssignment] = relationship(
        back_populates="scores",
        primaryjoin="JudgeAssignment.id == Score.assignment_id",
        foreign_keys="Score.assignment_id",
    )
    criterion: Mapped[RubricCriterion] = relationship(
        primaryjoin="Score.criterion_id == RubricCriterion.id",
        foreign_keys=[criterion_id],
        viewonly=True,
    )

    __table_args__ = (
        # One score per criterion per ballot. Re-scoring is an UPDATE, and the
        # database is what makes that true rather than handler discipline.
        UniqueConstraint("assignment_id", "criterion_id", name="uq_scores_one_per_criterion"),
        ForeignKeyConstraint(
            ["assignment_id", "event_id"],
            ["judge_assignments.id", "judge_assignments.event_id"],
            name="fk_scores_assignment_same_event",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["criterion_id", "event_id"],
            ["rubric_criteria.id", "rubric_criteria.event_id"],
            name="fk_scores_criterion_same_event",
            ondelete="CASCADE",
        ),
        Index("ix_scores_event_id", "event_id"),
    )


# --------------------------------------------------------------------------- #
# Judge calibration
# --------------------------------------------------------------------------- #


class CalibrationProject(Base):
    """A practice project every judge scores before real judging, with expected
    scores an organizer fixed in advance.

    Deliberately *not* a `Submission`: a practice project has no team, is never
    in the gallery, never gets a ballot, and must not reach `gather()` -- so
    calibration scores can never leak into a real result. It is its own small
    table for exactly that reason. See `app/calibration.py` for what is done
    with the scores, and what the flags do and do not claim.
    """

    __tablename__ = "calibration_projects"

    id: Mapped[uuid.UUID] = _pk()
    event_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("events.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _created_at()

    expected: Mapped[list[CalibrationExpected]] = relationship(
        cascade="all, delete-orphan",
        primaryjoin="CalibrationProject.id == CalibrationExpected.project_id",
        foreign_keys="CalibrationExpected.project_id",
    )

    __table_args__ = (
        UniqueConstraint("event_id", "name", name="uq_calibration_projects_event_name"),
        UniqueConstraint("id", "event_id", name="uq_calibration_projects_id_event"),
    )


class CalibrationExpected(Base):
    """The organizer's expected score for one criterion of one practice project."""

    __tablename__ = "calibration_expected"

    project_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    criterion_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    event_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    value: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False)

    __table_args__ = (
        ForeignKeyConstraint(
            ["project_id", "event_id"],
            ["calibration_projects.id", "calibration_projects.event_id"],
            name="fk_calibration_expected_project_same_event",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["criterion_id", "event_id"],
            ["rubric_criteria.id", "rubric_criteria.event_id"],
            name="fk_calibration_expected_criterion_same_event",
            ondelete="CASCADE",
        ),
    )


class CalibrationScore(Base):
    """One judge's score for one criterion of one practice project."""

    __tablename__ = "calibration_scores"

    id: Mapped[uuid.UUID] = _pk()
    event_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    project_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    judge_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    criterion_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    value: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False)
    updated_at: Mapped[datetime] = _updated_at()

    __table_args__ = (
        UniqueConstraint(
            "project_id", "judge_id", "criterion_id", name="uq_calibration_scores_one_each"
        ),
        ForeignKeyConstraint(
            ["project_id", "event_id"],
            ["calibration_projects.id", "calibration_projects.event_id"],
            name="fk_calibration_scores_project_same_event",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["judge_id", "event_id"],
            ["judges.id", "judges.event_id"],
            name="fk_calibration_scores_judge_same_event",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["criterion_id", "event_id"],
            ["rubric_criteria.id", "rubric_criteria.event_id"],
            name="fk_calibration_scores_criterion_same_event",
            ondelete="CASCADE",
        ),
        Index("ix_calibration_scores_event", "event_id"),
    )


# --------------------------------------------------------------------------- #
# Pairwise judging (Phase 5)
# --------------------------------------------------------------------------- #


class PairwiseComparison(Base):
    """One judge's answer to "which of these two is better" -- the unit the
    Gavel-style alternative to scored judging is built from.

    Unlike `JudgeAssignment`, there is no persistent row created *before* the
    comparison happens: `app/pairwise.pick_next_pair()` computes which two
    submissions to show next from the comparisons that already exist, and a row
    here is only written once the judge actually answers. There is nothing to
    isolate a judge from seeing (the two submissions shown are public, low-stakes
    information, not another judge's opinion), so there is no pending state to
    protect the way a `JudgeAssignment` protects an unscored ballot.

    `app/pairwise.rank_submissions()` fits one Bradley-Terry ranking over every
    row in this table for an event; this row is one data point in that fit, not
    a ranking in itself.
    """

    __tablename__ = "pairwise_comparisons"

    id: Mapped[uuid.UUID] = _pk()
    event_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("events.id", ondelete="CASCADE"), nullable=False
    )
    judge_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    submission_a_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    submission_b_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    # NULL means the judge could not decide. Kept rather than discarded -- see
    # app/pairwise.py's module docstring, point 2, on why a tie is half a win each
    # rather than thrown away.
    winner_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True))
    created_at: Mapped[datetime] = _created_at()

    event: Mapped[Event] = relationship(back_populates="pairwise_comparisons")
    judge: Mapped[Judge] = relationship(
        primaryjoin="Judge.id == PairwiseComparison.judge_id",
        foreign_keys="PairwiseComparison.judge_id",
        viewonly=True,
    )
    submission_a: Mapped[Submission] = relationship(
        primaryjoin="Submission.id == PairwiseComparison.submission_a_id",
        foreign_keys="PairwiseComparison.submission_a_id",
        viewonly=True,
    )
    submission_b: Mapped[Submission] = relationship(
        primaryjoin="Submission.id == PairwiseComparison.submission_b_id",
        foreign_keys="PairwiseComparison.submission_b_id",
        viewonly=True,
    )

    __table_args__ = (
        CheckConstraint(
            "submission_a_id != submission_b_id", name="ck_pairwise_distinct_submissions"
        ),
        # Enforced in the database, not just the route: a row that named a
        # winner outside the pair it is about would corrupt the Bradley-Terry
        # fit silently, counting a win for neither real contestant's opponent.
        CheckConstraint(
            "winner_id IS NULL OR winner_id = submission_a_id OR winner_id = submission_b_id",
            name="ck_pairwise_winner_is_a_or_b",
        ),
        ForeignKeyConstraint(
            ["judge_id", "event_id"],
            ["judges.id", "judges.event_id"],
            name="fk_pairwise_judge_same_event",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["submission_a_id", "event_id"],
            ["submissions.id", "submissions.event_id"],
            name="fk_pairwise_a_same_event",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["submission_b_id", "event_id"],
            ["submissions.id", "submissions.event_id"],
            name="fk_pairwise_b_same_event",
            ondelete="CASCADE",
        ),
        Index("ix_pairwise_event", "event_id"),
        Index("ix_pairwise_judge", "judge_id"),
    )


# --------------------------------------------------------------------------- #
# Public voting, comments and audit (Phase 3)
# --------------------------------------------------------------------------- #


class Voter(Base):
    """A voting identity, scoped to one event.

    This table is the whole of duplicate-vote detection, and it is worth being
    precise about what each mode actually buys:

    * `AUTHENTICATED` -- one voter row per user per event. A real constraint: a
      second ballot needs a second account, and accounts are rate-limited.
    * `EMAIL_GATED` -- one voter row per email address per event. **There is no
      mail delivery in this system**, so nothing proves the address belongs to the
      voter. This is a speed bump, not a wall, and it is documented as one.
    * `OPEN_LINK` -- one voter row per issued token, held in a cookie. Clearing
      cookies gets you another. Deliberately weak; the rate limiter and the audit
      log are what make it survivable.

    Each mode's uniqueness is a *partial* unique index, because the column it
    constrains is NULL in the other modes.
    """

    __tablename__ = "voters"

    id: Mapped[uuid.UUID] = _pk()
    event_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("events.id", ondelete="CASCADE"), nullable=False
    )
    access: Mapped[VotingAccess] = mapped_column(
        _pg_enum(VotingAccess, "voting_access"), nullable=False
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE")
    )
    email: Mapped[str | None] = mapped_column(String(320))
    # HMAC of the cookie token, exactly as sessions does it: a stolen dump does
    # not yield usable ballots.
    token_hash: Mapped[str | None] = mapped_column(String(64), unique=True)
    # Per-voter ballot ordering. Stored rather than derived from the session so a
    # refresh does not reshuffle the page under the voter -- random *between*
    # voters kills position bias; random *within* one voter is just confusing.
    ordering_seed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = _created_at()
    last_seen_at: Mapped[datetime] = _created_at()
    ip_address: Mapped[str | None] = mapped_column(String(45))
    user_agent: Mapped[str | None] = mapped_column(String(400))

    event: Mapped[Event] = relationship()
    user: Mapped[User | None] = relationship()
    votes: Mapped[list[Vote]] = relationship(
        back_populates="voter",
        cascade="all, delete-orphan",
        primaryjoin="Voter.id == Vote.voter_id",
        foreign_keys="Vote.voter_id",
    )

    __table_args__ = (
        # One ballot per account per event.
        Index(
            "uq_voters_event_user",
            "event_id",
            "user_id",
            unique=True,
            postgresql_where=text("user_id IS NOT NULL"),
        ),
        # One ballot per claimed address per event.
        Index(
            "uq_voters_event_email",
            "event_id",
            "email",
            unique=True,
            postgresql_where=text("email IS NOT NULL"),
        ),
        CheckConstraint("email IS NULL OR email = lower(email)", name="ck_voters_email_lower"),
        # Every mode must identify its voter somehow.
        CheckConstraint(
            "user_id IS NOT NULL OR email IS NOT NULL OR token_hash IS NOT NULL",
            name="ck_voters_identified",
        ),
        Index("ix_voters_event_id", "event_id"),
        UniqueConstraint("id", "event_id", name="uq_voters_id_event"),
    )

    @property
    def label(self) -> str:
        """How this voter appears in the audit log. Never the raw token."""
        if self.user is not None:
            return self.user.email
        if self.email:
            return self.email
        return f"anonymous:{str(self.id)[:8]}"


class Vote(Base):
    """One voter's support for one project.

    `credits` is the number of votes cast, not the cost. In quadratic mode the
    cost is `credits²` and the budget check is done in `app.voting`; storing the
    count rather than the cost means the budget rule can change without
    rewriting history.
    """

    __tablename__ = "votes"

    id: Mapped[uuid.UUID] = _pk()
    event_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("events.id", ondelete="CASCADE"), nullable=False
    )
    voter_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    submission_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    credits: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()

    voter: Mapped[Voter] = relationship(
        back_populates="votes",
        primaryjoin="Voter.id == Vote.voter_id",
        foreign_keys="Vote.voter_id",
    )
    submission: Mapped[Submission] = relationship(
        primaryjoin="Submission.id == Vote.submission_id",
        foreign_keys="Vote.submission_id",
    )

    __table_args__ = (
        # THE anti-abuse constraint. One row per voter per project, so a double
        # submit is a conflict in the database rather than a race in the handler.
        UniqueConstraint("voter_id", "submission_id", name="uq_votes_one_per_project"),
        CheckConstraint("credits > 0", name="ck_votes_credits_positive"),
        ForeignKeyConstraint(
            ["voter_id", "event_id"],
            ["voters.id", "voters.event_id"],
            name="fk_votes_voter_same_event",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["submission_id", "event_id"],
            ["submissions.id", "submissions.event_id"],
            name="fk_votes_submission_same_event",
            ondelete="CASCADE",
        ),
        Index("ix_votes_submission", "submission_id"),
    )


class Comment(Base):
    """A public comment on a gallery project.

    Authenticated only, and that is an anti-abuse decision rather than an
    oversight: an anonymous comment box on a public gallery is a spam endpoint,
    and the brief asks for anti-abuse that means something. Hidden rather than
    deleted, so moderation is reversible and auditable.
    """

    __tablename__ = "comments"

    id: Mapped[uuid.UUID] = _pk()
    event_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("events.id", ondelete="CASCADE"), nullable=False
    )
    submission_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    author_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    body: Mapped[str] = mapped_column(Text, nullable=False)
    is_hidden: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    hidden_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    hidden_reason: Mapped[str | None] = mapped_column(String(300))
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()

    author: Mapped[User] = relationship(foreign_keys=[author_id])
    hidden_by: Mapped[User | None] = relationship(foreign_keys=[hidden_by_id])
    submission: Mapped[Submission] = relationship(
        primaryjoin="Submission.id == Comment.submission_id",
        foreign_keys="Comment.submission_id",
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["submission_id", "event_id"],
            ["submissions.id", "submissions.event_id"],
            name="fk_comments_submission_same_event",
            ondelete="CASCADE",
        ),
        CheckConstraint("length(btrim(body)) > 0", name="ck_comments_body_not_blank"),
        CheckConstraint(
            "(is_hidden = false AND hidden_by_id IS NULL)"
            " OR (is_hidden = true AND hidden_by_id IS NOT NULL)",
            name="ck_comments_hidden_has_moderator",
        ),
        Index("ix_comments_submission", "submission_id"),
    )


class Announcement(Base):
    """A message from the organizers, shown on the event page.

    `visible_to_visitors` is the one flag: unset, an announcement is for
    people who are actually part of the event (participant rank or above --
    `_check_announcement` in `access.py`); set, it is also shown to a signed-
    out visitor on the public event page. There is no third tier and no
    per-announcement audience list -- an organizer who needs that is running
    a mailing list, not posting a notice.
    """

    __tablename__ = "announcements"

    id: Mapped[uuid.UUID] = _pk()
    event_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("events.id", ondelete="CASCADE"), nullable=False
    )
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    visible_to_visitors: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    posted_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    posted_at: Mapped[datetime] = _created_at()

    event: Mapped[Event] = relationship(back_populates="announcements")
    posted_by: Mapped[User | None] = relationship()

    __table_args__ = (
        CheckConstraint("length(btrim(title)) > 0", name="ck_announcements_title_not_blank"),
        CheckConstraint("length(btrim(body)) > 0", name="ck_announcements_body_not_blank"),
        Index("ix_announcements_event", "event_id"),
    )


class OverrideTier(str, enum.Enum):
    """The three explicit outcomes an admin override can set -- the same
    vocabulary `results.compute_tiers()` already uses for the judge-computed
    version, plus `NEITHER` made explicit: an override *removing* a
    submission from a tier is a real, distinct decision from that submission
    never having been computed into one, and both need to be expressible.
    """

    WINNER = "winner"
    COMMUNITY_TIER = "community_tier"
    NEITHER = "neither"


class ResultOverride(Base):
    """An admin's correction to one submission's final winner/community-tier
    status, for cause.

    Deliberately does not touch a ballot, a score, or `compute_tiers()`'s own
    output -- the judge-computed ranking stays exactly what it always was,
    recoverable and shown alongside this on `GET /events/{slug}/results`
    (never silently overwritten, per the brief). This table is the *other*
    half of "live + override": `results.effective_tiers()` is the merge of
    the two, and it -- not `compute_tiers()` alone -- is what
    `app/routers/voting.py`'s community-vote scoping and a team's own
    `GET /submissions/{id}` tier both read. An override is the authoritative
    final answer; the computed one is the record of what the algorithm said
    before a human corrected it.

    One row per (event, submission): changing an override updates it in
    place rather than growing a history table, because `audit_log` is
    already that history -- every set and every clear is its own audited
    action with its own reason.
    """

    __tablename__ = "result_overrides"

    id: Mapped[uuid.UUID] = _pk()
    event_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("events.id", ondelete="CASCADE"), nullable=False
    )
    submission_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    tier: Mapped[OverrideTier] = mapped_column(
        _pg_enum(OverrideTier, "override_tier"), nullable=False
    )
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    overridden_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()

    event: Mapped[Event] = relationship()
    submission: Mapped[Submission] = relationship(
        primaryjoin="Submission.id == ResultOverride.submission_id",
        foreign_keys="ResultOverride.submission_id",
    )
    overridden_by: Mapped[User | None] = relationship()

    __table_args__ = (
        UniqueConstraint(
            "event_id", "submission_id", name="uq_result_overrides_one_per_submission"
        ),
        CheckConstraint("length(btrim(reason)) > 0", name="ck_result_overrides_reason_not_blank"),
        ForeignKeyConstraint(
            ["submission_id", "event_id"],
            ["submissions.id", "submissions.event_id"],
            name="fk_result_overrides_submission_same_event",
            ondelete="CASCADE",
        ),
        Index("ix_result_overrides_event", "event_id"),
    )


class AuditEntry(Base):
    """One line of the trail an organizer can read without a database client.

    The shape is chosen around that requirement. `summary` is a complete English
    sentence written at the moment of the action, when the context to write it
    still exists -- not a template applied later over ids nobody can resolve. The
    structured columns are there so the log can be filtered; the sentence is there
    so it can be read.

    Append-only by convention and by API surface: there is no update or delete
    route for this table. A log an organizer can edit is not a log.

    **Hash-chained, Phase 5.** Append-only-by-convention was the honest limit
    named in THREAT-MODEL.md through Phase 4: nothing stopped someone with
    direct database access from editing a row, and the log could not tell you
    if they had. `prev_hash` and `entry_hash` close that. Every entry's hash
    covers its own fields *and* the previous entry's hash, so altering any row
    -- or deleting one out of the middle -- changes what every later hash
    should be, and `GET /api/audit/verify` (admin-only, global, because the
    chain itself is global) walks the whole table recomputing them. This does
    not stop a database-level attacker from rewriting the *entire* chain from
    the point of tampering forward; it makes that the only way to hide a change,
    and doing it is loud (every hash after the edit changes) rather than
    surgical. `app/signing.py` already had the canonical-JSON primitive this reuses
    (`canonical()`) -- this is the second thing built on top of it, chaining plain
    SHA-256 hashes rather than a signature, since the audience here already has
    database access and the point is detecting an edit, not proving authorship to
    someone who does not.

    `seq` is the ordering the chain is built on, not `created_at` or `id`: two
    entries can share a timestamp at the resolution the database actually
    stores (concurrent requests, same transaction), and a `uuid4` carries no
    order at all. A Postgres `IDENTITY` column is one strictly increasing
    integer with no gaps under normal operation, which is what a hash chain
    needs a "previous" to mean anything.
    """

    __tablename__ = "audit_log"

    id: Mapped[uuid.UUID] = _pk()
    seq: Mapped[int] = mapped_column(BigInteger, Identity(always=True), nullable=False)
    # SET NULL, not CASCADE -- found while wiring EVENT_DELETED in Phase 5.
    # Cascading here would mean deleting an event destroys the *entire* audit
    # trail of everything that ever happened in it, including the very entry
    # recording the deletion. An audit log that a delete can erase is not a log.
    # `event_slug` is denormalised for the same reason `actor_label` already is:
    # a row has to keep reading correctly after the thing it names is gone.
    event_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("events.id", ondelete="SET NULL")
    )
    event_slug: Mapped[str | None] = mapped_column(String(80))
    action: Mapped[AuditAction] = mapped_column(
        _pg_enum(AuditAction, "audit_action"), nullable=False
    )
    # The actor as a foreign key when they had an account, and always as text.
    # Deleting a user must not erase what they did, so the label survives the row.
    actor_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    actor_label: Mapped[str] = mapped_column(String(320), nullable=False)
    actor_role: Mapped[str | None] = mapped_column(String(20))
    resource_type: Mapped[str | None] = mapped_column(String(40))
    resource_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True))
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    ip_address: Mapped[str | None] = mapped_column(String(45))
    # Set explicitly in Python at write time (see `app.audit.record`), not by a
    # server default: the hash has to cover the exact timestamp that gets
    # stored, so the value has to be known *before* the row is written, not
    # generated by Postgres during the insert.
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # NULL only for the very first row the table has ever held (the genesis
    # entry). Every other row's `prev_hash` is the `entry_hash` of the row
    # immediately before it in `seq` order.
    prev_hash: Mapped[str | None] = mapped_column(String(64))
    entry_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    actor: Mapped[User | None] = relationship()
    event: Mapped[Event | None] = relationship()

    __table_args__ = (
        Index("ix_audit_event_created", "event_id", "created_at"),
        Index("ix_audit_action", "action"),
        UniqueConstraint("seq", name="uq_audit_log_seq"),
        CheckConstraint("length(btrim(summary)) > 0", name="ck_audit_summary_not_blank"),
    )


class RateLimit(Base):
    """A fixed-window counter, in Postgres.

    ARCHITECTURE.md left this open: in-process is simplest but wrong across
    replicas, Postgres-backed survives a restart. Phase 3 picks Postgres, because
    the thing being limited is vote and comment abuse and a limiter that resets
    when the container restarts is one an attacker can reset for us.

    Fixed window rather than sliding: one row per (key, window), so the whole
    limiter is a single upsert and a comparison. A sliding window is more
    accurate and needs either a row per request or a Lua script in something we
    do not run.
    """

    __tablename__ = "rate_limits"

    # e.g. "vote:<event>:<ip>" -- composed in app.ratelimit, never here.
    key: Mapped[str] = mapped_column(String(200), primary_key=True)
    window_started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), primary_key=True
    )
    count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_at: Mapped[datetime] = _updated_at()

    __table_args__ = (
        Index("ix_rate_limits_window", "window_started_at"),
        CheckConstraint("count >= 0", name="ck_rate_limits_count_nonnegative"),
    )


# --------------------------------------------------------------------------- #
# Webhooks, certificates and signed records (Phase 4)
# --------------------------------------------------------------------------- #


class Webhook(Base):
    """An organizer-registered outbound hook.

    The `secret` is stored in plaintext, and that is a deliberate, documented
    exception to how every other secret in this schema is handled: an HMAC has to
    be *computed* from it on every delivery, so it cannot be stored as a digest
    the way a password or a session token is. The mitigation is scope -- it signs
    outbound payloads and authenticates nothing inbound -- and it is named in
    THREAT-MODEL.md rather than glossed.
    """

    __tablename__ = "webhooks"

    id: Mapped[uuid.UUID] = _pk()
    event_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("events.id", ondelete="CASCADE"), nullable=False
    )
    url: Mapped[str] = mapped_column(String(URL_LEN), nullable=False)
    secret: Mapped[str] = mapped_column(String(64), nullable=False, default=new_token)
    # Which topics this hook wants. Empty means every topic, which is the common case
    # for a "send everything to my Slack relay" integration.
    topics: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, default=list)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    description: Mapped[str | None] = mapped_column(String(200))
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = _created_at()
    # Consecutive failures. A hook that has failed repeatedly is disabled rather
    # than retried forever -- an organizer's dead endpoint should not slow every
    # request for the rest of the weekend.
    failure_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_delivery_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    event: Mapped[Event] = relationship(back_populates="webhooks")
    deliveries: Mapped[list[WebhookDelivery]] = relationship(
        back_populates="webhook",
        cascade="all, delete-orphan",
        primaryjoin="Webhook.id == WebhookDelivery.webhook_id",
        foreign_keys="WebhookDelivery.webhook_id",
    )

    __table_args__ = (
        UniqueConstraint("event_id", "url", name="uq_webhooks_event_url"),
        CheckConstraint("failure_count >= 0", name="ck_webhooks_failures_nonnegative"),
        # Composite target for webhook_deliveries, so a delivery record cannot point
        # at a hook from another event. Same trick as everywhere else in this file.
        UniqueConstraint("id", "event_id", name="uq_webhooks_id_event"),
        Index("ix_webhooks_event_id", "event_id"),
    )

    def wants(self, topic: WebhookEvent) -> bool:
        return not self.topics or topic.value in self.topics


class WebhookDelivery(Base):
    """One attempt to call one hook.

    Stored rather than logged, because "did my integration receive the submission"
    is a question an organizer asks mid-incident and a log line cannot answer. The
    response status and body excerpt are kept so they can debug their own endpoint
    without access to ours.
    """

    __tablename__ = "webhook_deliveries"

    id: Mapped[uuid.UUID] = _pk()
    webhook_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    event_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("events.id", ondelete="CASCADE"), nullable=False
    )
    topic: Mapped[str] = mapped_column(String(60), nullable=False)
    payload: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[DeliveryStatus] = mapped_column(
        _pg_enum(DeliveryStatus, "delivery_status"),
        nullable=False,
        default=DeliveryStatus.PENDING,
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    response_code: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = _created_at()
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    webhook: Mapped[Webhook] = relationship(
        back_populates="deliveries",
        primaryjoin="Webhook.id == WebhookDelivery.webhook_id",
        foreign_keys="WebhookDelivery.webhook_id",
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["webhook_id", "event_id"],
            ["webhooks.id", "webhooks.event_id"],
            name="fk_deliveries_webhook_same_event",
            ondelete="CASCADE",
        ),
        Index("ix_deliveries_event_created", "event_id", "created_at"),
        Index("ix_deliveries_status", "status"),
    )


class Certificate(Base):
    """A signed, publicly verifiable record.

    Three kinds: a participant took part, a judge reviewed n projects, a team
    placed. All three are the same shape, because the interesting property is not
    the content -- it is that **a third party can check the signature without an
    account and without trusting the page it is displayed on**.

    `payload` is the canonical JSON that was signed, stored verbatim. Re-serialising
    it at verification time would make the signature depend on Python's dict
    ordering and on every future change to the response model; storing exactly what
    was signed means the check is over bytes, not over an object graph.

    `code` is the public handle. Short enough to read down a phone, random enough
    not to be enumerable.

    **Ed25519, not HMAC (Phase 5).** `signature` is an asymmetric signature now,
    not a keyed hash, and `key_id` names which key made it. That is what makes
    verification genuinely third-party: checking an HMAC means trusting whatever
    told you it matched, because only the server that holds the secret could have
    produced it *or checked it*. An Ed25519 signature can be checked by anyone
    holding the public key, which `GET /api/signing/public-keys` publishes --
    nobody has to trust this server's opinion of its own signature. `key_id`
    exists because a signing key eventually gets rotated, and a certificate has
    to keep saying which key to check it against for the rest of its life; the
    old key's public half moves to `settings.signing_retired_keys` and keeps
    verifying, rather than every certificate it ever signed going stale at once.
    """

    __tablename__ = "certificates"

    id: Mapped[uuid.UUID] = _pk()
    event_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("events.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[CertificateKind] = mapped_column(
        _pg_enum(CertificateKind, "certificate_kind"), nullable=False
    )
    # Who it is about. A participation or judging record names a person; a placement
    # record names a team's submission.
    subject_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE")
    )
    submission_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True))
    # Denormalised on purpose: a certificate has to keep reading correctly after a
    # display name changes or an account is deleted. It is a record of a moment.
    subject_name: Mapped[str] = mapped_column(String(200), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    code: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)
    payload: Mapped[str] = mapped_column(Text, nullable=False)
    # Ed25519 signatures are 64 bytes; hex-encoded that is 128 characters, twice
    # the HMAC-SHA256 hex this column held through Phase 4.
    signature: Mapped[str] = mapped_column(String(128), nullable=False)
    # Which key signed it -- see the class docstring on why this exists.
    key_id: Mapped[str] = mapped_column(String(40), nullable=False)
    issued_at: Mapped[datetime] = _created_at()
    issued_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_reason: Mapped[str | None] = mapped_column(String(300))

    event: Mapped[Event] = relationship(back_populates="certificates")
    subject: Mapped[User | None] = relationship(foreign_keys=[subject_user_id])
    issued_by: Mapped[User | None] = relationship(foreign_keys=[issued_by_id])
    submission: Mapped[Submission | None] = relationship(
        primaryjoin="Certificate.submission_id == Submission.id",
        foreign_keys=[submission_id],
        viewonly=True,
    )

    __table_args__ = (
        # One certificate of a given kind per person per event. Re-issuing is an
        # update, so a verification code stays stable once it has been shared.
        Index(
            "uq_certificates_event_kind_user",
            "event_id",
            "kind",
            "subject_user_id",
            unique=True,
            postgresql_where=text("subject_user_id IS NOT NULL"),
        ),
        Index(
            "uq_certificates_event_kind_submission",
            "event_id",
            "kind",
            "submission_id",
            unique=True,
            postgresql_where=text("submission_id IS NOT NULL"),
        ),
        CheckConstraint(
            "subject_user_id IS NOT NULL OR submission_id IS NOT NULL",
            name="ck_certificates_has_subject",
        ),
        CheckConstraint(
            "(revoked_at IS NULL AND revoked_reason IS NULL)"
            " OR (revoked_at IS NOT NULL AND revoked_reason IS NOT NULL)",
            name="ck_certificates_revocation_has_reason",
        ),
        Index("ix_certificates_event_id", "event_id"),
    )

    @property
    def is_valid(self) -> bool:
        return self.revoked_at is None
