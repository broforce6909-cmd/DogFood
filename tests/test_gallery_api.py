"""The public gallery: what is in it, and how it is searched.

The last test in this file is the important one. The gallery is the only place
where a SQL filter decides what a caller sees, so it is the only place where
access control could quietly diverge from `check_access`. That test asserts it
does not.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.access import ANONYMOUS, PUBLIC_SUBMISSION_CRITERIA, Action, check_access
from app.models import Event, Submission, SubmissionStatus, utcnow


def test_only_submitted_projects_in_published_events_appear(
    client: TestClient, make_event, make_team, make_submission, make_user
) -> None:
    published, unpublished = make_event(slug="live"), make_event(slug="draft", published=False)

    make_submission(
        make_team(published, make_user()), name="Entered", status=SubmissionStatus.SUBMITTED
    )
    make_submission(make_team(published, make_user()), name="Draft Project")
    make_submission(
        make_team(unpublished, make_user()),
        name="Hidden Event Entry",
        status=SubmissionStatus.SUBMITTED,
    )

    names = [item["name"] for item in client.get("/api/gallery").json()["items"]]
    assert names == ["Entered"]


def test_search_covers_name_tagline_description_and_team(
    client: TestClient, make_event, make_team, make_submission, make_user
) -> None:
    event = make_event()
    make_submission(
        make_team(event, make_user(), name="Quorum Collective"),
        name="Quorum",
        tagline="Consensus scoring for panels",
        description="A tool about calibration.",
        status=SubmissionStatus.SUBMITTED,
    )
    make_submission(
        make_team(event, make_user(), name="Latchkey Labs"),
        name="Latchkey",
        tagline="Invite links that expire",
        description="Nothing related in this one.",
        status=SubmissionStatus.SUBMITTED,
    )

    def search(q: str) -> list[str]:
        return [i["name"] for i in client.get(f"/api/gallery?q={q}").json()["items"]]

    assert search("quorum") == ["Quorum"]  # name, case-insensitively
    assert search("panels") == ["Quorum"]  # tagline
    assert search("calibration") == ["Quorum"]  # description
    assert search("Latchkey Labs") == ["Latchkey"]  # team name
    assert sorted(search("e")) == ["Latchkey", "Quorum"]


def test_filtering_by_tag_and_by_track(
    client: TestClient, make_event, make_track, make_team, make_submission, make_user
) -> None:
    event = make_event()
    tools = make_track(event, key="devtools", name="Developer Tools")
    data = make_track(event, key="open-data", name="Open Data")

    make_submission(
        make_team(event, make_user()),
        name="Marginalia",
        tags=["rust", "wasm"],
        track=tools,
        status=SubmissionStatus.SUBMITTED,
    )
    make_submission(
        make_team(event, make_user()),
        name="Ledgerline",
        tags=["python", "svelte"],
        track=data,
        status=SubmissionStatus.SUBMITTED,
    )

    assert [i["name"] for i in client.get("/api/gallery?tag=rust").json()["items"]] == [
        "Marginalia"
    ]
    assert [i["name"] for i in client.get("/api/gallery?track=open-data").json()["items"]] == [
        "Ledgerline"
    ]
    assert client.get("/api/gallery?tag=cobol").json()["total"] == 0


def test_filters_combine(
    client: TestClient, make_event, make_track, make_team, make_submission, make_user
) -> None:
    event = make_event(slug="combo")
    track = make_track(event, key="devtools")
    make_submission(
        make_team(event, make_user()),
        name="Marginalia",
        tags=["rust"],
        track=track,
        status=SubmissionStatus.SUBMITTED,
    )
    assert (
        client.get(f"/api/gallery?event=combo&track=devtools&tag=rust&q=margin").json()["total"]
        == 1
    )
    assert (
        client.get(f"/api/gallery?event=combo&track=devtools&tag=cobol").json()["total"] == 0
    )


def test_the_tag_vocabulary_comes_from_projects_in_the_gallery(
    client: TestClient, make_event, make_team, make_submission, make_user
) -> None:
    event = make_event()
    make_submission(
        make_team(event, make_user()),
        tags=["rust", "wasm"],
        status=SubmissionStatus.SUBMITTED,
    )
    # A draft's tags must not appear -- that would leak what an unentered
    # project is built with.
    make_submission(make_team(event, make_user()), name="Draft", tags=["cobol"])

    assert client.get("/api/gallery/tags").json() == ["rust", "wasm"]


def test_pagination_reports_a_usable_total(
    client: TestClient, make_event, make_team, make_submission, make_user
) -> None:
    event = make_event()
    for index in range(5):
        make_submission(
            make_team(event, make_user()),
            name=f"Project {index}",
            status=SubmissionStatus.SUBMITTED,
        )

    first = client.get("/api/gallery?per_page=2&page=1&sort=name").json()
    assert first["total"] == 5 and first["pages"] == 3 and len(first["items"]) == 2

    last = client.get("/api/gallery?per_page=2&page=3&sort=name").json()
    assert len(last["items"]) == 1
    assert first["items"][0]["name"] != last["items"][0]["name"]


@pytest.fixture()
def two_events(db: Session, make_event, make_team, make_submission, make_user):
    """A newer event with four entries and an older one with two.

    Submission times are set outright, hours apart, so "newest first" is not a
    race against the clock: N1 > N2 > N3 > N4 > O1 > O2.
    """
    newer, older = make_event(slug="newer"), make_event(slug="older")
    now = utcnow()
    for event, names, first in ((newer, ["N1", "N2", "N3", "N4"], 0), (older, ["O1", "O2"], 4)):
        for offset, name in enumerate(names, start=first):
            submission = make_submission(
                make_team(event, make_user()), name=name, status=SubmissionStatus.SUBMITTED
            )
            submission.submitted_at = now - timedelta(hours=offset)
    db.commit()
    return newer, older


def _names(client: TestClient, query: str = "") -> list[str]:
    return [item["name"] for item in client.get(f"/api/gallery?{query}").json()["items"]]


def test_the_cross_event_default_takes_turns_between_events(client: TestClient, two_events) -> None:
    """Strictly newest-first would put all four of the newer event's projects
    ahead of any from the older one, and a small page one would never show it."""
    assert _names(client) == ["N1", "O1", "N2", "O2", "N3", "N4"]
    assert _names(client, "per_page=2") == ["N1", "O1"]


def test_recent_is_still_strictly_newest_first(client: TestClient, two_events) -> None:
    assert _names(client, "sort=recent") == ["N1", "N2", "N3", "N4", "O1", "O2"]


def test_one_event_defaults_to_newest_first(client: TestClient, two_events) -> None:
    """Nothing to take turns with, and the event's own page is what run.py reads."""
    assert _names(client, "event=newer") == ["N1", "N2", "N3", "N4"]
    assert _names(client, "event=newer&sort=mixed") == ["N1", "N2", "N3", "N4"]


