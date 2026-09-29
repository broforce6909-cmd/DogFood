"""Request and response models.

These are the API's contract, and because FastAPI derives the OpenAPI document
from them, they are also the published spec. Validation that belongs to the
*shape* of the data lives here; validation that belongs to the *rules* of the
event (deadlines, membership) lives in `app.access` and the routers.

Where a constraint also exists in the database, the duplication is deliberate:
the check constraint is the guarantee, this is the 422 that explains it.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime
from typing import Annotated, Literal

from pydantic import (
    AnyHttpUrl,
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from .models import (
    URL_LEN,
    AdminLevel,
    AssignmentStatus,
    AuditAction,
    CertificateKind,
    DeliveryStatus,
    QuestionKind,
    Role,
    SubmissionStatus,
    TeamRole,
    VotingAccess,
    VotingMethod,
)
from .security import MIN_PASSWORD_LENGTH

SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

Slug = Annotated[str, StringConstraints(min_length=2, max_length=80)]
Password = Annotated[str, StringConstraints(min_length=MIN_PASSWORD_LENGTH, max_length=200)]
ShortText = Annotated[str, StringConstraints(min_length=1, max_length=200)]
# A reason an admin-oversight action requires. `min_length` alone counts spaces,
# so "   " would pass it: for the result override that reached
# `ck_result_overrides_reason_not_blank` and became a 500, and for the removal and
# disqualification routes it was silently accepted as a "reason". Stripping first
# makes a blank reason the same 422 as an empty one, and stores the trimmed text.
RequiredReason = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)
]
# Free-form prose fields (descriptions, help text, custom-question answers) had
# no length cap at all -- every other string field on the same schemas does.
# 20,000 characters is far past any legitimate use here and far short of a
# payload that could exhaust memory; the point is a bound exists, not that
# this exact number is load-bearing.
LongText = Annotated[str, StringConstraints(max_length=20_000)]

MAX_TECH_TAGS = 12
MAX_GALLERY_IMAGES = 8


_DATA_IMAGE_RE = re.compile(
    r"^data:image/(png|jpe?g|gif|webp|svg\+xml)[;,]", re.IGNORECASE
)


def _image_url(value: str | None) -> str | None:
    """http(s), or a small inline `data:image/...` URI. Never anything else.

    This project has no object storage by design (see ARCHITECTURE.md's
    "deliberate non-goals") -- a thumbnail is a URL or a tiny inline image, and
    the seeded fixtures use the inline form for exactly that reason. `<img
    src>` is a safe context for a data URI: a browser never executes script
    reached only through an <img> element, even for `data:image/svg+xml`,
    because SVG script execution needs a document context (`<object>`,
    `<iframe>`, direct navigation) that `<img>` does not provide. That is what
    makes this validator different from `_http_url`, which guards fields
    rendered as a clickable `<a href>` instead -- a context where a
    `javascript:` URI does run.

    The MIME token is checked against an explicit allow-list rather than
    accepting any `data:` URI, so this cannot be widened by accident into
    accepting `data:text/html` or similar.
    """
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    if len(value) > URL_LEN:
        raise ValueError(f"must be {URL_LEN} characters or fewer")
    if re.match(r"^https?://", value, re.IGNORECASE):
        return value
    if _DATA_IMAGE_RE.match(value):
        return value
    raise ValueError("must be an http:// or https:// URL, or a data:image/... URI")


def _http_url(value: str | None) -> str | None:
    """Reject anything that is not a real http(s) URL.

    Every field this guards ends up as an anchor's `href` somewhere -- the
    project page, the embed widget, an export re-opened in a browser. Before
    this validator existed, these fields were plain strings with a length cap
    and nothing else, which meant a participant could set `repo_url` to
    `javascript:fetch('/api/...')` and the frontend would render it as a
    clickable link with no further checking: `<a href={project.repo_url}>`
    executes a `javascript:` URI in the page's own origin the moment a judge
    clicks "View repo". `rel="noopener noreferrer"` does nothing against that
    scheme -- those attributes only change navigation behaviour, and a
    `javascript:` URI is not a navigation.

    Rejecting anything without an `http://` or `https://` prefix closes it at
    the one place every consumer of this data agrees to trust: the write path.
    The frontend also refuses to render a non-http(s) href as a second layer,
    in `lib/format.safeHref` -- belt and braces, because this is a stored-XSS
    vector and one layer failing silently is not an acceptable outcome.
    """
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    if not re.match(r"^https?://", value, re.IGNORECASE):
        raise ValueError("must be an http:// or https:// URL")
    if len(value) > URL_LEN:
        raise ValueError(f"must be {URL_LEN} characters or fewer")
    return value



class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# --------------------------------------------------------------------------- #
# Identity
# --------------------------------------------------------------------------- #


class UserPublic(ORMModel):
    """What anybody may see about a user: a name on a team, nothing more."""

    id: uuid.UUID
    display_name: str
    role: Role


class UserOut(UserPublic):
    """Adds the fields only the user themselves and staff may read."""

    email: EmailStr
    is_active: bool
    created_at: datetime
    # Only an admin has a level. Everyone else gets None rather than the
    # column's stored default, which would read as "owner" for a participant.
    admin_level: AdminLevel | None = None

    @model_validator(mode="after")
    def _level_only_for_admins(self) -> UserOut:
        if self.role is not Role.ADMIN:
            self.admin_level = None
        return self


class RegisterIn(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "email": "priya@example.com",
                    "display_name": "Priya Rivera",
                    "password": "a-real-password-not-this-one",
                }
            ]
        }
    )

    email: EmailStr
    display_name: ShortText
    password: Password


class LoginIn(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [{"email": "priya@example.com", "password": "a-real-password-not-this-one"}]
        }
    )

    email: EmailStr
    password: str
    # Optional. The sign-in page's role tab, when the person explicitly picked
    # one. If given, it is checked *after* the password is verified and *before*
    # any session exists: an account whose real role differs is refused with a
    # message naming the right tab, and is not signed in. Omitted, login is
    # unchanged. `visitor` is not a role an account can have, so it is refused.
    expected_role: Role | None = None

    @field_validator("expected_role")
    @classmethod
    def _not_visitor(cls, value: Role | None) -> Role | None:
        if value is Role.VISITOR:
            raise ValueError("visitor is not a role an account can sign in as")
        return value


class SessionOut(BaseModel):
    """Returned by login and register.

    The raw token is in the body *and* in a `HttpOnly` cookie. The body is for
    API clients; the cookie is for browsers. The frontend only ever calls this
    from the server, so the token never reaches client-side JavaScript.
    """

    token: str
    expires_at: datetime
    user: UserOut


class MeOut(BaseModel):
    """`GET /api/auth/me` answers 200 whether or not you are logged in --
    "anonymous" is a valid answer to "who am I", not an error."""

    authenticated: bool
    user: UserOut | None = None
    role: Role


class SessionRowOut(ORMModel):
    """One live session, so a user can see and revoke their own."""

    id: uuid.UUID
    created_at: datetime
    last_seen_at: datetime
    expires_at: datetime
    user_agent: str | None = None
    ip_address: str | None = None


class RoleUpdateIn(BaseModel):
    role: Role
    # Only meaningful when `role` is admin. Omitted, an existing admin keeps its
    # level and a newly promoted one becomes a `manager` -- the least a writing
    # admin can be -- rather than silently a full owner.
    admin_level: AdminLevel | None = None


class UserCreateIn(BaseModel):
    """Admin-provisioned account -- a judge or organizer who never went
    through the public `/auth/register` form. No password field: one is
    generated and emailed, never typed by the admin. See
    `routers/users.py`'s `create_user`."""

    email: EmailStr
    display_name: ShortText
    role: Role
    # Only for `role: admin`; defaults to `manager` there. See `RoleUpdateIn`.
    admin_level: AdminLevel | None = None


