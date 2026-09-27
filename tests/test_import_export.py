"""Bulk import, and the round trip DATA-MODEL.md has promised since Phase 1.

That promise is specific: *every entity gets a matching pair, with identical column
sets in both directions, so an export can be re-imported without editing*. It has
been a documented gap for three phases. These tests are what make it true, and the
round-trip tests are the ones that matter — anything else is an importer that
happens to accept a file shaped like ours.

The brief's framing is the reason this is worth the trouble: *a platform you cannot
leave is a trap.*
"""

from __future__ import annotations

import csv
import io

import pytest
from fastapi.testclient import TestClient

from app.models import Role, SubmissionStatus


@pytest.fixture()
def populated(db, make_event, make_user, make_team, make_submission, make_track):
    """An event with tracks, teams and submissions worth exporting."""

    def _build():
        event = make_event()
        make_track(event, key="civic", name="Civic")
        for i in range(3):
            team = make_team(event, make_user(Role.PARTICIPANT), name=f"Team {i}")
            make_submission(
                team,
                name=f"Project {i}",
                status=SubmissionStatus.SUBMITTED,
                tags=["python", "postgres"],
            )
        return event, make_user(Role.ORGANIZER)

    return _build


def export(client: TestClient, slug: str, headers, entity: str):
    return client.get(f"/api/events/{slug}/export/{entity}.csv", headers=headers)


def _rows(response) -> list[list[str]]:
    return list(csv.reader(io.StringIO(response.text)))


def do_import(
    client: TestClient, slug: str, headers, entity: str, body: str, *, dry_run: bool = False
):
    return client.post(
        f"/api/events/{slug}/import/{entity}",
        params={"dry_run": "true"} if dry_run else None,
        headers={**headers, "content-type": "text/csv"},
        content=body.encode(),
    )


# --------------------------------------------------------------------------- #
# Authorization first
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("role", [Role.PARTICIPANT, Role.JUDGE])
def test_only_staff_import(client: TestClient, auth, make_user, populated, role):
    """Import writes rows. It is the most dangerous organizer-only route here."""
    event, _organizer = populated()
    r = do_import(
        client, event.slug, auth(make_user(role)), "submissions", "submission_id,name\n"
    )
    assert r.status_code == 403


def test_an_anonymous_caller_cannot_import(client: TestClient, populated):
    event, _organizer = populated()
    r = do_import(client, event.slug, {}, "submissions", "submission_id,name\n")
    assert r.status_code == 403


def test_an_unknown_entity_is_a_404(client: TestClient, auth, populated):
    event, organizer = populated()
    r = do_import(client, event.slug, auth(organizer), "nonsense", "a,b\n")
    assert r.status_code == 404
    assert "Available" in r.json()["detail"]


# --------------------------------------------------------------------------- #
# The round trip
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("entity", ["submissions", "teams", "tracks"])
def test_an_export_re_imports_without_editing(
    client: TestClient, auth, populated, entity: str
):
    """The promise, tested literally: take the file out, put the same file back.

    A clean re-import of unchanged data must be all updates and no errors. If the
    column sets disagree in either direction, this is where it shows.
    """
    event, organizer = populated()
    headers = auth(organizer)

    out = export(client, event.slug, headers, entity)
    assert out.status_code == 200, out.text

    back = do_import(client, event.slug, headers, entity, out.text)
    assert back.status_code == 200, back.text
    body = back.json()
    assert body["errors"] == [], body["errors"]
    assert body["created"] == 0, "re-importing unchanged data created rows"
    assert body["updated"] >= 1


def test_a_round_trip_does_not_change_the_data(client: TestClient, auth, populated):
    """Stronger than "it was accepted": the export after the import is identical."""
    event, organizer = populated()
    headers = auth(organizer)

    before = export(client, event.slug, headers, "submissions").text
    do_import(client, event.slug, headers, "submissions", before)
    after = export(client, event.slug, headers, "submissions").text
    assert before == after


def test_an_edited_export_applies_the_edit(client: TestClient, auth, populated):
    """The actual workflow: export, fix thirty taglines in a spreadsheet, import."""
    event, organizer = populated()
    headers = auth(organizer)

    rows = list(csv.DictReader(io.StringIO(export(client, event.slug, headers, "submissions").text)))
    for row in rows:
        row["tagline"] = "edited in a spreadsheet"

    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=list(rows[0]))
    writer.writeheader()
    writer.writerows(rows)

    result = do_import(client, event.slug, headers, "submissions", buffer.getvalue())
    assert result.status_code == 200, result.text
    assert result.json()["updated"] == 3

    after = export(client, event.slug, headers, "submissions").text
    assert after.count("edited in a spreadsheet") == 3


