"""Loads the organizers' published fixtures (`fixtures.json`) into the portal.

Why this is its own module rather than a section of `app.seed`: `app.seed` builds
*our* invented events for the README tour and is gated on one marker row, so a
database that already holds them would never see a new fixture set. This loader
has its own marker (the fixture event's slug), so `docker compose up` on an old
volume picks the fixtures up too, and neither seeder can disturb the other.

The organizers' brief is explicit that the file is *input, not our data model*:
"load it, transform it, put it in whatever schema you can defend". Where our
schema is stricter than the file, the loader adapts and says so -- every
adaptation is a line in `Plan.notes`, printed at load and asserted by
`tests/test_fixture_loader.py`, never a silent drop:

* one submission per team (`submissions.team_id` is unique): when a team appears
  on several projects, the *latest* entry is the record -- resubmitting is how a
  team edits before the deadline -- and an earlier entry's reviews are kept only
  for judges who did not also review the later one;
* team names are unique per event: a name that repeats keeps its first owner and
  the later teams are suffixed with their fixture id;
* a judge lists several tracks in the file, but a `Judge` row holds one track or
  none: those judges load as all-tracks, which is wider than the file says.

The event's `submissions_close` is loaded exactly as written. It is in the past,
deliberately, and "a closed event refuses submissions" is checked against it --
substituting a date of our own would make that check pass or fail for the wrong
reason. Every other date on the event is derived from it.

Nothing here invents evidence: the file carries no review timestamps, so
`completed_at` stays NULL, and no vote, comment or audit row is fabricated.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from .config import settings
from .db import SessionLocal
from .models import (
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
    UserSession,
    utcnow,
)
from .security import hash_password, hash_session_token
from .seed import FIXTURE_PASSWORD, NS

# The organizer account the base seed already creates. The fixture file has no
# organizer of its own, and staff can run any event, so this one runs the fixture
# event too. Created here if the base seed has not run.
ORGANIZER_EMAIL = "organizer@example.com"
ORGANIZER_NAME = "Marion Vasquez"

# Sessions minted for the acceptance suite are recognisable by this marker in
# `sessions.user_agent`, so a reboot can replace exactly them and nothing else.
ACCEPTANCE_MARKER = "dogfood-acceptance-seed"

# Long-lived on purpose: the tokens are committed in `.dogfood.toml`, and a
# 72-hour session would turn every one of them into a 401 within days.
ACCEPTANCE_TTL = timedelta(days=3650)

_TOKEN_PREFIX = {
    "organizer": "org",
    "judge_a": "jdg_a",
    "judge_b": "jdg_b",
    "participant": "prt",
}

# Every criterion in the file scores 2-5 on what is plainly a 1-5 scale. The file
# publishes no rubric, so weights are equal; an organizer can re-weight them.
CRITERION_MIN, CRITERION_MAX = 1, 5


# --------------------------------------------------------------------------- #
# The plan: pure data, no database
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class PlannedTeam:
    fixture_id: str
    name: str  # after de-duplication
    members: tuple[str, ...]  # emails; the first is the owner


@dataclass(frozen=True)
class PlannedProject:
    fixture_id: str
    team_id: str
    track_id: str
    title: str
    summary: str
    repo_url: str
    submitted_at: datetime


@dataclass(frozen=True)
class PlannedBallot:
    judge_id: str
    project_id: str  # always a kept project, never a superseded one
    criteria: dict[str, int]
    comment: str


@dataclass(frozen=True)
class PlannedJudge:
    fixture_id: str
    name: str
    email: str
    track_ids: tuple[str, ...]


@dataclass(frozen=True)
class Probe:
    """The pair of judges the acceptance suite plays off against each other."""

    project: PlannedProject
    judge_a: PlannedJudge
    judge_b: PlannedJudge
    ballot_a: PlannedBallot
    ballot_b: PlannedBallot


@dataclass
class Plan:
    slug: str
    name: str
    close: datetime
    tracks: list[dict]
    criteria_keys: list[str]
    judges: list[PlannedJudge]
    teams: list[PlannedTeam]
    projects: list[PlannedProject]
    ballots: list[PlannedBallot]
    notes: list[str] = field(default_factory=list)


def parse_utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def build_plan(data: dict) -> Plan:
    """Turn the file into what our schema can hold, recording every adaptation."""
    event = data["event"]
    notes: list[str] = []

    # -- tracks -------------------------------------------------------------- #
    tracks, taken = [], set()
    for position, t in enumerate(data["tracks"]):
        key = slugify(t["name"])
        if key in taken:
            key = f"{key}-{t['id']}"
        taken.add(key)
        tracks.append({"fixture_id": t["id"], "key": key, "name": t["name"], "position": position})

    # -- teams: names are unique per event ---------------------------------- #
    teams, seen = [], set()
    for t in data["teams"]:
        name = t["name"]
        if name.casefold() in seen:
            name = f"{name} ({t['id']})"
            notes.append(
                f"team {t['id']} shares its name with an earlier team; loaded as {name!r} "
                "because team names are unique per event"
            )
        seen.add(name.casefold())
        teams.append(PlannedTeam(t["id"], name, tuple(m.strip().lower() for m in t["members"])))

    # -- projects: one submission per team, the latest entry wins ------------ #
    by_team: dict[str, list[dict]] = {}
    for p in data["projects"]:
        by_team.setdefault(p["team"], []).append(p)

    file_order = {p["id"]: n for n, p in enumerate(data["projects"])}
    superseded_by: dict[str, str] = {}
    kept: list[PlannedProject] = []
    for p in data["projects"]:
        rivals = by_team[p["team"]]
        # Latest submission wins; a tie goes to whichever the file lists last.
        latest = max(rivals, key=lambda r: (parse_utc(r["submitted_at"]), file_order[r["id"]]))
        if latest["id"] != p["id"]:
            superseded_by[p["id"]] = latest["id"]
            notes.append(
                f"project {p['id']} ({p['title']!r}) is superseded by {latest['id']}: same team "
                f"{p['team']}, and a team has one submission; the later entry is the record"
            )
            continue
        kept.append(
            PlannedProject(
                fixture_id=p["id"],
                team_id=p["team"],
                track_id=p["track"],
                title=p["title"],
                summary=p["summary"],
                repo_url=p["repo_url"],
                submitted_at=parse_utc(p["submitted_at"]),
            )
        )

    # -- judges ------------------------------------------------------------- #
    judges = [
        PlannedJudge(j["id"], j["name"], j["email"].strip().lower(), tuple(j["tracks"]))
        for j in data["judges"]
    ]
    multi = [j.fixture_id for j in judges if len(j.track_ids) > 1]
    if multi:
        notes.append(
            f"{len(multi)} judges ({', '.join(multi)}) list more than one track, and a judge "
            "record holds one track or all of them; they load as all-tracks judges, which is "
            "wider than the file says (no ballot in the file falls outside their tracks)"
        )

    # -- ballots: one per (judge, project) ---------------------------------- #
    criteria_keys: list[str] = []
    for s in data["scores"]:
        for key in s["criteria"]:
            if key not in criteria_keys:
                criteria_keys.append(key)

    def ballot(s: dict, project_id: str) -> PlannedBallot:
        return PlannedBallot(s["judge"], project_id, dict(s["criteria"]), s.get("comment") or "")

    ballots: dict[tuple[str, str], PlannedBallot] = {}
    for s in data["scores"]:  # ballots on kept projects win outright
        if s["project"] not in superseded_by:
            ballots[(s["judge"], s["project"])] = ballot(s, s["project"])
    merged = dropped = 0
    for s in data["scores"]:  # then earlier-entry ballots, only where the judge has none
        target = superseded_by.get(s["project"])
        if target is None:
            continue
        if (s["judge"], target) in ballots:
            dropped += 1
        else:
            ballots[(s["judge"], target)] = ballot(s, target)
            merged += 1
    if merged or dropped:
        notes.append(
            f"reviews of superseded entries: {merged} kept (judge had not reviewed the later "
            f"entry), {dropped} dropped (the judge's review of the later entry stands)"
        )

    return Plan(
        slug=slugify(event["name"]),
        name=event["name"],
        close=parse_utc(event["submissions_close"]),
        tracks=tracks,
        criteria_keys=criteria_keys,
        judges=judges,
        teams=teams,
        projects=kept,
        ballots=list(ballots.values()),
        notes=notes,
    )


def pick_probe(plan: Plan) -> Probe | None:
    """The first project, in file order, that two *track* judges of its own track
    both reviewed. Chosen by rule rather than by name so it survives a corrected
    fixture file, and so both judges are unambiguously entitled to the ballot's
    project: the only thing between judge B and judge A's scores is the check."""
    judges = {j.fixture_id: j for j in plan.judges}
    for project in plan.projects:
        eligible = [
            b
            for b in plan.ballots
            if b.project_id == project.fixture_id
            and judges[b.judge_id].track_ids == (project.track_id,)
        ]
        if len(eligible) >= 2:
            a, b = eligible[0], eligible[1]
            return Probe(project, judges[a.judge_id], judges[b.judge_id], a, b)
    return None


