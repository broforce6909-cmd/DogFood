"""The organizers' `fixtures.json`, loaded, and the acceptance checks it feeds.

These load the *real* file at the repo root -- the same one `run.py` reads and the
api container mounts -- not a hand-made sample, because the point is what the real
file's awkward cases do to our schema: a team entered twice, a name used three
times, a judge who lists two tracks.

Every check `run.py` makes is repeated here with the reason it should pass spelled
out, including the one that can pass for the wrong reason: a POST whose body fails
validation is a 4xx too, which says nothing about the deadline. That is why the
"submit" route is the per-submission action and why the deadline is asserted with
a well-formed request, a control on the same predicate, and the response text.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select

from app import seed_fixtures as sf
from app.access import Action, check_access
from app.config import settings
from app.models import (
    AuditEntry,
    Comment,
    Event,
    Judge,
    JudgeAssignment,
    Score,
    Submission,
    Team,
    UserSession,
    Vote,
)

FIXTURE_FILE = Path(__file__).resolve().parents[1] / "fixtures.json"
DATA = json.loads(FIXTURE_FILE.read_text(encoding="utf-8"))


@pytest.fixture()
def loaded(db):
    """The fixtures, loaded and committed, with the acceptance sessions issued."""
    plan = sf.build_plan(DATA)
    probe = sf.pick_probe(plan)
    sf.load(db, plan)
    tokens = sf.issue_acceptance_sessions(db, plan, probe)
    db.commit()
    return plan, probe, tokens


def as_(tokens: dict[str, str], name: str) -> dict[str, str]:
    """The header the checker attaches: `Cookie: <name>=<token>`, verbatim."""
    return {"Cookie": f"{settings.session_cookie_name}={tokens[name]}"}


# --------------------------------------------------------------------------- #
# The plan: what the awkward cases turn into
# --------------------------------------------------------------------------- #


def test_the_plan_adapts_each_awkward_case_and_says_so():
    plan = sf.build_plan(DATA)
    notes = "\n".join(plan.notes)

    # Forty teams, forty projects: 41 in the file, one of them a duplicate entry.
    assert len(DATA["projects"]) == 41
    assert (len(plan.teams), len(plan.projects), len(plan.judges)) == (40, 40, 30)

    # Team names are unique per event, and every renamed team is named in the notes.
    assert len({t.name.casefold() for t in plan.teams}) == 40
    for renamed in ("tm_16", "tm_30", "tm_34", "tm_40"):
        assert f"team {renamed}" in notes
    # The first bearer of a name keeps it.
    assert next(t for t in plan.teams if t.fixture_id == "tm_03").name == "StillTrail"

    # One submission per team: the later of the two entries is the record.
    ids = {p.fixture_id for p in plan.projects}
    assert "prj_41" in ids and "prj_07" not in ids
    assert "prj_07" in notes and "superseded by prj_41" in notes

    # Nine judges list two tracks; the loader says it widened them.
    assert "9 judges" in notes and "all-tracks" in notes


def test_a_judge_who_reviewed_both_entries_keeps_the_review_of_the_later_one():
    plan = sf.build_plan(DATA)
    on_kept = {b.judge_id: b for b in plan.ballots if b.project_id == "prj_41"}

    # jdg_19 reviewed both entries, with different numbers: the later review stands.
    original = next(
        s for s in DATA["scores"] if s["judge"] == "jdg_19" and s["project"] == "prj_41"
    )
    assert on_kept["jdg_19"].criteria == original["criteria"]
    # jdg_01 and jdg_12 only reviewed the earlier entry: those reviews are carried over.
    assert {"jdg_01", "jdg_12"} <= set(on_kept)
    # 5 + 4 reviews of the two entries, 3 judges on both -> 6 ballots, 3 dropped.
    assert len(on_kept) == 6
    assert len(plan.ballots) == len(DATA["scores"]) - 3


def test_no_ballot_is_left_pointing_at_a_project_that_was_not_loaded():
    plan = sf.build_plan(DATA)
    kept = {p.fixture_id for p in plan.projects}
    assert all(b.project_id in kept for b in plan.ballots)
    assert len({(b.judge_id, b.project_id) for b in plan.ballots}) == len(plan.ballots)


def test_the_probe_is_two_track_judges_of_the_projects_own_track():
    plan = sf.build_plan(DATA)
    probe = sf.pick_probe(plan)
    assert probe.project.fixture_id == "prj_01"
    assert probe.judge_a.track_ids == probe.judge_b.track_ids == (probe.project.track_id,)
    assert probe.judge_a.fixture_id != probe.judge_b.fixture_id
    assert probe.ballot_a.project_id == probe.ballot_b.project_id == "prj_01"


# --------------------------------------------------------------------------- #
# The load
# --------------------------------------------------------------------------- #


def test_the_event_keeps_the_files_own_close_date(db, loaded):
    plan, *_ = loaded
    event = db.execute(select(Event).where(Event.slug == plan.slug)).scalar_one()

    # Exactly the file's value -- not a date of ours -- and therefore in the past.
    assert event.submission_deadline == sf.parse_utc(DATA["event"]["submissions_close"])
    assert event.deadline_passed() and not event.submissions_open()
    # Derived dates are consistent with it, and every fixture submission fits inside.
    assert event.submission_opens_at < event.submission_deadline
    earliest = min(sf.parse_utc(p["submitted_at"]) for p in DATA["projects"])
    assert event.submission_opens_at <= earliest


def test_the_load_holds_the_expected_rows_and_is_recognised_as_loaded(db, loaded):
    plan, *_ = loaded
    assert sf.already_loaded(db, plan)

    def count(model):
        return db.execute(select(func.count()).select_from(model)).scalar_one()

    event = db.execute(select(Event).where(Event.slug == plan.slug)).scalar_one()
    assert count(Submission) == 40
    assert count(Team) == 40
    assert count(Judge) == 30
    assert count(JudgeAssignment) == 123
    assert count(Score) == 123 * 3
    assert [c.key for c in event.criteria] == ["functionality", "quality", "innovation"]
    assert len(event.tracks) == 8

    judges = {j.user.email: j for j in db.execute(select(Judge)).scalars()}
    two_tracks = next(j for j in plan.judges if len(j.track_ids) == 2)
    one_track = next(j for j in plan.judges if len(j.track_ids) == 1)
    assert judges[two_tracks.email].track_id is None
    assert judges[one_track.email].track_id is not None


def test_nothing_is_invented(db, loaded):
    """The file has no review timestamps, votes, comments or audit history, and the
    loader does not make any up."""
    assert db.execute(select(func.count()).select_from(Vote)).scalar_one() == 0
    assert db.execute(select(func.count()).select_from(Comment)).scalar_one() == 0
    assert db.execute(select(func.count()).select_from(AuditEntry)).scalar_one() == 0
    ballots = db.execute(select(JudgeAssignment)).scalars().all()
    assert all(b.completed_at is None for b in ballots)
    # An empty comment in the file is no comment, not an empty string.
    assert all(b.comment is None or b.comment.strip() for b in ballots)


# --------------------------------------------------------------------------- #
# The four accounts handed to the checker
# --------------------------------------------------------------------------- #


def test_the_acceptance_tokens_are_stable_and_replaced_rather_than_piled_up(db, loaded):
    plan, probe, tokens = loaded
    again = sf.issue_acceptance_sessions(db, plan, probe)
    db.commit()

    assert again == tokens  # same secret, same tokens: `down -v && up` reproduces them
    rows = db.execute(
        select(UserSession).where(UserSession.user_agent == sf.ACCEPTANCE_MARKER)
    ).scalars().all()
    assert len(rows) == 4
    # Long-lived, so the committed header does not expire before it is used.
    assert all(r.expires_at > datetime.now(UTC) + timedelta(days=3000) for r in rows)


def test_the_acceptance_tokens_are_the_accounts_they_claim_to_be(client, loaded):
    plan, probe, tokens = loaded
    expected = {
        "organizer": (sf.ORGANIZER_EMAIL, "organizer"),
        "judge_a": (probe.judge_a.email, "judge"),
        "judge_b": (probe.judge_b.email, "judge"),
        "participant": (plan.teams[0].members[0], "participant"),
    }
    for name, (email, role) in expected.items():
        body = client.get("/api/auth/me", headers=as_(tokens, name)).json()
        assert (body["user"]["email"], body["role"]) == (email, role), name


def test_acceptance_sessions_are_refused_in_production(monkeypatch):
    monkeypatch.setattr(settings, "environment", "production")
    assert sf.acceptance_sessions_allowed() is False
    monkeypatch.setattr(settings, "environment", "development")
    assert sf.acceptance_sessions_allowed() is True


def test_the_printed_block_is_the_shape_the_organizers_show(loaded):
    plan, probe, tokens = loaded
    block = sf.login_block(plan, probe, tokens)
    lines = block.splitlines()
    assert lines[0] == "seeded. test logins:"
    names = ("organizer", "judge_a", "judge_b", "participant")
    for line, name in zip(lines[1:5], names, strict=True):
        assert line.split() == [name, "Cookie:", f"{settings.session_cookie_name}={tokens[name]}"]
    # The routes it prints are the ones the tests below drive.
    assert f"/api/judging/assignments/{sf.ballot_id(plan, probe.ballot_a)}" in block


# --------------------------------------------------------------------------- #
# The seven checks in run.py, and why each passes
# --------------------------------------------------------------------------- #


def routes(plan, probe) -> dict[str, str]:
    """The same mapping `.dogfood.toml` carries, derived instead of typed."""
    ballot = f"/api/judging/assignments/{sf.ballot_id(plan, probe.ballot_a)}"
    _, entry = sf.participant_entry(plan)
    return {
        "gallery": f"/api/gallery?event={plan.slug}",
        "submit": f"/api/submissions/{sf._fid('submission', plan.slug, entry.fixture_id)}/submit",
        "judge_scores": ballot,
        "peer_scores": ballot,
        "csv_export": f"/api/events/{plan.slug}/export/results.csv",
    }


def test_gallery_is_public_and_page_one_shows_a_fixture_title(client, loaded):
    plan, probe, _ = loaded
    response = client.get(routes(plan, probe)["gallery"])  # no credentials
    assert response.status_code == 200
    page = response.json()
    assert (page["total"], page["per_page"]) == (40, 24)  # paginated; this is page one
    # run.py's rule: any of the first three fixture titles in the body.
    titles = [p["title"] for p in DATA["projects"][:3]]
    assert any(t.lower() in response.text.lower() for t in titles)


def test_a_late_submission_is_refused_because_of_the_deadline(client, db, loaded):
    plan, probe, tokens = loaded
    route = routes(plan, probe)["submit"]

    # run.py's own request, verbatim body included.
    late = client.post(
        route,
        headers=as_(tokens, "participant"),
        json={"title": "dogfood-late-submission-probe", "summary": "probe"},
    )
    assert late.status_code == 409
    assert late.json()["detail"] == "The submission deadline has passed"

    # A well-formed create is refused too -- 403, not a validation error (422).
    team = db.execute(select(Team).where(Team.name == "NorthKiln")).scalar_one()
    create = client.post(
        "/api/submissions",
        headers=as_(tokens, "participant"),
        json={"team_id": str(team.id), "name": "late entry"},
    )
    assert create.status_code == 403

    # And so are edits and un-submitting -- "edit until the deadline" means until.
    sid = route.split("/")[3]
    for method, path, body in (
        ("patch", f"/api/submissions/{sid}", {"tagline": "edited after the close"}),
        ("post", f"/api/submissions/{sid}/unsubmit", None),
    ):
        response = getattr(client, method)(path, headers=as_(tokens, "participant"), json=body)
        assert response.status_code == 409, path
        assert response.json()["detail"] == "The submission deadline has passed"


def test_the_deadline_is_the_only_thing_standing_in_the_way(db, loaded):
    """Control: the very same team member, team and action are permitted an hour
    before the close and refused a second after it. Nothing but the clock differs."""
    plan, *_ = loaded
    team = db.execute(select(Team).where(Team.name == "NorthKiln")).scalar_one()
    owner = team.members[0].user
    close = sf.parse_utc(DATA["event"]["submissions_close"])

    before, after = close - timedelta(hours=1), close + timedelta(seconds=1)
    assert check_access(owner, team, Action.CREATE_SUBMISSION, now=before)
    assert not check_access(owner, team, Action.CREATE_SUBMISSION, now=after)
    submission = team.submission
    assert check_access(owner, submission, Action.SUBMIT, now=before)
    assert not check_access(owner, submission, Action.SUBMIT, now=after)


def test_a_judge_reads_their_own_ballot_with_its_scores(client, loaded):
    plan, probe, tokens = loaded
    response = client.get(routes(plan, probe)["judge_scores"], headers=as_(tokens, "judge_a"))
    assert response.status_code == 200
    body = response.json()
    assert body["judge_name"] == probe.judge_a.name
    assert body["submission"]["name"] == probe.project.title
    assert len(body["scores"]) == 3  # the real numbers, not an empty shell


def test_a_peer_judge_is_refused_the_same_ballot(client, loaded):
    plan, probe, tokens = loaded
    url = routes(plan, probe)["peer_scores"]

    # judge_b is a real, active judge who reviewed this very project ...
    own = client.get("/api/judging/queue", headers=as_(tokens, "judge_b")).json()
    assert probe.project.title in {b["submission"]["name"] for b in own}
    own_id = next(b["id"] for b in own if b["submission"]["name"] == probe.project.title)
    mine = client.get(f"/api/judging/assignments/{own_id}", headers=as_(tokens, "judge_b"))
    assert mine.status_code == 200

    # ... and is still refused judge_a's, for reading and for writing.
    assert client.get(url, headers=as_(tokens, "judge_b")).status_code == 403
    rubric = client.get(f"/api/events/{plan.slug}/criteria", headers=as_(tokens, "judge_b")).json()
    body = {"scores": [{"criterion_id": c["id"], "value": 5} for c in rubric], "complete": True}
    # A well-formed body, so a 403 here is the ownership check and not a 422.
    forged = client.put(f"{url}/scores", headers=as_(tokens, "judge_b"), json=body)
    assert forged.status_code == 403
    # Nor does their own queue list it.
    assert str(sf.ballot_id(plan, probe.ballot_a)) not in {b["id"] for b in own}


def test_a_participant_is_not_a_judge(client, loaded):
    plan, probe, tokens = loaded
    url = routes(plan, probe)["judge_scores"]
    assert client.get(url, headers=as_(tokens, "participant")).status_code == 403
    assert client.get(url).status_code in (401, 403)  # and neither is a stranger


def test_the_organizer_exports_results_as_csv(client, loaded):
    plan, probe, tokens = loaded
    response = client.get(routes(plan, probe)["csv_export"], headers=as_(tokens, "organizer"))
    assert response.status_code == 200
    lines = response.text.splitlines()
    assert "," in lines[0] and lines[0].startswith("rank,")
    assert len(lines) == 1 + 40  # header + one row per loaded project

    # A judge and a participant do not get the export (staff only).
    for name in ("judge_a", "participant"):
        refused = client.get(routes(plan, probe)["csv_export"], headers=as_(tokens, name))
        assert refused.status_code == 403, name