class ProfileUpdateIn(BaseModel):
    display_name: ShortText | None = None
    password: Password | None = None


# --------------------------------------------------------------------------- #
# Event configuration
# --------------------------------------------------------------------------- #


class TrackIn(BaseModel):
    key: Slug
    name: ShortText
    description: LongText | None = None
    position: int = 0

    @field_validator("key")
    @classmethod
    def _slug(cls, value: str) -> str:
        value = value.strip().lower()
        if not SLUG_RE.match(value):
            raise ValueError("key must be lowercase alphanumeric words separated by hyphens")
        return value


class TrackOut(ORMModel):
    id: uuid.UUID
    key: str
    name: str
    description: str | None = None
    position: int


class PrizeIn(BaseModel):
    title: ShortText
    description: LongText | None = None
    value: str | None = Field(default=None, max_length=80)
    track_id: uuid.UUID | None = None
    position: int = 0


class PrizeOut(ORMModel):
    id: uuid.UUID
    title: str
    description: str | None = None
    value: str | None = None
    track_id: uuid.UUID | None = None
    position: int


class QuestionIn(BaseModel):
    prompt: Annotated[str, StringConstraints(min_length=1, max_length=1000)]
    help_text: LongText | None = None
    kind: QuestionKind = QuestionKind.TEXTAREA
    options: list[Annotated[str, StringConstraints(min_length=1, max_length=200)]] | None = None
    required: bool = False
    position: int = 0

    @field_validator("options")
    @classmethod
    def _bounded_option_list(cls, value: list[str] | None) -> list[str] | None:
        if value is not None and len(value) > 100:
            raise ValueError("at most 100 options")
        return value

    @model_validator(mode="after")
    def _options_only_for_select(self) -> QuestionIn:
        if self.kind is QuestionKind.SELECT and not self.options:
            raise ValueError("a select question needs options")
        if self.kind is not QuestionKind.SELECT and self.options:
            raise ValueError("options are only meaningful for a select question")
        return self


class QuestionOut(ORMModel):
    id: uuid.UUID
    prompt: str
    help_text: str | None = None
    kind: QuestionKind
    options: list[str] | None = None
    required: bool
    position: int


class EventDates(BaseModel):
    starts_at: datetime
    ends_at: datetime
    registration_opens_at: datetime
    submission_opens_at: datetime
    submission_deadline: datetime
    judging_opens_at: datetime | None = None
    judging_closes_at: datetime | None = None

    @model_validator(mode="after")
    def _windows_are_ordered(self) -> EventDates:
        if self.ends_at < self.starts_at:
            raise ValueError("ends_at must not precede starts_at")
        if self.submission_deadline < self.submission_opens_at:
            raise ValueError("submission_deadline must not precede submission_opens_at")
        if (
            self.judging_opens_at
            and self.judging_closes_at
            and self.judging_closes_at < self.judging_opens_at
        ):
            raise ValueError("judging_closes_at must not precede judging_opens_at")
        return self


class EventCreate(EventDates):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "slug": "raptors-winter",
                    "name": "Raptors Winter Hackathon",
                    "tagline": "48 hours, one weekend, no sleep",
                    "starts_at": "<ISO 8601 timestamp, UTC>",
                    "ends_at": "<ISO 8601 timestamp, UTC>",
                    "registration_opens_at": "<ISO 8601 timestamp, UTC>",
                    "submission_opens_at": "<ISO 8601 timestamp, UTC>",
                    "submission_deadline": "<ISO 8601 timestamp, UTC>",
                    "judging_opens_at": "<ISO 8601 timestamp, UTC>",
                    "judging_closes_at": "<ISO 8601 timestamp, UTC>",
                    "max_team_size": 4,
                }
            ]
        }
    )

    slug: Slug
    name: ShortText
    tagline: str | None = Field(default=None, max_length=300)
    description: LongText | None = None
    website_url: str | None = Field(default=None, max_length=2048)
    max_team_size: int = Field(default=4, ge=1, le=50)
    is_published: bool = False

    # Optional at creation, same as the judging window above -- see
    # `EventUpdate`'s comment for why these exist at all. Defaults mirror the
    # `Event` model's own (authenticated + single + 100 credits + 3 votes +
    # comments on), so leaving them unset at creation is indistinguishable from
    # today's behaviour.
    voting_opens_at: datetime | None = None
    voting_closes_at: datetime | None = None
    voting_access: VotingAccess = VotingAccess.AUTHENTICATED
    voting_method: VotingMethod = VotingMethod.SINGLE
    vote_credits: int = Field(default=100, ge=1, le=10_000)
    votes_per_voter: int = Field(default=3, ge=1, le=100)
    comments_enabled: bool = True
    pairwise_enabled: bool = False

    # -- winner / community-vote split (Phase 6) ---------------------------- #
    # Both 0 by default: "this event does not use the split", the exact
    # behaviour every event had before this feature existed.
    winner_slots: int = Field(default=0, ge=0, le=1000)
    community_vote_slots: int = Field(default=0, ge=0, le=1000)

    @field_validator("slug")
    @classmethod
    def _slug(cls, value: str) -> str:
        value = value.strip().lower()
        if not SLUG_RE.match(value):
            raise ValueError("slug must be lowercase alphanumeric words separated by hyphens")
        return value

    @field_validator("website_url")
    @classmethod
    def _url(cls, value: str | None) -> str | None:
        return _http_url(value)

    @model_validator(mode="after")
    def _voting_window_is_ordered(self) -> EventCreate:
        # Not on `EventDates` itself: that class's own `_windows_are_ordered`
        # covers the fields every event has (dates, judging window); voting is
        # optional configuration `EventUpdate` also needs to check, and
        # `EventUpdate` does not inherit from `EventDates` at all -- its dates
        # validation instead runs on the merged ORM object in
        # `routers/events.py`'s `_validate_windows()`, because a PATCH only
        # carries the fields that changed and validating the patch alone could
        # miss a conflict with a field the request left untouched.
        if (
            self.voting_opens_at
            and self.voting_closes_at
            and self.voting_closes_at < self.voting_opens_at
        ):
            raise ValueError("voting_closes_at must not precede voting_opens_at")
        return self