# --------------------------------------------------------------------------- #
# Creating, and refusing to
# --------------------------------------------------------------------------- #


def test_a_new_row_is_created(client: TestClient, auth, populated):
    event, organizer = populated()
    headers = auth(organizer)
    body = "key,name,description,position\nhardware,Hardware,Things with wires,1\n"
    r = do_import(client, event.slug, headers, "tracks", body)
    assert r.status_code == 200, r.text
    assert r.json()["created"] == 1

    tracks = client.get(f"/api/events/{event.slug}/tracks").json()
    assert "hardware" in {t["key"] for t in tracks}


def test_a_dry_run_reports_the_outcome_but_writes_nothing(
    client: TestClient, auth, populated
):
    """Upload -> preview -> confirm: `dry_run=true` runs the real importer and
    its real validation, then rolls back instead of committing, so an
    organizer can read the outcome before anything actually changes."""
    event, organizer = populated()
    headers = auth(organizer)
    body = "key,name,description,position\nhardware,Hardware,Things with wires,1\n"

    r = do_import(client, event.slug, headers, "tracks", body, dry_run=True)
    assert r.status_code == 200, r.text
    result = r.json()
    assert result["created"] == 1
    assert result["dry_run"] is True

    tracks = client.get(f"/api/events/{event.slug}/tracks").json()
    assert "hardware" not in {t["key"] for t in tracks}, "a dry run wrote a row"


def test_a_dry_run_leaves_no_audit_entry(client: TestClient, auth, populated):
    event, organizer = populated()
    headers = auth(organizer)
    body = "key,name,description,position\nhardware,Hardware,Things with wires,1\n"

    do_import(client, event.slug, headers, "tracks", body, dry_run=True)

    rows = client.get(f"/api/events/{event.slug}/audit", headers=headers).json()["rows"]
    assert not any(row["action"] == "bulk_import" for row in rows)


def test_a_dry_run_still_reports_per_row_errors(client: TestClient, auth, populated):
    """The whole point of a preview: a bad row shows up before the organizer
    commits to it, identically to a real import's own per-row error
    reporting -- and the good rows in the same file are still reported as
    what *would* be created, not discarded because one row was bad."""
    event, organizer = populated()
    headers = auth(organizer)
    body = (
        "key,name,description,position\n"
        "good,Good Track,,1\n"
        ",Missing Key,,2\n"
    )

    r = do_import(client, event.slug, headers, "tracks", body, dry_run=True)
    assert r.status_code == 200, r.text
    result = r.json()
    assert result["created"] == 1
    assert len(result["errors"]) == 1
    assert result["dry_run"] is True

    tracks = client.get(f"/api/events/{event.slug}/tracks").json()
    assert "good" not in {t["key"] for t in tracks}, "a dry run wrote a row"


def test_confirming_after_a_dry_run_actually_writes_the_row(
    client: TestClient, auth, populated
):
    event, organizer = populated()
    headers = auth(organizer)
    body = "key,name,description,position\nhardware,Hardware,Things with wires,1\n"

    preview = do_import(client, event.slug, headers, "tracks", body, dry_run=True)
    assert preview.json()["created"] == 1

    confirmed = do_import(client, event.slug, headers, "tracks", body)
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["created"] == 1
    assert confirmed.json()["dry_run"] is False

    tracks = client.get(f"/api/events/{event.slug}/tracks").json()
    assert "hardware" in {t["key"] for t in tracks}


def test_a_row_for_another_event_is_refused_not_stolen(
    client: TestClient, auth, populated, make_event
):
    """Attack: import a CSV naming another event's submission id, to edit it.

    The importer scopes every row to the event in the URL. A foreign id is an error
    on that row, never a write to somebody else's event.
    """
    event, organizer = populated()
    other, _ = populated()
    headers = auth(organizer)

    foreign = export(client, other.slug, headers, "submissions").text
    r = do_import(client, event.slug, headers, "submissions", foreign)
    assert r.status_code == 200, r.text
    assert r.json()["updated"] == 0
    assert r.json()["errors"], "a foreign row was silently accepted"

    # And the other event is untouched.
    assert export(client, other.slug, headers, "submissions").text == foreign