# --------------------------------------------------------------------------- #
# Ids, tokens, display
# --------------------------------------------------------------------------- #


def _fid(kind: str, *parts: str) -> uuid.UUID:
    """Deterministic, so a reseeded database gets the same ids -- which is what
    lets `.dogfood.toml` name a ballot by URL and have it survive `down -v`."""
    return uuid.uuid5(NS, f"fixture:{kind}:{':'.join(parts)}")


def user_id(email: str) -> uuid.UUID:
    return uuid.uuid5(NS, f"user:{email}")  # same derivation as `app.seed`


def ballot_id(plan: Plan, ballot: PlannedBallot) -> uuid.UUID:
    return _fid("assignment", plan.slug, ballot.judge_id, ballot.project_id)


def participant_entry(plan: Plan) -> tuple[str, PlannedProject | None]:
    """The acceptance `participant`: the owner of the file's first team, and that
    team's project (its submission is what they try to submit after the close)."""
    team = plan.teams[0]
    project = next((p for p in plan.projects if p.team_id == team.fixture_id), None)
    return team.members[0], project


def display_name(email: str) -> str:
    return re.sub(r"[._]+", " ", email.split("@")[0]).strip().title() or email


def acceptance_token(name: str) -> str:
    """Stable for a given `SESSION_SECRET`, so `docker compose down -v && up`
    reproduces the values committed in `.dogfood.toml`; a deployment with its own
    secret gets tokens nobody can derive from this repository."""
    digest = hashlib.sha256(
        f"dogfood-acceptance:{settings.session_secret}:{name}".encode()
    ).hexdigest()
    return f"{_TOKEN_PREFIX[name]}_{digest[:24]}"


