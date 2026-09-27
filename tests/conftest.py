"""Fixtures for the tests that need a real database.

These run against Postgres rather than SQLite on purpose. Half of what this
schema asserts -- composite foreign keys, `ARRAY` containment, partial-ish check
constraints, native enums -- does not exist on SQLite, so a green SQLite suite
would be testing a different schema from the one that ships.

If Postgres is not reachable, the database-backed tests skip with a reason
rather than failing: `tests/test_access_control.py` and
`tests/test_phase0_smoke.py` still run anywhere.

    docker compose up -d db
    cd backend && python -m pytest ../tests -q

Override the target with `DOGFOOD_TEST_DATABASE_URL`. The default points at the
database the compose file publishes on localhost, and uses a *separate*
database name so running the suite never touches seeded development data.
"""

from __future__ import annotations

import os
import sys
import uuid
from collections.abc import Iterator
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

BACKEND = Path(__file__).resolve().parents[1] / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from fastapi.testclient import TestClient  # noqa: E402

from app.db import Base, get_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import (  # noqa: E402
    AssignmentStatus,
    Comment,
    Event,
    EventRegistration,
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
    Voter,
    VotingAccess,
    VotingMethod,
    utcnow,
)
from app.security import hash_password  # noqa: E402

TEST_DATABASE_URL = os.environ.get(
    "DOGFOOD_TEST_DATABASE_URL",
    "postgresql+psycopg://dogfood:dogfood@localhost:5432/dogfood_test",
)

PASSWORD = "correct-horse-battery"


def _ensure_database() -> Engine:
    """Create the test database if it is missing; skip the suite if Postgres is
    not there at all."""
    url = make_url(TEST_DATABASE_URL)
    admin = create_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as conn:
            exists = conn.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :name"),
                {"name": url.database},
            ).first()
            if not exists:
                conn.execute(text(f'CREATE DATABASE "{url.database}"'))
    except OperationalError as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"Postgres not reachable at {url.host}:{url.port} ({exc.__class__.__name__})")
    finally:
        admin.dispose()
    return create_engine(TEST_DATABASE_URL, future=True)


@pytest.fixture(scope="session")
def engine() -> Iterator[Engine]:
    eng = _ensure_database()
    Base.metadata.drop_all(bind=eng)
    Base.metadata.create_all(bind=eng)
    yield eng
    Base.metadata.drop_all(bind=eng)
    eng.dispose()


@pytest.fixture()
def db(engine: Engine) -> Iterator[Session]:
    """A session per test, with every table emptied afterwards.

    Truncate rather than a rolled-back outer transaction, because the routes
    under test commit, and a test that cannot see its own committed writes is
    testing something other than the application.
    """
    factory = sessionmaker(bind=engine, autoflush=False, future=True)
    session = factory()
    try:
        yield session
    finally:
        session.rollback()
        tables = ", ".join(f'"{name}"' for name in Base.metadata.tables)
        session.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
        session.commit()
        session.close()


@pytest.fixture()
def client(db: Session) -> Iterator[TestClient]:
    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


# --------------------------------------------------------------------------- #
# Factories
# --------------------------------------------------------------------------- #