def test_a_missing_required_column_is_reported_not_guessed(
    client: TestClient, auth, populated
):
    event, organizer = populated()
    r = do_import(client, event.slug, auth(organizer), "tracks", "name\nNo Key Here\n")
    assert r.status_code == 422
    assert "key" in r.text


def test_a_bad_row_does_not_take_the_good_ones_with_it(
    client: TestClient, auth, populated
):
    """Partial success is reported per row.

    All-or-nothing would mean one typo in row 40 discards the other 39 corrections,
    and an organizer would stop using the importer.
    """
    event, organizer = populated()
    headers = auth(organizer)
    body = (
        "key,name,description,position\n"
        "good,Good Track,,1\n"
        ",Missing Key,,2\n"
        "another,Another,,3\n"
    )
    r = do_import(client, event.slug, headers, "tracks", body)
    assert r.status_code == 200, r.text
    assert r.json()["created"] == 2
    assert len(r.json()["errors"]) == 1
    assert "2" in r.json()["errors"][0] or "row" in r.json()["errors"][0].lower()


# --------------------------------------------------------------------------- #
# The importer must not be a side door around the link-field XSS guard
# --------------------------------------------------------------------------- #


def test_a_javascript_uri_in_repo_url_is_refused_not_stored(
    client: TestClient, auth, populated
):
    """`schemas.SubmissionUpdate` refuses a `javascript:` URI in `repo_url` --
    otherwise the project page's `<a href={repo_url}>` executes it the moment a
    judge clicks "View repo". The bulk importer wrote straight to the ORM
    column with `setattr`, skipping that validator entirely: the exact same
    payload that a 422 refuses over the JSON API sailed through as a CSV.
    """
    event, organizer = populated()
    headers = auth(organizer)
    submission_id = client.get(
        f"/api/events/{event.slug}/submissions", headers=headers
    ).json()["items"][0]["id"]

    body = (
        "submission_id,name,repo_url\n"
        f"{submission_id},Project 0,javascript:alert(document.cookie)\n"
    )
    r = do_import(client, event.slug, headers, "submissions", body)
    assert r.status_code == 200, r.text
    result = r.json()
    assert result["updated"] == 0, "the malicious row must not be applied"
    assert result["errors"], "the malicious row must be reported, not silently dropped"

    stored = client.get(f"/api/submissions/{submission_id}", headers=headers).json()
    assert stored["repo_url"] != "javascript:alert(document.cookie)"


def test_a_javascript_uri_in_thumbnail_url_is_refused_not_stored(
    client: TestClient, auth, populated
):
    """Same vector, the image field. `_image_url` allows http(s) and safe inline
    `data:image/...` but nothing else -- a `javascript:` URI must not reach
    storage through the importer either."""
    event, organizer = populated()
    headers = auth(organizer)
    submission_id = client.get(
        f"/api/events/{event.slug}/submissions", headers=headers
    ).json()["items"][0]["id"]

    body = (
        "submission_id,name,thumbnail_url\n"
        f"{submission_id},Project 0,javascript:alert(1)\n"
    )
    r = do_import(client, event.slug, headers, "submissions", body)
    assert r.status_code == 200, r.text
    assert r.json()["updated"] == 0
    assert r.json()["errors"]


def test_a_legitimate_github_repo_url_still_imports_cleanly(
    client: TestClient, auth, populated
):
    """The fix must not make the importer refuse ordinary data -- only the
    dangerous scheme, and (for this column specifically) a URL that is not
    shaped like a GitHub repository -- see `_github_repo_url`."""
    event, organizer = populated()
    headers = auth(organizer)
    submission_id = client.get(
        f"/api/events/{event.slug}/submissions", headers=headers
    ).json()["items"][0]["id"]

    body = (
        "submission_id,name,repo_url\n"
        f"{submission_id},Project 0,https://github.com/example/repo\n"
    )
    r = do_import(client, event.slug, headers, "submissions", body)
    assert r.status_code == 200, r.text
    assert r.json()["updated"] == 1
    assert r.json()["errors"] == []

    stored = client.get(f"/api/submissions/{submission_id}", headers=headers).json()
    assert stored["repo_url"] == "https://github.com/example/repo"


