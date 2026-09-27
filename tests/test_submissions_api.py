"""Project submission: draft, edit, submit -- and the deadline.

The deadline tests are the point of this file. "Deadline enforcement that
actually holds" means every write verb is refused by the API after the deadline,
for every actor who is not staff, whether or not a form was rendered.
"""

from __future__ import annotations

from datetime import timedelta

from fastapi.testclient import TestClient

from app.models import Role, SubmissionStatus, utcnow


def past_deadline():
    return utcnow() - timedelta(hours=1)


# --------------------------------------------------------------------------- #
# Draft and edit
# --------------------------------------------------------------------------- #


def test_a_team_member_starts_a_draft(
    client: TestClient, make_event, make_team, make_user, auth
) -> None:
    event = make_event()
    owner = make_user()
    team = make_team(event, owner)

    response = client.post(
        "/api/submissions",
        json={"team_id": str(team.id), "name": "Quorum"},
        headers=auth(owner),
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == SubmissionStatus.DRAFT.value
    assert body["can_edit"] is True
    assert body["submitted_at"] is None


def test_a_stranger_cannot_start_a_draft_for_somebody_elses_team(
    client: TestClient, make_event, make_team, make_user, auth
) -> None:
    event = make_event()
    team = make_team(event, make_user())
    response = client.post(
        "/api/submissions",
        json={"team_id": str(team.id), "name": "Hijack"},
        headers=auth(make_user()),
    )
    assert response.status_code == 403


def test_one_submission_per_team(
    client: TestClient, make_event, make_team, make_user, auth
) -> None:
    event = make_event()
    owner = make_user()
    team = make_team(event, owner)
    headers = auth(owner)
    body = {"team_id": str(team.id), "name": "First"}

    assert client.post("/api/submissions", json=body, headers=headers).status_code == 201
    assert client.post("/api/submissions", json=body, headers=headers).status_code == 409


def test_the_full_field_set_round_trips(
    client: TestClient, make_event, make_track, make_team, make_submission, make_user, auth
) -> None:
    """The field set the brief calls stable across every platform studied."""
    event = make_event()
    track = make_track(event, key="devtools", name="Developer Tools")
    owner = make_user()
    submission = make_submission(make_team(event, owner))

    payload = {
        "name": "Latchkey",
        "tagline": "Invite links that expire",
        "description": "A longer description.",
        "thumbnail_url": "https://example.invalid/thumb.png",
        "gallery_image_urls": ["https://example.invalid/1.png", "https://example.invalid/2.png"],
        "demo_video_url": "https://example.invalid/demo",
        "repo_url": "https://github.com/example/latchkey",
        "live_url": "https://example.invalid/live",
        "linkedin_url": "https://example.invalid/in/latchkey",
        "tech_tags": ["Go", "  SQLite ", "go"],
        "discord_usernames": [" priyar ", "kwame.o", "priyar"],
        "track_id": str(track.id),
    }
    response = client.patch(
        f"/api/submissions/{submission.id}", json=payload, headers=auth(owner)
    )
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["name"] == "Latchkey"
    assert body["track"]["key"] == "devtools"
    assert len(body["gallery_image_urls"]) == 2
    assert body["repo_url"] == "https://github.com/example/latchkey"
    assert body["linkedin_url"] == "https://example.invalid/in/latchkey"
    # Tags are normalized and de-duplicated, so the gallery filter has one
    # spelling to match against.
    assert body["tech_tags"] == ["go", "sqlite"]
    # Discord usernames are trimmed but not de-duplicated or case-folded --
    # unlike tech tags, two different people can share a display quirk, and
    # there is no reason to silently drop a name a team actually typed twice.
    assert body["discord_usernames"] == ["priyar", "kwame.o", "priyar"]


def test_a_track_from_another_event_is_refused(
    client: TestClient, make_event, make_track, make_team, make_submission, make_user, auth
) -> None:
    home, elsewhere = make_event(slug="home"), make_event(slug="elsewhere")
    owner = make_user()
    submission = make_submission(make_team(home, owner))
    foreign = make_track(elsewhere, key="theirs")

    response = client.patch(
        f"/api/submissions/{submission.id}",
        json={"track_id": str(foreign.id)},
        headers=auth(owner),
    )
    assert response.status_code == 422


def test_custom_question_answers_are_saved_with_the_project(
    client: TestClient, make_event, make_team, make_submission, make_user, auth
) -> None:
    event = make_event()
    organizer, owner = make_user(Role.ORGANIZER), make_user()
    question = client.post(
        f"/api/events/{event.slug}/questions",
        json={"prompt": "What did you cut?", "kind": "textarea", "required": True},
        headers=auth(organizer),
    ).json()
    submission = make_submission(make_team(event, owner))

    response = client.patch(
        f"/api/submissions/{submission.id}",
        json={"answers": {question["id"]: "The plugin system."}},
        headers=auth(owner),
    )
    assert response.status_code == 200, response.text
    answers = {a["question_id"]: a["value"] for a in response.json()["answers"]}
    assert answers[question["id"]] == "The plugin system."


# --------------------------------------------------------------------------- #
# New required/validated fields: Discord handles, GitHub repo, LinkedIn
# --------------------------------------------------------------------------- #


def test_a_non_github_repo_url_is_rejected(
    client: TestClient, make_event, make_team, make_submission, make_user, auth
) -> None:
    event = make_event()
    owner = make_user()
    submission = make_submission(make_team(event, owner))

    r = client.patch(
        f"/api/submissions/{submission.id}",
        json={"repo_url": "https://gitlab.com/example/repo"},
        headers=auth(owner),
    )
    assert r.status_code == 422
    assert "GitHub" in r.text


def test_the_github_validator_checks_shape_not_that_the_repo_is_real(
    client: TestClient, make_event, make_team, make_submission, make_user, auth
) -> None:
    """Named honestly as a limitation, not a bug: `github.com/settings/profile`
    parses as owner=settings, repo=profile and is accepted, the same way
    `_http_url` accepts a dead link elsewhere in this schema. Checking that a
    path is *shaped* like `owner/repo` is what a validator can do; checking
    that the repository exists would mean this API making an outbound
    request to GitHub on every submission edit, which is not a trade this
    project makes for any other link field either."""
    event = make_event()
    owner = make_user()
    submission = make_submission(make_team(event, owner))

    r = client.patch(
        f"/api/submissions/{submission.id}",
        json={"repo_url": "https://github.com/settings/profile"},
        headers=auth(owner),
    )
    assert r.status_code == 200, r.text


def test_optional_links_do_not_block_submission_when_omitted(
    client: TestClient, make_event, make_team, make_user, auth
) -> None:
    """live_url, demo_video_url and linkedin_url are all optional -- a
    complete project with none of them set must still submit cleanly."""
    event = make_event()
    owner = make_user()
    team = make_team(event, owner)
    headers = auth(owner)
    submission = client.post(
        "/api/submissions", json={"team_id": str(team.id), "name": "Bare Links"}, headers=headers
    ).json()
    client.patch(
        f"/api/submissions/{submission['id']}",
        json={
            "tagline": "t",
            "description": "d",
            "repo_url": "https://github.com/example/bare-links",
            "discord_usernames": ["solo"],
        },
        headers=headers,
    )
    r = client.post(f"/api/submissions/{submission['id']}/submit", headers=headers)
    assert r.status_code == 200, r.text


def test_an_optional_linkedin_url_is_validated_when_present(
    client: TestClient, make_event, make_team, make_submission, make_user, auth
) -> None:
    event = make_event()
    owner = make_user()
    submission = make_submission(make_team(event, owner))

    bad = client.patch(
        f"/api/submissions/{submission.id}",
        json={"linkedin_url": "javascript:alert(1)"},
        headers=auth(owner),
    )
    assert bad.status_code == 422

    good = client.patch(
        f"/api/submissions/{submission.id}",
        json={"linkedin_url": "https://www.linkedin.com/in/example"},
        headers=auth(owner),
    )
    assert good.status_code == 200, good.text
    assert good.json()["linkedin_url"] == "https://www.linkedin.com/in/example"


def test_an_empty_discord_username_between_commas_is_rejected(
    client: TestClient, make_event, make_team, make_submission, make_user, auth
) -> None:
    """The frontend splits a comma-separated field into an array before this
    ever reaches the API (see `commas()` in actions.ts), but the API cannot
    trust that every caller did -- an empty slot is refused here too."""
    event = make_event()
    owner = make_user()
    submission = make_submission(make_team(event, owner))

    r = client.patch(
        f"/api/submissions/{submission.id}",
        json={"discord_usernames": ["priyar", "", "kwame.o"]},
        headers=auth(owner),
    )
    assert r.status_code == 422


def test_discord_usernames_are_trimmed_but_not_deduplicated(
    client: TestClient, make_event, make_team, make_submission, make_user, auth
) -> None:
    event = make_event()
    owner = make_user()
    submission = make_submission(make_team(event, owner))

    r = client.patch(
        f"/api/submissions/{submission.id}",
        json={"discord_usernames": ["  priyar  ", "priyar"]},
        headers=auth(owner),
    )
    assert r.status_code == 200, r.text
    assert r.json()["discord_usernames"] == ["priyar", "priyar"]


# --------------------------------------------------------------------------- #
# Submitting
# --------------------------------------------------------------------------- #


def test_submitting_an_incomplete_project_lists_everything_missing(
    client: TestClient, make_event, make_team, make_user, auth
) -> None:
    event = make_event()
    owner = make_user()
    team = make_team(event, owner)
    headers = auth(owner)
    submission = client.post(
        "/api/submissions", json={"team_id": str(team.id), "name": "Bare"}, headers=headers
    ).json()

    response = client.post(f"/api/submissions/{submission['id']}/submit", headers=headers)
    assert response.status_code == 422
    missing = response.json()["detail"]["missing"]
    assert any("tagline" in m for m in missing)
    assert any("description" in m for m in missing)
    assert any("repo_url" in m for m in missing)
    assert any("discord_usernames" in m for m in missing)


def test_a_required_custom_question_blocks_submission(
    client: TestClient, make_event, make_team, make_submission, make_user, auth
) -> None:
    event = make_event()
    organizer, owner = make_user(Role.ORGANIZER), make_user()
    client.post(
        f"/api/events/{event.slug}/questions",
        json={"prompt": "What did you cut?", "kind": "textarea", "required": True},
        headers=auth(organizer),
    )
    submission = make_submission(make_team(event, owner))

    response = client.post(f"/api/submissions/{submission.id}/submit", headers=auth(owner))
    assert response.status_code == 422
    assert any("What did you cut?" in m for m in response.json()["detail"]["missing"])


def test_submitting_a_complete_project_puts_it_in_the_gallery(
    client: TestClient, make_event, make_team, make_submission, make_user, auth
) -> None:
    event = make_event()
    owner = make_user()
    submission = make_submission(make_team(event, owner), name="Tidewater")

    response = client.post(f"/api/submissions/{submission.id}/submit", headers=auth(owner))
    assert response.status_code == 200, response.text
    assert response.json()["status"] == SubmissionStatus.SUBMITTED.value
    assert response.json()["submitted_at"] is not None

    gallery = client.get(f"/api/gallery?event={event.slug}").json()
    assert [item["name"] for item in gallery["items"]] == ["Tidewater"]


def test_submitting_twice_is_a_no_op(
    client: TestClient, make_event, make_team, make_submission, make_user, auth
) -> None:
    event = make_event()
    owner = make_user()
    submission = make_submission(make_team(event, owner))
    headers = auth(owner)

    first = client.post(f"/api/submissions/{submission.id}/submit", headers=headers)
    second = client.post(f"/api/submissions/{submission.id}/submit", headers=headers)
    assert first.status_code == second.status_code == 200
    assert first.json()["submitted_at"] == second.json()["submitted_at"]


def test_unsubmitting_takes_it_back_out_of_the_gallery(
    client: TestClient, make_event, make_team, make_submission, make_user, auth
) -> None:
    event = make_event()
    owner = make_user()
    submission = make_submission(
        make_team(event, owner), status=SubmissionStatus.SUBMITTED, name="Second Thoughts"
    )
    assert client.get(f"/api/gallery?event={event.slug}").json()["total"] == 1

    response = client.post(f"/api/submissions/{submission.id}/unsubmit", headers=auth(owner))
    assert response.status_code == 200
    assert client.get(f"/api/gallery?event={event.slug}").json()["total"] == 0


# --------------------------------------------------------------------------- #
# The deadline
# --------------------------------------------------------------------------- #


def test_every_write_verb_is_refused_after_the_deadline(
    client: TestClient, make_event, make_team, make_submission, make_user, auth
) -> None:
    event = make_event(deadline=past_deadline())
    owner = make_user()
    submission = make_submission(make_team(event, owner))
    headers = auth(owner)

    assert (
        client.patch(
            f"/api/submissions/{submission.id}", json={"name": "Sneaky Edit"}, headers=headers
        ).status_code
        == 409
    )
    assert client.post(f"/api/submissions/{submission.id}/submit", headers=headers).status_code == 409
    assert (
        client.post(f"/api/submissions/{submission.id}/unsubmit", headers=headers).status_code
        == 409
    )
    assert client.delete(f"/api/submissions/{submission.id}", headers=headers).status_code == 409


def test_a_draft_that_missed_the_deadline_stays_a_readable_draft(
    client: TestClient, make_event, make_team, make_submission, make_user, auth
) -> None:
    """Not deleted, not published. Its team can still see it; the gallery cannot."""
    event = make_event(deadline=past_deadline())
    owner = make_user()
    submission = make_submission(make_team(event, owner), name="Too Late")

    mine = client.get(f"/api/submissions/{submission.id}", headers=auth(owner))
    assert mine.status_code == 200
    assert mine.json()["status"] == SubmissionStatus.DRAFT.value
    assert mine.json()["can_edit"] is False and mine.json()["can_submit"] is False
    assert client.get(f"/api/gallery?event={event.slug}").json()["total"] == 0


def test_no_new_draft_can_be_started_after_the_deadline(
    client: TestClient, make_event, make_team, make_user, auth
) -> None:
    event = make_event(deadline=past_deadline())
    owner = make_user()
    team = make_team(event, owner)
    response = client.post(
        "/api/submissions",
        json={"team_id": str(team.id), "name": "Late Start"},
        headers=auth(owner),
    )
    assert response.status_code == 403


def test_staff_may_still_edit_after_the_deadline(
    client: TestClient, make_event, make_team, make_submission, make_user, auth
) -> None:
    """A documented, deliberate exception -- somebody has to be able to fix a
    broken repository link, and the alternative is that it happens in psql."""
    event = make_event(deadline=past_deadline())
    submission = make_submission(make_team(event, make_user()))
    response = client.patch(
        f"/api/submissions/{submission.id}",
        json={"repo_url": "https://github.com/example/fixed"},
        headers=auth(make_user(Role.ORGANIZER)),
    )
    assert response.status_code == 200


# --------------------------------------------------------------------------- #
# Disqualification
# --------------------------------------------------------------------------- #


def test_disqualifying_a_submission_requires_a_reason(
    client: TestClient, make_event, make_team, make_submission, make_user, auth
) -> None:
    event = make_event()
    submission = make_submission(
        make_team(event, make_user()), status=SubmissionStatus.SUBMITTED
    )
    response = client.post(
        f"/api/submissions/{submission.id}/disqualify",
        json={},
        headers=auth(make_user(Role.ORGANIZER)),
    )
    assert response.status_code == 422


def test_disqualifying_a_submitted_project_removes_it_from_the_gallery(
    client: TestClient, make_event, make_team, make_submission, make_user, auth
) -> None:
    event = make_event()
    submission = make_submission(
        make_team(event, make_user()), name="Suspect Entry", status=SubmissionStatus.SUBMITTED
    )
    assert client.get(f"/api/gallery?event={event.slug}").json()["total"] == 1

    response = client.post(
        f"/api/submissions/{submission.id}/disqualify",
        json={"reason": "Plagiarized starter code"},
        headers=auth(make_user(Role.ORGANIZER)),
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == SubmissionStatus.DISQUALIFIED.value

    assert client.get(f"/api/gallery?event={event.slug}").json()["total"] == 0


def test_disqualifying_is_staff_only(
    client: TestClient, make_event, make_team, make_submission, make_user, auth
) -> None:
    event = make_event()
    owner = make_user()
    submission = make_submission(
        make_team(event, owner), status=SubmissionStatus.SUBMITTED
    )
    payload = {"reason": "Rule violation"}

    assert (
        client.post(
            f"/api/submissions/{submission.id}/disqualify", json=payload, headers=auth(owner)
        ).status_code
        == 403
    )
    assert (
        client.post(f"/api/submissions/{submission.id}/disqualify", json=payload).status_code
        == 403
    )


def test_a_disqualified_submission_cannot_be_resubmitted_or_unsubmitted(
    client: TestClient, make_event, make_team, make_submission, make_user, auth
) -> None:
    """Neither team-facing lifecycle route may reverse a disqualification --
    not even for staff, who are otherwise exempt from every other restriction
    on these same two routes."""
    event = make_event()
    owner = make_user()
    submission = make_submission(
        make_team(event, owner), status=SubmissionStatus.SUBMITTED
    )
    organizer = make_user(Role.ORGANIZER)
    client.post(
        f"/api/submissions/{submission.id}/disqualify",
        json={"reason": "Rule violation"},
        headers=auth(organizer),
    )

    owner_headers = auth(owner)
    assert (
        client.post(f"/api/submissions/{submission.id}/submit", headers=owner_headers).status_code
        == 409
    )
    assert (
        client.post(
            f"/api/submissions/{submission.id}/unsubmit", headers=owner_headers
        ).status_code
        == 409
    )

    organizer_headers = auth(organizer)
    assert (
        client.post(
            f"/api/submissions/{submission.id}/submit", headers=organizer_headers
        ).status_code
        == 409
    )
    assert (
        client.post(
            f"/api/submissions/{submission.id}/unsubmit", headers=organizer_headers
        ).status_code
        == 409
    )


def test_a_disqualified_draft_cannot_be_deleted(
    client: TestClient, make_event, make_team, make_submission, make_user, auth
) -> None:
    """A plain `DRAFT` is normally deletable by its own owner (there is
    nothing to lose) and always deletable by staff -- disqualifying it must
    take away both, or the "never hard-deleted" guarantee has a hole in it.

    The owner's case is refused a layer up: `_check_submission`'s existing
    DELETE rule already only allows deleting a `DRAFT`, and `DISQUALIFIED`
    is not one, so the owner gets the plain access-denied 403 that rule
    always gave for a non-draft. Staff bypass that same rule (deliberately,
    for every other status), so the new guard is the only thing standing
    between an organizer and a disqualified project's evidence -- that path
    is checked here as the 409 it should be."""
    event = make_event()
    owner = make_user()
    submission = make_submission(make_team(event, owner))
    organizer = make_user(Role.ORGANIZER)
    client.post(
        f"/api/submissions/{submission.id}/disqualify",
        json={"reason": "Rule violation"},
        headers=auth(organizer),
    )

    assert (
        client.delete(f"/api/submissions/{submission.id}", headers=auth(owner)).status_code
        == 403
    )
    assert (
        client.delete(f"/api/submissions/{submission.id}", headers=auth(organizer)).status_code
        == 409
    )


def test_reinstating_a_disqualified_submission_restores_submitted_status(
    client: TestClient, make_event, make_team, make_submission, make_user, auth
) -> None:
    event = make_event()
    submission = make_submission(
        make_team(event, make_user()), name="Reinstated", status=SubmissionStatus.SUBMITTED
    )
    headers = auth(make_user(Role.ORGANIZER))
    submitted_at = submission.submitted_at

    client.post(
        f"/api/submissions/{submission.id}/disqualify",
        json={"reason": "Under review"},
        headers=headers,
    )
    response = client.post(
        f"/api/submissions/{submission.id}/reinstate",
        json={"reason": "Review cleared the project"},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == SubmissionStatus.SUBMITTED.value
    assert body["submitted_at"] == submitted_at.isoformat().replace("+00:00", "Z")

    assert client.get(f"/api/gallery?event={event.slug}").json()["total"] == 1


def test_reinstating_a_disqualified_draft_restores_draft_status(
    client: TestClient, make_event, make_team, make_submission, make_user, auth
) -> None:
    """A draft has no `submitted_at` -- disqualifying and reinstating it must
    not manufacture one, or the CHECK constraint that ties the two together
    would be violated."""
    event = make_event()
    submission = make_submission(make_team(event, make_user()), name="Never Entered")
    headers = auth(make_user(Role.ORGANIZER))

    client.post(
        f"/api/submissions/{submission.id}/disqualify",
        json={"reason": "Preemptive, pending review"},
        headers=headers,
    )
    response = client.post(
        f"/api/submissions/{submission.id}/reinstate",
        json={"reason": "Cleared"},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == SubmissionStatus.DRAFT.value
    assert response.json()["submitted_at"] is None


def test_reinstating_a_submission_that_is_not_disqualified_is_rejected(
    client: TestClient, make_event, make_team, make_submission, make_user, auth
) -> None:
    event = make_event()
    submission = make_submission(
        make_team(event, make_user()), status=SubmissionStatus.SUBMITTED
    )
    response = client.post(
        f"/api/submissions/{submission.id}/reinstate",
        json={"reason": "Nothing to undo"},
        headers=auth(make_user(Role.ORGANIZER)),
    )
    assert response.status_code == 409


def test_disqualification_is_recorded_in_the_audit_log_with_the_reason(
    client: TestClient, make_event, make_team, make_submission, make_user, auth
) -> None:
    event = make_event()
    submission = make_submission(
        make_team(event, make_user()), name="Flagged Project", status=SubmissionStatus.SUBMITTED
    )
    organizer = make_user(Role.ORGANIZER)
    headers = auth(organizer)
    client.post(
        f"/api/submissions/{submission.id}/disqualify",
        json={"reason": "Used a pre-built template as the entire submission"},
        headers=headers,
    )
    client.post(
        f"/api/submissions/{submission.id}/reinstate",
        json={"reason": "Determined to be within the rules after all"},
        headers=headers,
    )

    rows = client.get(f"/api/events/{event.slug}/audit", headers=headers).json()["rows"]
    disqualified = next(r for r in rows if r["action"] == "submission_disqualified")
    reinstated = next(r for r in rows if r["action"] == "submission_reinstated")
    assert "Used a pre-built template as the entire submission" in disqualified["summary"]
    assert "Flagged Project" in disqualified["summary"]
    assert "Determined to be within the rules after all" in reinstated["summary"]


# --------------------------------------------------------------------------- #
# Reading
# --------------------------------------------------------------------------- #


def test_a_draft_is_invisible_to_everyone_outside_the_team(
    client: TestClient, make_event, make_team, make_submission, make_user, auth
) -> None:
    event = make_event()
    owner = make_user()
    submission = make_submission(make_team(event, owner), name="Halfpipe")

    assert client.get(f"/api/submissions/{submission.id}").status_code == 404
    assert (
        client.get(f"/api/submissions/{submission.id}", headers=auth(make_user())).status_code
        == 404
    )
    assert (
        client.get(
            f"/api/submissions/{submission.id}", headers=auth(make_user(Role.JUDGE))
        ).status_code
        == 404
    )
    assert client.get(f"/api/submissions/{submission.id}", headers=auth(owner)).status_code == 200
    assert (
        client.get(
            f"/api/submissions/{submission.id}", headers=auth(make_user(Role.ORGANIZER))
        ).status_code
        == 200
    )


def test_the_event_submission_list_shows_staff_the_drafts_and_others_the_gallery(
    client: TestClient, make_event, make_team, make_submission, make_user, auth
) -> None:
    event = make_event()
    a, b = make_user(), make_user()
    make_submission(make_team(event, a), name="Entered", status=SubmissionStatus.SUBMITTED)
    make_submission(make_team(event, b), name="Still Drafting")

    visitor = client.get(f"/api/events/{event.slug}/submissions").json()["items"]
    assert [s["name"] for s in visitor] == ["Entered"]

    staff = client.get(
        f"/api/events/{event.slug}/submissions", headers=auth(make_user(Role.ORGANIZER))
    ).json()["items"]
    assert sorted(s["name"] for s in staff) == ["Entered", "Still Drafting"]


def test_my_submissions_spans_events(
    client: TestClient, make_event, make_team, make_submission, make_user, auth
) -> None:
    user = make_user()
    for slug in ("spring", "autumn"):
        event = make_event(slug=slug)
        make_submission(make_team(event, user), name=f"Project {slug}")

    names = {s["name"] for s in client.get("/api/me/submissions", headers=auth(user)).json()}
    assert names == {"Project spring", "Project autumn"}