def test_taking_turns_pages_cleanly(client: TestClient, two_events) -> None:
    """Pagination is untouched: every project appears once across the pages."""
    seen: list[str] = []
    for page in (1, 2, 3):
        body = client.get(f"/api/gallery?per_page=2&page={page}").json()
        assert body["total"] == 6 and body["pages"] == 3
        seen += [item["name"] for item in body["items"]]
    assert seen == ["N1", "O1", "N2", "O2", "N3", "N4"]


def test_the_turns_are_taken_among_the_filtered_projects_only(
    db: Session, client: TestClient, two_events
) -> None:
    """The rank is computed after the filters. If it were computed before, a
    project the search excludes would still take its event's slot and leave a hole."""
    for name in ("N1", "N2"):
        db.execute(
            update(Submission).where(Submission.name == name).values(tech_tags=["rust"])
        )
    db.execute(update(Submission).where(Submission.name == "O2").values(tech_tags=["rust"]))
    db.commit()

    assert _names(client, "tag=rust") == ["N1", "O2", "N2"]


def test_a_single_card_is_only_served_for_gallery_projects(
    client: TestClient, make_event, make_team, make_submission, make_user
) -> None:
    event = make_event()
    entered = make_submission(
        make_team(event, make_user()), status=SubmissionStatus.SUBMITTED
    )
    draft = make_submission(make_team(event, make_user()), name="Draft")

    assert client.get(f"/api/gallery/{entered.id}").status_code == 200
    assert client.get(f"/api/gallery/{draft.id}").status_code == 404


def test_gallery_query_matches_predicate(
    db: Session, make_event, make_team, make_submission, make_user
) -> None:
    """The invariant that keeps the gallery honest.

    Build every combination of (event published?) x (submission status), then
    compare two answers to "what may a visitor see":

      * the SQL the gallery actually runs, and
      * `check_access(ANONYMOUS, submission, READ)` applied row by row.

    If these ever disagree, either the gallery is leaking or the predicate is
    lying, and both are the same bug.
    """
    for published in (True, False):
        for status in (SubmissionStatus.DRAFT, SubmissionStatus.SUBMITTED):
            event = make_event(slug=f"e-{published}-{status.value}", published=published)
            make_submission(
                make_team(event, make_user()),
                name=f"{published}-{status.value}",
                status=status,
            )

    by_query = {
        row.id
        for row in db.execute(
            select(Submission)
            .join(Event, Event.id == Submission.event_id)
            .where(*PUBLIC_SUBMISSION_CRITERIA)
        ).scalars()
    }
    by_predicate = {
        row.id
        for row in db.execute(select(Submission)).scalars()
        if check_access(ANONYMOUS, row, Action.READ)
    }

    assert by_query == by_predicate
    assert len(by_query) == 1  # and it is the one combination that should pass