def test_a_non_github_repo_url_is_refused_on_import(client: TestClient, auth, populated):
    """The same GitHub-shaped check the JSON API applies to `repo_url` --
    otherwise a CSV import could set a repo link the API would refuse, and
    the "export re-imports without editing" round trip would quietly stop
    being true for exactly this column."""
    event, organizer = populated()
    headers = auth(organizer)
    submission_id = client.get(
        f"/api/events/{event.slug}/submissions", headers=headers
    ).json()["items"][0]["id"]

    body = (
        "submission_id,name,repo_url\n"
        f"{submission_id},Project 0,https://example.com/repo\n"
    )
    r = do_import(client, event.slug, headers, "submissions", body)
    assert r.status_code == 200, r.text
    assert r.json()["updated"] == 0
    assert r.json()["errors"]


def test_an_empty_file_is_not_an_error(client: TestClient, auth, populated):
    """A header with no rows is a legitimate no-op, and the exporter emits exactly
    that for a quiet event."""
    event, organizer = populated()
    r = do_import(client, event.slug, auth(organizer), "tracks", "key,name,description,position\n")
    assert r.status_code == 200
    assert r.json() == {
        "created": 0,
        "updated": 0,
        "skipped": 0,
        "errors": [],
        "dry_run": False,
    }


def test_garbage_is_rejected_cleanly(client: TestClient, auth, populated):
    event, organizer = populated()
    r = do_import(client, event.slug, auth(organizer), "tracks", "this is not a csv at all")
    assert r.status_code == 422


def test_import_is_recorded_in_the_audit_log(client: TestClient, auth, populated):
    """A bulk write by an organizer is exactly the kind of thing somebody asks about
    later."""
    from app.models import AuditAction

    event, organizer = populated()
    headers = auth(organizer)
    do_import(
        client, event.slug, headers, "tracks", "key,name,description,position\nnew,New,,9\n"
    )
    log = client.get(f"/api/events/{event.slug}/audit", headers=headers).json()
    summaries = [r["summary"] for r in log["rows"]]
    assert any("import" in s.lower() for s in summaries), summaries


# --------------------------------------------------------------------------- #
# Formula injection
# --------------------------------------------------------------------------- #


def test_a_formula_in_a_project_name_is_escaped_on_export(
    client: TestClient, auth, db, populated, make_team, make_user, make_submission
):
    """Attack: name your project `=cmd|'/c calc'!A1` and wait for an organizer to
    open submissions.csv in Excel.

    The attacker is a participant, the victim is the organizer, and the vector is a
    file *we* generated and told them to trust -- which is what makes it ours to fix.
    """
    event, organizer = populated()
    team = make_team(event, make_user(Role.PARTICIPANT), name="Sneaky")
    make_submission(
        team, name="=cmd|'/c calc'!A1", status=SubmissionStatus.SUBMITTED
    )

    text = export(client, event.slug, auth(organizer), "submissions").text
    assert "=cmd" in text, "the value should still be present"
    # ...but never at the start of a cell, where a spreadsheet would execute it.
    for row in csv.reader(io.StringIO(text)):
        for cell in row:
            assert not cell.startswith(("=", "+", "@")), cell


def test_the_guard_is_stripped_on_import_so_the_round_trip_holds(
    client: TestClient, auth, populated, make_team, make_user, make_submission
):
    """The escape must not accumulate: two round trips cannot add two quotes."""
    event, organizer = populated()
    team = make_team(event, make_user(Role.PARTICIPANT), name="Sneaky")
    make_submission(team, name="=danger()", status=SubmissionStatus.SUBMITTED)
    headers = auth(organizer)

    first = export(client, event.slug, headers, "submissions").text
    do_import(client, event.slug, headers, "submissions", first)
    second = export(client, event.slug, headers, "submissions").text
    assert first == second

    do_import(client, event.slug, headers, "submissions", second)
    third = export(client, event.slug, headers, "submissions").text
    assert second == third


# --------------------------------------------------------------------------- #
# The directory export: one row per submitted project, for an outside
# consumer (sponsors, press) rather than for round-tripping back in.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("role", [Role.PARTICIPANT, Role.JUDGE])
def test_only_staff_can_export_the_directory(client: TestClient, auth, make_user, populated, role):
    event, _organizer = populated()
    r = export(client, event.slug, auth(make_user(role)), "directory")
    assert r.status_code == 403


def test_an_anonymous_caller_cannot_export_the_directory(client: TestClient, populated):
    event, _organizer = populated()
    r = export(client, event.slug, {}, "directory")
    assert r.status_code == 403