class EventUpdate(BaseModel):
    """PATCH: only the fields actually sent are applied."""

    name: ShortText | None = None
    tagline: str | None = Field(default=None, max_length=300)
    description: LongText | None = None
    website_url: str | None = Field(default=None, max_length=2048)

    @field_validator("website_url")
    @classmethod
    def _url(cls, value: str | None) -> str | None:
        return _http_url(value)

    starts_at: datetime | None = None
    ends_at: datetime | None = None
    registration_opens_at: datetime | None = None
    submission_opens_at: datetime | None = None
    submission_deadline: datetime | None = None
    judging_opens_at: datetime | None = None
    judging_closes_at: datetime | None = None
    max_team_size: int | None = Field(default=None, ge=1, le=50)
    is_published: bool | None = None

    # -- voting configuration (Phase 3, wired to a route in Phase 5) -------- #
    # `EventOut` has published `voting_access`, `voting_method`, `vote_credits`
    # and `votes_per_voter` since Phase 3 -- every voter-facing route reads
    # them -- but until this self-audit pass, nothing ever *wrote* them: there
    # was no field for any of this on `EventCreate` or `EventUpdate`, so a real
    # organizer had no way to turn on quadratic voting, open-link voting, or
    # email-gated voting at all. Every event that has ever used anything but the
    # default (`authenticated` + `single`) was seeded data, never a real one.
    # Found the same way the dead `AuditAction` and `WebhookEvent` members were:
    # by checking a feature's read side against its write side rather than
    # trusting that the existence of one implied the other.
    #
    # Deliberately not restricted once voting has started -- an organizer who
    # changes the method mid-event can confuse voters, but that is a judgment
    # call for them, the same trust this codebase already extends for changing
    # `max_team_size` after teams have formed or `is_published` at any time.
    voting_opens_at: datetime | None = None
    voting_closes_at: datetime | None = None
    voting_access: VotingAccess | None = None
    voting_method: VotingMethod | None = None
    vote_credits: int | None = Field(default=None, ge=1, le=10_000)
    votes_per_voter: int | None = Field(default=None, ge=1, le=100)
    comments_enabled: bool | None = None
    pairwise_enabled: bool | None = None

    # -- winner / community-vote split (Phase 6) ---------------------------- #
    winner_slots: int | None = Field(default=None, ge=0, le=1000)
    community_vote_slots: int | None = Field(default=None, ge=0, le=1000)


class EventSummary(ORMModel):
    id: uuid.UUID
    slug: str
    name: str
    tagline: str | None = None
    starts_at: datetime
    ends_at: datetime
    submission_deadline: datetime
    is_published: bool
    # Set while the event is archived (frozen, read-only). See `Event.archived_at`.
    archived_at: datetime | None = None


class EventOut(EventSummary):
    description: str | None = None
    website_url: str | None = None
    registration_opens_at: datetime
    submission_opens_at: datetime
    judging_opens_at: datetime | None = None
    judging_closes_at: datetime | None = None
    max_team_size: int
    tracks: list[TrackOut] = []
    prizes: list[PrizeOut] = []
    questions: list[QuestionOut] = []
    # Computed server-side so the UI never has to decide whether a deadline has
    # passed. The API is the authority on its own clock.
    submissions_open: bool
    registration_open: bool

    # -- voting configuration (Phase 3) ------------------------------------ #
    voting_opens_at: datetime | None = None
    voting_closes_at: datetime | None = None
    voting_access: VotingAccess = VotingAccess.AUTHENTICATED
    voting_method: VotingMethod = VotingMethod.SINGLE
    vote_credits: int = 100
    votes_per_voter: int = 3
    comments_enabled: bool = True
    # Computed, like `submissions_open`: the window compared against the clock, so a
    # client never has to do date arithmetic to decide what to render.
    voting_open: bool = False
    # NULL until an organizer publishes. Exposed so the organizer UI can show the
    # state; `results_public` is the decided answer.
    results_public_at: datetime | None = None
    results_public: bool = False
    # -- pairwise judging (Phase 5) ----------------------------------------- #
    pairwise_enabled: bool = False
    # -- winner / community-vote split (Phase 6) ---------------------------- #
    winner_slots: int = 0
    community_vote_slots: int = 0


# --------------------------------------------------------------------------- #
# Registration -- ahead of team formation
# --------------------------------------------------------------------------- #


class RegistrationIn(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "discord_username": "priyar",
                    "is_team_leader": True,
                    "leader_name": "Priya Rivera",
                }
            ]
        }
    )

    # Defaults to the caller's own account email when omitted -- most
    # registrants want exactly that; this exists for the minority who want
    # event mail somewhere else.
    email: EmailStr | None = None
    discord_username: Annotated[str, StringConstraints(min_length=2, max_length=64)]
    is_team_leader: bool = False
    leader_name: ShortText | None = None

    @model_validator(mode="after")
    def _leader_name_matches_intent(self) -> RegistrationIn:
        if self.is_team_leader and not self.leader_name:
            raise ValueError("leader_name is required when is_team_leader is true")
        if not self.is_team_leader and self.leader_name:
            raise ValueError("leader_name is only meaningful when is_team_leader is true")
        return self


class RegistrationOut(ORMModel):
    id: uuid.UUID
    event_id: uuid.UUID
    user_id: uuid.UUID
    user_display_name: str
    email: str
    discord_username: str
    is_team_leader: bool
    leader_name: str | None = None
    registered_at: datetime
    # Denormalised for the admin roster (item 13): whether this registrant has
    # gone on to form or join a team yet, so an organizer can see who signed
    # up but never returned without a second lookup per row.
    has_team: bool = False


class RegistrationRemovalIn(BaseModel):
    """Same shape and reasoning as `JudgeRemovalIn`/`SubmissionDisqualifyIn`:
    the one deliberately required field on an admin-oversight action."""

    reason: RequiredReason


# --------------------------------------------------------------------------- #
# Teams
# --------------------------------------------------------------------------- #


class TeamCreate(BaseModel):
    name: Annotated[str, StringConstraints(min_length=2, max_length=140)]


class TeamUpdate(BaseModel):
    name: Annotated[str, StringConstraints(min_length=2, max_length=140)] | None = None


class TeamMemberOut(BaseModel):
    user: UserPublic
    team_role: TeamRole
    joined_at: datetime


class TeamOut(ORMModel):
    id: uuid.UUID
    event_id: uuid.UUID
    event_slug: str
    name: str
    created_at: datetime
    members: list[TeamMemberOut]
    submission_id: uuid.UUID | None = None


class InviteOut(BaseModel):
    """The invite link. Behind its own endpoint so that reading it is a distinct,
    separately-authorized act rather than a field somebody forgets to strip."""

    token: str
    url: str


class JoinIn(BaseModel):
    token: Annotated[str, StringConstraints(min_length=8, max_length=64)]


class TeamMemberRemovalIn(BaseModel):
    """Staff removing a participant, for cause -- the plain `DELETE` a member
    or team owner uses to leave or manage their own roster needs no
    justification for either; this route is the separate, admin-only one
    that does. Same shape as `JudgeRemovalIn`/`SubmissionDisqualifyIn`."""

    reason: RequiredReason


# --------------------------------------------------------------------------- #
# Submissions
# --------------------------------------------------------------------------- #


MAX_TAG_LENGTH = 40


def _clean_tags(tags: list[str]) -> list[str]:
    seen: list[str] = []
    for tag in tags:
        cleaned = tag.strip().lower()
        if len(cleaned) > MAX_TAG_LENGTH:
            raise ValueError(f"'{cleaned[:20]}...' is longer than {MAX_TAG_LENGTH} characters")
        if cleaned and cleaned not in seen:
            seen.append(cleaned)
    if len(seen) > MAX_TECH_TAGS:
        raise ValueError(f"at most {MAX_TECH_TAGS} tech tags")
    return seen