@pytest.fixture()
def make_user(db: Session):
    def _make(role: Role = Role.PARTICIPANT, *, email: str | None = None, name: str = "Fixture"):
        user = User(
            email=email or f"{uuid.uuid4().hex[:10]}@example.com",
            display_name=name,
            password_hash=hash_password(PASSWORD),
            role=role,
            is_active=True,
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        return user

    return _make


@pytest.fixture()
def make_event(db: Session):
    def _make(
        *,
        slug: str | None = None,
        published: bool = True,
        deadline: datetime | None = None,
        max_team_size: int = 4,
        registration_opens_at: datetime | None = None,
        starts_at: datetime | None = None,
    ) -> Event:
        now = utcnow()
        event = Event(
            slug=slug or f"event-{uuid.uuid4().hex[:8]}",
            name="Test Event",
            starts_at=starts_at or (now - timedelta(days=1)),
            ends_at=now + timedelta(days=7),
            registration_opens_at=registration_opens_at or (now - timedelta(days=2)),
            submission_opens_at=now - timedelta(days=1),
            submission_deadline=deadline or (now + timedelta(days=7)),
            max_team_size=max_team_size,
            is_published=published,
        )
        db.add(event)
        db.commit()
        db.refresh(event)
        return event

    return _make


@pytest.fixture()
def make_track(db: Session):
    def _make(event: Event, key: str = "general", name: str = "General") -> Track:
        track = Track(event_id=event.id, key=key, name=name)
        db.add(track)
        db.commit()
        db.refresh(track)
        return track

    return _make


@pytest.fixture()
def make_team(db: Session):
    def _make(event: Event, owner: User, *members: User, name: str | None = None) -> Team:
        team = Team(
            event_id=event.id,
            name=name or f"Team {uuid.uuid4().hex[:6]}",
            created_by_id=owner.id,
        )
        team.members.append(
            TeamMember(user_id=owner.id, event_id=event.id, team_role=TeamRole.OWNER)
        )
        for member in members:
            team.members.append(
                TeamMember(user_id=member.id, event_id=event.id, team_role=TeamRole.MEMBER)
            )
        db.add(team)
        db.commit()
        db.refresh(team)
        return team

    return _make


@pytest.fixture()
def make_submission(db: Session):
    def _make(
        team: Team,
        *,
        name: str = "Fixture Project",
        status: SubmissionStatus = SubmissionStatus.DRAFT,
        tags: list[str] | None = None,
        track: Track | None = None,
        tagline: str = "A tagline",
        description: str = "A description",
        repo_url: str = "https://github.com/example/fixture-repo",
        discord_usernames: list[str] | None = None,
    ) -> Submission:
        submission = Submission(
            event_id=team.event_id,
            team_id=team.id,
            track_id=track.id if track else None,
            name=name,
            tagline=tagline,
            description=description,
            repo_url=repo_url,
            tech_tags=tags or [],
            # Non-empty by default so a fixture-built submission can go
            # through the real `/submit` route unmodified, the same reason
            # `tagline`/`description`/`repo_url` all default to real values
            # above instead of `None`.
            discord_usernames=discord_usernames if discord_usernames is not None else ["fixture-user"],
            status=status,
            submitted_at=utcnow() if status is SubmissionStatus.SUBMITTED else None,
        )
        db.add(submission)
        db.commit()
        db.refresh(submission)
        return submission

    return _make


@pytest.fixture()
def auth(client: TestClient):
    """Log a fixture user in and return the header an API client would send.

    Bearer rather than the cookie, because that is how the acceptance suite and
    anybody with curl will reach this API.
    """

    def _auth(user: User) -> dict[str, str]:
        response = client.post(
            "/api/auth/login", json={"email": user.email, "password": PASSWORD}
        )
        assert response.status_code == 200, response.text
        # Logging in sets an HttpOnly session cookie, and `TestClient` keeps a
        # cookie jar -- so without this, a later request made with no headers would
        # still carry the last-logged-in user's session and a test asserting what a
        # *visitor* sees would quietly be asserting what an organizer sees.
        # Bearer is the credential under test; the cookie is cleared so that
        # "no headers" means anonymous.
        client.cookies.clear()
        return {"Authorization": f"Bearer {response.json()['token']}"}

    return _auth


# --------------------------------------------------------------------------- #
# Judging factories (Phase 2)
# --------------------------------------------------------------------------- #


@pytest.fixture()
def make_judge(db: Session):
    """A judge record. `track` is the whole of track isolation: None means the
    judge sees every track, a Track means they see only that one."""

    def _make(event: Event, user: User, *, track: Track | None = None, active: bool = True) -> Judge:
        judge = Judge(
            event_id=event.id,
            user_id=user.id,
            track_id=track.id if track else None,
            is_active=active,
            accepted_at=utcnow(),
        )
        db.add(judge)
        db.commit()
        db.refresh(judge)
        return judge

    return _make


@pytest.fixture()
def make_criterion(db: Session):
    def _make(
        event: Event,
        *,
        key: str | None = None,
        name: str = "Criterion",
        weight: float = 1.0,
        min_score: int = 1,
        max_score: int = 5,
        position: int = 0,
    ) -> RubricCriterion:
        criterion = RubricCriterion(
            event_id=event.id,
            key=key or f"c{uuid.uuid4().hex[:6]}",
            name=name,
            weight=Decimal(str(weight)),
            min_score=min_score,
            max_score=max_score,
            position=position,
        )
        db.add(criterion)
        db.commit()
        db.refresh(criterion)
        return criterion

    return _make


@pytest.fixture()
def make_registration(db: Session):
    """Registers a user for an event, with a controllable `registered_at` --
    the one field the real `/register` route always sets to `utcnow()`, and
    the one this fixture exists to override, for the community-vote round's
    registration-cutoff tests."""

    def _make(
        event: Event,
        user: User,
        *,
        registered_at: datetime | None = None,
        discord_username: str = "fixture-registrant",
    ) -> EventRegistration:
        registration = EventRegistration(
            event_id=event.id,
            user_id=user.id,
            email=user.email,
            discord_username=discord_username,
            registered_at=registered_at or utcnow(),
        )
        db.add(registration)
        db.commit()
        db.refresh(registration)
        return registration

    return _make


@pytest.fixture()
def make_assignment(db: Session):
    def _make(
        judge: Judge,
        submission: Submission,
        *,
        status: AssignmentStatus = AssignmentStatus.PENDING,
    ) -> JudgeAssignment:
        assignment = JudgeAssignment(
            event_id=judge.event_id,
            judge_id=judge.id,
            submission_id=submission.id,
            status=status,
            completed_at=utcnow() if status is AssignmentStatus.COMPLETE else None,
        )
        db.add(assignment)
        db.commit()
        db.refresh(assignment)
        return assignment

    return _make


@pytest.fixture()
def make_score(db: Session):
    def _make(
        assignment: JudgeAssignment, criterion: RubricCriterion, value: int, *, comment: str | None = None
    ) -> Score:
        score = Score(
            event_id=assignment.event_id,
            assignment_id=assignment.id,
            criterion_id=criterion.id,
            value=value,
            comment=comment,
        )
        db.add(score)
        db.commit()
        db.refresh(score)
        return score

    return _make


@pytest.fixture()
def judging_event(make_event):
    """An event whose judging window is open right now."""

    def _make(*, judging_open: bool = True, **kwargs) -> Event:
        event = make_event(**kwargs)
        now = utcnow()
        if judging_open:
            event.judging_opens_at = now - timedelta(hours=1)
            event.judging_closes_at = now + timedelta(days=3)
        else:
            event.judging_opens_at = now + timedelta(days=1)
            event.judging_closes_at = now + timedelta(days=3)
        db = inspect(event).session
        db.commit()
        db.refresh(event)
        return event

    return _make


# --------------------------------------------------------------------------- #
# Public voting, comments and audit factories (Phase 3)
# --------------------------------------------------------------------------- #


@pytest.fixture()
def voting_event(db, make_event):
    """An event with a voting window, configurable access and method.

    `results_public_at` is left NULL by default, which is the state every real
    event starts in: results are organizers-only until somebody publishes them.
    """

    def _make(
        *,
        access: VotingAccess = VotingAccess.AUTHENTICATED,
        method: VotingMethod = VotingMethod.SINGLE,
        open_now: bool = True,
        credits: int = 100,
        votes_per_voter: int = 3,
        results_public: bool | None = False,
        comments: bool = True,
        **kwargs,
    ) -> Event:
        event = make_event(**kwargs)
        now = utcnow()
        event.voting_access = access
        event.voting_method = method
        event.vote_credits = credits
        event.votes_per_voter = votes_per_voter
        event.comments_enabled = comments
        if open_now:
            event.voting_opens_at = now - timedelta(hours=1)
            event.voting_closes_at = now + timedelta(days=2)
        else:
            event.voting_opens_at = now + timedelta(days=1)
            event.voting_closes_at = now + timedelta(days=3)
        if results_public is True:
            event.results_public_at = now - timedelta(minutes=5)
        elif results_public is None:
            event.results_public_at = now + timedelta(days=3)  # scheduled, not yet
        else:
            event.results_public_at = None
        db.commit()
        db.refresh(event)
        return event

    return _make


@pytest.fixture()
def tiered_event(
    db, make_event, make_user, make_team, make_submission, make_judge, make_assignment,
    make_criterion, make_score,
):
    """Four judged submissions, ranked 1-4 by a single judge's scores, with
    `winner_slots=1, community_vote_slots=2` -- rank 1 is the winner, ranks 2
    and 3 are the community tier, rank 4 is neither. Voting is open now.

    Shared between Part 3's (`test_community_vote_tier.py`) and Part 4's
    (`test_result_overrides.py`) tests -- both need the identical judged,
    tiered event as their starting point.
    """

    def _build(*, starts_at=None):
        now = utcnow()
        event = make_event(starts_at=starts_at or (now - timedelta(days=30)))
        event.winner_slots = 1
        event.community_vote_slots = 2
        event.voting_opens_at = now - timedelta(hours=1)
        event.voting_closes_at = now + timedelta(days=2)
        event.voting_access = VotingAccess.AUTHENTICATED
        db.commit()

        only = make_criterion(event, key="overall", name="Overall", weight=1)
        judge = make_judge(event, make_user(Role.JUDGE))

        subs = []
        for i, value in enumerate([5, 4, 3, 2]):  # rank 1..4, best first
            team = make_team(event, make_user(Role.PARTICIPANT), name=f"Team {i}")
            submission = make_submission(
                team, name=f"Project {i}", status=SubmissionStatus.SUBMITTED
            )
            subs.append(submission)
            ballot = make_assignment(judge, submission, status=AssignmentStatus.COMPLETE)
            make_score(ballot, only, value)

        return event, subs  # subs[0] = winner, [1],[2] = community tier, [3] = neither

    return _build


@pytest.fixture()
def make_voter(db):
    def _make(
        event: Event,
        *,
        user: User | None = None,
        email: str | None = None,
        token_hash: str | None = None,
        seed: int = 1,
    ) -> Voter:
        voter = Voter(
            event_id=event.id,
            access=event.voting_access,
            user_id=user.id if user else None,
            email=email,
            token_hash=token_hash,
            ordering_seed=seed,
        )
        db.add(voter)
        db.commit()
        db.refresh(voter)
        return voter

    return _make


@pytest.fixture()
def make_comment(db):
    def _make(submission: Submission, author: User, body: str = "Nice work.") -> Comment:
        comment = Comment(
            event_id=submission.event_id,
            submission_id=submission.id,
            author_id=author.id,
            body=body,
        )
        db.add(comment)
        db.commit()
        db.refresh(comment)
        return comment

    return _make


@pytest.fixture(autouse=True)
def _reset_rate_limits(db):
    """Rate-limit rows are shared state keyed by ip and event.

    The `db` fixture truncates every table after each test, but the limiter is
    the one thing where a leftover row from a previous test would make the next
    one fail for an unrelated reason -- so this is belt and braces, and it also
    documents that the limiter is database-backed rather than in-process.
    """
    yield
    db.execute(text("TRUNCATE rate_limits"))
    db.commit()