def test_the_directory_export_has_the_requested_columns_in_order(
    client: TestClient, auth, populated, make_team, make_user, make_submission, db
):
    event, organizer = populated()
    team = make_team(event, make_user(Role.PARTICIPANT), name="Team Full")
    submission = make_submission(
        team,
        name="Full Project",
        status=SubmissionStatus.SUBMITTED,
        description="A project with everything filled in.",
        discord_usernames=["alice#0001", "bob#0002"],
    )
    submission.linkedin_url = "https://linkedin.com/in/alice"
    submission.live_url = "https://example.invalid/live"
    submission.demo_video_url = "https://example.invalid/video"
    db.add(submission)
    db.commit()

    rows = _rows(export(client, event.slug, auth(organizer), "directory"))
    assert rows[0] == [
        "team_name", "project_name", "discord_usernames", "linkedin_url",
        "description", "live_url", "video_url", "repo_url",
    ]
    data_rows = [row for row in rows[1:] if row]
    matching = [row for row in data_rows if row[1] == "Full Project"]
    assert len(matching) == 1
    assert matching[0] == [
        "Team Full",
        "Full Project",
        "alice#0001, bob#0002",
        "https://linkedin.com/in/alice",
        "A project with everything filled in.",
        "https://example.invalid/live",
        "https://example.invalid/video",
        submission.repo_url,
    ]


def test_the_directory_export_leaves_optional_fields_blank_not_null(
    client: TestClient, auth, populated, make_team, make_user, make_submission
):
    """LinkedIn, live and video links are all optional -- an unfilled one is an
    empty cell, never the string "None" or "N/A"."""
    event, organizer = populated()
    team = make_team(event, make_user(Role.PARTICIPANT), name="Team Sparse")
    make_submission(
        team,
        name="Sparse Project",
        status=SubmissionStatus.SUBMITTED,
        discord_usernames=[],
    )

    rows = _rows(export(client, event.slug, auth(organizer), "directory"))
    row = next(r for r in rows[1:] if r and r[1] == "Sparse Project")
    _team, _name, discord, linkedin, _desc, live, video, _repo = row
    assert discord == ""
    assert linkedin == ""
    assert live == ""
    assert video == ""


def test_the_directory_export_excludes_drafts_and_disqualified(
    client: TestClient, auth, populated, make_team, make_user, make_submission
):
    event, organizer = populated()
    drafts_team = make_team(event, make_user(Role.PARTICIPANT), name="Team Draft")
    make_submission(drafts_team, name="Draft Project", status=SubmissionStatus.DRAFT)
    dq_team = make_team(event, make_user(Role.PARTICIPANT), name="Team DQ")
    make_submission(dq_team, name="DQ Project", status=SubmissionStatus.DISQUALIFIED)

    rows = _rows(export(client, event.slug, auth(organizer), "directory"))
    names = {row[1] for row in rows[1:] if row}
    assert "Draft Project" not in names
    assert "DQ Project" not in names
    # The three submitted fixtures from `populated` are still there.
    assert "Project 0" in names


def test_the_directory_export_is_one_row_per_submission_not_per_member(
    client: TestClient, auth, populated, make_team, make_user, make_submission
):
    event, organizer = populated()
    team = make_team(
        event,
        make_user(Role.PARTICIPANT),
        make_user(Role.PARTICIPANT),
        make_user(Role.PARTICIPANT),
        name="Team Trio",
    )
    make_submission(
        team,
        name="Trio Project",
        status=SubmissionStatus.SUBMITTED,
        discord_usernames=["a#1", "b#2", "c#3"],
    )

    rows = _rows(export(client, event.slug, auth(organizer), "directory"))
    matching = [row for row in rows[1:] if row and row[1] == "Trio Project"]
    assert len(matching) == 1
    assert matching[0][2] == "a#1, b#2, c#3"


def test_a_formula_in_a_directory_description_is_escaped_on_export(
    client: TestClient, auth, populated, make_team, make_user, make_submission
):
    event, organizer = populated()
    team = make_team(event, make_user(Role.PARTICIPANT), name="Sneaky Directory")
    make_submission(
        team,
        name="Sneaky Description Project",
        status=SubmissionStatus.SUBMITTED,
        description="=cmd|'/c calc'!A1",
    )

    text = export(client, event.slug, auth(organizer), "directory").text
    assert "=cmd" in text
    for row in csv.reader(io.StringIO(text)):
        for cell in row:
            assert not cell.startswith(("=", "+", "@")), cell
