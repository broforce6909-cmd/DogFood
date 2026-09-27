"""Comments on gallery projects, and the moderation around them."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.models import Role, SubmissionStatus


@pytest.fixture()
def project(db, make_user, make_team, make_submission, make_event):
    """One submitted project in a published event, plus an organizer."""

    def _build(*, published: bool = True, comments: bool = True, submitted: bool = True):
        event = make_event(published=published)
        event.comments_enabled = comments
        db.commit()
        team = make_team(event, make_user(Role.PARTICIPANT), name="Quorum Collective")
        submission = make_submission(
            team,
            name="Quorum",
            status=SubmissionStatus.SUBMITTED if submitted else SubmissionStatus.DRAFT,
        )
        return event, submission, make_user(Role.ORGANIZER)

    return _build


def post(client, submission, headers, body="Nice work."):
    return client.post(
        f"/api/gallery/{submission.id}/comments", headers=headers, json={"body": body}
    )


def listing(client, submission, headers=None):
    return client.get(f"/api/gallery/{submission.id}/comments", headers=headers or {})


# --------------------------------------------------------------------------- #
# Posting
# --------------------------------------------------------------------------- #


def test_an_authenticated_user_can_comment(client: TestClient, auth, make_user, project):
    _event, submission, _organizer = project()
    r = post(client, submission, auth(make_user(Role.PARTICIPANT)), "Clean schema.")
    assert r.status_code == 201, r.text
    assert r.json()["body"] == "Clean schema."


def test_an_anonymous_visitor_cannot_comment(client: TestClient, project):
    """Anti-abuse, not an oversight: an anonymous comment box on a public gallery
    is a spam endpoint."""
    _event, submission, _organizer = project()
    r = post(client, submission, {})
    assert r.status_code in (401, 403), r.text


def test_comments_are_public_to_read(client: TestClient, auth, make_user, project):
    _event, submission, _organizer = project()
    post(client, submission, auth(make_user(Role.PARTICIPANT)), "Visible to all.")
    r = listing(client, submission)
    assert r.status_code == 200
    assert [c["body"] for c in r.json()["items"]] == ["Visible to all."]


def test_a_blank_comment_is_refused(client: TestClient, auth, make_user, project):
    _event, submission, _organizer = project()
    for body in ("", "   ", "\n\t "):
        r = post(client, submission, auth(make_user(Role.PARTICIPANT)), body)
        assert r.status_code == 422, body


def test_comments_can_be_switched_off_per_event(
    client: TestClient, auth, make_user, project
):
    _event, submission, _organizer = project(comments=False)
    r = post(client, submission, auth(make_user(Role.PARTICIPANT)))
    assert r.status_code == 409
    assert "switched off" in r.json()["detail"]


def test_a_draft_has_no_comment_thread(client: TestClient, auth, make_user, project):
    """The gallery is the comment surface, and a draft is not in the gallery."""
    _event, submission, _organizer = project(submitted=False)
    assert post(client, submission, auth(make_user(Role.PARTICIPANT))).status_code == 404
    assert listing(client, submission).status_code == 404


def test_an_unpublished_event_has_no_comment_thread(
    client: TestClient, auth, make_user, project
):
    _event, submission, _organizer = project(published=False)
    assert listing(client, submission).status_code == 404


# --------------------------------------------------------------------------- #
# Moderation
# --------------------------------------------------------------------------- #


def test_an_organizer_hides_a_comment_and_it_leaves_the_public_thread(
    client: TestClient, auth, make_user, project
):
    _event, submission, organizer = project()
    posted = post(client, submission, auth(make_user(Role.PARTICIPANT)), "SPAM-TRIPWIRE")
    comment_id = posted.json()["id"]

    hidden = client.post(
        f"/api/comments/{comment_id}/hide",
        headers=auth(organizer),
        json={"reason": "off topic"},
    )
    assert hidden.status_code == 200, hidden.text
    assert hidden.json()["is_hidden"] is True

    public = listing(client, submission)
    assert public.json()["items"] == []
    assert "SPAM-TRIPWIRE" not in public.text


def test_a_hidden_comment_is_withheld_from_its_own_author(
    client: TestClient, auth, make_user, project
):
    """So a moderated comment cannot simply be read back and re-posted verbatim."""
    _event, submission, organizer = project()
    author = make_user(Role.PARTICIPANT)
    posted = post(client, submission, auth(author), "SPAM-TRIPWIRE")
    client.post(
        f"/api/comments/{posted.json()['id']}/hide",
        headers=auth(organizer),
        json={"reason": "off topic"},
    )
    mine = listing(client, submission, auth(author))
    assert mine.json()["items"] == []
    assert "SPAM-TRIPWIRE" not in mine.text


def test_staff_still_see_hidden_comments(client: TestClient, auth, make_user, project):
    """Moderation has to be reviewable, so hiding is not deleting."""
    _event, submission, organizer = project()
    posted = post(client, submission, auth(make_user(Role.PARTICIPANT)), "SPAM-TRIPWIRE")
    client.post(
        f"/api/comments/{posted.json()['id']}/hide",
        headers=auth(organizer),
        json={"reason": "off topic"},
    )
    staff = listing(client, submission, auth(organizer)).json()["items"]
    assert len(staff) == 1
    assert staff[0]["is_hidden"] is True
    assert staff[0]["hidden_reason"] == "off topic"


def test_a_participant_cannot_moderate(client: TestClient, auth, make_user, project):
    _event, submission, _organizer = project()
    posted = post(client, submission, auth(make_user(Role.PARTICIPANT)))
    r = client.post(
        f"/api/comments/{posted.json()['id']}/hide",
        headers=auth(make_user(Role.PARTICIPANT)),
        json={"reason": "I disagree"},
    )
    assert r.status_code == 403


def test_hiding_requires_a_reason(client: TestClient, auth, make_user, project):
    """"No reason recorded" is not an answer the log should be able to give."""
    _event, submission, organizer = project()
    posted = post(client, submission, auth(make_user(Role.PARTICIPANT)))
    r = client.post(
        f"/api/comments/{posted.json()['id']}/hide",
        headers=auth(organizer),
        json={"reason": ""},
    )
    assert r.status_code == 422


def test_a_judge_cannot_moderate(client: TestClient, auth, make_user, project):
    """Moderation is an organizer job. A judge is not staff."""
    _event, submission, _organizer = project()
    posted = post(client, submission, auth(make_user(Role.PARTICIPANT)))
    r = client.post(
        f"/api/comments/{posted.json()['id']}/hide",
        headers=auth(make_user(Role.JUDGE)),
        json={"reason": "no"},
    )
    assert r.status_code == 403


# --------------------------------------------------------------------------- #
# Retraction
# --------------------------------------------------------------------------- #


def test_an_author_can_retract_their_own_comment(
    client: TestClient, auth, make_user, project
):
    _event, submission, _organizer = project()
    author = make_user(Role.PARTICIPANT)
    posted = post(client, submission, auth(author))
    r = client.delete(f"/api/comments/{posted.json()['id']}", headers=auth(author))
    assert r.status_code == 204
    assert listing(client, submission).json()["items"] == []


def test_one_user_cannot_retract_another_users_comment(
    client: TestClient, auth, make_user, project
):
    _event, submission, _organizer = project()
    posted = post(client, submission, auth(make_user(Role.PARTICIPANT)))
    r = client.delete(
        f"/api/comments/{posted.json()['id']}", headers=auth(make_user(Role.PARTICIPANT))
    )
    assert r.status_code == 403


def test_can_remove_tells_the_ui_the_truth(client: TestClient, auth, make_user, project):
    """`can_remove` is computed by the same predicate that enforces it."""
    _event, submission, _organizer = project()
    author = make_user(Role.PARTICIPANT)
    post(client, submission, auth(author))

    mine = listing(client, submission, auth(author)).json()["items"]
    assert mine[0]["can_remove"] is True

    stranger = listing(client, submission, auth(make_user(Role.PARTICIPANT))).json()["items"]
    assert stranger[0]["can_remove"] is False

    anonymous = listing(client, submission).json()["items"]
    assert anonymous[0]["can_remove"] is False


# --------------------------------------------------------------------------- #
# Rate limiting
# --------------------------------------------------------------------------- #


def test_comment_flooding_is_rate_limited(client: TestClient, auth, make_user, project):
    """Attack: script the comment endpoint."""
    _event, submission, _organizer = project()
    headers = auth(make_user(Role.PARTICIPANT))
    statuses = [post(client, submission, headers, f"comment {i}").status_code for i in range(30)]
    assert 429 in statuses, sorted(set(statuses))
