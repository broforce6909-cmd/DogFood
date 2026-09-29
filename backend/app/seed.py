"""Fixture seeding -- idempotent, safe to run on every container start.

`docker compose up` has to produce a portal with something in it, so this builds
a small but complete event: users in every role, tracks, prizes, organizer
questions, teams with real memberships, and a gallery of submitted projects.

Two events are seeded on purpose:

* **dogfood** is live. Registration is open, the submission deadline is two
  weeks out, and a team can be created, invited to, and submitted from.
* **raptors-summer** is over. Its deadline is thirty days in the past, so
  the very first thing a reviewer can check is that the deadline actually holds:
  every write to its submissions is refused, by the same predicate that permits
  them in the live event.

Ids are derived with `uuid5` from a fixed namespace, so a reseeded database
produces the same ids and any URL in the documentation keeps working.

Everything here is our own invented data. The organizers' published fixture set
is not in this repository; when it is, it loads through the same path.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .audit import record
from .db import SessionLocal
from .models import (
    AdminLevel,
    AssignmentStatus,
    AuditAction,
    AuditEntry,
    CalibrationExpected,
    CalibrationProject,
    CalibrationScore,
    Certificate,
    CertificateKind,
    Comment,
    Event,
    EventQuestion,
    Judge,
    JudgeAssignment,
    Prize,
    QuestionKind,
    Role,
    RubricCriterion,
    Score,
    Submission,
    SubmissionAnswer,
    SubmissionStatus,
    Team,
    TeamMember,
    TeamRole,
    Track,
    User,
    Vote,
    Voter,
    VotingAccess,
    VotingMethod,
    Webhook,
    WebhookEvent,
    utcnow,
)
from .security import hash_password
from .signing import CODE_ALPHABET, CODE_LENGTH, sign_payload

NS = uuid.UUID("1b4f0e9f-6f6a-5a3e-9f3a-2a6b1c0d4e5f")

# Every fixture account shares this password. It is printed in the README, and
# it is the reason `SESSION_SECRET` has "change-me-in-production" in its name.
FIXTURE_PASSWORD = "dogfood2026"


def _id(kind: str, key: str) -> uuid.UUID:
    return uuid.uuid5(NS, f"{kind}:{key}")


# --------------------------------------------------------------------------- #
# The cast
# --------------------------------------------------------------------------- #

STAFF: list[tuple[str, str, Role]] = [
    ("admin@example.com", "Ada Okonkwo", Role.ADMIN),
    ("organizer@example.com", "Marion Vasquez", Role.ORGANIZER),
    ("organizer2@example.com", "Theo Lindqvist", Role.ORGANIZER),
    ("judge.rivera@example.com", "Priya Rivera", Role.JUDGE),
    ("judge.osei@example.com", "Kwame Osei", Role.JUDGE),
    ("judge.tanaka@example.com", "Hana Tanaka", Role.JUDGE),
    ("judge.novak@example.com", "Ivan Novak", Role.JUDGE),
    # Invited, assigned, and deliberately idle, so `not_started` on the progress
    # dashboard is non-empty on a fresh install.
    ("judge.whitfield@example.com", "Dale Whitfield", Role.JUDGE),
]

# The other two admin levels, so `AdminLevel` is demonstrable on a fresh install:
# a manager (everything but account administration) and an auditor (read-only).
# `admin@example.com` above is an owner -- the default.
LEVELED_ADMINS: list[tuple[str, str, AdminLevel]] = [
    ("manager@example.com", "Morgan Reyes", AdminLevel.MANAGER),
    ("auditor@example.com", "Avery Chen", AdminLevel.AUDITOR),
]

PARTICIPANTS: list[tuple[str, str]] = [
    ("sam@example.com", "Sam Ferreira"),
    ("noor@example.com", "Noor Haddad"),
    ("wei@example.com", "Wei Zhang"),
    ("leah@example.com", "Leah Brennan"),
    ("diego@example.com", "Diego Morales"),
    ("aisha@example.com", "Aisha Bello"),
    ("tomas@example.com", "Tomas Nilsson"),
    ("ruth@example.com", "Ruth Agyeman"),
    ("kenji@example.com", "Kenji Watanabe"),
    ("mira@example.com", "Mira Kaur"),
    ("owen@example.com", "Owen Doyle"),
    ("zora@example.com", "Zora Petrovic"),
    ("felix@example.com", "Felix Andersen"),
    ("nadia@example.com", "Nadia Rahman"),
    ("cole@example.com", "Cole Whitfield"),
    ("ines@example.com", "Ines Oliveira"),
]

TRACKS: list[tuple[str, str, str]] = [
    ("ai-agents", "AI & Agents", "Anything where a model is doing the deciding."),
    ("developer-tools", "Developer Tools", "Things you would install and actually use."),
    ("open-data", "Open Data", "Public data, made usable by somebody who cared."),
]

# A small SVG data URI: a real thumbnail that needs no network and no object
# store. Two projects use one so the image path is exercised; the rest are left
# blank so the generated placeholder is exercised too.
THUMBNAIL = (
    "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 8 5'%3E"
    "%3Crect width='8' height='5' fill='%23b4451f'/%3E"
    "%3Ccircle cx='6' cy='1.4' r='1.8' fill='%23e8763f'/%3E%3C/svg%3E"
)

# name, tagline, track key, tags, repo, live, submitted?
PROJECTS: list[tuple[str, str, str, list[str], bool]] = [
    (
        "Quorum",
        "Consensus scoring for panels who disagree loudly",
        "ai-agents",
        ["python", "fastapi", "postgres", "scikit-learn"],
        True,
    ),
    (
        "Latchkey",
        "Self-hosted invite links that expire like they should",
        "developer-tools",
        ["go", "sqlite", "htmx"],
        True,
    ),
    (
        "Tidewater",
        "Municipal flood data, finally in one shape",
        "open-data",
        ["typescript", "duckdb", "deck.gl"],
        True,
    ),
    (
        "Understudy",
        "A rehearsal environment for on-call engineers",
        "ai-agents",
        ["python", "langgraph", "redis"],
        True,
    ),
    (
        "Marginalia",
        "Code review comments that survive a rebase",
        "developer-tools",
        ["rust", "git", "wasm"],
        True,
    ),
    (
        "Ledgerline",
        "Council spending, diffed month over month",
        "open-data",
        ["python", "pandas", "svelte"],
        True,
    ),
    (
        "Halfpipe",
        "Still writing the description, honestly",
        "developer-tools",
        ["typescript"],
        False,
    ),
]

TEAMS: list[tuple[str, list[str]]] = [
    ("Quorum Collective", ["sam@example.com", "noor@example.com", "wei@example.com"]),
    ("Latchkey Labs", ["leah@example.com", "diego@example.com"]),
    ("Tidewater", ["aisha@example.com", "tomas@example.com", "ruth@example.com"]),
    ("Understudy", ["kenji@example.com", "mira@example.com"]),
    ("Marginalia", ["owen@example.com", "zora@example.com"]),
    ("Ledgerline", ["felix@example.com", "nadia@example.com"]),
    ("Halfpipe", ["cole@example.com"]),
    # A team with no submission: the organizer dashboard should show it, and the
    # gallery should not.
    ("Still Deciding", ["ines@example.com"]),
]

ARCHIVE_PROJECTS: list[tuple[str, str, list[str]]] = [
    ("Coastline", "Shoreline erosion, six satellites, one timeline", ["python", "gdal"]),
    ("Pennywhistle", "Budget tracking for very small charities", ["ruby", "postgres"]),
    ("Signalman", "Rail delay notifications that are not a spreadsheet", ["elixir", "phoenix"]),
]


# --------------------------------------------------------------------------- #
# Seeding
# --------------------------------------------------------------------------- #


def already_seeded(db: Session) -> bool:
    """One marker row decides it. Cheap, and true the moment anything exists."""
    return db.execute(select(User.id).where(User.email == STAFF[0][0])).first() is not None


def _user(email: str, name: str, role: Role, password_hash: str) -> User:
    return User(
        id=_id("user", email),
        email=email,
        display_name=name,
        password_hash=password_hash,
        role=role,
    )


def _live_event(now: datetime, organizer: User) -> Event:
    """Dates are relative to the moment of seeding, so a laptop that runs this
    in a year still lands on an event with an open submission window."""
    return Event(
        id=_id("event", "dogfood"),
        slug="dogfood",
        name="Dogfood",
        tagline="Build the platform that will judge you.",
        description=(
            "One product, four tiers, one weekend. Everyone builds a submission and "
            "judging portal against the same published spec. The winner gets forked, "
            "self-hosted, and run for real events.\n\n"
            "This is seeded fixture data: the event, the teams and the projects below "
            "are invented so that a fresh install has something to look at."
        ),
        starts_at=now - timedelta(days=2),
        ends_at=now + timedelta(days=14),
        registration_opens_at=now - timedelta(days=7),
        submission_opens_at=now - timedelta(days=2),
        submission_deadline=now + timedelta(days=14),
        judging_opens_at=now + timedelta(days=14),
        judging_closes_at=now + timedelta(days=21),
        max_team_size=4,
        is_published=True,
        created_by_id=organizer.id,
    )


def _archive_event(now: datetime, organizer: User) -> Event:
    """Closed, and closed on purpose: this is the fixture that proves the
    deadline is enforced rather than displayed."""
    return Event(
        id=_id("event", "raptors-summer"),
        slug="raptors-summer",
        name="Raptors Summer",
        tagline="Archived. The submission deadline passed thirty days ago.",
        description=(
            "A finished event, kept so that the closed-deadline path is reachable "
            "from a fresh install. Every write to these submissions is refused."
        ),
        starts_at=now - timedelta(days=40),
        ends_at=now - timedelta(days=30),
        registration_opens_at=now - timedelta(days=60),
        submission_opens_at=now - timedelta(days=40),
        submission_deadline=now - timedelta(days=30),
        judging_opens_at=now - timedelta(days=30),
        judging_closes_at=now - timedelta(days=23),
        max_team_size=4,
        is_published=True,
        created_by_id=organizer.id,
    )


def seed(db: Session) -> None:
    now = utcnow()
    # One hash, reused across fixture accounts. They all share a password
    # anyway, and hashing twenty times turns a cold start into a coffee break.
    password_hash = hash_password(FIXTURE_PASSWORD)

    users = {
        email: _user(email, name, role, password_hash) for email, name, role in STAFF
    }
    users.update(
        {
            email: _user(email, name, Role.PARTICIPANT, password_hash)
            for email, name in PARTICIPANTS
        }
    )
    for email, name, level in LEVELED_ADMINS:
        admin = _user(email, name, Role.ADMIN, password_hash)
        admin.admin_level = level
        users[email] = admin
    db.add_all(users.values())
    db.flush()

    organizer = users["organizer@example.com"]

    # -- the live event ----------------------------------------------------- #

    event = _live_event(now, organizer)
    db.add(event)
    db.flush()

    tracks: dict[str, Track] = {}
    for position, (key, name, description) in enumerate(TRACKS):
        track = Track(
            id=_id("track", f"dogfood:{key}"),
            event_id=event.id,
            key=key,
            name=name,
            description=description,
            position=position,
        )
        tracks[key] = track
        db.add(track)
    # Flushed before the prizes below, one of which points at a track through a
    # composite foreign key the unit of work does not order for us.
    db.flush()

    db.add_all(
        [
            Prize(
                id=_id("prize", "grand"),
                event_id=event.id,
                title="Grand Prize",
                description="Forked, self-hosted, and run for real events.",
                value="$800",
                position=0,
            ),
            Prize(
                id=_id("prize", "runner-up"),
                event_id=event.id,
                title="Runner-Up",
                description="Close enough that we will be reading it for ideas.",
                value="$500",
                position=1,
            ),
            Prize(
                id=_id("prize", "judging-engine"),
                event_id=event.id,
                title="Best Judging Engine",
                description="Assignment, normalization, role isolation, audit trail.",
                value="$100",
                position=2,
            ),
            # Track-specific, which is what the composite foreign key on
            # prizes(track_id, event_id) exists to keep honest.
            Prize(
                id=_id("prize", "best-tooling"),
                event_id=event.id,
                title="Best Developer Tool",
                description="Judged within the Developer Tools track only.",
                value="$150",
                track_id=tracks["developer-tools"].id,
                position=3,
            ),
        ]
    )

    questions = [
        EventQuestion(
            id=_id("question", "cut"),
            event_id=event.id,
            prompt="What did you cut, and do you regret it?",
            help_text="Two or three sentences. Honest scope beats a feature list.",
            kind=QuestionKind.TEXTAREA,
            required=True,
            position=0,
        ),
        EventQuestion(
            id=_id("question", "demo"),
            event_id=event.id,
            prompt="Anything a reviewer should run first?",
            kind=QuestionKind.TEXT,
            required=False,
            position=1,
        ),
    ]
    db.add_all(questions)
    db.flush()

    for index, (team_name, member_emails) in enumerate(TEAMS):
        team = Team(
            id=_id("team", f"dogfood:{team_name}"),
            event_id=event.id,
            name=team_name,
            invite_token=f"seed-{_id('invite', team_name).hex}",
            created_by_id=users[member_emails[0]].id,
        )
        for position, email in enumerate(member_emails):
            team.members.append(
                TeamMember(
                    user_id=users[email].id,
                    event_id=event.id,
                    team_role=TeamRole.OWNER if position == 0 else TeamRole.MEMBER,
                )
            )
        db.add(team)

        if index >= len(PROJECTS):
            continue  # "Still Deciding" has a team and no project, deliberately

        name, tagline, track_key, tags, is_submitted = PROJECTS[index]
        submission = Submission(
            id=_id("submission", f"dogfood:{name}"),
            event_id=event.id,
            team_id=team.id,
            track_id=tracks[track_key].id,
            name=name,
            tagline=tagline,
            description=(
                f"{tagline}.\n\n"
                f"{name} was built over one weekend by {len(member_emails)} "
                "people who had all hit the same problem and got tired of "
                "working around it. The repository is the argument; this "
                "paragraph is just the summary."
            ),
            thumbnail_url=THUMBNAIL if index < 2 else None,
            demo_video_url=None,
            repo_url=f"https://example.invalid/{name.lower()}",
            live_url=f"https://example.invalid/{name.lower()}/demo" if is_submitted else None,
            tech_tags=tags,
            status=SubmissionStatus.SUBMITTED if is_submitted else SubmissionStatus.DRAFT,
            submitted_at=now - timedelta(hours=6 * index + 1) if is_submitted else None,
        )
        if is_submitted:
            submission.answers.append(
                SubmissionAnswer(
                    question_id=questions[0].id,
                    value=(
                        "Cut the plugin system. It was two days of work to support "
                        "one hypothetical user, and the thing we shipped instead is "
                        "the thing we would have used anyway."
                    ),
                )
            )
        db.add(submission)

    # -- the archived event ------------------------------------------------- #

    archive = _archive_event(now, organizer)
    db.add(archive)
    db.flush()

    archive_track = Track(
        id=_id("track", "raptors-summer:general"),
        event_id=archive.id,
        key="general",
        name="General",
        position=0,
    )
    db.add(archive_track)
    db.flush()

    for index, (name, tagline, tags) in enumerate(ARCHIVE_PROJECTS):
        email = PARTICIPANTS[index][0]
        team = Team(
            id=_id("team", f"raptors-summer:{name}"),
            event_id=archive.id,
            name=f"Team {name}",
            invite_token=f"seed-{_id('invite', f'archive:{name}').hex}",
            created_by_id=users[email].id,
        )
        team.members.append(
            TeamMember(user_id=users[email].id, event_id=archive.id, team_role=TeamRole.OWNER)
        )
        db.add(team)
        db.add(
            Submission(
                id=_id("submission", f"raptors-summer:{name}"),
                event_id=archive.id,
                team_id=team.id,
                track_id=archive_track.id,
                name=name,
                tagline=tagline,
                description=f"{tagline}. Archived entry from a closed event.",
                repo_url=f"https://example.invalid/{name.lower()}",
                tech_tags=tags,
                status=SubmissionStatus.SUBMITTED,
                submitted_at=now - timedelta(days=30, hours=index + 1),
            )
        )

    # -- a second live event, deliberately empty ---------------------------- #

    demo_event = _demo_event(now, organizer)
    db.add(demo_event)
    db.flush()

    db.add(
        Track(
            id=_id("track", "nightowl:general"),
            event_id=demo_event.id,
            key="general",
            name="General",
            position=0,
        )
    )
    db.flush()

    # -- the mid-judging event --------------------------------------------- #

    judged = _judged_event(now, organizer)
    db.add(judged)
    db.flush()
    _seed_judging(db, judged, users, now)

    # The live event gets the same rubric, published early. Participants are
    # entitled to know what they will be judged against before they submit.
    _seed_rubric(db, event)

    # -- public voting and comments ---------------------------------------- #

    _seed_public(db, judged, users, now)
    _seed_archive_voting(db, archive, users, now)

    # -- integrations: a webhook and signed records ------------------------ #

    _seed_integrations(db, judged, users, now)

    ballots = db.query(JudgeAssignment).count()
    scored = db.query(JudgeAssignment).filter(
        JudgeAssignment.status == AssignmentStatus.COMPLETE
    ).count()
    print(
        f"[seed] {len(users)} users, 3 events, "
        f"{len(TEAMS) + len(ARCHIVE_PROJECTS) + len(JUDGED_PROJECTS)} teams, "
        f"{len(PROJECTS) + len(ARCHIVE_PROJECTS) + len(JUDGED_PROJECTS)} submissions"
    )
    print(
        f"[seed] judging: {len(JUDGE_STYLES)} judges, {ballots} ballots, "
        f"{scored} complete -- see /api/events/raptors-winter/results"
    )
    print(
        f"[seed] public: {db.query(Voter).count()} voters, {db.query(Vote).count()} votes, "
        f"{db.query(Comment).count()} comments, {db.query(AuditEntry).count()} audit entries"
    )
    print(
        "[seed] voting is OPEN and results are HIDDEN on raptors-winter; "
        "the archived event has published totals"
    )
    print(
        f"[seed] integrations: {db.query(Webhook).count()} webhook, "
        f"{db.query(Certificate).count()} signed records -- verify one at /verify"
    )
    print(f"[seed] every fixture account signs in with password: {FIXTURE_PASSWORD}")


# --------------------------------------------------------------------------- #
# Judging fixtures (Phase 2)
# --------------------------------------------------------------------------- #

# The rubric every seeded event shares. Weighted unevenly on purpose: an equal
# rubric would not demonstrate that weights are applied at all, and the brief is
# specific that the market leader cannot weight criteria.
RUBRIC: list[tuple[str, str, str, float]] = [
    ("impact", "Impact", "Does solving this matter, and to whom?", 3.0),
    ("execution", "Execution", "Does it work, and how much of it is real?", 3.0),
    ("craft", "Craft", "Would a senior reviewer call this idiomatic?", 2.0),
    ("presentation", "Presentation", "Can a stranger understand it in two minutes?", 1.0),
]

# Projects for the mid-judging event.
JUDGED_PROJECTS: list[tuple[str, str, str, list[str]]] = [
    (
        "Switchyard", "Freight routing that admits when it is guessing", "logistics",
        ["rust", "postgres"],
    ),
    ("Pocketful", "Microgrants without the grant-writing", "civic", ["python", "htmx"]),
    ("Quietline", "Noise complaints that reach the right desk", "civic", ["go", "sqlite"]),
    ("Tidemark", "Flood risk for streets, not regions", "logistics", ["python", "deck.gl"]),
    ("Foundry", "Apprenticeship matching for trades", "civic", ["typescript", "redis"]),
    ("Crosshatch", "Map diffing for survey crews", "logistics", ["rust", "wasm"]),
]

# How each judge uses the scale. The three archetypes JUDGING.md argues about, so
# the normalization has its own pathology to demonstrate on a fresh install:
#
#   harsh   -- uses the bottom of the range, discriminates well
#   generous-- uses the top of the range, discriminates well
#   flat    -- marks everything a 3, which is the case the method must survive
#
# Values are per-project offsets applied to a base, not absolute scores, so the
# orderings are deliberately *similar but not identical* -- real judges disagree
# at the margins, and a fixture where they agree perfectly would prove nothing.
JUDGE_STYLES: dict[str, dict[str, object]] = {
    "judge.rivera@example.com": {
        "label": "generous",
        "track": None,
        "base": 4,
        "offsets": [1, 0, -1, 1, 0, -1],
    },
    "judge.osei@example.com": {
        "label": "harsh",
        "track": None,
        "base": 2,
        "offsets": [1, 1, 0, 0, -1, 0],
    },
    "judge.tanaka@example.com": {
        "label": "flat",
        "track": None,
        "base": 3,
        "offsets": [0, 0, 0, 0, 0, 0],
    },
    "judge.novak@example.com": {
        "label": "track judge, civic only",
        "track": "civic",
        "base": 4,
        "offsets": [0, 1, -1, 0, 1, 0],
    },
    # Deliberately assigned and deliberately idle, so `not_started` on the
    # progress dashboard is non-empty on a fresh install.
    "judge.whitfield@example.com": {
        "label": "has not started",
        "track": None,
        "base": None,
        "offsets": [],
    },
}

JUDGED_TRACKS: list[tuple[str, str]] = [
    ("civic", "Civic"),
    ("logistics", "Logistics"),
]


def _demo_event(now: datetime, organizer: User) -> Event:
    """A second live event, deliberately empty: no teams, no submissions.

    Exists so a demo recording can register, form a team, and submit a
    project on a real event without needing to also create a track live --
    registration and the submission window are already open on a fresh
    install."""
    return Event(
        id=_id("event", "nightowl"),
        slug="nightowl",
        name="Nightowl",
        tagline="Late-night build, one track, wide open.",
        description=(
            "A second live event, kept deliberately empty on a fresh install: "
            "registration and the submission window are already open, so "
            "signing up and entering a project here work immediately."
        ),
        starts_at=now - timedelta(days=1),
        ends_at=now + timedelta(days=20),
        registration_opens_at=now - timedelta(days=3),
        submission_opens_at=now - timedelta(days=1),
        submission_deadline=now + timedelta(days=20),
        judging_opens_at=now + timedelta(days=20),
        judging_closes_at=now + timedelta(days=27),
        max_team_size=4,
        is_published=True,
        created_by_id=organizer.id,
    )


def _judged_event(now: datetime, organizer: User) -> Event:
    """Submissions closed, judging open right now.

    The third seeded event exists because the other two cannot demonstrate T2: the
    live event's judging window has not opened, and the archived one's has closed.
    An organizer landing on a fresh install should find a judging round in progress.
    """
    return Event(
        id=_id("event", "raptors-winter"),
        slug="raptors-winter",
        name="Raptors Winter",
        tagline="Submissions closed. Judging is open and half done.",
        description=(
            "A seeded event mid-judging, so the judge console, the progress "
            "dashboard and the normalization have real data on a fresh install.\n\n"
            "Three of its judges score differently on purpose: one generous, one "
            "harsh, and one who marks everything a 3. That last one is the case "
            "JUDGING.md argues about, and you can see what the method does with it "
            "on the results page."
        ),
        starts_at=now - timedelta(days=12),
        ends_at=now - timedelta(days=3),
        registration_opens_at=now - timedelta(days=20),
        submission_opens_at=now - timedelta(days=12),
        submission_deadline=now - timedelta(days=3),
        judging_opens_at=now - timedelta(days=1),
        judging_closes_at=now + timedelta(days=5),
        max_team_size=4,
        is_published=True,
        created_by_id=organizer.id,
    )


def _seed_rubric(db: Session, event: Event) -> dict[str, RubricCriterion]:
    criteria: dict[str, RubricCriterion] = {}
    for position, (key, name, description, weight) in enumerate(RUBRIC):
        criterion = RubricCriterion(
            id=_id("criterion", f"{event.slug}:{key}"),
            event_id=event.id,
            key=key,
            name=name,
            description=description,
            weight=Decimal(str(weight)),
            min_score=1,
            max_score=5,
            position=position,
        )
        criteria[key] = criterion
        db.add(criterion)
    db.flush()
    return criteria


def _seed_judging(db: Session, event: Event, users: dict[str, User], now: datetime) -> None:
    """Build a half-finished judging round on `event`.

    Deliberately *not* built by calling the assignment algorithm: a fixture that
    depends on the algorithm would change shape whenever the algorithm did, and
    the numbers in JUDGING.md would stop matching. Every judge here reviews every
    project they are eligible for, which also makes the normalization example
    legible -- the calibration differences are the only thing moving the ranking.
    """
    tracks: dict[str, Track] = {}
    for position, (key, name) in enumerate(JUDGED_TRACKS):
        track = Track(
            id=_id("track", f"{event.slug}:{key}"),
            event_id=event.id,
            key=key,
            name=name,
            position=position,
        )
        tracks[key] = track
        db.add(track)
    db.flush()

    criteria = _seed_rubric(db, event)

    # -- teams and submissions --------------------------------------------- #
    submissions: list[Submission] = []
    for index, (name, tagline, track_key, tags) in enumerate(JUDGED_PROJECTS):
        # Offset into PARTICIPANTS so these teams do not collide with the live
        # event's, where a user may only be on one team per event anyway.
        owner = users[PARTICIPANTS[(index + 8) % len(PARTICIPANTS)][0]]
        team = Team(
            id=_id("team", f"{event.slug}:{name}"),
            event_id=event.id,
            name=f"Team {name}",
            invite_token=f"seed-{_id('invite', f'{event.slug}:{name}').hex}",
            created_by_id=owner.id,
        )
        team.members.append(
            TeamMember(user_id=owner.id, event_id=event.id, team_role=TeamRole.OWNER)
        )
        db.add(team)
        db.flush()

        submission = Submission(
            id=_id("submission", f"{event.slug}:{name}"),
            event_id=event.id,
            team_id=team.id,
            track_id=tracks[track_key].id,
            name=name,
            tagline=tagline,
            description=(
                f"{tagline}. Seeded entry on a closed event so the judging "
                "surface has something to score."
            ),
            repo_url=f"https://example.invalid/{name.lower()}",
            live_url=f"https://example.invalid/{name.lower()}/demo",
            tech_tags=tags,
            status=SubmissionStatus.SUBMITTED,
            submitted_at=now - timedelta(days=3, hours=index + 1),
        )
        db.add(submission)
        submissions.append(submission)
    db.flush()

    # -- judges, ballots and scores ---------------------------------------- #
    judge_rows: dict[str, Judge] = {}
    for email, style in JUDGE_STYLES.items():
        user_row = users[email]
        track_key = style["track"]
        judge = Judge(
            id=_id("judge", f"{event.slug}:{email}"),
            event_id=event.id,
            user_id=user_row.id,
            track_id=tracks[track_key].id if track_key else None,
            is_active=True,
            accepted_at=now - timedelta(days=2),
        )
        db.add(judge)
        db.flush()
        judge_rows[email] = judge

        base = style["base"]
        offsets = style["offsets"]

        for index, submission in enumerate(submissions):
            # Track judges only see their own track -- the same rule the
            # assignment algorithm enforces, applied here by hand.
            if track_key and submission.track_id != tracks[track_key].id:
                continue

            scored = base is not None
            assignment = JudgeAssignment(
                id=_id("assignment", f"{event.slug}:{email}:{submission.name}"),
                event_id=event.id,
                judge_id=judge.id,
                submission_id=submission.id,
                status=AssignmentStatus.COMPLETE if scored else AssignmentStatus.PENDING,
                comment=(
                    f"Seeded feedback from a {style['label']} judge."
                    if scored
                    else None
                ),
                assigned_at=now - timedelta(days=1, hours=2),
                completed_at=now - timedelta(hours=index + 1) if scored else None,
            )
            db.add(assignment)
            if not scored:
                continue

            offset = offsets[index % len(offsets)]
            for criterion_index, (key, criterion) in enumerate(criteria.items()):
                # Vary a little across criteria too, so a ballot is not four
                # identical numbers and the weighting has something to bite on.
                value = base + offset + (1 if criterion_index == 0 else 0)
                if style["label"] == "flat":
                    value = base  # the whole point of this judge
                db.add(
                    Score(
                        id=_id("score", f"{event.slug}:{email}:{submission.name}:{key}"),
                        event_id=event.id,
                        assignment_id=assignment.id,
                        criterion_id=criterion.id,
                        value=max(criterion.min_score, min(criterion.max_score, value)),
                    )
                )
    db.flush()
    _seed_calibration(db, event, criteria, judge_rows)


# What an organizer expects of the practice project, one per criterion in
# `RUBRIC` order, and how far each seeded judge lands from it. Two judges are
# scored off on purpose (one over, one under) and one is spot on, so the
# calibration report shows all three verdicts on a fresh install. The other
# judges are left unscored, which the report shows as "not started".
CALIBRATION_EXPECTED = [4, 3, 3, 3]
CALIBRATION_JUDGE_OFFSETS = {
    "judge.rivera@example.com": 1,  # generous
    "judge.osei@example.com": -1,  # harsh
    "judge.novak@example.com": 0,  # aligned
}


def _seed_calibration(
    db: Session,
    event: Event,
    criteria: dict[str, RubricCriterion],
    judges: dict[str, Judge],
) -> None:
    project = CalibrationProject(
        id=_id("calibration", f"{event.slug}:ferrylink"),
        event_id=event.id,
        name="Practice: Ferrylink",
        description=(
            "A ferry-timetable app that merges three operators' feeds. Solid data "
            "work, a plain interface, and no offline mode. Score it as you would a "
            "real entry."
        ),
    )
    db.add(project)
    db.flush()
    ordered = list(criteria.values())
    for criterion, expected in zip(ordered, CALIBRATION_EXPECTED, strict=True):
        db.add(
            CalibrationExpected(
                project_id=project.id,
                criterion_id=criterion.id,
                event_id=event.id,
                value=Decimal(expected),
            )
        )
    db.flush()
    for email, offset in CALIBRATION_JUDGE_OFFSETS.items():
        for criterion, expected in zip(ordered, CALIBRATION_EXPECTED, strict=True):
            db.add(
                CalibrationScore(
                    id=_id("calibration-score", f"{event.slug}:{email}:{criterion.key}"),
                    event_id=event.id,
                    project_id=project.id,
                    judge_id=judges[email].id,
                    criterion_id=criterion.id,
                    value=Decimal(
                        max(criterion.min_score, min(criterion.max_score, expected + offset))
                    ),
                )
            )
    db.flush()


# --------------------------------------------------------------------------- #
# Public voting, comments and audit fixtures (Phase 3)
# --------------------------------------------------------------------------- #

# Seeded comments. One is hidden on purpose so the moderation path, the
# organizer-only view of hidden text, and the audit entry behind it are all
# reachable on a fresh install.
SEED_COMMENTS: list[tuple[str, str, str, str | None]] = [
    (
        "Switchyard", "wei@example.com",
        "The routing explanation in the README is the clearest thing I have read all weekend.",
        None,
    ),
    (
        "Switchyard", "ruth@example.com",
        "Does it handle partial outages, or is that the next thing?", None,
    ),
    (
        "Pocketful", "noor@example.com",
        "Having run a tiny charity: this is the form I actually wanted.", None,
    ),
    ("Quietline", "diego@example.com", "BUY CHEAP FOLLOWERS AT example.invalid", "off-topic spam"),
    (
        "Tidemark", "tomas@example.com",
        "Street-level resolution is the whole point. Nicely judged.", None,
    ),
]

# Seeded ballots, as (voter email, [(project, votes)]). Quadratic, 100 credits.
# Deliberately varied: one voter spends everything on a single shout, most spread,
# and the totals are close enough that the method visibly matters.
SEED_BALLOTS: list[tuple[str, list[tuple[str, int]]]] = [
    ("wei@example.com", [("Switchyard", 7), ("Pocketful", 5), ("Tidemark", 4)]),
    ("noor@example.com", [("Pocketful", 8), ("Foundry", 5)]),
    ("ruth@example.com", [("Switchyard", 6), ("Crosshatch", 6), ("Quietline", 4)]),
    ("tomas@example.com", [("Tidemark", 9)]),
    ("diego@example.com", [("Switchyard", 5), ("Pocketful", 4), ("Foundry", 5), ("Tidemark", 5)]),
    ("mira@example.com", [("Crosshatch", 7), ("Quietline", 5), ("Foundry", 3)]),
    ("zora@example.com", [("Pocketful", 6), ("Switchyard", 6), ("Quietline", 4)]),
    ("kenji@example.com", [("Foundry", 10)]),
]


def _seed_public(db: Session, event: Event, users: dict[str, User], now: datetime) -> None:
    """Open community voting on the mid-judging event, with ballots and comments.

    Voting runs alongside judging here, which is what actually happens: the public
    gallery opens when submissions close and the judges work in parallel.

    Results stay **private** -- `results_public_at` is left NULL -- because that is
    the state the brief requires during the voting window, and a fresh install
    should demonstrate the closed state rather than the open one. Publishing is one
    organizer click away on the voting page.
    """
    event.voting_opens_at = now - timedelta(days=1)
    event.voting_closes_at = now + timedelta(days=4)
    event.voting_access = VotingAccess.AUTHENTICATED
    event.voting_method = VotingMethod.QUADRATIC
    event.vote_credits = 100
    event.comments_enabled = True
    event.pairwise_enabled = True
    event.results_public_at = None
    db.flush()

    submissions = {
        s.name: s
        for s in db.execute(
            select(Submission).where(Submission.event_id == event.id)
        ).scalars()
    }

    # -- ballots ----------------------------------------------------------- #
    for email, picks in SEED_BALLOTS:
        user = users[email]
        voter = Voter(
            id=_id("voter", f"{event.slug}:{email}"),
            event_id=event.id,
            access=VotingAccess.AUTHENTICATED,
            user_id=user.id,
            ordering_seed=int(_id("seed", f"{event.slug}:{email}").int % (2**31)),
            created_at=now - timedelta(hours=20),
            last_seen_at=now - timedelta(hours=2),
        )
        db.add(voter)
        db.flush()

        spent = 0
        for name, credits in picks:
            submission = submissions.get(name)
            if submission is None:
                continue
            db.add(
                Vote(
                    id=_id("vote", f"{event.slug}:{email}:{name}"),
                    event_id=event.id,
                    voter_id=voter.id,
                    submission_id=submission.id,
                    credits=credits,
                )
            )
            spent += credits * credits

        record(
            db,
            action=AuditAction.VOTE_CAST,
            summary=(
                f"{user.email} voted "
                + ", ".join(f"{c} on \"{n}\"" for n, c in picks)
                + f" ({spent} of 100 credits) on {event.slug}"
            ),
            principal=user,
            event=event,
            resource_type="voter",
            resource_id=voter.id,
            created_at=now - timedelta(hours=19),
        )

    # -- comments ---------------------------------------------------------- #
    organizer = users["organizer@example.com"]
    for index, (project, email, body, hide_reason) in enumerate(SEED_COMMENTS):
        submission = submissions.get(project)
        if submission is None:
            continue
        author = users[email]
        comment = Comment(
            id=_id("comment", f"{event.slug}:{project}:{email}"),
            event_id=event.id,
            submission_id=submission.id,
            author_id=author.id,
            body=body,
            is_hidden=hide_reason is not None,
            hidden_by_id=organizer.id if hide_reason else None,
            hidden_reason=hide_reason,
            created_at=now - timedelta(hours=18 - index),
        )
        db.add(comment)
        record(
            db,
            action=AuditAction.COMMENT_POSTED,
            summary=f'{author.email} commented on "{project}" in {event.slug}',
            principal=author,
            event=event,
            resource_type="submission",
            resource_id=submission.id,
            created_at=now - timedelta(hours=18 - index),
        )
        if hide_reason:
            record(
                db,
                action=AuditAction.COMMENT_HIDDEN,
                summary=(
                    f'{organizer.email} hid a comment by {author.email} on '
                    f'"{project}": {hide_reason}'
                ),
                principal=organizer,
                event=event,
                resource_type="comment",
                resource_id=comment.id,
                created_at=now - timedelta(hours=17 - index),
            )
    db.flush()


def _seed_archive_voting(db: Session, event: Event, users: dict[str, User], now: datetime) -> None:
    """The archived event, with voting finished and the tally published.

    The counterpart to the live event above: one install, both states, so the
    difference between "hidden during the window" and "published afterwards" is
    visible without editing a date by hand.
    """
    event.voting_opens_at = now - timedelta(days=29)
    event.voting_closes_at = now - timedelta(days=24)
    event.voting_access = VotingAccess.EMAIL_GATED
    event.voting_method = VotingMethod.SINGLE
    event.votes_per_voter = 3
    event.results_public_at = now - timedelta(days=23)
    event.comments_enabled = True
    db.flush()

    submissions = list(
        db.execute(select(Submission).where(Submission.event_id == event.id)).scalars()
    )
    if not submissions:
        return

    # Email-gated, so these voters are addresses rather than accounts -- including
    # two that never had one, which is the realistic shape.
    addresses = [
        "alumna@example.com",
        "mentor@example.com",
        "visitor1@example.com",
        "visitor2@example.com",
        "visitor3@example.com",
    ]
    for index, address in enumerate(addresses):
        voter = Voter(
            id=_id("voter", f"{event.slug}:{address}"),
            event_id=event.id,
            access=VotingAccess.EMAIL_GATED,
            email=address,
            token_hash=f"seed-{_id('votertoken', f'{event.slug}:{address}').hex}"[:64],
            ordering_seed=index * 7919,
            created_at=now - timedelta(days=26),
            last_seen_at=now - timedelta(days=26),
        )
        db.add(voter)
        db.flush()
        # One vote each on up to two projects, rotated so the tally is not a tie.
        for offset in range(2):
            submission = submissions[(index + offset) % len(submissions)]
            db.add(
                Vote(
                    id=_id("vote", f"{event.slug}:{address}:{submission.name}"),
                    event_id=event.id,
                    voter_id=voter.id,
                    submission_id=submission.id,
                    credits=1,
                )
            )
    record(
        db,
        action=AuditAction.RESULTS_PUBLISHED,
        summary=f"Community vote totals for {event.slug} were made public",
        principal=users["organizer@example.com"],
        event=event,
        resource_type="event",
        resource_id=event.id,
        created_at=now - timedelta(days=23),
    )
    db.flush()


# --------------------------------------------------------------------------- #
# Webhooks and signed records (Phase 4)
# --------------------------------------------------------------------------- #


def _seed_integrations(db: Session, event: Event, users: dict[str, User], now: datetime) -> None:
    """One registered webhook and a full set of signed records.

    The webhook points at a `.invalid` host on purpose: it is a real registration an
    organizer can inspect, test and delete, and a test delivery will visibly fail
    rather than silently pretending to work. Seeding a hook that appears to deliver
    would be a fixture that lies.

    Records are issued with the same signing path the API uses, so every seeded code
    verifies at /verify on a fresh install.
    """
    db.add(
        Webhook(
            id=_id("webhook", f"{event.slug}:relay"),
            event_id=event.id,
            url="https://hooks.example.invalid/dogfood-relay",
            secret=f"seed-{_id('whsecret', event.slug).hex[:24]}",
            topics=[
                WebhookEvent.SUBMISSION_SUBMITTED.value,
                WebhookEvent.BALLOT_COMPLETED.value,
                WebhookEvent.VOTE_CAST.value,
            ],
            description="Example relay (unreachable host, so a test delivery fails)",
            created_by_id=users["organizer@example.com"].id,
            created_at=now - timedelta(days=2),
        )
    )
    db.flush()

    organizer = users["organizer@example.com"]

    def issue(kind, title, subject_name, payload, *, user_id=None, submission_id=None):
        key = f"{event.slug}:{kind.value}:{user_id or submission_id}"
        code = "".join(
            CODE_ALPHABET[b % len(CODE_ALPHABET)]
            for b in _id("certcode", key).bytes[:CODE_LENGTH]
        )
        body, signature, key_id = sign_payload({**payload, "code": code})
        db.add(
            Certificate(
                id=_id("cert", key),
                event_id=event.id,
                kind=kind,
                subject_user_id=user_id,
                submission_id=submission_id,
                subject_name=subject_name,
                title=title,
                code=code,
                payload=body.decode(),
                signature=signature,
                key_id=key_id,
                issued_at=now - timedelta(hours=6),
                issued_by_id=organizer.id,
            )
        )

    submissions = list(
        db.execute(select(Submission).where(Submission.event_id == event.id)).scalars()
    )

    # Participation, for the owner of every entered project.
    for submission in submissions:
        team = db.get(Team, submission.team_id)
        if team is None:
            continue
        for member in team.members:
            member_user = db.get(User, member.user_id)
            if member_user is None:
                continue
            issue(
                CertificateKind.PARTICIPATION,
                f"Participation — {event.name}",
                member_user.display_name,
                {
                    "kind": CertificateKind.PARTICIPATION.value,
                    "event": event.slug,
                    "event_name": event.name,
                    "subject": member_user.display_name,
                    "team": team.name,
                    # Same fields `issue()` in routers/certificates.py signs; without
                    # the project a seeded record renders with no Project row.
                    "project": submission.name,
                    "issued_at": (now - timedelta(hours=6)).isoformat(),
                },
                user_id=member_user.id,
            )

    # Judging, with the review count that makes the record worth having.
    counts = db.execute(
        select(Judge.user_id, func.count(JudgeAssignment.id))
        .join(JudgeAssignment, JudgeAssignment.judge_id == Judge.id)
        .where(
            Judge.event_id == event.id,
            JudgeAssignment.status == AssignmentStatus.COMPLETE,
        )
        .group_by(Judge.user_id)
    ).all()
    for user_id, reviews in counts:
        judge_user = db.get(User, user_id)
        if judge_user is None:
            continue
        issue(
            CertificateKind.JUDGING,
            f"Judging — {event.name}",
            judge_user.display_name,
            {
                "kind": CertificateKind.JUDGING.value,
                "event": event.slug,
                "event_name": event.name,
                "subject": judge_user.display_name,
                "reviews_completed": int(reviews),
                "issued_at": (now - timedelta(hours=6)).isoformat(),
            },
            user_id=judge_user.id,
        )

    db.flush()


def main() -> int:
    db = SessionLocal()
    try:
        if already_seeded(db):
            print("[seed] fixtures already present, skipping")
            return 0
        seed(db)
        db.commit()
        print("[seed] done")
        return 0
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