# --------------------------------------------------------------------------- #
# Applying the plan
# --------------------------------------------------------------------------- #


@dataclass
class Loaded:
    users: int
    teams: int
    submissions: int
    judges: int
    ballots: int
    scores: int


def already_loaded(db: Session, plan: Plan) -> bool:
    return db.execute(select(Event.id).where(Event.slug == plan.slug)).first() is not None


def _get_or_create_users(
    db: Session, wanted: dict[str, tuple[str, Role]], password_hash: str
) -> dict[str, User]:
    existing = {
        u.email: u
        for u in db.execute(select(User).where(User.email.in_(list(wanted)))).scalars()
    }
    for email, (name, role) in wanted.items():
        if email not in existing:
            user = User(
                id=user_id(email),
                email=email,
                display_name=name,
                password_hash=password_hash,
                role=role,
            )
            db.add(user)
            existing[email] = user
    db.flush()
    return existing


def load(db: Session, plan: Plan) -> Loaded:
    """Write the plan. One transaction's worth: the caller commits, so a failure
    leaves nothing behind and the next boot simply tries again."""
    password_hash = hash_password(FIXTURE_PASSWORD)

    wanted: dict[str, tuple[str, Role]] = {ORGANIZER_EMAIL: (ORGANIZER_NAME, Role.ORGANIZER)}
    for j in plan.judges:
        wanted.setdefault(j.email, (j.name, Role.JUDGE))
    for t in plan.teams:
        for email in t.members:
            wanted.setdefault(email, (display_name(email), Role.PARTICIPANT))
    users = _get_or_create_users(db, wanted, password_hash)
    organizer = users[ORGANIZER_EMAIL]

    # -- the event: the fixture's own close date, everything else derived ---- #
    close = plan.close
    earliest = min((p.submitted_at for p in plan.projects), default=close)
    opens = min(close - timedelta(days=4), earliest - timedelta(hours=1))
    event = Event(
        id=_fid("event", plan.slug),
        slug=plan.slug,
        name=plan.name,
        tagline="Loaded from the organizers' published fixtures. Submissions are closed.",
        description=(
            "This event is the organizers' published `fixtures.json`, loaded as written: "
            "its submission deadline is the file's own `submissions_close`, which is in the "
            "past, so every write to its submissions is refused. Dates other than that one "
            "are derived from it, because the file does not carry them."
        ),
        starts_at=opens,
        ends_at=close,
        registration_opens_at=opens - timedelta(days=17),
        submission_opens_at=opens,
        submission_deadline=close,
        judging_opens_at=close,
        judging_closes_at=close + timedelta(days=14),
        max_team_size=max([4, *(len(t.members) for t in plan.teams)]),
        is_published=True,
        created_by_id=organizer.id,
    )
    db.add(event)
    db.flush()

    tracks: dict[str, Track] = {}
    for t in plan.tracks:
        tracks[t["fixture_id"]] = Track(
            id=_fid("track", plan.slug, t["fixture_id"]),
            event_id=event.id,
            key=t["key"],
            name=t["name"],
            position=t["position"],
        )
    db.add_all(tracks.values())
    criteria: dict[str, RubricCriterion] = {}
    for position, key in enumerate(plan.criteria_keys):
        criteria[key] = RubricCriterion(
            id=_fid("criterion", plan.slug, key),
            event_id=event.id,
            key=key,
            name=key.replace("_", " ").title(),
            weight=Decimal(1),
            min_score=CRITERION_MIN,
            max_score=CRITERION_MAX,
            position=position,
        )
    db.add_all(criteria.values())
    db.flush()

    # -- teams, then submissions (composite FKs need the parents flushed) ---- #
    teams: dict[str, Team] = {}
    for t in plan.teams:
        team = Team(
            id=_fid("team", plan.slug, t.fixture_id),
            event_id=event.id,
            name=t.name,
            invite_token=f"seed-{_fid('invite', plan.slug, t.fixture_id).hex}",
            created_by_id=users[t.members[0]].id,
        )
        for position, email in enumerate(t.members):
            team.members.append(
                TeamMember(
                    user_id=users[email].id,
                    event_id=event.id,
                    team_role=TeamRole.OWNER if position == 0 else TeamRole.MEMBER,
                )
            )
        teams[t.fixture_id] = team
        db.add(team)
    db.flush()

    submissions: dict[str, Submission] = {}
    for p in plan.projects:
        submissions[p.fixture_id] = Submission(
            id=_fid("submission", plan.slug, p.fixture_id),
            event_id=event.id,
            team_id=teams[p.team_id].id,
            track_id=tracks[p.track_id].id,
            name=p.title,
            tagline=p.summary[:240],
            description=p.summary,
            repo_url=p.repo_url,
            status=SubmissionStatus.SUBMITTED,
            submitted_at=p.submitted_at,
            created_at=p.submitted_at,
        )
    db.add_all(submissions.values())

    judges: dict[str, Judge] = {}
    for j in plan.judges:
        judges[j.fixture_id] = Judge(
            id=_fid("judge", plan.slug, j.fixture_id),
            event_id=event.id,
            user_id=users[j.email].id,
            # One track -> a track judge. Several -> all tracks (see the module note).
            track_id=tracks[j.track_ids[0]].id if len(j.track_ids) == 1 else None,
            is_active=True,
            invited_at=close - timedelta(days=7),
            accepted_at=close - timedelta(days=5),
        )
    db.add_all(judges.values())
    db.flush()

    # -- ballots and scores -------------------------------------------------- #
    score_count = 0
    for b in plan.ballots:
        complete = all(k in b.criteria for k in plan.criteria_keys)
        assignment = JudgeAssignment(
            id=ballot_id(plan, b),
            event_id=event.id,
            judge_id=judges[b.judge_id].id,
            submission_id=submissions[b.project_id].id,
            status=AssignmentStatus.COMPLETE if complete else AssignmentStatus.IN_PROGRESS,
            comment=b.comment or None,
            assigned_at=close,
            completed_at=None,  # the file has no timestamps and we do not invent them
        )
        db.add(assignment)
        db.flush()
        for key, value in b.criteria.items():
            db.add(
                Score(
                    id=_fid("score", plan.slug, b.judge_id, b.project_id, key),
                    event_id=event.id,
                    assignment_id=assignment.id,
                    criterion_id=criteria[key].id,
                    value=Decimal(value),
                )
            )
            score_count += 1
    db.flush()

    return Loaded(
        users=len(users),
        teams=len(teams),
        submissions=len(submissions),
        judges=len(judges),
        ballots=len(plan.ballots),
        scores=score_count,
    )