MAX_DISCORD_USERNAME_LENGTH = 40
MAX_DISCORD_USERNAMES = 10


def _clean_discord_usernames(values: list[str]) -> list[str]:
    """One per team member submitting -- trimmed, and an empty entry between
    commas (`"a,,b"`, or a stray trailing comma) is a 422, not a silently
    dropped slot: it usually means somebody's handle is missing, not that
    they intended to submit fewer than they typed.
    """
    cleaned: list[str] = []
    for value in values:
        stripped = value.strip()
        if not stripped:
            raise ValueError("Discord usernames must not be empty between commas")
        if len(stripped) > MAX_DISCORD_USERNAME_LENGTH:
            raise ValueError(
                f"'{stripped[:20]}...' is longer than {MAX_DISCORD_USERNAME_LENGTH} characters"
            )
        cleaned.append(stripped)
    if len(cleaned) > MAX_DISCORD_USERNAMES:
        raise ValueError(f"at most {MAX_DISCORD_USERNAMES} Discord usernames")
    return cleaned


# `owner/repo`, optionally with a trailing slash -- deliberately not "any
# github.com URL", so `https://github.com/settings/profile` (a real URL on
# the right domain, but not a repository) is still refused.
_GITHUB_REPO_RE = re.compile(r"^https://github\.com/[\w.-]+/[\w.-]+/?$", re.IGNORECASE)


def _github_repo_url(value: str | None) -> str | None:
    """A GitHub repository URL specifically, not just any http(s) link.

    `_http_url` already closes the `javascript:` XSS vector every link field
    in this schema guards against; this is a stricter, additional check for
    exactly one field, because "the repo link" being a Notion page or a
    personal site is a real, common failure mode `_http_url` alone does not
    catch, and a judge clicking "View repo" expects exactly that.
    """
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    if not _GITHUB_REPO_RE.match(value):
        raise ValueError("must be a GitHub repository URL, e.g. https://github.com/owner/repo")
    if len(value) > URL_LEN:
        raise ValueError(f"must be {URL_LEN} characters or fewer")
    return value


class SubmissionCreate(BaseModel):
    team_id: uuid.UUID
    name: Annotated[str, StringConstraints(min_length=1, max_length=160)]