# --------------------------------------------------------------------------- #
# The four accounts the acceptance suite is handed
# --------------------------------------------------------------------------- #


def acceptance_sessions_allowed() -> bool:
    """Ten-year organizer and judge sessions with derivable tokens are a demo
    convenience, never something a production database should be handed."""
    return settings.environment != "production"


def issue_acceptance_sessions(db: Session, plan: Plan, probe: Probe) -> dict[str, str]:
    """(Re)create the four fixed sessions and return `{name: token}`.

    Replaced wholesale on every boot rather than looked up: the tokens depend on
    `SESSION_SECRET`, so a rotated secret must not leave stale rows behind, and
    the expiry must not run down toward the day the committed header stops working.
    """
    accounts = {
        "organizer": ORGANIZER_EMAIL,
        "judge_a": probe.judge_a.email,
        "judge_b": probe.judge_b.email,
        "participant": participant_entry(plan)[0],
    }
    db.execute(delete(UserSession).where(UserSession.user_agent == ACCEPTANCE_MARKER))
    db.flush()
    tokens, now = {}, utcnow()
    for name, email in accounts.items():
        user = db.execute(select(User).where(User.email == email)).scalar_one()
        token = acceptance_token(name)
        db.add(
            UserSession(
                user_id=user.id,
                token_hash=hash_session_token(token),
                expires_at=now + ACCEPTANCE_TTL,
                user_agent=ACCEPTANCE_MARKER,
            )
        )
        tokens[name] = token
    db.flush()
    return tokens