class SubmissionUpdate(BaseModel):
    """Every field optional: this is the draft-and-edit path, and a team that
    fills in one box at a time should not have to resend the whole project."""

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "name": "Crosshatch",
                    "tagline": "Routes freight around the outage it just admitted to",
                    "description": (
                        "Crosshatch re-plans freight routes in real time and shows its "
                        "own confidence instead of pretending certainty it doesn't have."
                    ),
                    "repo_url": "https://github.com/example/crosshatch",
                    "demo_video_url": "https://example.com/crosshatch-demo",
                    "tech_tags": ["rust", "postgres"],
                    "discord_usernames": ["priyar", "kwame.o"],
                }
            ]
        }
    )

    name: Annotated[str, StringConstraints(min_length=1, max_length=160)] | None = None
    tagline: str | None = Field(default=None, max_length=240)
    description: LongText | None = None
    thumbnail_url: str | None = Field(default=None, max_length=2048)
    gallery_image_urls: list[str] | None = None
    demo_video_url: str | None = Field(default=None, max_length=2048)
    repo_url: str | None = Field(default=None, max_length=2048)
    live_url: str | None = Field(default=None, max_length=2048)
    linkedin_url: str | None = Field(default=None, max_length=2048)
    tech_tags: list[str] | None = None
    discord_usernames: list[str] | None = None
    track_id: uuid.UUID | None = None
    answers: dict[uuid.UUID, LongText] | None = None

    @field_validator("tech_tags")
    @classmethod
    def _tags(cls, value: list[str] | None) -> list[str] | None:
        return None if value is None else _clean_tags(value)

    @field_validator("discord_usernames")
    @classmethod
    def _discord(cls, value: list[str] | None) -> list[str] | None:
        return None if value is None else _clean_discord_usernames(value)

    @field_validator("thumbnail_url")
    @classmethod
    def _thumb(cls, value: str | None) -> str | None:
        return _image_url(value)

    @field_validator("repo_url")
    @classmethod
    def _repo(cls, value: str | None) -> str | None:
        return _github_repo_url(value)

    @field_validator("demo_video_url", "live_url", "linkedin_url")
    @classmethod
    def _one_url(cls, value: str | None) -> str | None:
        return _http_url(value)

    @field_validator("gallery_image_urls")
    @classmethod
    def _images(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        if len(value) > MAX_GALLERY_IMAGES:
            raise ValueError(f"at most {MAX_GALLERY_IMAGES} gallery images")
        checked = [_image_url(url) for url in value]
        return [url for url in checked if url is not None]


class AnswerOut(BaseModel):
    question_id: uuid.UUID
    prompt: str
    value: str | None = None


class SubmissionCard(BaseModel):
    """The gallery row. Deliberately smaller than `SubmissionOut` -- a gallery
    page of forty projects should not ship forty long descriptions."""

    id: uuid.UUID
    name: str
    tagline: str | None = None
    thumbnail_url: str | None = None
    tech_tags: list[str]
    track: TrackOut | None = None
    team_name: str
    submitted_at: datetime | None = None


class SubmissionOut(ORMModel):
    id: uuid.UUID
    event_id: uuid.UUID
    event_slug: str
    team_id: uuid.UUID
    team_name: str
    name: str
    tagline: str | None = None
    description: str | None = None
    thumbnail_url: str | None = None
    gallery_image_urls: list[str]
    demo_video_url: str | None = None
    repo_url: str | None = None
    live_url: str | None = None
    linkedin_url: str | None = None
    tech_tags: list[str]
    discord_usernames: list[str]
    track: TrackOut | None = None
    status: SubmissionStatus
    submitted_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
    answers: list[AnswerOut] = []
    members: list[UserPublic] = []
    # What the caller may do next, computed by the same predicate that enforces
    # it. The UI hides buttons off this; the API does not trust that it did.
    can_edit: bool = False
    can_submit: bool = False
    # Part 3's winner/community-vote split -- `None` for every event that does
    # not use it, and for a caller this event's own `results_public()` switch
    # has not yet been flipped for (staff see it always). Only ever set by
    # `GET /submissions/{id}`, never by a list route: computing it means
    # running `gather()`, which is exactly the per-row cost a list of
    # submissions must not pay.
    tier: Literal["winner", "community_tier"] | None = None


class GalleryPage(BaseModel):
    items: list[SubmissionCard]
    total: int
    page: int
    per_page: int
    pages: int


GallerySort = Literal["mixed", "recent", "name", "team"]


# --------------------------------------------------------------------------- #
# Judging (Phase 2)
# --------------------------------------------------------------------------- #


class CriterionIn(BaseModel):
    """A rubric row as an organizer writes it.

    `weight` is whatever number they like: 1 and 2 mean the same thing as 10 and
    20. Division by the sum happens at scoring time, documented in JUDGING.md.
    """

    key: Annotated[str, StringConstraints(min_length=1, max_length=60)]
    name: ShortText
    description: LongText | None = None
    weight: float = Field(default=1.0, gt=0, le=1000)
    min_score: int = Field(default=1, ge=0, le=100)
    max_score: int = Field(default=5, ge=1, le=100)
    position: int = 0

    @field_validator("key")
    @classmethod
    def _slug(cls, value: str) -> str:
        value = value.strip().lower()
        if not SLUG_RE.match(value):
            raise ValueError("key must be lowercase alphanumeric words separated by hyphens")
        return value

    @model_validator(mode="after")
    def _range_ordered(self) -> CriterionIn:
        if self.max_score <= self.min_score:
            raise ValueError("max_score must be greater than min_score")
        return self


class CriterionUpdate(BaseModel):
    name: ShortText | None = None
    description: LongText | None = None
    weight: float | None = Field(default=None, gt=0, le=1000)
    min_score: int | None = Field(default=None, ge=0, le=100)
    max_score: int | None = Field(default=None, ge=1, le=100)
    position: int | None = None


class CriterionOut(ORMModel):
    id: uuid.UUID
    key: str
    name: str
    description: str | None = None
    weight: float
    min_score: int
    max_score: int
    position: int


class JudgeIn(BaseModel):
    """Invite a judge to an event.

    Identified by email rather than id: an organizer inviting judges is working
    from a list of people, not a list of UUIDs. `track_id` of None means the
    judge sees every track.
    """

    email: EmailStr
    track_id: uuid.UUID | None = None


class JudgeUpdate(BaseModel):
    track_id: uuid.UUID | None = None
    is_active: bool | None = None
    # Explicit, because `track_id: None` is ambiguous in a PATCH body: it could
    # mean "promote to all-tracks" or "leave alone".
    clear_track: bool = False


class JudgeRemovalIn(BaseModel):
    """Removal for cause -- distinct from the plain `is_active` toggle above,
    which needs no justification because it changes nothing already written.
    This does: incomplete ballots are dropped (see `remove_judge`'s own
    docstring), so a reason is mandatory, the one deliberately required field
    in this whole admin-oversight surface."""

    reason: RequiredReason


class SubmissionDisqualifyIn(BaseModel):
    """Same shape and reasoning as `JudgeRemovalIn`: the one deliberately
    required field on an otherwise soft, reversible admin action."""

    reason: RequiredReason


class JudgeOut(ORMModel):
    id: uuid.UUID
    event_id: uuid.UUID
    # Denormalised, same reasoning as `AuditEntry.event_slug`: `GET /api/judging/me`
    # spans every event a judge is on, and the judge console needs a slug to link
    # to each one without an extra round trip per row.
    event_slug: str
    pairwise_enabled: bool = False
    user: UserPublic
    # Carried explicitly rather than by widening UserPublic: this endpoint is
    # staff-only and an organizer manages judges by address, but a judge's email
    # must not start appearing on a team roster.
    email: str
    track: TrackOut | None = None
    is_active: bool
    invited_at: datetime
    accepted_at: datetime | None = None


class ScoreIn(BaseModel):
    criterion_id: uuid.UUID
    # Continuous, not discrete: a judge scores in 0.1 steps, not whole numbers.
    # Bounded 0..100 for the same reason `_validate_against_rubric` gives --
    # Pydantic cannot know the event's actual rubric range, so the real bound
    # is enforced there against the criterion's own min/max.
    value: float = Field(ge=0, le=100, multiple_of=0.1)
    comment: str | None = None


class BallotIn(BaseModel):
    """A judge's whole ballot, submitted at once.

    All-at-once rather than per-criterion PATCHes: a ballot is a judgement, and
    half of one is not a smaller judgement. `complete=False` saves progress
    without claiming the review is finished.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "scores": [
                        {
                            "criterion_id": "3b1f6e2a-2f7d-4b8e-9c6a-6b6f1a0e2d10",
                            "value": 4,
                            "comment": "Clean demo, the failure-mode question was answered well.",
                        },
                        {"criterion_id": "7d2c9a44-5e11-4f3a-8b90-1a2b3c4d5e6f", "value": 3},
                    ],
                    "comment": "Strong technical execution; the pitch undersold it.",
                    "complete": True,
                }
            ]
        }
    )

    scores: list[ScoreIn] = Field(min_length=1, max_length=50)
    comment: str | None = None
    complete: bool = True

    @model_validator(mode="after")
    def _no_duplicate_criteria(self) -> BallotIn:
        seen = {s.criterion_id for s in self.scores}
        if len(seen) != len(self.scores):
            raise ValueError("one score per criterion")
        return self


class ScoreOut(ORMModel):
    criterion_id: uuid.UUID
    criterion_key: str
    value: float
    comment: str | None = None


class AssignmentOut(ORMModel):
    """One ballot. Only ever served to the judge who owns it, or to staff."""

    id: uuid.UUID
    event_id: uuid.UUID
    event_slug: str
    judge_id: uuid.UUID
    judge_name: str
    status: AssignmentStatus
    comment: str | None = None
    # True when this ballot was created by `POST .../third-review` to
    # adjudicate a flagged disagreement, rather than by ordinary batch
    # assignment. See `models.JudgeAssignment.is_adjudication`.
    is_adjudication: bool = False
    assigned_at: datetime
    completed_at: datetime | None = None
    submission: SubmissionCard
    scores: list[ScoreOut] = []
    raw_score: float | None = None
    can_score: bool = False


class AssignRequest(BaseModel):
    reviews_per_submission: int = Field(default=3, ge=1, le=20)
    # Reproducibility: the same seed and the same population give the same plan,
    # so an organizer can demonstrate the assignment was not arbitrary.
    seed: int | None = None
    dry_run: bool = False


class ShortfallOut(BaseModel):
    submission_id: uuid.UUID
    submission_name: str
    requested: int
    achieved: int
    eligible: int
    reason: str


class AssignResult(BaseModel):
    created: int
    reviews_per_submission: int
    submissions: int
    judges: int
    balanced: bool
    spread: int
    loads: dict[str, int]
    shortfalls: list[ShortfallOut] = []
    dry_run: bool = False


class RecusalIn(BaseModel):
    """An organizer recording a judge's conflict of interest on one project.

    A judge's own self-service equivalent (`POST
    /api/judging/assignments/{id}/recuse`) does not need `submission_id`: the
    assignment they are recusing from already names it.
    """

    submission_id: uuid.UUID
    reason: str | None = None


class SelfRecusalIn(BaseModel):
    reason: str | None = None


class RecusalOut(ORMModel):
    id: uuid.UUID
    judge_id: uuid.UUID
    judge_name: str
    submission_id: uuid.UUID
    submission_name: str
    reason: str | None = None
    created_at: datetime


class ThirdReviewResult(BaseModel):
    """The outcome of routing a flagged submission to an uninvolved judge.

    Additive, never a replacement: the ballots that disagreed are untouched,
    and this is just one more `JudgeAssignment` -- see
    `models.JudgeAssignment.is_adjudication`.
    """

    assignment_id: uuid.UUID
    judge_id: uuid.UUID
    judge_name: str
    submission_id: uuid.UUID


class JudgeProgressRow(BaseModel):
    judge_id: uuid.UUID
    judge_name: str
    track: str | None = None
    assigned: int
    complete: int
    in_progress: int
    pending: int

    @property
    def started(self) -> bool:
        return self.complete + self.in_progress > 0


class ProgressOut(BaseModel):
    """The live dashboard. The question it exists to answer is "who has not
    started", which is why `not_started` is computed here rather than left for
    the caller to derive."""

    event_slug: str
    judging_opens_at: datetime | None = None
    judging_closes_at: datetime | None = None
    judging_open: bool
    judges: list[JudgeProgressRow]
    submissions_total: int
    submissions_fully_reviewed: int
    assignments_total: int
    assignments_complete: int
    percent_complete: float
    not_started: list[str] = []


class CalibrationOut(BaseModel):
    judge_id: uuid.UUID
    judge_name: str
    # `None` means the untracked-submissions group, not "unknown" -- see
    # `app/scoring.py`'s `normalize_by_track`. A judge who reviews across
    # every track appears once per track they actually scored in, each row
    # with a different `track`: their calibration in one track says nothing
    # about how they used the scale in another.
    track: str | None = None
    n: int
    mean: float
    sd: float
    shrunk_mean: float
    shrunk_sd: float
    flat: bool
    note: str


class ResultRow(BaseModel):
    submission_id: uuid.UUID
    submission_name: str
    team_name: str
    track: str | None = None
    n_reviews: int
    raw_mean: float
    normalized_mean: float
    raw_rank: int
    normalized_rank: int
    rank_delta: int
    # Part 3's winner/community-vote split, computed live from `normalized_rank`
    # -- `None` for every row on an event that does not use it (both
    # `winner_slots`/`community_vote_slots` default to 0).
    computed_tier: Literal["winner", "community_tier"] | None = None
    # Part 4: the effective tier after an admin override, if any -- this is
    # the one voting eligibility and a team's own `GET /submissions/{id}`
    # actually use. Equal to `computed_tier` when there is no override.
    # Never the reverse: `computed_tier` above is never adjusted to match
    # this, so the judge-computed ranking stays visible and recoverable.
    tier: Literal["winner", "community_tier"] | None = None
    override_reason: str | None = None
    overridden_by: str | None = None
    overridden_at: datetime | None = None


class ResultOverrideIn(BaseModel):
    """Set or change one submission's final tier, for cause. `tier` is
    `winner`/`community_tier`/`neither` -- `neither` is its own explicit
    choice, not the absence of a field, because removing a submission from a
    tier is a real decision distinct from it never having been computed into
    one."""

    tier: Literal["winner", "community_tier", "neither"]
    reason: RequiredReason


class ResultOverrideClearIn(BaseModel):
    reason: RequiredReason


class ScoringGridCell(BaseModel):
    criterion_id: uuid.UUID
    value: float


class ScoringGridJudgeRow(BaseModel):
    judge_id: uuid.UUID
    judge_name: str
    status: str
    is_adjudication: bool
    scores: list[ScoringGridCell]
    comment: str | None = None
    # This judge's own weighted mean for this one submission, and their
    # *normalized* contribution to it -- the same per-judge value
    # `normalize_by_track()` already computed, not recomputed here.
    raw_mean: float | None = None
    normalized_mean: float | None = None


class ScoringGridOut(BaseModel):
    """One project, every judge's ballot side by side. Read-only: nothing
    reachable from this response writes to a `Score` row -- see
    `results.scoring_grid`'s own docstring."""

    submission_id: uuid.UUID
    submission_name: str
    criteria: list[CriterionOut]
    judges: list[ScoringGridJudgeRow]
    n_reviews: int
    final_raw_mean: float | None = None
    final_normalized_mean: float | None = None


class JudgeReportRow(BaseModel):
    """One judge's ballot, anonymized. `label` is `"Judge 1"`, `"Judge 2"`,
    ... in a stable order -- never a name or id. `access.py` already treats
    who is judging as staff-only information; this is that same rule applied
    to the one place a participant is shown ballot content at all."""

    label: str
    scores: list[ScoringGridCell]
    comment: str | None = None
    raw_mean: float | None = None
    normalized_mean: float | None = None


class JudgeReportOut(BaseModel):
    """A team's own project, judge feedback included -- and nothing else.
    Every field here is scoped to the one submission this was requested for;
    see `results.judge_report`'s own docstring for the isolation this is
    built to guarantee."""

    submission_id: uuid.UUID
    submission_name: str
    criteria: list[CriterionOut]
    judges: list[JudgeReportRow]
    n_reviews: int
    final_raw_mean: float | None = None
    final_normalized_mean: float | None = None
    tier: Literal["winner", "community_tier"] | None = None


class PublicResultRow(BaseModel):
    submission_id: uuid.UUID
    submission_name: str
    team_name: str
    track: str | None = None
    tier: Literal["winner", "community_tier"] | None = None
    # Both null for a submitted project nobody has judged yet -- shown, not
    # hidden: "all submitted projects, not just winners" means exactly that.
    normalized_rank: int | None = None
    normalized_score: float | None = None
    thumbnail_url: str | None = None
    repo_url: str | None = None
    live_url: str | None = None
    demo_video_url: str | None = None


class PublicResultsOut(BaseModel):
    """The public results dashboard (Part 5) -- distinct from the community-
    vote tally (`GET /{slug}/voting/results`, a vote count) and from the
    gallery (`GET /api/gallery`, no rank or score at all). Gated by the same
    `results_public()` switch as both of those."""

    event_slug: str
    rows: list[PublicResultRow]


class ResultsOut(BaseModel):
    """Staff-only in T2. The normalization is published alongside the numbers
    because a ranking you cannot explain is not a ranking anybody will accept."""

    event_slug: str
    method: str
    global_mean: float
    global_sd: float
    shrinkage_k: float
    rows: list[ResultRow]
    calibrations: list[CalibrationOut]


class DisagreementRowOut(BaseModel):
    submission_id: uuid.UUID
    submission_name: str
    team_name: str
    track: str | None = None
    n_reviews: int
    sd: float
    mean_abs_pairwise_diff: float
    needs_review: bool


class JudgeDeviationOut(BaseModel):
    judge_id: uuid.UUID
    judge_name: str
    n_reviews: int
    mean_abs_deviation: float


class DisagreementOut(BaseModel):
    """Inter-rater reliability: which projects need a third opinion, and which
    judges are furthest from consensus even after normalization already
    corrected for how they use the scale. See `app/scoring.py`'s
    `disagreement()` and `judge_deviation()` for the method and why these two
    are different questions."""

    threshold: float
    needs_review_count: int
    submissions: list[DisagreementRowOut]
    judges: list[JudgeDeviationOut]


class ConsistencyFlagOut(BaseModel):
    judge_id: uuid.UUID
    judge_name: str
    reason: str
    detail: str


class ConsistencyOut(BaseModel):
    """Suspicious-pattern flags for organizer review only -- never a verdict.

    See `app/scoring.py`'s `near_identical_score_flags()` and
    `fast_completion_flags()` for the two signals and why each is worded as a
    pattern in the data rather than a conclusion about a judge.
    """

    flags: list[ConsistencyFlagOut]


# --------------------------------------------------------------------------- #
# Pairwise judging (Phase 5)
# --------------------------------------------------------------------------- #


class PairOut(BaseModel):
    """The next two projects to show one judge. Full `SubmissionOut` shape --
    the same thing the public project page renders -- because deciding "which
    is better" well needs the description and the links, not a gallery card."""

    submission_a: SubmissionOut
    submission_b: SubmissionOut


class PairwiseVoteIn(BaseModel):
    """`winner_id` is `None` for "I can't decide" -- see `app/pairwise.py` on why
    a tie is kept, not discarded. When given, it must equal one of the two ids;
    enforced by the route against the judge's own Judge row and by a database
    CHECK constraint against forged or mismatched data either way."""

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "submission_a_id": "3b1f6e2a-2f7d-4b8e-9c6a-6b6f1a0e2d10",
                    "submission_b_id": "7d2c9a44-5e11-4f3a-8b90-1a2b3c4d5e6f",
                    "winner_id": "3b1f6e2a-2f7d-4b8e-9c6a-6b6f1a0e2d10",
                }
            ]
        }
    )

    submission_a_id: uuid.UUID
    submission_b_id: uuid.UUID
    winner_id: uuid.UUID | None = None

    @model_validator(mode="after")
    def _distinct_and_consistent(self) -> PairwiseVoteIn:
        if self.submission_a_id == self.submission_b_id:
            raise ValueError("submission_a_id and submission_b_id must be different")
        if self.winner_id is not None and self.winner_id not in (
            self.submission_a_id,
            self.submission_b_id,
        ):
            raise ValueError("winner_id must be submission_a_id, submission_b_id, or null")
        return self


class PairwiseComparisonOut(BaseModel):
    id: uuid.UUID
    submission_a_id: uuid.UUID
    submission_b_id: uuid.UUID
    winner_id: uuid.UUID | None = None
    created_at: datetime


class PairwiseResultRow(BaseModel):
    submission_id: uuid.UUID
    submission_name: str
    team_name: str
    track: str | None = None
    rank: int
    rating: float
    strength: float
    n_comparisons: int
    wins: float
    losses: float
    win_rate: float | None = None


class PairwiseJudgeProgressRow(BaseModel):
    judge_id: uuid.UUID
    judge_name: str
    n_comparisons: int


class PairwiseCoverageOut(BaseModel):
    """Is this round actually balanced, without reading the per-project table
    row by row. See `app/pairwise.py`'s `coverage()`."""

    total_submissions: int
    total_comparisons: int
    min_comparisons: int
    max_comparisons: int
    mean_comparisons: float
    coverage_pct: float


class PairwiseResultsOut(BaseModel):
    """Organizer-only, same as `ResultsOut`. The method is published for the same
    reason: a ranking built from comparisons nobody can re-derive is not one an
    organizer can defend when a team asks why they placed where they did."""

    event_slug: str
    method: str
    total_comparisons: int
    converged: bool
    coverage: PairwiseCoverageOut
    rows: list[PairwiseResultRow]
    judges: list[PairwiseJudgeProgressRow]


# --------------------------------------------------------------------------- #
# Public voting, comments and audit (Phase 3)
# --------------------------------------------------------------------------- #


class ClaimIn(BaseModel):
    """Claim a ballot. `email` is required only in email-gated mode."""

    email: EmailStr | None = None


class VoterOut(BaseModel):
    """The ballot identity handed back on a claim.

    `token` is returned **only** for open-link voting, where there is no session to
    hang the identity off. `verified` and `caveat` are part of the contract rather
    than documentation: a client showing "you are voting as a@example.com" should
    also be able to say that nobody checked.
    """

    id: uuid.UUID
    event_slug: str
    access: VotingAccess
    method: VotingMethod
    token: str | None = None
    email: str | None = None
    verified: bool
    caveat: str | None = None
    credits_total: int
    credits_spent: int
    credits_remaining: int
    max_projects: int
    voting_open: bool
    voting_closes_at: datetime | None = None


class VoteIn(BaseModel):
    submission_id: uuid.UUID
    credits: int = Field(default=1, ge=1, le=1000)


class BallotIn2(BaseModel):
    """A whole ballot, replacing whatever was there.

    Replacement rather than increment: in quadratic mode the budget is global, so
    a partial update would have to be validated against the rest of the ballot
    anyway. An empty list is a withdrawal.
    """

    votes: list[VoteIn] = Field(default_factory=list, max_length=200)


class VoteOut(BaseModel):
    submission_id: uuid.UUID
    submission_name: str
    credits: int
    cost: int


class BallotOut(BaseModel):
    """What the voter currently has down, plus what it cost them."""

    voter_id: uuid.UUID
    event_slug: str
    method: VotingMethod
    credits_total: int
    credits_spent: int
    credits_remaining: int
    max_projects: int
    voting_open: bool
    votes: list[VoteOut] = []
    # Randomised per voter, stable across refreshes. See app.voting.ballot_order.
    projects: list[SubmissionCard] = []


class TallyRow(BaseModel):
    submission_id: uuid.UUID
    submission_name: str
    team_name: str
    track: str | None = None
    rank: int
    votes: int
    voters: int
    # Quadratic only: what voters collectively paid, which is not the same shape as
    # the vote count and is worth publishing beside it.
    credits: int


class TallyOut(BaseModel):
    event_slug: str
    method: VotingMethod
    access: VotingAccess
    voting_open: bool
    public: bool
    total_voters: int
    rows: list[TallyRow]
    caveat: str | None = None


class CommentIn(BaseModel):
    body: Annotated[str, StringConstraints(min_length=1, max_length=4000)]

    @field_validator("body")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        """`min_length` counts characters, including spaces.

        Without this, "   " reaches the database, trips
        `ck_comments_body_not_blank` and becomes a 500. The constraint is the
        guarantee; this is the 422 that explains it -- the same split as everywhere
        else in this file.
        """
        stripped = value.strip()
        if not stripped:
            raise ValueError("a comment cannot be blank")
        return stripped


class CommentOut(ORMModel):
    id: uuid.UUID
    submission_id: uuid.UUID
    author: UserPublic
    body: str
    created_at: datetime
    is_hidden: bool = False
    hidden_reason: str | None = None
    can_remove: bool = False


class HideIn(BaseModel):
    reason: Annotated[str, StringConstraints(min_length=1, max_length=300)]

    @field_validator("reason")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("a moderation reason is required")
        return stripped


class AnnouncementIn(BaseModel):
    """`visible_to_visitors` defaults to the narrower reading -- an organizer
    who wants a signed-out visitor to see this notice has to say so."""

    title: ShortText
    body: Annotated[str, StringConstraints(min_length=1, max_length=10_000)]
    visible_to_visitors: bool = False

    @field_validator("title", "body")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        """`min_length` counts characters, including spaces -- without this,
        an all-whitespace value reaches the database, trips one of
        `ck_announcements_title_not_blank`/`ck_announcements_body_not_blank`
        and becomes a 500 instead of a 422."""
        stripped = value.strip()
        if not stripped:
            raise ValueError("cannot be blank")
        return stripped


class AnnouncementOut(ORMModel):
    id: uuid.UUID
    event_id: uuid.UUID
    title: str
    body: str
    visible_to_visitors: bool
    posted_by_display_name: str | None = None
    posted_at: datetime


class AuditRow(BaseModel):
    """One line an organizer can read. `summary` is the line; the rest is filter.

    `event_slug` is denormalised on the row (see `AuditEntry.event_slug`), so it
    survives the event being deleted -- `event_id` is `ON DELETE SET NULL`,
    deliberately, so that deleting an event orphans its audit history rather
    than destroying it. On the per-event endpoint this is always the event you
    asked for; on the global one (admin-only) it is what tells the rows apart.
    """

    id: uuid.UUID
    created_at: datetime
    event_slug: str | None = None
    action: AuditAction
    actor_label: str
    actor_role: str | None = None
    resource_type: str | None = None
    resource_id: uuid.UUID | None = None
    summary: str
    ip_address: str | None = None


class AuditPage(BaseModel):
    event_slug: str | None = None
    total: int
    page: int
    per_page: int
    pages: int
    rows: list[AuditRow]


class ChainBreak(BaseModel):
    """Where the hash chain first stopped matching itself. Absent when valid."""

    seq: int
    id: uuid.UUID
    reason: str


class ChainVerification(BaseModel):
    """The result of walking the whole audit log and recomputing every hash.

    `valid=True, checked=0` on a brand-new install with no entries yet -- an
    empty chain has nothing to have been tampered with.
    """

    valid: bool
    checked: int
    first_break: ChainBreak | None = None


# --------------------------------------------------------------------------- #
# Webhooks, certificates and bulk import (Phase 4)
# --------------------------------------------------------------------------- #


class WebhookCreate(BaseModel):
    """`AnyHttpUrl` rejects a missing scheme and obvious nonsense; it does **not**
    know that an address is private. The SSRF check in `app.hooks` does that, and it
    runs in the route -- validation here is about shape, never about safety."""

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "url": "https://hooks.example.com/dogfood",
                    "topics": ["submission.submitted", "results.published"],
                    "description": "Slack relay for the organizer channel",
                }
            ]
        }
    )

    url: AnyHttpUrl
    topics: list[str] = Field(default_factory=list, max_length=40)
    description: str | None = Field(default=None, max_length=200)


class WebhookUpdate(BaseModel):
    url: AnyHttpUrl | None = None
    topics: list[str] | None = None
    description: str | None = Field(default=None, max_length=200)
    is_active: bool | None = None


class WebhookOut(BaseModel):
    id: uuid.UUID
    event_id: uuid.UUID
    url: str
    topics: list[str]
    is_active: bool
    description: str | None = None
    created_at: datetime
    failure_count: int
    last_delivery_at: datetime | None = None


class WebhookCreated(WebhookOut):
    """Adds the signing secret, returned exactly once.

    A receiver needs it to verify deliveries. Returning it on every list would be a
    second exposure for no gain, so `WebhookOut` does not carry it.
    """

    secret: str


class DeliveryOut(BaseModel):
    id: uuid.UUID
    topic: str
    status: DeliveryStatus
    attempts: int
    response_code: int | None = None
    error: str | None = None
    created_at: datetime
    delivered_at: datetime | None = None


class CertificateOut(BaseModel):
    id: uuid.UUID
    event_id: uuid.UUID
    kind: CertificateKind
    subject_name: str
    title: str
    code: str
    # The exact bytes that were signed, as stored. Not re-serialised -- see
    # app/signing.py for why that matters.
    payload: str
    signature: str
    key_id: str
    issued_at: datetime
    revoked_at: datetime | None = None
    revoked_reason: str | None = None
    valid: bool


class CertificatePublic(BaseModel):
    """What an outsider gets. No ids, no account, and enough to check the maths.

    `key_id` names which key in `GET /api/signing/public-keys` to check the
    signature against -- Ed25519 verification needs the specific public key,
    not just "some key we hold", so a verifier who wants to run the check
    themselves rather than trust `signature_valid` needs this field.
    """

    code: str
    kind: CertificateKind
    title: str
    subject_name: str
    issued_at: datetime
    payload: str
    signature: str
    key_id: str
    signature_valid: bool
    revoked: bool
    revoked_reason: str | None = None
    # Both conditions: the signature matches AND it has not been withdrawn.
    valid: bool
    detail: str | None = None


class IssueResult(BaseModel):
    issued: int
    updated: int
    certificates: list[CertificateOut] = []


class CertificateIssueForUserIn(BaseModel):
    """Admin-issued, for one specific judge or organizer -- distinct from
    the bulk pass, and the only way an `ORGANIZING` record is ever
    created (there is no automatic pass for it, the way there is for
    `PARTICIPATION`/`JUDGING`/`PLACEMENT`: organizer is a platform-wide
    role, not a per-event assignment like `Judge`, so there is nothing to
    scan for "everyone who organized this event")."""

    user_id: uuid.UUID
    kind: Literal["judging", "organizing"]


class CertificateLookupRow(BaseModel):
    """One of the caller's own certificates for a project.

    `code` is enough for the frontend to fetch the full record and its
    PDF/PNG directly -- which is exactly why this is no longer a public
    lookup (Phase 6 fix): `code` is the one thing that actually lets someone
    download a certificate, and `GET /submissions/{id}/certificates` used to
    hand out everyone's, to anyone, with no check that the caller was the
    person named. See that route's own docstring."""

    code: str
    kind: CertificateKind
    title: str
    subject_name: str


class RevokeIn(BaseModel):
    reason: Annotated[str, StringConstraints(min_length=1, max_length=300)]

    @field_validator("reason")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("a revocation reason is required")
        return stripped


class SigningKeyOut(BaseModel):
    """One entry in the published key set. Public half only, obviously."""

    kid: str
    public_key_pem: str
    status: Literal["active", "retired"]


class ImportResult(BaseModel):
    """Per-row outcomes, because all-or-nothing would make one typo discard the
    other thirty-nine corrections.

    `dry_run=True` means exactly what it says elsewhere in this API
    (`AssignRequest.dry_run`): every row was parsed and validated against the
    database, and this is what *would* happen, but nothing was written.
    """

    created: int
    updated: int
    skipped: int
    errors: list[str] = []
    dry_run: bool = False


# --------------------------------------------------------------------------- #
# Judge calibration
# --------------------------------------------------------------------------- #


class CalibrationValueIn(BaseModel):
    criterion_id: uuid.UUID
    value: float = Field(ge=0, le=100, multiple_of=0.1)


class CalibrationProjectIn(BaseModel):
    """A practice project plus the scores an organizer expects for it -- one per
    criterion, no more and no fewer (checked in the router against the event's
    real rubric, which Pydantic cannot see)."""

    name: str = Field(min_length=1, max_length=160)
    description: str | None = Field(default=None, max_length=4000)
    expected: list[CalibrationValueIn] = Field(min_length=1, max_length=50)


class CalibrationProjectOut(BaseModel):
    """Staff view: includes the expected scores. Judges never get this shape."""

    id: uuid.UUID
    event_slug: str
    name: str
    description: str | None
    expected: list[CalibrationValueIn]
    created_at: datetime


class CalibrationPracticeOut(BaseModel):
    """Judge view of one practice project: what to score, and what they have
    scored so far -- never the expected values."""

    id: uuid.UUID
    event_slug: str
    name: str
    description: str | None
    criteria: list[CriterionOut]
    my_scores: list[CalibrationValueIn]
    complete: bool


class CalibrationScoresIn(BaseModel):
    scores: list[CalibrationValueIn] = Field(min_length=1, max_length=50)


class CalibrationJudgeRow(BaseModel):
    judge_id: uuid.UUID
    judge_name: str
    projects_scored: int
    projects_total: int
    mean_signed_deviation: float | None
    mean_abs_deviation: float | None
    verdict: str


class CalibrationReportOut(BaseModel):
    event_slug: str
    threshold: float
    projects_total: int
    rows: list[CalibrationJudgeRow]