def login_block(plan: Plan, probe: Probe, tokens: dict[str, str]) -> str:
    """What the acceptance suite's `[auth]` and `[routes]` tables need, in the shape
    the organizers' own example shows, so it can be copied straight across."""
    cookie = settings.session_cookie_name
    _, entry = participant_entry(plan)
    submit = (
        f"/api/submissions/{_fid('submission', plan.slug, entry.fixture_id)}/submit"
        if entry
        else "/api/submissions"
    )
    lines = ["seeded. test logins:"]
    lines += [f"  {name:<12} Cookie: {cookie}={token}" for name, token in tokens.items()]
    lines += [
        "",
        f"routes for .dogfood.toml ({plan.name}):",
        f"  gallery      /api/gallery?event={plan.slug}",
        f"  submit       {submit}",
        f"  judge_scores /api/judging/assignments/{ballot_id(plan, probe.ballot_a)}",
        f"  peer_scores  /api/judging/assignments/{ballot_id(plan, probe.ballot_a)}",
        f"  csv_export   /api/events/{plan.slug}/export/results.csv",
        f"  (the ballot is judge_a's on {probe.project.title!r} ({probe.project.fixture_id}), "
        f"which judge_b also reviewed)",
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def find_fixture_file() -> Path | None:
    explicit = os.environ.get("FIXTURES_PATH")
    if explicit:
        path = Path(explicit)
        if not path.is_file():
            raise SystemExit(f"[fixtures] FIXTURES_PATH={explicit} is not a file")
        return path
    here = Path(__file__).resolve()
    for candidate in (
        Path("/fixtures/fixtures.json"),
        here.parents[2] / "fixtures.json",  # repo root, when run from a checkout
        Path.cwd() / "fixtures.json",
    ):
        if candidate.is_file():
            return candidate
    return None


def main() -> int:
    path = find_fixture_file()
    if path is None:
        print("[fixtures] no fixtures.json found (set FIXTURES_PATH); skipping")
        return 0

    plan = build_plan(json.loads(path.read_text(encoding="utf-8")))
    probe = pick_probe(plan)

    db = SessionLocal()
    try:
        if already_loaded(db, plan):
            print(f"[fixtures] {plan.slug} already present, skipping load")
        else:
            result = load(db, plan)
            print(
                f"[fixtures] loaded {path.name} as {plan.slug}: {result.submissions} submissions, "
                f"{result.teams} teams, {result.judges} judges, {result.ballots} ballots "
                f"({result.scores} scores), {result.users} accounts; "
                f"submissions closed {plan.close.isoformat()}"
            )
            for note in plan.notes:
                print(f"[fixtures]   adapted: {note}")

        if not acceptance_sessions_allowed():
            print("[fixtures] production: not creating fixed acceptance sessions")
        elif probe is None:
            print("[fixtures] no project reviewed by two track judges; no judge sessions issued")
        else:
            tokens = issue_acceptance_sessions(db, plan, probe)
            print(login_block(plan, probe, tokens))
        db.commit()
        return 0
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
